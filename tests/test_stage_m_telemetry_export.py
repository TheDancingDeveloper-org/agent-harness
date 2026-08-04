"""Stage M: the event stream projected to OpenTelemetry spans. Export only.

**These tests run with the OpenTelemetry SDK deliberately not installed.** That
is not an omission — "with the exporter absent, every test still passes and no
work stops" is §10.3's second criterion, and running the whole suite without the
package is how it is asserted rather than hoped.

What is therefore tested is the **projection**: which events become spans, what
those spans carry, and that nothing anywhere reads one back. What is *not*
tested is the OTLP wire, and §9 of the evidence report says so.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_harness.adapters import otlp


def model_call(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "run_id": "run-1",
        "seq": 3,
        "ts": 1000.0,
        "role": "implementer",
        "model": "a-model",
        "endpoint": "https://api.example",
        "provider": "generic",
        "preset": "chat-completions",
        "outcome": "ok",
        "latency_s": 2.5,
        "tokens_in": 1200,
        "tokens_out": 300,
    }
    event.update(overrides)
    return event


def work(outcome: str, **overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "ts": 2000.0,
        "kind": "work",
        "worker": "host:1",
        "item_id": "T4",
        "project_id": "widgets",
        "outcome": outcome,
    }
    event.update(overrides)
    return event


# ------------------------------------------------ the three kinds of span


def test_a_model_call_becomes_a_genai_span() -> None:
    """§10.3's first criterion, model-call half. GenAI semantic conventions as
    far as they honestly reach."""
    span = otlp.span_for(model_call())
    assert span is not None
    assert span.kind == otlp.MODEL_CALL
    assert span.name == "chat a-model"
    assert span.attributes["gen_ai.operation.name"] == "chat"
    assert span.attributes["gen_ai.request.model"] == "a-model"
    assert span.attributes["gen_ai.usage.input_tokens"] == 1200
    assert span.attributes["gen_ai.usage.output_tokens"] == 300
    # Latency turned into a real interval rather than a zero-length span.
    assert span.start == pytest.approx(997.5)
    assert span.end == 1000.0


def test_a_call_that_reported_no_tokens_carries_no_token_attributes() -> None:
    """Absent, not zero. A span claiming zero tokens is a measurement saying
    the call was free — the same rule `pricing.py` keeps."""
    span = otlp.span_for(model_call(tokens_in=None, tokens_out=None))
    assert span is not None
    assert "gen_ai.usage.input_tokens" not in span.attributes
    assert "gen_ai.usage.output_tokens" not in span.attributes


@pytest.mark.parametrize(
    ("outcome", "gate", "status"),
    [
        ("checks_passed", "checks", "ok"),
        ("checks_failed", "checks", "error"),
        ("review_approved", "review", "ok"),
        ("review_rejected", "review", "error"),
        ("apply_failed", "apply", "error"),
        ("budget_exceeded", "budget", "error"),
    ],
)
def test_a_gate_becomes_a_gate_span(outcome: str, gate: str, status: str) -> None:
    """§10.3's first criterion, gate half."""
    span = otlp.span_for(work(outcome))
    assert span is not None
    assert span.kind == otlp.GATE
    assert span.attributes["harness.gate"] == gate
    assert span.status == status


@pytest.mark.parametrize("outcome", ["started", "done", "error", "held", "resumed"])
def test_an_item_transition_becomes_an_item_span(outcome: str) -> None:
    """§10.3's first criterion, lifecycle half."""
    span = otlp.span_for(work(outcome))
    assert span is not None
    assert span.kind == otlp.ITEM
    assert span.attributes["harness.outcome"] == outcome


def test_an_event_that_is_none_of_the_three_produces_nothing() -> None:
    """A trace in which every log line is a span is a trace nobody reads."""
    assert otlp.span_for(work("calling")) is None
    assert otlp.span_for(work("planner_targets")) is None


def test_every_span_joins_back_to_the_event_rows() -> None:
    """§10.3: a run and item identity that joins to the event rows.

    A span nobody can join to the append-only record is a second story about
    the same run, which is the thing this stage must not become.
    """
    call = otlp.span_for(model_call(item_id="T4", project_id="widgets"))
    gate = otlp.span_for(work("checks_passed"))
    item = otlp.span_for(work("done"))
    assert call is not None and gate is not None and item is not None

    assert call.attributes["harness.run_id"] == "run-1"
    assert call.attributes["harness.seq"] == 3
    for span in (call, gate, item):
        assert span.attributes["harness.item_id"] == "T4"
        assert span.attributes["harness.project_id"] == "widgets"


def test_a_failure_class_reaches_the_span() -> None:
    span = otlp.span_for(model_call(outcome="rate_limited", error_class="window_cap"))
    assert span is not None
    assert span.attributes["error.type"] == "window_cap"
    assert span.status == "error"


def test_a_whole_stream_projects(tmp_path: Path) -> None:
    events = [work("started"), model_call(), work("checks_passed"), work("done"), work("calling")]
    spans = otlp.spans_for(events)
    assert [s.kind for s in spans] == [otlp.ITEM, otlp.MODEL_CALL, otlp.GATE, otlp.ITEM]


# --------------------------------------------- absent, and still harmless


def test_with_no_endpoint_the_exporter_is_unavailable_and_silent() -> None:
    """§10.3's second criterion. This is the mode the entire suite runs in."""
    exporter = otlp.Exporter(endpoint="")
    assert not exporter.available
    assert exporter.export(model_call()) is not None  # still projected
    assert exporter.coverage.dropped == 0


def test_a_broken_exporter_never_stops_work() -> None:
    """Observation must never stop work — the rule the audit layer keeps."""

    def explode(span: otlp.Span) -> None:
        raise RuntimeError("the collector is on fire")

    exporter = otlp.Exporter(emit=explode)
    span = exporter.export(model_call())
    assert span is not None, "it still projected"
    assert exporter.coverage.dropped == 1
    assert exporter.coverage.exported == 0


def test_the_tap_writes_the_event_first_and_exports_second() -> None:
    """The event store is the source of truth. Exporting must never come
    between an event and the record of it."""
    order: list[str] = []
    exporter = otlp.Exporter(emit=lambda span: order.append("span"))

    def downstream(event: dict[str, Any]) -> None:
        order.append("event")

    sink = exporter.tap(downstream)
    sink(model_call())
    assert order == ["event", "span"]


def test_a_failing_event_store_is_not_swallowed_by_the_tap() -> None:
    """The downstream sink's exceptions propagate. A telemetry wrapper that
    silently ate a failed write to the record of truth would be worse than no
    telemetry at all."""
    exporter = otlp.Exporter(emit=lambda span: None)

    def downstream(event: dict[str, Any]) -> None:
        raise OSError("the disk is full")

    with pytest.raises(OSError, match="disk is full"):
        exporter.tap(downstream)(model_call())


def test_the_sdk_is_not_installed_and_that_is_the_point() -> None:
    """Stated as a test so that installing it becomes a deliberate decision
    with a visible consequence rather than a quiet change to what the suite
    is proving."""
    with pytest.raises(ImportError):
        import opentelemetry  # noqa: F401


def test_asking_for_an_endpoint_without_the_sdk_says_so_and_carries_on() -> None:
    exporter = otlp.Exporter(endpoint="http://collector.invalid:4318")
    assert exporter.available, "configured, even though it cannot deliver"
    assert exporter.export(model_call()) is not None
    # No tracer could be built, so nothing was sent and nothing was dropped:
    # this is a supported configuration, not a failure.
    assert exporter.coverage.dropped == 0


# ------------------------------------------------------------ coverage


def test_coverage_reports_what_fraction_was_exported() -> None:
    """§10.3's third criterion, as a number rather than a caveat."""
    exporter = otlp.Exporter(emit=lambda span: None)
    for _ in range(3):
        exporter.export(model_call())
    assert exporter.coverage.exported == 3
    assert exporter.coverage.fraction == 1.0


def test_a_self_reported_call_lowers_the_fraction_rather_than_raising_it() -> None:
    """A count with no per-call record is honestly *less* than a span, and the
    number says so."""
    exporter = otlp.Exporter(emit=lambda span: None)
    exporter.export(model_call())
    exporter.record_agent_usage("T4", calls=3)
    assert exporter.coverage.exported == 1
    assert exporter.coverage.self_reported == 3
    assert exporter.coverage.fraction == pytest.approx(0.25)


def test_no_traffic_at_all_is_not_a_hundred_percent() -> None:
    """None, not 1.0. "Nothing was observed" and "everything was captured" are
    different facts and must not share a representation."""
    assert otlp.Coverage().fraction is None
    assert "no model traffic" in otlp.Coverage().describe()


def test_the_coverage_note_names_session_mode_as_excluded() -> None:
    """§10.2: it must not claim to close #128, and the claim it does make is
    the one an operator reads."""
    note = otlp.Coverage(exported=5, self_reported=0).describe()
    assert "#128" in note
    assert "Session-mode" in note
    assert "never reaches" in note


# ----------------------------------------------- what it must not become


def test_nothing_is_ever_read_back() -> None:
    """D13, and the safe answer: export only.

    A span is a projection, never a source. Asserted against the code because
    "we would never read it back" is exactly the sort of thing that stops being
    true one convenient afternoon.
    """
    source = (
        Path(__file__).resolve().parents[1] / "src" / "agent_harness" / "adapters" / "otlp.py"
    ).read_text()
    for forbidden in ("EventStore", "WorkQueue", "AuditStore", "UPDATE ", "INSERT "):
        assert forbidden not in source, f"otlp.py reaches for {forbidden!r}"
    assert otlp.EXPORT_ONLY is True


def test_no_core_module_imports_an_opentelemetry_package() -> None:
    """`tests/test_generic.py` already asserts core imports no adapter. This is
    the other direction: nothing outside `adapters/` may reach for the SDK.

    `__main__.py` is exempt and is the one door, exactly as it is for the
    `oxidex` adapter: it names the environment variable in a help string and
    imports the adapter lazily inside the branch that asks for it. The test
    above pins that shape.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "agent_harness"
    offenders = [
        path.name
        for path in src.glob("*.py")
        if path.name != "__main__.py" and "opentelemetry" in path.read_text()
    ]
    assert not offenders, f"core reaches for OpenTelemetry: {offenders}"

    main = (src / "__main__.py").read_text()
    assert "import opentelemetry" not in main, "the CLI must go through the adapter"


def test_the_cli_is_the_only_door_and_it_is_opt_in() -> None:
    """Lazily imported, inside the branch that asks for it."""
    main = (
        Path(__file__).resolve().parents[1] / "src" / "agent_harness" / "__main__.py"
    ).read_text()
    assert "from .adapters.otlp import Exporter" in main
    assert 'getattr(args, "otel", False)' in main


def test_importing_the_adapter_builds_no_tracer() -> None:
    """It costs nothing until a route asks for it — the same property every
    other adapter in this repository keeps."""
    exporter = otlp.Exporter(endpoint="http://collector.invalid:4318")
    assert exporter._tracer is None
    assert not exporter._tried


# ------------------------------------------------------ end to end, quietly


def test_a_demo_run_with_otel_on_and_no_collector_behaves_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim, exercised through the real CLI rather than argued.

    Same demo, `--otel` on, no endpoint configured: the item still completes
    and the event file is still written.
    """
    import shutil

    if shutil.which("git") is None:
        pytest.skip("the demo needs git")

    from agent_harness import demo as demo_module
    from agent_harness.__main__ import main

    monkeypatch.delenv(otlp.ENDPOINT_ENV, raising=False)
    target = tmp_path / "demo"
    assert main(["init", "--demo", "--into", str(target)]) == 0
    code = main(
        [
            "--db",
            str(target / "queue.sqlite"),
            "run",
            "--demo",
            "--otel",
            "--project",
            demo_module.PROJECT_ID,
            "--work",
            str(target / "repo"),
            "--events",
            str(target / "events.jsonl"),
            "--no-push",
            "--limit",
            "1",
            "--check",
            demo_module.CHECK,
        ]
    )
    assert code == 0
    events = [
        json.loads(line)
        for line in (target / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert any(e.get("outcome") == "done" for e in events)
    # And the same stream projects to the three kinds of span.
    kinds = {span.kind for span in otlp.spans_for(events)}
    assert kinds == {otlp.ITEM, otlp.GATE, otlp.MODEL_CALL}
