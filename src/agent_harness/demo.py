"""A first run that needs no credentials, no network and no model.

`agent-harness init --demo` builds everything an item needs — a git repository,
a plan, a queue, a project and a route — in one directory, and leaves the
project **stopped**. One documented command then runs the real API executor
against a deterministic in-process transport and reports the tree, the outcome
and the event file.

**What this proves and what it does not.** It proves the wiring: that a plan
parses into work, that work is admitted by the graph, that a diff is applied in
a worktree, that the configured checks run before the reviewer, that the
reviewer's verdict is a gate, and that every step lands in the event stream. It
proves nothing whatsoever about model quality, because there is no model. The
answers are fixed. A demo that passed because the harness works and a demo that
passed because the answers were written to pass look identical from inside, so
the distinction is stated here rather than left for someone to discover.

The transport is the seam, and it is the *only* thing replaced. The executor,
the queue, the graph, the worktree handling, the patch validator, the checks
and the reviewer gate are the same code a real run uses. Substituting anything
above the transport would make this a demonstration of the demo.

Nothing here is workload- or vendor-specific: the fixture repository is
generated, and the replies are shaped by the `chat-completions` preset because
that is what the CLI defaults to, read back through the same reader a real
reply goes through.
"""

from __future__ import annotations

import difflib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The demo's project id, item id and role model name. Named once because the
#: printed commands, the queue rows and the route all have to agree, and three
#: string literals that have to agree are two too many.
PROJECT_ID = "demo"
ITEM_ID = "T1"
MODEL = "demo-deterministic"

#: The endpoint written into the demo's role map. It is not reachable and is
#: not meant to be: `run --demo` never builds an HTTP transport. It is a URL
#: rather than an empty string because a route with no endpoint is an
#: *unroutable* role, and the demo should exercise routing, not bypass it.
ENDPOINT = "http://demo.invalid/v1"

#: The check the demo project runs before the reviewer sees anything. A real
#: argv, run by the same `Checks` a real project uses, which fails if the
#: change is wrong.
CHECK = "python -m unittest discover -s tests -q"

_OPERATIONS = '''\
"""Arithmetic, deliberately small."""


def add(left, right):
    """Return the sum of two numbers."""
    return left + right


def subtract(left, right):
    """Return the difference of two numbers."""
    return left - right
'''

_TEST = """\
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from calc import operations  # noqa: E402


class ArithmeticTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(operations.add(2, 3), 5)

    def test_subtract(self):
        self.assertEqual(operations.subtract(5, 3), 2)


if __name__ == "__main__":
    unittest.main()
"""

_README = """\
# calc

A generated fixture repository. It exists so the harness has something real to
change: a git history, a package, and a test suite that actually runs.

Nothing here is a real project and nothing depends on it.
"""

_PLAN = """\
# Demo plan

One item, so a first run finishes in one pass.

### T1: Add a multiply function to the calculator

`calc/operations.py` has `add` and `subtract` and no `multiply`. Add one, with
a docstring in the same style as its neighbours, and a test for it in
`tests/test_operations.py` alongside the existing cases.

Do not change anything else.
"""

#: The files the fixture repository is created with, relative to its root.
FIXTURE_FILES: dict[str, str] = {
    "README.md": _README,
    "calc/__init__.py": "",
    "calc/operations.py": _OPERATIONS,
    "tests/test_operations.py": _TEST,
}

#: What the implementer "writes". Appended to the file's current contents, so
#: the diff is computed against the tree rather than hand-maintained — a
#: hardcoded patch would rot the first time the fixture changed, and would rot
#: silently, as a demo that stopped applying.
_ADDITIONS: dict[str, str] = {
    "calc/operations.py": '''

def multiply(left, right):
    """Return the product of two numbers."""
    return left * right
''',
    "tests/test_operations.py": """
class MultiplyTests(unittest.TestCase):
    def test_multiply(self):
        self.assertEqual(operations.multiply(6, 7), 42)
""",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(root), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def create_fixture_repo(root: Path, *, base_branch: str = "main") -> Path:
    """A real git repository with a real history, generated from nothing.

    Committed with `--no-gpg-sign` and an explicit identity so it works on a
    machine whose git is configured for something else, or not configured at
    all. A demo that needs the user's git set up first is not a demo of a clean
    checkout.
    """
    root.mkdir(parents=True, exist_ok=True)
    for name, content in FIXTURE_FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(root, "init", "-q", "-b", base_branch)
    _git(root, "config", "user.email", "demo@invalid")
    _git(root, "config", "user.name", "agent-harness demo")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--no-gpg-sign", "-m", "initial commit")
    return root


@dataclass(frozen=True)
class Demo:
    """Where everything the demo made is, and the command that runs it."""

    root: Path
    repo: Path
    plan: Path
    db: Path
    events: Path

    def run_command(self) -> list[str]:
        """The one documented command. Returned as argv so nothing is quoted
        into existence by a shell that the caller then has to unquote."""
        return [
            "agent-harness",
            "--db",
            str(self.db),
            "run",
            "--demo",
            "--project",
            PROJECT_ID,
            "--work",
            str(self.repo),
            "--events",
            str(self.events),
            "--no-push",
            "--limit",
            "1",
            "--check",
            CHECK,
        ]


def create_demo(root: Path, *, base_branch: str = "main") -> Demo:
    """Build the whole demo under `root`, and leave the project stopped.

    Stopped is the point. A demo that starts working the moment it is created
    has decided on the user's behalf that they wanted a fleet running, and the
    first-run path is supposed to default to doing nothing external.
    """
    from .api import ROLE_MAP_KEY
    from .plan import parse_plan_file
    from .work import Project, WorkQueue, WorkRecord

    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    repo = create_fixture_repo(root / "repo", base_branch=base_branch)
    plan = root / "PLAN.md"
    plan.write_text(_PLAN)
    db = root / "queue.sqlite"
    events = root / "events.jsonl"

    queue = WorkQueue(str(db))
    queue.add_project(
        Project(
            project_id=PROJECT_ID,
            name="Deterministic demo",
            # No repo: nothing may reach GitHub from here. `run --no-push`
            # needs no repo, and leaving it unset means a mistyped command
            # cannot open a pull request against somebody's repository.
            repo=None,
            work_dir=str(repo),
            base_branch=base_branch,
            checks=[CHECK],
            plan_path=str(plan),
        )
    )
    parsed = parse_plan_file(plan)
    queue.add(
        [
            WorkRecord(
                item_id=item.id,
                title=item.title,
                brief=item.brief(),
                depends_on=item.depends_on,
                project_id=PROJECT_ID,
            )
            for item in parsed.deduplicated()
            if not item.done
        ],
        project_id=PROJECT_ID,
    )
    queue.set_setting(
        ROLE_MAP_KEY,
        {
            role: {"model": MODEL, "models": [MODEL], "endpoint": ENDPOINT}
            for role in ("planner", "implementer", "reviewer")
        },
    )
    return Demo(root=root, repo=repo, plan=plan, db=db, events=events)


# ------------------------------------------------------- the fixed answers


def _prompt_of(messages: Any) -> str:
    return "\n".join(str(m.get("content", "")) for m in messages)


def _diff_for(work_dir: Path) -> str:
    """A unified diff adding `multiply`, computed against the tree on disk.

    Real `difflib` output over the real file, so it applies for the same
    reason a model's diff would: because it describes the file that is there.
    """
    chunks: list[str] = []
    for name, addition in _ADDITIONS.items():
        path = work_dir / name
        before = path.read_text()
        after = before.rstrip("\n") + "\n" + addition
        body = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{name}",
                tofile=f"b/{name}",
                n=3,
            )
        )
        chunks.append(f"diff --git a/{name} b/{name}\n{body}")
    return "".join(chunks)


def _planner_reply() -> str:
    return json.dumps(
        {
            "plan": (
                "Add a `multiply` function to calc/operations.py in the same style as "
                "`add` and `subtract`, then add a MultiplyTests case to "
                "tests/test_operations.py. Verify with the project's unittest check."
            ),
            "targets": [
                {"path": "calc/operations.py", "reason": "the function goes here"},
                {"path": "tests/test_operations.py", "reason": "the test for it goes here"},
            ],
            "cannot_identify_target": None,
        }
    )


_REVIEW = """\
APPROVED

**What I verified** — the diff adds `multiply(left, right)` to
`calc/operations.py` returning `left * right`, with a docstring matching the
style of `add` and `subtract`, and adds a `MultiplyTests` case asserting
`multiply(6, 7) == 42`. Nothing else in either file is touched, and no other
file appears in the diff.

**What I could not verify** — nothing beyond the diff. This verdict is fixed:
it is the demo's scripted reviewer, not a model, and it would say the same
thing about a diff that did none of the above. A real reviewer's approval is
evidence; this one is wiring.

**Why** — the change is exactly what the item asked for and no more.
"""


def scripted_reply(prompt: str, work_dir: Path) -> str:
    """The answer for one prompt. Dispatched on what the prompt asks for.

    Reading the prompt rather than being told the role is deliberate: it is
    what a model does, and it keeps the demo honest about the fact that the
    transport is handed a request and nothing else.
    """
    from .executor import IMPLEMENT_PROMPT, PLAN_PROMPT, REVIEW_PROMPT

    if prompt.startswith(PLAN_PROMPT.split("\n", 1)[0]):
        return _planner_reply()
    if prompt.startswith(IMPLEMENT_PROMPT.split("\n", 1)[0]):
        return _diff_for(work_dir)
    if prompt.startswith(REVIEW_PROMPT.split("\n", 1)[0]):
        return _REVIEW
    # Not an error: `preflight`'s reachability probe sends "ping", and a
    # transport that raised on an unrecognised prompt would make the demo fail
    # at readiness rather than at the thing it is demonstrating.
    return "ok"


def demo_transport(work_dir: Path) -> Any:
    """A `ModelClient` transport that answers without a network.

    Returns the `chat-completions` body shape, so the reply is read back
    through the same `JsonResponseReader` a real reply goes through. A
    transport that returned plain text would skip the reader and quietly stop
    testing it.
    """
    from .model_client import Response

    def transport(route: Any, messages: Any, options: Any) -> Any:
        text = scripted_reply(_prompt_of(messages), work_dir)
        body = {
            "id": "demo",
            "model": getattr(route, "model", MODEL),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
            # Reported and deliberately zero. The demo spends nothing, and a
            # made-up token count would appear in the audit totals as a cost
            # that never happened.
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        return Response(200, {"content-type": "application/json"}, json.dumps(body))

    return transport
