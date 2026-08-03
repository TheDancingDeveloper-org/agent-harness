"""Building the thing that actually executes a project's work.

`serve` owned the API and could not execute; `run` executed and exposed no
API. A session host asking the harness to start queued work therefore had
nowhere to send the request: the API refused, correctly, because starting
would only have set a flag with no worker behind it.

This is the missing half — a factory that turns a **registered project** into
an executor. Everything project-shaped (the checkout, the repo, the checks,
the base branch) comes from the project row rather than from a flag, because
a supervised deployment serves several projects and cannot have one checkout
on its command line. Everything deployment-shaped (which session host, which
agent command, who reviews) is supplied once.

Nothing here starts anything. A factory is a way to build a worker, not a
worker; only `Fleet.start`, reached through the API's start action, creates
one.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .executor import Checks
from .session_executor import AgentSpec, SessionExecutor
from .work import Project, WorkQueue

#: Given a project id, build something with `.serve()`. Matches
#: `fleet.ExecutorFactory`; repeated here so this module does not depend on
#: the fleet to describe its own return type.
ExecutorFactory = Callable[[str], Any]


class NotExecutable(RuntimeError):
    """This project cannot be executed as configured.

    Raised at build time rather than returning a broken executor: a worker
    that starts and fails every item costs money to discover.
    """


def session_executor_factory(
    queue: WorkQueue,
    *,
    host: Any,
    agent: AgentSpec | None = None,
    reviewer: Any | None = None,
    github_for: Callable[[str], Any] | None = None,
    ui_base_url: str = "",
    on_event: Callable[[dict[str, Any]], None] | None = None,
    push: bool = True,
) -> ExecutorFactory:
    """An executor factory backed by hosted terminal sessions.

    `host`, `reviewer` and `github_for` are injected so a deployment can be
    exercised end to end without a session host, a model or a network — which
    is the only way the wiring gets tested at all, since every real component
    here costs money or credentials to touch.
    """

    def build(project_id: str) -> SessionExecutor:
        project = queue.get_project(project_id)
        if project is None:
            raise NotExecutable(f"no project {project_id!r}")
        if not project.work_dir:
            raise NotExecutable(
                f"project {project_id!r} has no work_dir, so there is nothing to "
                "make a worktree from"
            )
        return SessionExecutor(
            queue,
            host,
            Path(project.work_dir),
            agent=agent or AgentSpec(),
            checks=_checks_for(project),
            reviewer=reviewer,
            github=(github_for(project.repo) if github_for and project.repo else None),
            base_branch=project.base_branch,
            ui_base_url=ui_base_url,
            on_event=on_event,
            push=push,
            project_id=project_id,
        )

    return build


def _checks_for(project: Project) -> Checks:
    """The project's own verification commands, split without a shell.

    `shlex`, never `shell=True`: these strings come from an API request, and
    a check command is not a place to accept arbitrary shell.
    """
    return Checks(commands=[shlex.split(command) for command in (project.checks or [])])
