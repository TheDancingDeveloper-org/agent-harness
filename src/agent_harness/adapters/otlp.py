"""The event stream, projected to OpenTelemetry spans. Export only.

Every comparable system exports OpenTelemetry and this one exported none. That
is a real gap for anyone running it beside other services: the harness's own
record is complete and lives in a place nothing else can join to.

This is an **adapter**, in `adapters/`, opt-in and lazily loaded. No core module
imports an OpenTelemetry package, and a build with the exporter absent runs
identically — `tests/test_generic.py` asserts the first and this module's own
tests assert the second.

## Export only, and what that costs

**D13, and the safe answer: a span is a projection, never a source.** Nothing
here is written back to the event store, nothing is read back to answer a
question the events could answer, and no code path consults a span for anything.
`AGENTS.md`'s rule about projections applies unchanged: a second source of truth
is a second thing to disagree with the first.

The practical consequence is that a deployment can lose every span with no
effect on correctness, and that is deliberate. **Observation must never stop
work**, so an exporter that raises is caught, counted and ignored.

## What it does not claim

**It does not close #128.** Session-mode implementer traffic never passes
through `ModelClient`, so it produces no `model_call` event, so it produces no
span. Exporting what we have does not create what we do not have. What this adds
for session mode is a *place to self-report* — `record_agent_usage` is a door an
agent process can call through — and a door nobody has walked through yet is not
a measurement.

`coverage()` reports the fraction of model traffic represented, so the gap is a
number rather than a caveat somebody remembers.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: Where an operator points the exporter. Absent means the exporter is off,
#: which is the default: telemetry that turns itself on is telemetry nobody
#: consented to send anywhere.
ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"

#: The name this harness reports itself as, overridable because a deployment
#: running several fleets wants to tell them apart.
SERVICE_NAME_ENV = "OTEL_SERVICE_NAME"
DEFAULT_SERVICE_NAME = "agent-harness"

# ----------------------------------------------------------- span shapes

#: Span kinds, matching the three things the proposal asks for: model calls,
#: gates, and the item lifecycle.
MODEL_CALL = "model_call"
GATE = "gate"
ITEM = "item"

#: The event outcomes that are a *gate* answering, as opposed to the item
#: moving. Listed rather than inferred: "anything containing `check`" would
#: silently reclassify the next outcome somebody adds.
GATE_OUTCOMES = {
    "checks_passed": ("checks", "ok"),
    "checks_failed": ("checks", "error"),
    "fix_available": ("checks", "ok"),
    "review_approved": ("review", "ok"),
    "review_rejected": ("review", "error"),
    "patch_malformed": ("patch", "error"),
    "patch_suspect": ("patch", "ok"),
    "apply_failed": ("apply", "error"),
    "applied": ("apply", "ok"),
    "dependency_invalidated": ("graph", "error"),
    "budget_exceeded": ("budget", "error"),
    "budget_unenforceable": ("budget", "ok"),
}

#: Item lifecycle transitions. `started` opens the item's span conceptually;
#: the terminal ones close it. Recorded as discrete spans rather than one long
#: one because the harness does not hold an item's span open across a worker
#: death, and pretending otherwise would produce spans that never end.
ITEM_OUTCOMES = {
    "started": "ok",
    "done": "ok",
    "error": "error",
    "retry_exhausted": "error",
    "budget_exhausted": "error",
    "claim_lost": "error",
    "held": "ok",
    "resumed": "ok",
    "checkpointed": "ok",
    "no_diff": "error",
    "no_changes": "error",
    "agent_timeout": "error",
    "agent_failed": "error",
    "context_unavailable": "error",
}


@dataclass(frozen=True)
class Span:
    """One span, in this harness's own terms.

    Deliberately not an OpenTelemetry object. The projection is what is worth
    testing and it is testable without the SDK installed; handing these to a
    real tracer is a separate, thin step that needs the dependency.
    """

    name: str
    kind: str
    attributes: dict[str, Any] = field(default_factory=dict)
    start: float = 0.0
    end: float = 0.0
    status: str = "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "attributes": dict(self.attributes),
            "start": self.start,
            "end": self.end,
            "status": self.status,
        }


def _identity(event: Mapping[str, Any]) -> dict[str, Any]:
    """What joins a span back to the event rows it came from.

    Every span carries these, and that is the whole point of the exercise: a
    span nobody can join to the append-only record is a second story about the
    same run.
    """
    found: dict[str, Any] = {}
    for key, attribute in (
        ("run_id", "harness.run_id"),
        ("seq", "harness.seq"),
        ("item_id", "harness.item_id"),
        ("project_id", "harness.project_id"),
        ("worker", "harness.worker"),
        ("issue", "harness.issue"),
    ):
        value = event.get(key)
        if value not in (None, ""):
            found[attribute] = value
    return found


def span_for(event: Mapping[str, Any]) -> Span | None:
    """One event, as a span. None for events that are not one of the three.

    Returning None rather than inventing a span for everything is deliberate:
    a trace in which every log line is a span is a trace nobody reads.
    """
    outcome = str(event.get("outcome") or "")
    ts = float(event.get("ts") or 0.0)

    # A model call: it has a role and a model, which nothing else does.
    if event.get("role") and event.get("model"):
        latency = float(event.get("latency_s") or 0.0)
        attributes = {
            # OpenTelemetry GenAI semantic conventions, as far as they reach.
            # The ones this harness cannot honestly fill are simply absent
            # rather than defaulted.
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": event.get("model"),
            "gen_ai.system": event.get("provider") or event.get("preset") or "unknown",
            "harness.role": event.get("role"),
            "harness.endpoint": event.get("endpoint"),
            "harness.outcome": outcome,
            **_identity(event),
        }
        for key, attribute in (
            ("tokens_in", "gen_ai.usage.input_tokens"),
            ("tokens_out", "gen_ai.usage.output_tokens"),
        ):
            if event.get(key) is not None:
                attributes[attribute] = event[key]
        if event.get("error_class"):
            attributes["error.type"] = event["error_class"]
        return Span(
            name=f"chat {event.get('model')}",
            kind=MODEL_CALL,
            attributes=attributes,
            start=ts - latency,
            end=ts,
            status="error" if outcome not in ("ok", "") else "ok",
        )

    if outcome in GATE_OUTCOMES:
        gate, status = GATE_OUTCOMES[outcome]
        return Span(
            name=f"gate {gate}",
            kind=GATE,
            attributes={
                "harness.gate": gate,
                "harness.outcome": outcome,
                **({"error.type": event["error_class"]} if event.get("error_class") else {}),
                **_identity(event),
            },
            start=ts,
            end=ts,
            status=status,
        )

    if outcome in ITEM_OUTCOMES:
        return Span(
            name=f"item {outcome}",
            kind=ITEM,
            attributes={"harness.outcome": outcome, **_identity(event)},
            start=ts,
            end=ts,
            status=ITEM_OUTCOMES[outcome],
        )

    return None


# ------------------------------------------------------------- coverage


@dataclass
class Coverage:
    """How much of the model traffic these spans actually represent.

    A caveat in prose is a caveat somebody forgets. This is the same statement
    as a number, and it is deliberately pessimistic: `unknown` counts anything
    the exporter cannot vouch for.
    """

    exported: int = 0
    #: Calls the harness knows happened and cannot describe — session-mode
    #: agents that self-reported a count without a per-call record.
    self_reported: int = 0
    #: Spans an exporter refused. Observation never stops work, so these are
    #: counted and dropped rather than raised.
    dropped: int = 0

    @property
    def fraction(self) -> float | None:
        """The share of known model traffic that produced a span.

        None when nothing is known at all — which is not the same as 100%, and
        must never be reported as it.
        """
        total = self.exported + self.self_reported
        return (self.exported / total) if total else None

    def describe(self) -> str:
        share = self.fraction
        if share is None:
            return "no model traffic has been observed at all"
        return (
            f"{self.exported} of {self.exported + self.self_reported} known model calls "
            f"were exported as spans ({share:.0%}); {self.self_reported} were self-reported "
            f"by a session-mode agent with no per-call record, and {self.dropped} span(s) "
            "were dropped by the exporter. **Session-mode implementer traffic that "
            "self-reports nothing is not in either number** — it never reaches "
            "ModelClient, so nothing here can count it (#128)."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "exported": self.exported,
            "self_reported": self.self_reported,
            "dropped": self.dropped,
            "fraction": self.fraction,
            "note": self.describe(),
        }


# ------------------------------------------------------------- exporting


class Exporter:
    """Projects events to spans and hands them to a tracer, if there is one.

    Without the OpenTelemetry SDK installed, or without an endpoint
    configured, `available` is False and every call is a no-op that still
    counts coverage. That is the mode the whole test suite runs in, which is
    how "the harness runs identically without it" is asserted rather than
    hoped.
    """

    def __init__(
        self,
        emit: Any | None = None,
        *,
        endpoint: str | None = None,
        service_name: str | None = None,
    ) -> None:
        self.endpoint = endpoint if endpoint is not None else os.environ.get(ENDPOINT_ENV, "")
        self.service_name = service_name or os.environ.get(SERVICE_NAME_ENV, DEFAULT_SERVICE_NAME)
        self.coverage = Coverage()
        #: Injected sink, for a test or for a deployment that already owns a
        #: tracer. When absent, a real OTLP tracer is built lazily on first
        #: use — and only then, so importing this module costs nothing.
        self._emit = emit
        self._tracer: Any = None
        self._tried = False

    @property
    def available(self) -> bool:
        return self._emit is not None or bool(self.endpoint)

    def _tracer_or_none(self) -> Any:
        if self._tried:
            return self._tracer
        self._tried = True
        if not self.endpoint:
            return None
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError:
            # Named once, at the level an operator can act on. Not an error:
            # a build without the SDK is a supported configuration.
            log.info(
                "otlp: %s is set but the OpenTelemetry SDK is not installed; "
                "no spans will be exported and nothing else changes",
                ENDPOINT_ENV,
            )
            return None
        provider = TracerProvider(resource=Resource.create({"service.name": self.service_name}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=self.endpoint)))
        self._tracer = trace.get_tracer(__name__, tracer_provider=provider)
        return self._tracer

    def export(self, event: Mapping[str, Any]) -> Span | None:
        """One event out. Never raises, whatever the exporter does.

        Observation must never stop work — the rule the audit layer already
        keeps — so a broken collector costs a counter increment and nothing
        else.
        """
        try:
            span = span_for(event)
        except Exception:  # noqa: BLE001 - a projection bug must not fail an item
            log.warning("otlp: could not project an event to a span", exc_info=True)
            self.coverage.dropped += 1
            return None
        if span is None:
            return None
        try:
            self._send(span)
        except Exception:  # noqa: BLE001 - a broken collector must not stop the fleet
            log.warning("otlp: the exporter refused a span", exc_info=True)
            self.coverage.dropped += 1
            return span
        if span.kind == MODEL_CALL:
            self.coverage.exported += 1
        return span

    def _send(self, span: Span) -> None:
        if self._emit is not None:
            self._emit(span)
            return
        tracer = self._tracer_or_none()
        if tracer is None:
            return
        with tracer.start_as_current_span(span.name) as otel_span:
            for key, value in span.attributes.items():
                if value is not None:
                    otel_span.set_attribute(key, value)

    def record_agent_usage(self, item_id: str, calls: int = 1, **attributes: Any) -> None:
        """A door for a session-mode agent to say what it spent.

        It closes part of #128's hole and it does not close #128. An agent that
        calls this is counted as `self_reported`, which lowers the exported
        fraction rather than raising it — because a count with no per-call
        record is honestly *less* than a span, and the number should say so.

        Nobody calls this yet. It is a door, and a door nobody has walked
        through is not a measurement.
        """
        del item_id, attributes
        self.coverage.self_reported += max(0, calls)

    def tap(self, downstream: Any) -> Any:
        """Wrap an event sink so events flow on unchanged and also become spans.

        The downstream sink is called **first**, and its exceptions propagate.
        The event store is the source of truth; exporting must never come
        between an event and the record of it.
        """

        def sink(event: Mapping[str, Any]) -> None:
            downstream(event)
            self.export(event)

        return sink


def spans_for(events: Iterable[Mapping[str, Any]]) -> list[Span]:
    """A whole stream, projected. For a backfill, or for a report."""
    found = [span_for(event) for event in events]
    return [span for span in found if span is not None]


def exporter(emit: Any | None = None) -> Exporter:
    """The entry point a deployment reaches by name."""
    return Exporter(emit)


#: Stamped on nothing and read by nothing; here so a reader who arrives via
#: `grep OTEL` finds the rule rather than only the code.
EXPORT_ONLY = True


def now() -> float:
    return time.time()
