"""Reusable deterministic assets for the Stage A observable safety slice.

Nothing in this module names a real workload or provider.  The repository is
generated in a temporary directory, and the transport is an in-process script
that implements the public ``ModelClient`` transport callable.
"""

from __future__ import annotations

import json
import subprocess
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_harness import providers as P
from agent_harness.events import MODEL_CALL, WORK, Event
from agent_harness.model_client import Response, Route
from agent_harness.providers import Classification, parse_retry_after


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@dataclass(frozen=True)
class FixtureRepository:
    root: Path
    checks: tuple[tuple[str, ...], ...] = (
        ("python", "-m", "compileall", "-q", "src", "extensions"),
        ("python", "-m", "unittest", "discover", "-s", "tests", "-q"),
    )

    def canonical_change(self) -> str:
        """A realistic multi-file change, returned as a real git diff.

        The tree is restored before returning.  Asking git to render the
        patch keeps the fixture deterministic without hand-maintaining index
        hashes and hunk counts.
        """
        operations = self.root / "src/mathkit/operations.py"
        operations.write_text(
            operations.read_text()
            + "\n\ndef multiply(left: int, right: int) -> int:\n"
            + "    return left * right\n"
        )
        tests = self.root / "tests/test_operations.py"
        tests.write_text(
            tests.read_text()
            + "\n\nclass MultiplyTests(unittest.TestCase):\n"
            + "    def test_multiply(self) -> None:\n"
            + "        self.assertEqual(operations.multiply(6, 7), 42)\n"
        )
        (self.root / "CHANGELOG.md").write_text("# Changes\n\n- Add multiplication.\n")
        # Make the untracked creation visible to `git diff` without staging
        # its contents into the eventual executor commit.
        git(self.root, "add", "-N", "CHANGELOG.md")
        git(self.root, "mv", "docs/OLD.md", "docs/ARCHIVE.md")
        git(self.root, "rm", "src/mathkit/deprecated.py")
        patch = git(self.root, "diff", "--binary", "HEAD")
        git(self.root, "reset", "--hard", "HEAD")
        git(self.root, "clean", "-fd")
        return patch

    def existing_file_zero_context(self) -> str:
        return """\
diff --git a/src/mathkit/operations.py b/src/mathkit/operations.py
--- a/src/mathkit/operations.py
+++ b/src/mathkit/operations.py
@@ -0,0 +1,2 @@
+def multiply(left, right):
+    return left * right
"""


def generated_repository(root: Path, *, irrelevant_files: int = 24) -> FixtureRepository:
    """Create and commit a small multi-directory package repository."""
    root.mkdir()
    for directory in (
        "src/mathkit",
        "extensions/textops",
        "tests",
        "docs/notes",
        "examples",
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "src/mathkit/__init__.py").write_text("from .operations import add\n")
    (root / "src/mathkit/operations.py").write_text(
        '"""Arithmetic operations; this docstring must remain first."""\n\n\n'
        "def add(left: int, right: int) -> int:\n"
        "    return left + right\n"
    )
    (root / "src/mathkit/deprecated.py").write_text("def old_add(a, b):\n    return a + b\n")
    (root / "extensions/textops/__init__.py").write_text(
        "def normalize(value: str) -> str:\n    return value.strip().lower()\n"
    )
    (root / "tests/test_operations.py").write_text(
        "import sys\n"
        "import unittest\n"
        "from pathlib import Path\n\n"
        "sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))\n"
        "from mathkit import operations\n\n\n"
        "class OperationTests(unittest.TestCase):\n"
        "    def test_add(self) -> None:\n"
        "        self.assertEqual(operations.add(2, 3), 5)\n\n"
        "    def test_module_header_is_preserved(self) -> None:\n"
        "        self.assertEqual(\n"
        "            operations.__doc__,\n"
        "            'Arithmetic operations; this docstring must remain first.',\n"
        "        )\n"
    )
    (root / "docs/OLD.md").write_text("# Historical notes\n")
    (root / "examples/example.txt").write_text("add(2, 3) -> 5\n")
    for number in range(irrelevant_files):
        (root / f"docs/notes/note-{number:02d}.md").write_text(
            f"# Note {number:02d}\n\nUnrelated fixture context.\n"
        )
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "fixture baseline")
    return FixtureRepository(root)


@dataclass(frozen=True)
class Reply:
    content: str = "ok"
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    body: str | None = None
    before: Callable[[], None] | None = None


@dataclass(frozen=True)
class Raise:
    error: Exception
    before: Callable[[], None] | None = None


Step = Reply | Raise


class DeterministicTransport:
    """A local scripted transport keyed by model, then by role.

    Scripts are queues, making retries and fallback order observable.  A
    single final step is reusable, which keeps healthy routes concise.
    """

    def __init__(self, scripts: Mapping[str, Sequence[Step] | Step]) -> None:
        self.scripts: dict[str, deque[Step]] = {}
        for key, value in scripts.items():
            steps = [value] if isinstance(value, (Reply, Raise)) else list(value)
            self.scripts[key] = deque(steps)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        route: Route,
        messages: Sequence[Mapping[str, Any]],
        options: Mapping[str, Any],
    ) -> Response:
        role = str(route.options.get("role", ""))
        key = route.model if route.model in self.scripts else role
        script = self.scripts.get(key)
        if not script:
            raise AssertionError(f"no scripted transport step for {route.model}/{role}")
        step = script[0] if len(script) == 1 else script.popleft()
        self.calls.append(
            {
                "model": route.model,
                "endpoint": route.endpoint,
                "role": role,
                "messages": list(messages),
                "options": dict(options),
            }
        )
        if step.before is not None:
            step.before()
        if isinstance(step, Raise):
            raise step.error
        body = step.body
        if body is None:
            body = json.dumps({"choices": [{"message": {"content": step.content}}]})
        return Response(step.status, dict(step.headers), body)


@dataclass(frozen=True)
class MatrixProvider:
    """Test-only classifier for neutral ``{"kind": ...}`` failure bodies."""

    name: str = "fixture-matrix"

    def classify(
        self,
        status: int,
        headers: Mapping[str, str] | None,
        body: bytes | str | None,
    ) -> Classification:
        try:
            payload = json.loads(body or "{}")
        except (TypeError, ValueError):
            payload = {}
        kind = payload.get("kind")
        if kind not in {P.RPM, P.WINDOW_CAP, P.TERMINAL_CAP, P.NON_RETRYABLE, P.TRANSIENT}:
            kind = P.TRANSIENT if status >= 500 else P.FATAL
        return Classification(str(kind), payload.get("message"), parse_retry_after(headers))


MATRIX_PROVIDER = MatrixProvider()


def failure(kind: str, *, status: int = 429, message: str | None = None) -> Reply:
    return Reply(
        status=status,
        body=json.dumps({"kind": kind, "message": message or kind.replace("_", " ")}),
    )


def event_sink(audit: Any, *, source: str = "stage-a") -> Callable[[dict[str, Any]], None]:
    """Translate both executor and model events into the public audit stream."""
    sequence: defaultdict[tuple[str, str], int] = defaultdict(int)

    def emit(raw: dict[str, Any]) -> None:
        kind = raw.get("kind", MODEL_CALL)
        if kind not in {WORK, MODEL_CALL}:
            kind = MODEL_CALL
        known = {
            "ts",
            "kind",
            "worker",
            "role",
            "model",
            "endpoint",
            "outcome",
            "error_class",
            "latency_s",
        }
        data = {key: value for key, value in raw.items() if key not in known}
        # Executor work events do not carry a sequence. Preserve identical
        # stage events as distinct append-only facts without wall-clock luck.
        identity = (str(data.get("project_id", "")), str(data.get("item_id", "")))
        data.setdefault("fixture_seq", sequence[identity])
        sequence[identity] += 1
        audit.append(
            [
                Event(
                    ts=float(raw.get("ts", 0.0)),
                    kind=str(kind),
                    source=source,
                    worker=raw.get("worker"),
                    role=raw.get("role"),
                    model=raw.get("model"),
                    endpoint=raw.get("endpoint"),
                    outcome=raw.get("outcome"),
                    error_class=raw.get("error_class"),
                    latency_s=raw.get("latency_s"),
                    data=data,
                )
            ]
        )

    return emit


class RecordingGitHub:
    """External-side-effect boundary with an inspectable public call record."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.comments: list[tuple[str, str]] = []
        self.ready: list[str] = []

    def find_open_pr(self, head: str) -> None:
        return None

    def create_pr(self, **fields: Any) -> str:
        self.created.append(fields)
        return f"https://example.invalid/pulls/{len(self.created)}"

    def comment_pr(self, pr: str, body: str) -> None:
        self.comments.append((pr, body))

    def mark_pr_ready(self, pr: str) -> None:
        self.ready.append(pr)
