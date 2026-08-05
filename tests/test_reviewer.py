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


def test_both_executors_review_against_one_rubric() -> None:
    """Two copies drifted, and two items were rejected for it.

    The rubric used to be duplicated in `executor.py` and
    `session_executor.py`. Every correction made from measurement landed in
    the headless one: that a diff is the whole change, that the harness ran
    the checks, that the touched files are supplied, that scope belongs to the
    task. The session reviewer had none of them, and rejected real work for
    precisely the artefacts those fixes had already retired — in the other
    file.

    Identity, not similarity: a test that allows "roughly the same" is a test
    that allows the drift back.
    """
    from agent_harness import executor, session_executor

    assert session_executor.REVIEW_PROMPT is executor.REVIEW_PROMPT


def test_the_session_reviewer_is_given_what_the_headless_one_is() -> None:
    """The rubric names fields; supplying an empty string for them would keep
    the two prompts identical and the two reviewers unequal."""
    import inspect

    from agent_harness import session_executor

    source = inspect.getsource(session_executor.SessionExecutor._review)
    assert "review_context(" in source, "the touched files must reach the session reviewer"
    assert "review_checks_prompt(" in source, "and what actually ran the checks"
    assert "...HEAD" in source, "and the diff must be against the base, not the working tree"


def test_a_session_retry_is_told_why_the_last_attempt_was_refused(tmp_path: Any) -> None:
    """A session attempt is minutes of a real agent, not one API call, so
    repeating one blind is the most expensive avoidable thing here.

    Measured: an item was rejected for widening a repository trait's return
    type — a specific, actionable criticism — and the retry would have been
    sent the identical brief with no mention of it.
    """
    from agent_harness.session_executor import (
        PROMPT_TEMPLATE,
        REFUSAL_FILE,
        SessionExecutor,
    )
    from agent_harness.work import WorkQueue, WorkRecord

    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    executor = SessionExecutor(queue, type("Host", (), {})(), tmp_path)

    refused = WorkRecord(
        item_id="R3",
        title="t",
        brief="b",
        last_error="review rejected: it widens the repository trait's return type",
    )
    prompt = PROMPT_TEMPLATE.format(
        title=refused.title,
        brief=refused.brief,
        checks_description="none",
        prior=executor._prior_failure(refused),
        refusal_file=REFUSAL_FILE,
    )

    assert "widens the repository trait" in prompt
    assert "not continuing that attempt" in prompt, "a new attempt, not a resumption"

    # A first attempt must read exactly as it did before this existed.
    fresh = PROMPT_TEMPLATE.format(
        title="t",
        brief="b",
        checks_description="none",
        prior=executor._prior_failure(WorkRecord(item_id="R3", title="t", brief="b")),
        refusal_file=REFUSAL_FILE,
    )
    assert "What happened last time" not in fresh


def test_the_reviewer_sees_the_work_after_the_checkpoint_committed_it(tmp_path: Any) -> None:
    """The measured bug: every session-mode reviewer was shown an empty diff.

    The checkpoint before the expensive gate commits the work — deliberately,
    so a worker killed during review does not lose what passed the cheap
    gates. `git diff HEAD` then answers "what is uncommitted?", and the
    answer is always "nothing". So the reviewer received an empty diff and
    said the only correct thing about one:

        "The supplied diff is empty, so it demonstrates none of that and
         cannot be judged as satisfying the request."

    Measured on a real 48-line change that had already passed its checks. No
    session-mode item could ever have been approved.
    """
    import subprocess

    from agent_harness.session_executor import SessionExecutor
    from agent_harness.work import WorkQueue, WorkRecord

    tree = tmp_path / "repo"
    tree.mkdir()
    for argv in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", "-C", str(tree), *argv], check=True, capture_output=True)
    (tree / "hello.txt").write_text("hello world\n")
    subprocess.run(["git", "-C", str(tree), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tree), "commit", "-q", "-m", "base"], check=True, capture_output=True
    )
    # The item's own branch, as the executor cuts one, then the agent's work
    # committed onto it by the pre-review checkpoint.
    subprocess.run(
        ["git", "-C", str(tree), "checkout", "-q", "-b", "harness/t1"],
        check=True,
        capture_output=True,
    )
    (tree / "hello.txt").write_text("hello harness\n")
    subprocess.run(["git", "-C", str(tree), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tree), "commit", "-q", "-m", "checkpoint"],
        check=True,
        capture_output=True,
    )

    seen: dict[str, str] = {}

    class Reviewer:
        def call(self, _role: str, messages: Any, **_: Any) -> Any:
            seen["prompt"] = messages[-1]["content"]
            body = {"choices": [{"message": {"content": "APPROVED\nok"}}]}
            return type("R", (), {"body": body})()

    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    executor = SessionExecutor(
        queue,
        type("Host", (), {})(),
        tree,
        reviewer=Reviewer(),  # type: ignore[arg-type]
    )

    executor._review(WorkRecord(item_id="T1", title="t", brief="b"), tree, True, "", base="main")

    assert "hello harness" in seen["prompt"], "the reviewer was not shown the committed work"
    assert "-hello world" in seen["prompt"], "nor what it replaced"


def test_the_kept_reason_is_the_objection_not_the_praise() -> None:
    """`last_error` is what a retry is told, and it kept the wrong end.

    The rubric asks for "what I verified" first and "why" last. Keeping the
    first 500 characters therefore kept the list of things the reviewer was
    happy with and cut off the objection — the only part anyone can act on.

    Measured: two items were rejected twice for the same fault, each retry
    having been told only the preamble praising the parts that were fine.
    """
    from agent_harness.executor import review_reason

    verdict = (
        "REJECTED\n\n"
        "1. **What I verified** — " + "the call site is right, " * 60 + "\n\n"
        "2. **What I could not verify** — " + "nothing much, " * 20 + "\n\n"
        "3. **Why**\nIt widens the repository trait's return type."
    )

    kept = review_reason(verdict)

    assert "widens the repository trait" in kept, "the objection must survive"
    assert len(kept) <= 1300
    # And the old behaviour must not come back: the head is a summary of a
    # diff the reader already has.
    assert not kept.startswith("REJECTED\n\n1. **What I verified**")


def test_a_short_verdict_is_kept_whole() -> None:
    from agent_harness.executor import review_reason

    assert review_reason("REJECTED\nToo narrow.") == "REJECTED\nToo narrow."


def test_the_session_reviewer_budget_is_configurable(tmp_path: Any) -> None:
    """A 272 KB file was always "too large to include", so the reviewer was
    denied the evidence it then correctly rejected for lacking:

        "web/src/main.tsx is not included in full and I cannot inspect the
         surrounding markup"

    The headless executor has taken this from configuration since #150; when
    the review helpers were shared the session side got the default hardcoded
    instead, which is a ceiling no deployment could raise.
    """
    from agent_harness.executor import DEFAULT_CONTEXT_BUDGET
    from agent_harness.session_executor import SessionExecutor
    from agent_harness.work import WorkQueue

    queue = WorkQueue(str(tmp_path / "w.sqlite"))
    host = type("Host", (), {})()

    default = SessionExecutor(queue, host, tmp_path)
    raised = SessionExecutor(queue, host, tmp_path, context_budget=700_000)

    assert default.context_budget == DEFAULT_CONTEXT_BUDGET, "unchanged by default"
    assert raised.context_budget == 700_000, "and a deployment can raise it"


# ------------------------------- a follow-up is not a refusal (#171)


def test_surplus_insight_is_kept_when_the_reviewer_approves(tmp_path: Any) -> None:
    """A reviewer's "it should also have done X" is a proposal, not a
    condition. Refusing work that did what it was asked discards the work
    *and* the observation — the item goes back to be rewritten identically and
    nothing records what was noticed.

    Both of the observations that motivated this were real: that two other
    Support-bundle controls explained nothing, and that a doc comment is all
    that stops `Snippet::text` reaching the bundle. Each cost an approval and
    was recorded nowhere.
    """
    from agent_harness.executor import APPROVED, record_follow_ups

    verdict = (
        "APPROVED\n\n"
        "3. **Why** — it does what was asked.\n\n"
        "4. **Follow-ups**\n"
        "- the other two Support bundle controls explain nothing\n"
        "- nothing structurally stops Snippet::text reaching the bundle\n"
    )

    found = record_follow_ups(tmp_path, "rdpapp", "R4", APPROVED, verdict, 1000.0)

    assert len(found) == 2
    kept = (tmp_path / "FOLLOW-UPS.md").read_text()
    assert "the other two Support bundle controls" in kept
    assert "R4" in kept and "rdpapp" in kept
    assert "Nothing is" in kept and "queued" in kept, "and it must say nothing waits on them"


def test_a_rejection_never_produces_follow_ups(tmp_path: Any) -> None:
    """The safety property, and the reason this cannot become a way to wave
    work through. A rejection has already said what is wrong; a second channel
    there would let "approve and defer" grow over a failed criterion."""
    from agent_harness.executor import REJECTED, record_follow_ups

    verdict = (
        "REJECTED\n\n3. **Why** — it fails criterion 2.\n\n"
        "4. **Follow-ups**\n- something else entirely\n"
    )

    found = record_follow_ups(tmp_path, "rdpapp", "R4", REJECTED, verdict, 1000.0)

    assert found == ()
    assert not (tmp_path / "FOLLOW-UPS.md").exists()


def test_no_follow_ups_writes_nothing(tmp_path: Any) -> None:
    """A reviewer answering "none" has answered the question. Turning that
    into a file would fill a backlog with the absence of findings."""
    from agent_harness.executor import APPROVED, record_follow_ups

    assert record_follow_ups(tmp_path, "p", "T1", APPROVED, "APPROVED\n\n- none", 1.0) == ()
    assert not (tmp_path / "FOLLOW-UPS.md").exists()


def test_prose_after_the_list_is_not_swallowed_as_an_item() -> None:
    """Attributing a proposal the reviewer did not make is worse than missing
    one: a person triages it, finds nothing behind it, and trusts the next
    one less."""
    from agent_harness.executor import parse_follow_ups

    verdict = (
        "APPROVED\n\n4. **Follow-ups**\n"
        "- a real one\n\n"
        "I should add that the overall design seems sound.\n"
    )

    assert parse_follow_ups(verdict) == ("a real one",)


def test_the_rubric_tells_the_reviewer_a_follow_up_is_not_a_rejection() -> None:
    """The behaviour lives in the prompt; without this the parser has nothing
    to parse."""
    from agent_harness.executor import REVIEW_PROMPT

    assert "A follow-up is not a rejection" in REVIEW_PROMPT
    assert "proposed work items for a person to accept or discard" in REVIEW_PROMPT
    # And the boundary must survive alongside it.
    assert "no follow-up substitutes for one" in REVIEW_PROMPT
