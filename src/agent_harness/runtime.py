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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .executor import Checks
from .model_client import Route
from .session_executor import AgentSpec, SessionExecutor
from .work import Project, WorkQueue

#: Given a project id, build something with `.serve()`. Matches
#: `fleet.ExecutorFactory`; repeated here so this module does not depend on
#: the fleet to describe its own return type.
ExecutorFactory = Callable[[str], Any]

#: The only role a `SessionExecutor` routes to a model. Planning and
#: implementation are done by the agent process, with its own credentials and
#: its own endpoint, so a `planner` or `implementer` route is configuration
#: this executor will never call.
SESSION_EXECUTOR_ROLES = frozenset({"reviewer"})


@dataclass(frozen=True)
class ExecutorRoles:
    """Which configured roles the executor this deployment builds can reach.

    The role map is how an operator answers "what am I paying for, and what is
    grading it?". In session mode it answered confidently and wrongly for two
    of three stages: `planner` and `implementer` were advertised, editable and
    echoed back, and nothing ever called them — so spend was looked for in an
    audit log where it could never appear.

    `calls` of None means every configured role is called, which is the
    non-session `Executor` and the honest default for a deployment that has
    not said otherwise.
    """

    calls: frozenset[str] | None = None
    #: What implements when implementation is not a routed role: the agent
    #: argv, in session mode. Empty when the executor routes every stage.
    implemented_by: str = ""

    @classmethod
    def for_session(cls, agent: AgentSpec | None = None) -> ExecutorRoles:
        command = tuple((agent or AgentSpec()).command)
        return cls(calls=SESSION_EXECUTOR_ROLES, implemented_by=shlex.join(command))

    def calls_role(self, role: str) -> bool:
        return self.calls is None or role in self.calls

    def unused_reason(self, role: str) -> str:
        """Why this deployment will never call `role`, in words. Empty when it will."""
        if self.calls_role(role):
            return ""
        if self.implemented_by:
            return (
                f"the agent process (`{self.implemented_by}`) does the {role}'s work, "
                "with its own credentials and endpoint; this route is only called by "
                "the non-session executor"
            )
        return "the active executor never calls this role"


class NotExecutable(RuntimeError):
    """This project cannot be executed as configured.

    Raised at build time rather than returning a broken executor: a worker
    that starts and fails every item costs money to discover.
    """


SHELL_METACHARACTERS = ("&&", "||", "|", ";", ">")


def validate_check_command(command: str) -> None:
    """Reject shell syntax before it can be silently passed as argv."""
    if any(token in command for token in SHELL_METACHARACTERS):
        raise ValueError(
            f"check commands are argv, not shell; {command!r} contains shell metacharacters"
        )
    if not shlex.split(command):
        raise ValueError("check commands must not be empty")


def session_executor_factory(
    queue: WorkQueue,
    *,
    host: Any,
    agent: AgentSpec | None = None,
    reviewer: Any | None = None,
    routes_for: Callable[[str], Mapping[str, Route]] | None = None,
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

    `routes_for` resolves one project's **effective** role map — the global
    map with that project's persisted overrides applied. Without it every
    project shared the one global reviewer: a project could pass preflight on
    its own reviewer override and then have its work reviewed by the global
    model, or fail with `no route for role reviewer` when the global map had
    none. It is a callable, consulted per call, so `PUT /api/roles` and a
    change to the project row both still take effect without a restart.
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
        executor = SessionExecutor(
            queue,
            host,
            Path(project.work_dir),
            agent=agent or AgentSpec(),
            checks=_checks_for(project),
            reviewer=_reviewer_for(project_id),
            github=(github_for(project.repo) if github_for and project.repo else None),
            base_branch=project.base_branch,
            ui_base_url=ui_base_url,
            on_event=on_event,
            push=push,
            project_id=project_id,
        )
        executor.reap_orphaned_worktrees()
        return executor

    def _reviewer_for(project_id: str) -> Any:
        """This project's reviewer, not the deployment's.

        The client is rebuilt per executor rather than per call because the
        route it resolves is already read live; what changes here is only
        *whose* map it reads.
        """
        if reviewer is None or routes_for is None:
            return reviewer
        return reviewer.routed_by(lambda: routes_for(project_id))

    return build


def _checks_for(project: Project) -> Checks:
    """The project's own verification commands, split without a shell.

    `shlex`, never `shell=True`: these strings come from an API request, and
    a check command is not a place to accept arbitrary shell.
    """
    commands = []
    for command in project.checks or []:
        validate_check_command(command)
        commands.append(shlex.split(command))
    return Checks(commands=commands)
