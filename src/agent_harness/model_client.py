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
from .pricing import PriceTable, load_price_table, usage_fields
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


class RetryExhausted(RuntimeError):
    """The retryable attempt budget was spent without a successful response.

    This is deliberately distinct from a generic ``RuntimeError``: callers
    can hand the item back for a later item-level attempt while still treating
    refusals and spend caps as terminal conditions.
    """

    def __init__(
        self,
        message: str,
        *,
        role: str,
        kind: str | None,
        endpoint: str,
        model: str,
        last: Classification | None = None,
    ) -> None:
        super().__init__(message)
        self.role = role
        self.kind = kind
        self.endpoint = endpoint
        self.model = model
        self.last = last


# A descriptive alias for callers that want to spell out the common case.
TransientExhausted = RetryExhausted


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


#: Roles whose calls are never blocked by another role's cap.
#:
#: The reviewer is here because of a specific, expensive failure: if an
#: implementer exhausts a spend window and that parks the whole endpoint, the
#: fleet ends up holding patches that passed every gate and cannot be
#: reviewed, committed or merged. The money is already spent; withholding the
#: cheap call that turns it into a pull request loses the work as well as the
#: money.
#:
#: The planner is here for the same reason in reverse: it is the cheapest call
#: and it gates every expensive one, so parking it wastes the budget that is
#: left.
RINGFENCED_ROLES = frozenset({"reviewer", "planner"})


@dataclass
class EndpointParks:
    """Per-endpoint, per-role cooldowns, local to this process.

    Not shared, not persisted, not locked. That is the design, not an
    omission: a shared park is a global pause wearing a different name.

    Keyed by role as well as endpoint so one role's cap cannot silently
    withhold another's. A single per-endpoint park is indistinguishable from
    a working fleet right up until you notice nothing has been reviewed for
    an hour.
    """

    _until: dict[tuple[str, str], float] = field(default_factory=dict)

    def park(self, endpoint: str, seconds: float, now: float, role: str = "") -> float:
        key = (endpoint, role)
        until = max(self._until.get(key, 0.0), now + seconds)
        self._until[key] = until
        return until

    def remaining(self, endpoint: str, now: float, role: str = "") -> float:
        """How long this role must wait on this endpoint.

        A ringfenced role ignores parks belonging to other roles: it is
        allowed to try and to be refused on its own merits, rather than being
        pre-emptively silenced by somebody else's spending.
        """
        own = max(0.0, self._until.get((endpoint, role), 0.0) - now)
        if role in RINGFENCED_ROLES:
            return own
        # Non-ringfenced roles also respect an endpoint-wide park, which is
        # what a park with no role recorded means.
        shared = max(0.0, self._until.get((endpoint, ""), 0.0) - now)
        return max(own, shared)

    def clear(self, endpoint: str | None = None) -> None:
        if endpoint is None:
            self._until.clear()
        else:
            for key in [k for k in self._until if k[0] == endpoint]:
                self._until.pop(key, None)


@dataclass(frozen=True)
class Route:
    """Where one role's calls go."""

    model: str
    endpoint: str
    provider: Provider = P.GENERIC
    api_key: str | None = None
    #: Anything the transport should pass through (temperature, max_tokens…).
    options: Mapping[str, Any] = field(default_factory=dict)


#: One role's routes, in the order they are tried. A single `Route` is the
#: one-element case, which is why almost nothing outside this module had to
#: change: `route_for` still answers with the preferred one.
Chain = tuple[Route, ...]


def _as_chain(value: Route | Sequence[Route]) -> Chain:
    return (value,) if isinstance(value, Route) else tuple(value)


def _chain_names(chain: Chain) -> str:
    """The chain as an operator reads it: which models, in which order."""
    return ", ".join(f"{route.model} via {route.endpoint}" for route in chain)


def _fell_back(chain: Chain, used: Route) -> str | None:
    """Said out loud when a call was served by anything but the first choice.

    A fleet quietly running on its third-choice model for a week is a fleet
    whose costs and results nobody can explain, so the event stream records
    which one answered rather than only that something did.
    """
    if used is chain[0]:
        return None
    return f"fell back to {used.model} (preferred {chain[0].model})"


@dataclass
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes | str


def routes_from_map(
    stored: Mapping[str, Mapping[str, Any]] | None,
    *,
    api_key: str | None = None,
    default_provider: Provider = P.CLAW_BAY,
) -> dict[str, Route]:
    """The persisted role map, as routes.

    One conversion, shared by `run`, by `serve` and by every readiness
    report, so the map an operator reads is the map the fleet calls. A role
    missing a model or an endpoint is dropped rather than half-built: it is
    not a route, and preflight's job is to name it as missing rather than to
    fail on the first call that uses it.
    """
    routes: dict[str, Route] = {}
    for name, spec in (stored or {}).items():
        chain = _chain_from_spec(spec, api_key=api_key, default_provider=default_provider)
        if chain:
            routes[name] = chain[0]
    return routes


def chains_from_map(
    stored: Mapping[str, Mapping[str, Any]] | None,
    *,
    api_key: str | None = None,
    default_provider: Provider = P.CLAW_BAY,
) -> dict[str, Chain]:
    """The persisted role map, as fallback chains.

    Same source as `routes_from_map`, which answers with each role's preferred
    route for everything that *reports* on configuration. This one is what the
    client calls with, because the second and third choices only matter at the
    moment the first will not answer.
    """
    chains: dict[str, Chain] = {}
    for name, spec in (stored or {}).items():
        chain = _chain_from_spec(spec, api_key=api_key, default_provider=default_provider)
        if chain:
            chains[name] = chain
    return chains


def _chain_from_spec(
    spec: Mapping[str, Any], *, api_key: str | None, default_provider: Provider
) -> Chain:
    """One role's stored spec as an ordered chain.

    Accepts `model` as a single name or as a list, so a map written before
    fallbacks existed still reads correctly and a role that names one model is
    not forced into list syntax. A role missing a model or an endpoint is
    dropped rather than half-built: it is not a route, and preflight's job is
    to name it as missing rather than to fail on the first call that uses it.
    """
    endpoint = spec.get("endpoint")
    models = spec.get("models") or spec.get("model")
    if not endpoint or not models:
        return ()
    if isinstance(models, str):
        models = [models]
    provider = P.PROVIDERS.get(str(spec.get("provider", "")), default_provider)
    return tuple(
        Route(str(model), str(endpoint), provider, api_key=api_key)
        for model in models
        if str(model).strip()
    )


def effective_routes[R](
    global_routes: Mapping[str, R], project_routes: Mapping[str, R] | None
) -> dict[str, R]:
    """The global role map with one project's overrides applied.

    Per role, not wholesale. Choosing one map or the other was the defect:
    a project that overrode only its reviewer passed preflight on that
    override and was then executed against the *global* reviewer, or failed
    with `no route for role reviewer` when the global map had none. A partial
    project map inherits every role it does not name.
    """
    return {**global_routes, **(project_routes or {})}


def reviewer_independence(
    roles: Mapping[str, Route], *, implemented_by: str = ""
) -> tuple[bool, str]:
    """Whether the reviewer is independent of whatever wrote the code.

    Returns (independent, why). This was documented in three places and
    enforced in none, which meant a reviewer could be the same model as the
    implementer and nothing would say so -- some share of reviews being a
    model grading its own work, invisibly.

    `implemented_by` names the thing that actually implements when it is not
    a routed role: the agent command, in session mode. Comparing against the
    *configured* implementer there describes a pairing that never happens --
    session mode never calls that route -- so the verdict was about two
    things that never meet.

    Reported rather than refused: a single-model setup is a legitimate thing
    to run deliberately, and blocking it would be the harness overruling an
    operator about their own budget. What it must not be is a surprise.
    """
    reviewer = roles.get("reviewer")
    if implemented_by:
        if reviewer is None:
            return (True, "no implementer/reviewer pair configured")
        return (
            True,
            f"{reviewer.model} reviews work written by `{implemented_by}`, which this "
            "harness does not route: the two are not the same model, and nothing here "
            "knows which vendor is behind the agent",
        )
    implementer = roles.get("implementer")
    if reviewer is None or implementer is None:
        return (True, "no implementer/reviewer pair configured")
    if reviewer.model == implementer.model:
        return (
            False,
            f"reviewer and implementer are the same model ({reviewer.model}): "
            "every review is a model grading its own work",
        )
    if reviewer.provider.name == implementer.provider.name:
        return (
            False,
            f"reviewer and implementer share a provider ({reviewer.provider.name}): "
            "reviews are independent of the model but not of the vendor",
        )
    return (True, f"{reviewer.model} reviews {implementer.model}")


#: The cheapest question that proves a model is actually being served. An
#: endpoint advertising a model in `/models` is not the same claim.
PROBE_MESSAGES: tuple[Mapping[str, Any], ...] = ({"role": "user", "content": "ping"},)


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
        roles: Mapping[str, Route | Sequence[Route]],
        transport: Transport,
        policy: RetryPolicy | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        prices: PriceTable | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
        parks: EndpointParks | None = None,
        run_id: str | None = None,
        routes_provider: Callable[[], Mapping[str, Route | Sequence[Route]]] | None = None,
    ) -> None:
        # Either form, on purpose: this is a public attribute that callers and
        # tests assign to, and a bare `Route` put there by hand is the
        # one-element chain it looks like. Normalised on read.
        self.roles: dict[str, Route | Chain] = {
            name: _as_chain(value) for name, value in roles.items()
        }
        # Consulted per call when set, so the role -> model map can be changed
        # while the fleet is running. The call site names a ROLE and never a
        # model, which is the whole reason that is possible; a provider lets
        # the new value come from somewhere outside this process.
        self.routes_provider = routes_provider
        self.transport = transport
        self.policy = policy or RetryPolicy()
        self.on_event = on_event
        # Loaded once. Unknown models stay unpriced rather than free -- the
        # cost lands as null and the API counts it separately, which is the
        # only way an incomplete total can announce itself.
        self.prices = prices if prices is not None else load_price_table()
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

    def reviewer_independence(self, implemented_by: str = "") -> tuple[bool, str]:
        """Whether this client's reviewer is independent of the implementer.

        The logic is a free function so that a caller holding a *project's*
        effective map -- which this client's own map may not be -- gets the
        same answer from the same code.
        """
        # The preferred route per role: a fallback that has not been needed
        # is not what the operator configured, and is not what they should be
        # told about their reviewer.
        preferred = {
            name: _as_chain(value)[0] for name, value in self.roles.items() if _as_chain(value)
        }
        return reviewer_independence(preferred, implemented_by=implemented_by)

    def routed_by(
        self, routes_provider: Callable[[], Mapping[str, Route | Sequence[Route]]]
    ) -> ModelClient:
        """A sibling client that resolves routes differently.

        Transport, retry policy, prices, telemetry and — deliberately — the
        endpoint parks are shared: a spend cap belongs to the endpoint and
        this process, not to whichever project happened to hit it first, and
        a per-project copy of the parks would let every project rediscover
        the same exhausted window at full price.

        The run id is *not* shared. Two clients emitting the same (run_id,
        seq) pair would make two different attempts indistinguishable to a
        consumer deduplicating a replayed stream, which is exactly what that
        identity exists to prevent.
        """
        return ModelClient(
            roles=routes_provider(),
            transport=self.transport,
            policy=self.policy,
            on_event=self.on_event,
            prices=self.prices,
            sleep=self.sleep,
            now=self.now,
            jitter=self.jitter,
            parks=self.parks,
            routes_provider=routes_provider,
        )

    def answers(self, route: Route, *, timeout: float = 10.0) -> tuple[bool, str]:
        """Whether a route's model replies at all, in one request.

        Deliberately not `call`: the ladder is six attempts with escalating
        backoff, which is the ~20 minutes *per item* that discovering an
        unusable model the expensive way costs. This asks once, briefly, and
        reports what came back.

        The detail names the model and the status, because
        "claude-sonnet-4-6 returned HTTP 504" names the thing to change and
        "not ready" does not. No parking and no telemetry: a probe must not
        idle an endpoint for the fleet, and a readiness poll is not a model
        call anybody should find in their cost rollup.
        """
        options = {**route.options, "max_tokens": 1, "timeout": timeout}
        try:
            response = self.transport(route, PROBE_MESSAGES, options)
        except Exception as exc:  # noqa: BLE001 - any failure is the same answer
            return (
                False,
                f"{route.model} could not be reached at {route.endpoint}: "
                f"{type(exc).__name__}: {str(exc)[:160]}",
            )
        if 200 <= response.status < 300:
            return (True, f"{route.model} answered")
        verdict = route.provider.classify(response.status, response.headers, response.body)
        return (
            False,
            f"{route.model} returned HTTP {response.status}"
            + (f": {verdict.message[:160]}" if verdict.message else ""),
        )

    def routes_for(self, role: str) -> Chain:
        """Every route for a role, preferred first.

        More than one is a fallback chain, not a pool: the first that answers
        does the work, and the rest exist because a provider being down is a
        normal Tuesday. Measured on the endpoint this runs against, 34 of 42
        advertised models were unavailable at once -- an ordering that names a
        second and third choice is the difference between a fleet that pauses
        and one that carries on.
        """
        if self.routes_provider is not None:
            live = self.routes_provider()
            if live:
                self.roles = {name: _as_chain(value) for name, value in live.items()}
        try:
            # Normalised on read as well as on write: `roles` is a plain
            # attribute callers assign to, and a bare `Route` put there by
            # hand is the one-element chain it looks like.
            return _as_chain(self.roles[role])
        except KeyError:
            raise KeyError(
                f"no route for role {role!r}; known roles: {sorted(self.roles)}"
            ) from None

    def route_for(self, role: str) -> Route:
        """The preferred route for a role: what this deployment means to use.

        Everything that *reports* on routing -- readiness, the role map,
        reviewer independence -- asks this, because a fallback that has not
        been needed is not what the operator configured.
        """
        return self.routes_for(role)[0]

    def call(self, role: str, messages: Sequence[Mapping[str, Any]], **options: Any) -> Response:
        """Call `role`'s model. Returns the successful response.

        Raises `CapExhausted` if the endpoint is out of budget, `RequestRefused`
        if the provider refused and retrying cannot help, or `RetryExhausted`
        once the retryable attempt ladder is spent.
        """
        chain = self.routes_for(role)
        last: Classification | None = None
        last_route = chain[0]

        for attempt in range(self.policy.max_attempts):
            if attempt > 0:
                delay = self.policy.delay_for(
                    attempt, last.retry_after if last else None, self.jitter()
                )
                self._emit(
                    role,
                    last_route,
                    "retry_wait",
                    last,
                    attempt=attempt,
                    detail=f"waiting {delay:.1f}s",
                )
                self.sleep(delay)

            # One pass down the chain before any backoff. A model that is
            # down answers immediately, so trying the alternatives first costs
            # nothing and gets the work moving; backing off against a dead
            # provider for minutes before even looking at the second choice
            # would waste the fallback entirely.
            kinds: list[str | None] = []
            for route in chain:
                merged = {**route.options, **options}
                last_route = route

                parked = self.parks.remaining(route.endpoint, self.now(), role)
                if parked > 0 and len(chain) > 1:
                    # Somewhere else to go, so skip rather than sleep.
                    self._emit(
                        role,
                        route,
                        "skipped",
                        None,
                        attempt=attempt,
                        detail=f"parked {parked:.0f}s",
                    )
                    kinds.append(P.WINDOW_CAP)
                    continue
                if parked > 0:
                    self._emit(
                        role,
                        route,
                        "parked",
                        None,
                        attempt=attempt,
                        detail=f"{parked:.0f}s remaining",
                    )
                    self.sleep(parked)

                started = self.now()
                try:
                    response = self.transport(route, messages, merged)
                except (ConnectionError, TimeoutError) as exc:
                    # Wire failures have no HTTP response for a provider to
                    # classify. They are nevertheless transient in the one
                    # useful, vendor-neutral sense: the same request may
                    # succeed when the connection is available again. Keep
                    # them inside the ordinary per-worker retry ladder so a
                    # timeout cannot escape as an item failure or create any
                    # fleet-wide state.
                    latency = self.now() - started
                    last = Classification(
                        P.TRANSIENT,
                        f"{type(exc).__name__}: {str(exc)[:300]}",
                    )
                    kinds.append(last.kind)
                    self._emit(
                        role,
                        route,
                        "error",
                        last,
                        attempt=attempt,
                        latency=latency,
                    )
                    continue
                latency = self.now() - started

                if 200 <= response.status < 300:
                    self._emit(
                        role,
                        route,
                        "ok",
                        None,
                        attempt=attempt,
                        latency=latency,
                        usage=usage_fields(response.body, route.model, self.prices),
                        detail=_fell_back(chain, route),
                    )
                    return response

                verdict = route.provider.classify(response.status, response.headers, response.body)
                last = verdict
                kinds.append(verdict.kind)
                self._emit(role, route, "error", verdict, attempt=attempt, latency=latency)

                if verdict.kind in P.CAPS:
                    # Out of budget on this endpoint. Park it -- retrying it
                    # cannot help -- and try the next model rather than
                    # stopping, which is the whole point of naming one.
                    self.parks.park(
                        route.endpoint,
                        self.policy.window_cap_park_seconds
                        if verdict.kind == P.WINDOW_CAP
                        else self.policy.terminal_cap_park_seconds,
                        self.now(),
                        role,
                    )
                # A refusal is NOT parked: it says something about this
                # request, not about the model's health, and idling a healthy
                # endpoint over one bad prompt would be a self-inflicted
                # outage. The next route is still tried, because a refusal
                # from one vendor is routinely an answer from another.

            if not any(kind == P.TRANSIENT or kind == P.RPM for kind in kinds):
                # Nothing another cycle could fix: every route was refused or
                # is out of budget. Say which, in the terms the executor acts
                # on -- a cap hands the item back untouched, a refusal is the
                # model's answer.
                if all(kind in P.CAPS for kind in kinds):
                    raise CapExhausted(
                        f"{role}: every route is out of budget "
                        f"({_chain_names(chain)}): {last.message if last else ''}",
                        kind=last.kind if last else P.WINDOW_CAP,
                        endpoint=last_route.endpoint,
                    )
                raise RequestRefused(
                    f"{role}: every route refused ({_chain_names(chain)})"
                    f": {last.message if last else ''}",
                    kind=last.kind if last else P.NON_RETRYABLE,
                )

        message = (
            f"{role}: {self.policy.max_attempts} attempts exhausted against "
            f"{_chain_names(chain)}" + (f"; last was {last.kind}" if last else "")
        )
        raise RetryExhausted(
            message,
            role=role,
            kind=last.kind if last else None,
            endpoint=last_route.endpoint,
            model=last_route.model,
            last=last,
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
        usage: Mapping[str, Any] | None = None,
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
                    # Absent, not zeroed, when the provider reported no usage.
                    # An event carrying zeros is a claim that the call was
                    # free; an event with no usage keys is honestly silent.
                    **(dict(usage) if usage else {}),
                }
            )
