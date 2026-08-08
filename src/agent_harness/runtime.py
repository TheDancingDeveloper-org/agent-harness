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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .execution_environment import EnvironmentMount
from .executor import DEFAULT_CONTEXT_BUDGET, Checks, ContextPolicy, Executor
from .guard import GUARD_KEY, CommandGuard
from .model_client import Route
from .plan_integration import PlanCoordinator, PromotionConflict, PromotionError
from .plan_publication import PlanPublisher
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
    routes_for: Callable[[str], Mapping[str, Route | Sequence[Route]]] | None = None,
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
        # The deployment's command policy, read when the worker is built
        # rather than closed over, so `agent-harness guard` reaches a
        # supervised deployment without a restart. Absent is not "off": an
        # unconfigured guard still carries the built-in default, and doctor
        # is where "nobody chose this" gets said.
        guard = CommandGuard.from_settings(queue.get_setting(GUARD_KEY))
        executor = SessionExecutor(
            queue,
            host,
            Path(project.work_dir),
            agent=agent or AgentSpec(),
            checks=_checks_for(project, guard),
            guard=guard,
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


def direct_executor_factory(
    queue: WorkQueue,
    *,
    reviewer: Any,
    routes_for: Callable[[str], Mapping[str, Route | Sequence[Route]]] | None = None,
    github_for: Callable[[str], Any] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    push: bool = True,
    role_runner: Any,
    runner_step_limit: int = 80,
    runner_command_timeout: int = 300,
    context_budget: int | None = None,
    context_fallback_budget: int | None = None,
    environment_factory: Any,
    environment_image: str,
    environment_mounts: tuple[EnvironmentMount, ...] = (),
    environment_variables: Mapping[str, str] | None = None,
    environment_network: str = "bridge",
    publication_remote: str = "origin",
) -> ExecutorFactory:
    """Build the in-process role-runner executor used by ``serve``.

    A worker owns a disposable checkout for its whole lifetime. The executor
    then creates a second, item-scoped worktree for the model loop and feeds
    its candidate through the existing authoritative gates. Keeping the
    worker checkout separate is essential: the gate path still commits an
    item branch, and two workers must never checkout those branches in the
    same directory.

    The selected environment backend is required here rather than silently
    falling back to the host shell. The host compatibility backend remains a
    fixture-only option for direct tests; a real ``serve`` fleet needs the
    operating-system boundary selected by deployment metadata.
    """
    import contextlib
    import subprocess
    import tempfile

    def git(repo: Path, *args: str, check: bool = True) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise NotExecutable(f"git {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout

    def build(project_id: str) -> Any:
        project = queue.get_project(project_id)
        if project is None:
            raise NotExecutable(f"no project {project_id!r}")
        if not project.work_dir:
            raise NotExecutable(
                f"project {project_id!r} has no work_dir, so there is nothing to "
                "make a worker checkout from"
            )
        ready, detail = environment_factory.check()
        if not ready:
            raise NotExecutable(f"execution environment is not ready: {detail}")

        source = Path(project.work_dir).resolve()
        if not (source / ".git").exists():
            raise NotExecutable(f"project {project_id!r} work_dir is not a git repository")
        worker_tree = Path(
            tempfile.mkdtemp(prefix=f".harness-worker-{project_id}-", dir=source.parent)
        )
        try:
            git(source, "worktree", "add", "--detach", str(worker_tree), project.base_branch)
        except Exception:
            with contextlib.suppress(OSError):
                worker_tree.rmdir()
            raise

        client = reviewer.routed_by(lambda: routes_for(project_id)) if routes_for else reviewer
        guard = CommandGuard.from_settings(queue.get_setting(GUARD_KEY))
        checks = _checks_for(project, guard)
        coordinator: PlanCoordinator | None = None
        publisher: PlanPublisher | None = None
        if project.plan_path and project.plan_branch:
            if push:
                # Publishing a plan never means publishing its items. When a
                # deployment asks for a remote, it gets exactly one plan
                # branch and one pull request (P7/P8); the executor keeps its
                # item branches local and is given no client of its own.
                if github_for is None or not project.repo:
                    raise NotExecutable(
                        "publishing a plan needs a configured GitHub client and a "
                        "project repo; set push=False to integrate locally only"
                    )
                publisher = PlanPublisher(
                    queue,
                    project_id,
                    source,
                    github_for(project.repo),
                    remote=publication_remote,
                    on_event=on_event,
                )
            coordinator = PlanCoordinator(
                queue,
                project_id,
                source,
                checks=checks,
                on_event=on_event,
            )
            coordinator.ensure(
                target_branch=project.base_branch,
                branch=project.plan_branch,
                plan_path=project.plan_path,
            )

        def plan_base_for(record: Any) -> tuple[str, str | None]:
            assert coordinator is not None
            return coordinator.base_for(record)

        def plan_promote(record: Any, item_branch: str, base: str) -> tuple[str, str]:
            assert coordinator is not None
            try:
                promotion = coordinator.promote(record, item_branch=item_branch, base=base)
            except PromotionConflict as exc:
                return "conflict", str(exc)
            except PromotionError as exc:
                return "deferred", str(exc)
            _publish_if_ready(promotion)
            return promotion.status, promotion.detail

        def _publish_if_ready(promotion: Any) -> None:
            """Offer the finished plan to a person, without risking the item.

            The item is already promoted and gated locally when this runs. A
            remote that is down, slow or refusing must not undo that or fail
            the item, so a publication failure is reported as an event and
            the promotion stands.
            """
            if publisher is None or coordinator is None or promotion.status != "promoted":
                return
            try:
                publisher.publish_if_ready(
                    coordinator.state(),
                    title=f"{project.name or project_id}: {project.plan_branch}",
                    summary=(
                        f"Promoted `{promotion.item_id}` to the plan branch "
                        f"({(promotion.new_head_sha or '')[:12]})."
                    ),
                    excluding=promotion.item_id,
                )
            except Exception as exc:  # noqa: BLE001 - a remote cannot fail local work
                if on_event is not None:
                    with contextlib.suppress(Exception):
                        on_event(
                            {
                                "kind": "work",
                                "outcome": "plan_publication_failed",
                                "project_id": project_id,
                                "item_id": promotion.item_id,
                                "detail": str(exc),
                            }
                        )

        executor = Executor(
            queue,
            client,
            worker_tree,
            checks=checks,
            github=(
                github_for(project.repo)
                if github_for and push and project.repo and coordinator is None
                else None
            ),
            base_branch=project.base_branch,
            on_event=on_event,
            # An item branch is never published when a plan owns integration:
            # the plan branch is the only thing that reaches a remote.
            push=push and coordinator is None,
            context_policy=ContextPolicy(
                budget=context_budget or DEFAULT_CONTEXT_BUDGET,
                fallback_budget=context_fallback_budget,
            ),
            project_id=project_id,
            role_runner=role_runner,
            runner_step_limit=runner_step_limit,
            runner_command_timeout=runner_command_timeout,
            environment_factory=environment_factory,
            environment_image=environment_image,
            environment_mounts=environment_mounts,
            environment_variables=environment_variables,
            environment_network=environment_network,
            durability=project.durability or None,
            plan_base_for=plan_base_for if coordinator else None,
            plan_promote=plan_promote if coordinator else None,
        )

        class ManagedExecutor:
            owner = executor.owner

            def serve(self, **kwargs: Any) -> Any:
                try:
                    return executor.serve(**kwargs)
                finally:
                    git(source, "worktree", "remove", "--force", str(worker_tree), check=False)
                    git(source, "worktree", "prune", check=False)
                    with contextlib.suppress(OSError):
                        worker_tree.rmdir()

            def __getattr__(self, name: str) -> Any:
                return getattr(executor, name)

        return ManagedExecutor()

    return build


def _checks_for(project: Project, guard: CommandGuard | None = None) -> Checks:
    """The project's own verification commands, split without a shell.

    `shlex`, never `shell=True`: these strings come from an API request, and
    a check command is not a place to accept arbitrary shell.
    """
    commands = []
    for command in project.checks or []:
        validate_check_command(command)
        commands.append(shlex.split(command))
    # A declared fix is argv too, and gets the same refusal: a string that
    # would be unsafe to run is unsafe to store as runnable — and since
    # `apply_fixes` can make it runnable, that is now literal.
    fixes: dict[str, list[str]] = {}
    for command, fix in (project.fixes or {}).items():
        if not fix:
            continue
        fixes[command] = list(fix)
    return Checks(
        commands=commands,
        fixes=fixes,
        apply_fixes=bool(project.apply_fixes),
        guard=guard or CommandGuard(),
    )
