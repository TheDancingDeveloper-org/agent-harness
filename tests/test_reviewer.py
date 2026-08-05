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
    from agent_harness.session_executor import PROMPT_TEMPLATE, SessionExecutor
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
    )

    assert "widens the repository trait" in prompt
    assert "not continuing that attempt" in prompt, "a new attempt, not a resumption"

    # A first attempt must read exactly as it did before this existed.
    fresh = PROMPT_TEMPLATE.format(
        title="t",
        brief="b",
        checks_description="none",
        prior=executor._prior_failure(WorkRecord(item_id="R3", title="t", brief="b")),
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
