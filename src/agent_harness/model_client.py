"""Role-routed model calls with a correct failure model.

This is the piece the harness exists for. It knows nothing about any
repository, language, or task — it routes a *role* to a model, calls it, and
reacts to failure in a way that does not take the fleet down with it.

Four rules, each of which is a specific observed failure rather than a
preference:

1. **Classify before reacting.** A 429 can mean "slow down" or "your budget
   is gone for a week". Handling them identically produced tens of thousands
   of rate-limit errors that nobody could break down after the fact.

2. **Never retry a spend cap.** Retrying cannot make budget appear. The
   window has to roll over. A ladder that retries it is a busy-wait that
   burns quota checking whether quota exists.

3. **Nothing global.** One worker's rejection must never pause another. A
   fleet-wide cooldown does not merely stall the fleet, it *phase-locks* it:
   every worker released at the same instant, one synchronised burst, all
   limited together, all parked together — precisely the shape a rate
   limiter is built to reject.

4. **Jitter is not rate shaping.** It is what stops N workers rejected in the
   same instant from retrying in the same instant.

State is per-process and per-endpoint by construction: a plain dict, no file,
no lock. Parking one endpoint cannot park another, and cannot affect another
worker at all.
"""

from __future__ import annotations

import contextlib
import itertools
import random
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import providers as P
from .providers import Classification, Provider


class CapExhausted(Exception):
    """A spend window or credential is exhausted. Not retried.

    Deliberately not caught inside the caller's work loop: it means the
    endpoint is out of budget, and the right response is to stop asking, not
    to try a different prompt. `.kind` distinguishes "back in a few hours"
    from "not this week".
    """

    def __init__(self, message: str, kind: str, endpoint: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.endpoint = endpoint


class RequestRefused(Exception):
    """The provider refused the request and retrying cannot help, but
    nothing is exhausted. The endpoint stays in service."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class RetryPolicy:
    """How this worker — and only this worker — backs off."""

    max_attempts: int = 6
    backoff_seconds: float = 2.0
    max_backoff_seconds: float = 120.0
    #: How long to park an endpoint whose short spend window is exhausted.
    window_cap_park_seconds: float = 300.0
    #: ...and its long window, or a rejected credential.
    terminal_cap_park_seconds: float = 3600.0

    def delay_for(self, attempt: int, retry_after: float | None, jitter: float) -> float:
        """Seconds to wait before `attempt` (1-based).

        The cap bounds the exponential *curve*, not the jittered result:
        capping the final value would put every capped worker back in
        lockstep, which is the phase-lock this jitter exists to break.
        """
        base = float(min(self.backoff_seconds * (2 ** (attempt - 1)), self.max_backoff_seconds))
        if retry_after is not None:
            base = max(base, retry_after)
        return base + base * jitter


@dataclass
class EndpointParks:
    """Per-endpoint cooldowns, local to this process.

    Not shared, not persisted, not locked. That is the design, not an
    omission: a shared park is a global pause wearing a different name.
    """

    _until: dict[str, float] = field(default_factory=dict)

    def park(self, endpoint: str, seconds: float, now: float) -> float:
        until = max(self._until.get(endpoint, 0.0), now + seconds)
        self._until[endpoint] = until
        return until

    def remaining(self, endpoint: str, now: float) -> float:
        return max(0.0, self._until.get(endpoint, 0.0) - now)

    def clear(self, endpoint: str | None = None) -> None:
        if endpoint is None:
            self._until.clear()
        else:
            self._until.pop(endpoint, None)


@dataclass(frozen=True)
class Route:
    """Where one role's calls go."""

    model: str
    endpoint: str
    provider: Provider = P.GENERIC
    api_key: str | None = None
    #: Anything the transport should pass through (temperature, max_tokens…).
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes | str


#: A transport is any callable that performs one request. Injected rather
#: than imported so the retry logic is testable without a network, and so a
#: caller can use whatever HTTP client it already has.
Transport = Callable[[Route, Sequence[Mapping[str, Any]], Mapping[str, Any]], Response]


class ModelClient:
    """Routes roles to models and survives their failures.

    `roles` maps a role name — "planner", "implementer", "reviewer", or
    whatever the caller's workflow uses — to a `Route`. The call site names
    the role; it never names a model. That is what makes the map changeable
    without touching the code that calls it.
    """

    def __init__(
        self,
        roles: Mapping[str, Route],
        transport: Transport,
        policy: RetryPolicy | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
        parks: EndpointParks | None = None,
        run_id: str | None = None,
        routes_provider: Callable[[], Mapping[str, Route]] | None = None,
    ) -> None:
        self.roles = dict(roles)
        # Consulted per call when set, so the role -> model map can be changed
        # while the fleet is running. The call site names a ROLE and never a
        # model, which is the whole reason that is possible; a provider lets
        # the new value come from somewhere outside this process.
        self.routes_provider = routes_provider
        self.transport = transport
        self.policy = policy or RetryPolicy()
        self.on_event = on_event
        self.sleep = sleep
        self.now = now
        self.jitter = jitter
        self.parks = parks or EndpointParks()
        # Identity for every event this client emits. A consumer deduplicating
        # a replayed stream needs to tell "the same attempt, read twice" from
        # "two attempts that happen to look alike" -- and two calls really can
        # be identical in every field including the timestamp. Inferring
        # identity from content silently merges them; assigning it here does
        # not. run_id changes per process, seq is monotonic within it.
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self._seq = itertools.count()

    def route_for(self, role: str) -> Route:
        if self.routes_provider is not None:
            live = self.routes_provider()
            if live:
                self.roles = dict(live)
        try:
            return self.roles[role]
        except KeyError:
            raise KeyError(
                f"no route for role {role!r}; known roles: {sorted(self.roles)}"
            ) from None

    def call(self, role: str, messages: Sequence[Mapping[str, Any]], **options: Any) -> Response:
        """Call `role`'s model. Returns the successful response.

        Raises `CapExhausted` if the endpoint is out of budget, `RequestRefused`
        if the provider refused and retrying cannot help, or `RuntimeError`
        once the attempt ladder is spent.
        """
        route = self.route_for(role)
        merged = {**route.options, **options}
        last: Classification | None = None

        for attempt in range(self.policy.max_attempts):
            if attempt > 0:
                delay = self.policy.delay_for(
                    attempt, last.retry_after if last else None, self.jitter()
                )
                self._emit(
                    role, route, "retry_wait", last, attempt=attempt, detail=f"waiting {delay:.1f}s"
                )
                self.sleep(delay)

            # This process's own park on this endpoint, from an earlier cap.
            parked = self.parks.remaining(route.endpoint, self.now())
            if parked > 0:
                self._emit(
                    role, route, "parked", None, attempt=attempt, detail=f"{parked:.0f}s remaining"
                )
                self.sleep(parked)

            started = self.now()
            response = self.transport(route, messages, merged)
            latency = self.now() - started

            if 200 <= response.status < 300:
                self._emit(role, route, "ok", None, attempt=attempt, latency=latency)
                return response

            verdict = route.provider.classify(response.status, response.headers, response.body)
            last = verdict
            self._emit(role, route, "error", verdict, attempt=attempt, latency=latency)

            if verdict.kind in P.CAPS:
                # Out of budget. Retrying cannot help, so park this endpoint
                # in this process and stop. Other workers and other endpoints
                # are untouched; the fleet still resumes unattended when the
                # window rolls over.
                park = (
                    self.policy.window_cap_park_seconds
                    if verdict.kind == P.WINDOW_CAP
                    else self.policy.terminal_cap_park_seconds
                )
                self.parks.park(route.endpoint, park, self.now())
                raise CapExhausted(
                    f"{route.model} via {route.endpoint}: {verdict.message or verdict.kind}",
                    kind=verdict.kind,
                    endpoint=route.endpoint,
                )
            if verdict.kind in (P.NON_RETRYABLE, P.FATAL):
                # Refused, but nothing is exhausted — so do NOT park. Parking
                # here would idle a healthy endpoint over one bad request.
                raise RequestRefused(
                    f"{route.model} via {route.endpoint}: {verdict.message or verdict.kind}",
                    kind=verdict.kind,
                )

        raise RuntimeError(
            f"{role}: {self.policy.max_attempts} attempts exhausted against "
            f"{route.model} via {route.endpoint}" + (f"; last was {last.kind}" if last else "")
        )

    def _emit(
        self,
        role: str,
        route: Route,
        outcome: str,
        verdict: Classification | None,
        attempt: int,
        latency: float | None = None,
        detail: str | None = None,
    ) -> None:
        """Append one structured outcome.

        Best-effort by contract: telemetry must never be able to fail a call
        that otherwise succeeded.
        """
        if self.on_event is None:
            return
        # Telemetry is never load-bearing: a broken sink must not turn a
        # call that succeeded into a failure.
        with contextlib.suppress(Exception):
            self.on_event(
                {
                    "run_id": self.run_id,
                    "seq": next(self._seq),
                    "ts": self.now(),
                    "role": role,
                    "model": route.model,
                    "endpoint": route.endpoint,
                    "provider": route.provider.name,
                    "outcome": outcome,
                    "error_class": verdict.kind if verdict else None,
                    "attempt": attempt,
                    "latency_s": None if latency is None else round(latency, 3),
                    "detail": detail or (verdict.message if verdict else None),
                }
            )
