"""The review gate, and whether it can be trusted.

A reviewer that approves everything is worse than no reviewer: the work
reaches a pull request carrying the word "reviewed", and the cost lands much
later on someone who believed it.
"""

from __future__ import annotations

from typing import Any

from agent_harness import providers
from agent_harness.model_client import ModelClient, Response, Route

#: A configured classifier standing in for "some vendor that states its
#: reasons". No adapter is loaded: what matters here is that two routes share
#: one classifier name, not whose envelope it reads.
ENVELOPE = providers.VendorEnvelopeProvider()


def client_with(implementer: str, reviewer: str, provider: Any = ENVELOPE) -> ModelClient:
    return ModelClient(
        roles={
            "implementer": Route(implementer, "https://api.example", ENVELOPE),
            "reviewer": Route(reviewer, "https://api.example", provider),
        },
        transport=lambda r, m, o: Response(200, {}, "{}"),
    )


def test_the_same_model_reviewing_itself_is_reported() -> None:
    """This was documented in three places and enforced in none, so it could
    happen silently -- every review a model grading its own work."""
    independent, why = client_with("m", "m").reviewer_independence()
    assert independent is False
    assert "grading its own work" in why


def test_the_same_vendor_is_reported_too() -> None:
    """Independent of the model is not independent of the vendor: shared
    training and shared blind spots survive a model swap within a family."""
    independent, why = client_with("model-a", "model-b").reviewer_independence()
    assert independent is False
    assert "share a provider" in why


def test_a_genuinely_independent_reviewer_passes() -> None:
    independent, why = client_with(
        "model-a", "model-b", provider=providers.GENERIC
    ).reviewer_independence()
    assert independent is True
    assert "model-b reviews model-a" in why


def test_it_is_reported_not_refused() -> None:
    """Running one model is a legitimate deliberate choice, and blocking it
    would be the harness overruling an operator about their own budget. What
    it must not be is a surprise."""
    client = client_with("m", "m")
    # The call still works; only the report says otherwise.
    assert client.call("reviewer", [{"role": "user", "content": "x"}]).status == 200


def test_an_incomplete_role_map_is_not_a_false_alarm() -> None:
    client = ModelClient(
        roles={"implementer": Route("m", "https://api.example", ENVELOPE)},
        transport=lambda r, m, o: Response(200, {}, "{}"),
    )
    independent, _ = client.reviewer_independence()
    assert independent is True


# ---------------------------------------------------------- the verdict


def test_no_reviewer_configured_rejects_rather_than_approves(tmp_path: Any) -> None:
    """Fails closed. Unreviewed work must never pass as reviewed."""
    from agent_harness.session_executor import SessionExecutor
    from agent_harness.work import WorkQueue, WorkRecord

    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    queue.add([WorkRecord(item_id="T1", title="t", brief="b")])

    class Host:
        pass

    executor = SessionExecutor(queue, Host(), tmp_path, reviewer=None)  # type: ignore[arg-type]
    verdict = executor._review(WorkRecord(item_id="T1", title="t", brief="b"), tmp_path, True, "")
    assert verdict.upper().startswith("REJECTED")


def test_the_prompt_demands_evidence_not_just_a_verdict() -> None:
    """A reviewer able to answer APPROVED with nothing else is exactly the
    shape a lazy approval takes."""
    from agent_harness.session_executor import REVIEW_PROMPT

    assert "What I verified" in REVIEW_PROMPT
    assert "What I could not verify" in REVIEW_PROMPT
    assert "Assume it is wrong" in REVIEW_PROMPT
    assert "cannot name any, that is a REJECTED" in REVIEW_PROMPT
