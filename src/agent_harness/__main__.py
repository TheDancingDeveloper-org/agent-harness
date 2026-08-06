"""CLI.

    agent-harness plan   PLAN.md --repo owner/name   # plan -> GitHub backlog
    agent-harness adopt  PLAN.md --project existing  # inspect work already done
    agent-harness run    --repo owner/name --work .  # execute the backlog
    agent-harness ingest --events ./run/events.jsonl
    agent-harness serve  --port 8099

Ingest is idempotent, so a cron loop, a one-off backfill and an accidental
double-run all produce the same store.

Sources are given explicitly. There is no default log location, because the
harness is not tied to any particular workload — `--events` takes the
harness's own stream, and `--adapter` opts into reading some other tool's
logs.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import shlex
import shutil
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .ingest import ingest
from .sources import Source, harness_source
from .store import EventStore

log = logging.getLogger(__name__)


def resolve_sources(args: argparse.Namespace) -> list[Source]:
    """Turn CLI arguments into sources. Adapters are imported lazily so the
    core never depends on one."""
    sources: list[Source] = [harness_source(path) for path in (args.events or [])]
    if args.adapter:
        if not args.adapter_path:
            raise SystemExit("--adapter requires --adapter-path")
        if args.adapter == "oxidex":
            from .adapters import oxidex

            sources.extend(oxidex.sources_for(args.adapter_path))
        else:  # pragma: no cover - argparse restricts the choices
            raise SystemExit(f"unknown adapter {args.adapter!r}")
    return sources


def _plan(args: argparse.Namespace) -> int:
    """Parse a plan and sync it to GitHub, refusing on anything ambiguous.

    Refusing beats guessing here: a bad sync creates real issues in a real
    repository, and cleaning that up by hand is far more expensive than
    being told to fix the plan first.
    """
    from .github import GitHub, GitHubError, sync
    from .plan import parse_plan_file

    if not args.repo and not args.dry_run:
        print("--repo OWNER/NAME is required to sync", file=sys.stderr)
        print("Pass --dry-run to parse and report without contacting GitHub.", file=sys.stderr)
        return 2

    plan = parse_plan_file(args.path)
    if not plan.items:
        print(f"{args.path}: no work items found.", file=sys.stderr)
        print(
            "Items are recognised as '### T1: Title' headings, '- [ ] T1 Title' "
            "checkboxes, or table rows with an id column.",
            file=sys.stderr,
        )
        return 1

    duplicates = plan.duplicate_ids()
    if duplicates and not args.allow_duplicates:
        print(f"{args.path}: these ids appear more than once:", file=sys.stderr)
        for item_id, lines in sorted(duplicates.items()):
            print(f"  {item_id} on lines {', '.join(str(n) for n in lines)}", file=sys.stderr)
        print(
            "Each id becomes one issue, so fix the plan or pass "
            "--allow-duplicates to keep the richest description of each.",
            file=sys.stderr,
        )
        return 2

    items = plan.deduplicated()
    dependencies = plan.dependency_report()
    if dependencies.lines():
        # Not fatal at sync time -- this command writes GitHub issues, and the
        # queue is where admission is decided. But every line here is
        # something that WILL hold work back, and saying so before the issues
        # exist is cheaper than explaining a stalled queue afterwards.
        print("dependencies:", file=sys.stderr)
        for line in dependencies.lines():
            print(f"  {line}", file=sys.stderr)

    print(f"{len(items)} work items, {len(plan.skipped)} headings skipped as narrative")
    if not args.repo:
        # Everything above is the local answer, and it is the whole answer
        # anyone asks a dry run for: what could the parser read, and what will
        # hold work back. There is nothing further to report without a repo.
        for item in items:
            print(f"  {item.id}: {item.title}")
        return 0
    try:
        report = sync(GitHub(args.repo), items, dry_run=args.dry_run)
    except GitHubError as exc:
        # A stack trace here tells the user nothing they can act on; the
        # gh error does.
        print(f"github: {exc}", file=sys.stderr)
        return 3
    for kind, names in (("label", report.labels_created), ("milestone", report.milestones_created)):
        if names:
            verb = "would create" if args.dry_run else "created"
            print(f"{verb} missing {kind}s: {', '.join(names)}")
    print(("would sync: " if args.dry_run else "synced: ") + str(report))
    if report.orphaned:
        print(
            "orphaned (in the backlog, no longer in the plan; left alone): "
            + ", ".join(report.orphaned)
        )
    return 0


def _init(args: argparse.Namespace) -> int:
    """Build the deterministic demo, and say what it is and is not.

    Refuses an existing directory. `init` is the command a stranger runs
    first, on a machine whose layout it knows nothing about, and a first
    command that can overwrite is a first command that can lose something.
    """
    from .demo import create_demo

    target = args.into
    if target.exists() and any(target.iterdir()):
        print(f"{target} already exists and is not empty", file=sys.stderr)
        print("Pass --into DIR with somewhere new, or remove that one.", file=sys.stderr)
        return 2
    if not shutil.which("git"):
        print("git is not on PATH, and the demo needs a real repository", file=sys.stderr)
        return 2

    demo = create_demo(target)
    print(f"built the demo in {demo.root}")
    print(f"  repository   {demo.repo}   (a real git repo, one commit)")
    print(f"  plan         {demo.plan}   (one item)")
    print(f"  queue        {demo.db}   (project `demo`, stopped)")
    print()
    print("Nothing is running and nothing external has happened: no network call,")
    print("no credential read, no GitHub anything. Run it with:")
    print()
    print("  " + shlex.join(demo.run_command()))
    print()
    print("That runs the real executor against fixed answers. It proves the wiring —")
    print("plan to queue to worktree to patch to checks to reviewer to commit — and it")
    print("proves nothing about model quality, because there is no model.")
    print()
    print(f"Afterwards: `git -C {demo.repo} log --oneline --all` for the branch it made,")
    print(f"and {demo.events} for every step it took.")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    """Say what is configured and what is missing, without spending anything.

    This exists because the expensive failure is not a broken configuration —
    it is a broken configuration that looks fine until the fleet has claimed
    work and paid for it. Preflight already refuses to *start* such a project;
    doctor is the same set of questions asked before you get that far, plus
    the ones preflight does not ask because they do not block a start.

    **Nothing here contacts a model, a gateway or GitHub's write API by
    default.** `--probe-models` opts into the one check that does. A diagnostic
    that spends money to tell you whether you can afford to spend money is a
    diagnostic nobody runs twice.
    """
    import json as _json

    from .doctor import diagnose, render
    from .work import WorkQueue

    queue = WorkQueue(args.db)
    projects = queue.projects()
    if args.project:
        projects = [p for p in projects if p.project_id == args.project]
        if not projects:
            print(f"no project {args.project!r} in {args.db}", file=sys.stderr)
            return 2

    ask = None
    if args.probe_models:
        api_key = os.environ.get("HARNESS_API_KEY", "")
        if not api_key:
            print("--probe-models needs HARNESS_API_KEY", file=sys.stderr)
            return 2
        if not args.endpoint:
            print("--probe-models needs --endpoint (or $HARNESS_ENDPOINT)", file=sys.stderr)
            return 2
        from .model_client import ModelClient

        # `answers`, not `call`: one brief request, no retry ladder, no
        # parking and no telemetry. A diagnostic must not idle an endpoint
        # for the fleet or appear in anybody's cost rollup.
        probe_client = ModelClient(roles={}, transport=_http_transport(api_key))

        def ask(route: Any) -> tuple[bool, str]:
            return probe_client.answers(route, timeout=20.0)

    report = diagnose(queue, projects, ask=ask)
    if args.json:
        print(_json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(render(report))
    # 0 when nothing blocks, 1 when something does. A diagnostic whose exit
    # code never changes cannot be used in a script, and "would this run?" is
    # exactly the question a script wants to ask.
    return 0 if report.ok else 1


def _guard(args: argparse.Namespace) -> int:
    """Show or set what this deployment refuses to run.

    Configuration, not code: what must never run here is a property of the
    machine, the credentials on its disk and the people sharing its remote —
    none of which this framework can know. The built-in default is deliberately
    tiny and names nothing belonging to any workload.

    Printing is the default action because the common question is "what is in
    force?", and a command that changes policy by accident is worse than one
    that has to be asked twice.
    """
    from .guard import DEFAULT_REFUSALS, GUARD_KEY, CommandGuard
    from .work import WorkQueue

    queue = WorkQueue(args.db)
    if args.clear:
        queue.set_setting(GUARD_KEY, None)
        print("command guard policy cleared; the built-in default is in force")
        print("doctor now reports the guard as NOT CONFIGURED, which is what it is")
        return 0

    changing = bool(args.refuse) or args.no_defaults or args.no_confine
    if changing:
        guard = CommandGuard(
            refusals=tuple(args.refuse),
            defaults=not args.no_defaults,
            confine=not args.no_confine,
            configured=True,
        )
        queue.set_setting(GUARD_KEY, guard.as_settings())
    else:
        guard = CommandGuard.from_settings(queue.get_setting(GUARD_KEY))

    print(f"command guard: {guard.describe()}")
    print(f"  configured by this deployment: {'yes' if guard.configured else 'no'}")
    for pattern in guard.refusals:
        print(f"  refuse  {pattern}")
    if guard.defaults:
        for pattern in DEFAULT_REFUSALS:
            print(f"  refuse  {pattern}   (built-in default)")
    if not guard.active:
        print("  NOTHING IS REFUSED. Every command a plan or a check names will run.")
    print(
        "A refused command is terminal for its item: it stops in `blocked` with "
        "disposition `blocked_by_policy`, and is never retried."
    )
    return 0


def _graph(args: argparse.Namespace) -> int:
    """Inspect, back up or rebuild the dependency graph.

    The rebuild/export half of this command is the operational half of
    `docs/MIGRATION-graph.md`. "The queue is disposable" is a statement about
    what the queue means, not a licence to assume an in-place upgrade is
    safe, so there has to be a supported way to take a copy that outlives the
    schema and a supported way to re-derive the graph from it.
    """
    import json as _json

    from .work import WorkQueue

    queue = WorkQueue(args.db)

    if args.action == "checkpoint":
        queue.checkpoint()
        print(f"{args.db}: WAL folded back into the database file")
        return 0

    if args.action == "export":
        payload = _json.dumps(queue.graph.export(), indent=2, sort_keys=True)
        if args.out:
            args.out.write_text(payload + "\n")
            print(f"wrote {args.out}")
        else:
            print(payload)
        return 0

    if args.action == "rebuild":
        revisions = queue.graph.rebuild(args.project)
        if not revisions:
            print("nothing to rebuild: no work rows")
            return 0
        for project, revision in sorted(revisions.items()):
            print(f"{project}: graph rebuilt, now at revision {revision}")
        return 0

    projects = [args.project] if args.project else [p.project_id for p in queue.projects()]
    if not projects:
        print("no projects")
        return 0
    blocked = 0
    for project in projects:
        report = queue.graph.report(project)
        print(f"{project}: revision {report.revision}, {len(report.edges)} edges")
        print(f"  ready: {', '.join(report.ready) or '(none)'}")
        for cycle in report.cycles:
            print("  cycle: " + " -> ".join([*cycle, cycle[0]]))
        for state in report.not_ready:
            blocked += 1
            print(f"  {state.item_id}: {state.explain()}")
    # A non-zero exit only when something is actually held back, so this is
    # usable as a gate in a script without parsing the text.
    return 0 if blocked == 0 else 4


def _describe(event: dict[str, Any]) -> str:
    """One event as a line a human can follow a run by.

    Deliberately not the JSON. The point is that someone watching a terminal
    can tell "thinking", "waiting on a rate limit" and "wedged" apart, and
    raw event dicts do not read that way at a glance.
    """
    who = event.get("item_id") or event.get("role") or "-"
    outcome = event.get("outcome") or "?"
    detail = str(event.get("detail") or "").strip().splitlines()
    first = detail[0][:160] if detail else ""
    return f"{who} {outcome}" + (f" — {first}" if first else "")


def _http_transport(api_key: str) -> Any:
    """A real transport, built only when `run` needs one.

    Imported lazily and constructed here rather than in model_client,
    because the client's whole design is that it does not own the HTTP —
    a caller with its own client should be able to keep it.

    It performs requests; it does not shape them. The URL, the payload and the
    credential header come from the route's preset, so a second wire protocol
    is a preset somebody registers rather than another branch in here. This
    used to hardcode one gateway's path and one authentication header, which
    meant "add a provider" was an edit to this function.
    """
    import httpx

    from .model_client import Response

    client = httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0))

    def transport(route: Any, messages: Any, options: Any) -> Any:
        asked = float(options.get("timeout") or 0.0)
        preset = route.resolve()
        # `timeout` and `role` instruct the transport; they are not completion
        # parameters, and sending them as ones would have the provider reject
        # the request. Preflight's reachability probe sets `timeout`, because a
        # probe that inherited the work timeout would take ten minutes to
        # establish that a model is not answering. The preset decides which
        # keys those are.
        request = preset.request.render(route, messages, options)
        headers = {
            "content-type": "application/json",
            **preset.auth.headers(route, api_key),
            **request.headers,
        }
        try:
            response = client.request(
                request.method,
                request.url,
                headers=headers,
                json=dict(request.payload),
                timeout=(
                    httpx.Timeout(asked, connect=min(30.0, asked))
                    if asked
                    else httpx.USE_CLIENT_DEFAULT
                ),
            )
        except httpx.TimeoutException as exc:
            # ModelClient has a vendor-neutral transport contract. Normalize
            # httpx's hierarchy here so a real wire timeout follows the same
            # per-worker retry path as an injected transport timeout.
            raise TimeoutError(str(exc)) from exc
        except httpx.NetworkError as exc:
            raise ConnectionError(str(exc)) from exc
        return Response(response.status_code, dict(response.headers), response.text)

    return transport


#: What a route means when it names no preset. A deployment picks one; the
#: library default stays generic, because a default wire shape that guesses is
#: a request sent to a URL nobody chose.
DEFAULT_PRESET = "chat-completions"


def _resolved_preset(name: str) -> Any:
    """The deployment's default preset, or a refusal that names the choices.

    Checked before anything claims work. The alternative is discovering that a
    preset is not installed on the first model call — after the item is claimed
    and the attempt is spent.
    """
    from . import protocols

    try:
        return protocols.resolve(name)
    except protocols.UnknownPreset as exc:
        print(str(exc), file=sys.stderr)
        return None


def _report_routes(merged: dict[str, dict[str, Any]], preset: Any) -> None:
    """Say which protocol each role will be spoken to with, and any suggestion.

    A suggestion is printed and acted on by nobody. Choosing a protocol from a
    hostname would mean a fleet talking to a URL nobody configured, and the
    only symptom would be failures the classifier cannot explain.
    """
    from . import protocols

    print(f"protocol: {preset.describe()} (default for routes naming no preset)")
    for role, spec in sorted(merged.items()):
        named = str(spec.get("preset") or "").strip()
        if named and named != preset.name:
            found = protocols.find(named)
            print(
                f"  {role}: preset {named}" + (f" — {found.describe()}" if found else " — UNKNOWN")
            )
    seen: set[str] = set()
    for spec in merged.values():
        endpoint = str(spec.get("endpoint") or "")
        if not endpoint or endpoint in seen or str(spec.get("preset") or "").strip():
            continue
        seen.add(endpoint)
        hint = protocols.suggest(endpoint)
        if hint is not None and hint.preset != preset.name:
            print(f"note: {hint.why}")


def _run(args: argparse.Namespace) -> int:
    """Execute work items. This is the command that spends money and writes
    code, so it says exactly what it will do before doing any of it."""
    import json as _json

    from .executor import Checks, ContextPolicy, Executor
    from .github import GitHub
    from .guard import GUARD_KEY, CommandGuard
    from .holds import fanout, webhook_hook
    from .model_client import Chain, ModelClient, chains_from_map
    from .work import RUNNING, WorkQueue, WorkRecord

    # With a session host the CLI agent does the implementing, so only the
    # reviewer needs a model. Demanding three would be asking for two that are
    # never called.
    session_mode = bool(args.session_host)
    demo_mode = bool(getattr(args, "demo", False))
    if demo_mode and session_mode:
        print("--demo and --session-host are different first runs; pick one", file=sys.stderr)
        print(
            "--demo replaces the transport with fixed answers and needs no "
            "network. --session-host runs a real CLI agent in a real session.",
            file=sys.stderr,
        )
        return 2
    if demo_mode:
        # The demo's route lives in the queue's role map, written by
        # `init --demo`. Filling the flags here rather than requiring them
        # keeps the documented command to one line, and keeps the route a
        # *stored* one, so the demo exercises the same lookup a fleet does.
        from .demo import ENDPOINT as DEMO_ENDPOINT
        from .demo import MODEL as DEMO_MODEL

        for role in ("planner", "implementer", "reviewer"):
            if not getattr(args, role):
                setattr(args, role, DEMO_MODEL)
        if not args.endpoint:
            args.endpoint = DEMO_ENDPOINT
        if not args.no_push:
            # Not a default, a refusal. The demo has no repository and must
            # not acquire one by inheriting a flag.
            print("--demo runs entirely locally; pass --no-push", file=sys.stderr)
            return 2
    roles = {"planner": args.planner, "implementer": args.implementer, "reviewer": args.reviewer}
    if session_mode:
        roles = {"reviewer": args.reviewer}
    # A role with no flag is only missing if the STORED map cannot supply it
    # either. Asking the flags alone made the flags simultaneously required to
    # start and inert once given: a database holding a complete three-role map
    # with fallback chains still refused with "no model configured", naming the
    # very roles it had routes for (#153).
    from .api import ROLE_MAP_KEY

    stored_roles = WorkQueue(args.db).get_setting(ROLE_MAP_KEY) or {}
    missing = [
        name
        for name, model in roles.items()
        if not model and not (stored_roles.get(name, {}) or {}).get("model")
    ]
    if missing:
        print(f"no model configured for: {', '.join(missing)}", file=sys.stderr)
        print(
            "Every role needs one, and the reviewer should be a DIFFERENT vendor "
            "from the implementer — otherwise some share of reviews is a model "
            "grading its own work. Pass the flags, or PUT /api/roles.",
            file=sys.stderr,
        )
        return 2
    if not args.endpoint:
        print("no --endpoint (or $HARNESS_ENDPOINT) for the model API", file=sys.stderr)
        return 2
    if not args.repo and not args.no_push:
        print("no --repo, so there is nowhere to push or open a pull request", file=sys.stderr)
        print(
            "Pass --repo OWNER/NAME, or --no-push to work entirely locally: "
            "the branch and its commits are still there to inspect.",
            file=sys.stderr,
        )
        return 2

    api_key = os.environ.get("HARNESS_API_KEY", "")
    if not api_key and not args.dry_run and not demo_mode:
        print("HARNESS_API_KEY is not set", file=sys.stderr)
        return 2

    # Absolute from here on. Worktrees are created beside the repository and
    # git is invoked with `-C`, so a relative `--work` resolves against
    # whatever directory each subprocess happens to be in — which worked from
    # the repository's own parent and failed everywhere else, as
    # "cannot change to 'x/y': No such file or directory" in the middle of an
    # apply.
    args.work = args.work.resolve()

    queue = WorkQueue(args.db)
    if args.plan:
        from .plan import parse_plan_file

        plan = parse_plan_file(args.plan)
        added = queue.add(
            [
                WorkRecord(
                    item_id=i.id,
                    title=i.title,
                    brief=i.brief(),
                    depends_on=i.depends_on,
                    deliverable=i.deliverable,
                    project_id=args.project,
                )
                for i in plan.deduplicated()
                if not i.done
            ],
            project_id=args.project,
        )
        print(f"loaded {added} new items from {args.plan}")

    # The deployment's refusal list, read from the database rather than from
    # this command's flags: it governs every worker in this deployment, and a
    # policy that depended on which flags one operator typed would be a policy
    # that varies per terminal. `agent-harness guard` writes it.
    guard = CommandGuard.from_settings(queue.get_setting(GUARD_KEY))
    checks = Checks(commands=[shlex.split(c) for c in args.check], guard=guard)
    role_runner = None
    runner_name = str(args.role_runner or queue.get_setting("role_runner") or "").strip()
    if runner_name:
        from .role_runners import describe, resolve

        try:
            role_runner = resolve(runner_name)
            runner_detail = describe(role_runner)
        except Exception as exc:  # noqa: BLE001 - configuration refusal
            print(f"role runner: {exc}", file=sys.stderr)
            return 2
        if args.role_runner and queue.get_setting("role_runner") != runner_name:
            queue.set_setting("role_runner", runner_name)
        print(f"role runner: {runner_detail}")
    else:
        print("role runner: direct single-shot implementer (historical path)")
    # This project's counts, not the rollup. `--project` decides which queue
    # this run works, so a cross-project total here would report items no
    # worker in this process can claim.
    counts = queue.counts(args.project)
    print(f"queue: {counts or 'empty'}   project: {args.project}")
    print(f"repo: {args.work}   base: {args.base}   push: {not args.no_push}")
    if session_mode:
        print(f"agents: `{args.agent}` as sessions on {args.session_host}")
        print(f"reviewer: {args.reviewer}")
    else:
        print(
            f"roles: planner={args.planner} implementer={args.implementer} "
            f"reviewer={args.reviewer}   (no --session-host: nothing to attach to)"
        )
    print(f"checks before review: {args.check or '(none — nothing verifies the diff)'}")
    if args.dry_run:
        print("\ndry run: no model calls, no commits, no pull requests.")
        return 0

    events_path = args.events
    events_path.parent.mkdir(parents=True, exist_ok=True)
    # A failed patch is otherwise gone the moment the item fails, and the only
    # way to see what the model produced is to pay for it again.
    artifacts = (
        events_path.parent / "artifacts"
        if args.artifacts is None
        else (args.artifacts if str(args.artifacts) else None)
    )

    def write_event(event: dict[str, Any]) -> None:
        with events_path.open("a") as handle:
            handle.write(_json.dumps(event) + "\n")
        # Also say it out loud. Every stage transition used to go only to the
        # JSONL file, so a run printed a header and then nothing at all --
        # and a legitimate 210-second backoff after a gateway error looked
        # exactly like a wedged process to the person watching.
        log.info("%s", _describe(event))

    emit = write_event
    if getattr(args, "otel", False):
        # Lazily, and only when asked. Core never imports this; the CLI is
        # the door, which is what makes "add a telemetry backend without
        # editing the harness" true rather than aspirational.
        from .adapters.otlp import Exporter

        telemetry = Exporter()
        # The file write happens first and its exceptions propagate. The
        # event store is the source of truth; exporting must never come
        # between an event and the record of it.
        emit = telemetry.tap(write_event)
        if telemetry.available:
            print(f"telemetry: exporting spans to {telemetry.endpoint}")
        else:
            print(
                "telemetry: --otel was passed but no OTEL_EXPORTER_OTLP_ENDPOINT is set, "
                "so nothing is exported and nothing else changes"
            )

    # A question now says so, once, instead of waiting to be discovered by a
    # poll (#188). The stream is always a consumer; the webhook is the one
    # thing an operator configures. Neither can reach the item: `holds.fanout`
    # drops what either of them raises.
    queue.holds.on_hold = fanout(emit, webhook_hook(args.hold_webhook))
    if args.hold_webhook:
        print(f"holds: notices POSTed to {args.hold_webhook}")

    from .api import ROLE_MAP_KEY

    # Seed the shared map from the command line, then read it back per call so
    # `PUT /api/roles` takes effect without a restart.
    # A role may name several models, comma-separated and in preference
    # order. The first that answers does the work; the rest exist because on
    # this endpoint 34 of 42 advertised models were unavailable at once, and a
    # fleet with one name per role simply stops when that name is down.
    from_cli = {
        name: {
            # Both, on purpose: `models` is the chain, `model` is the
            # preferred one, so a reader that predates fallbacks -- including
            # the `RoleRoute` wire schema -- still sees a route rather than a
            # role it thinks is unconfigured.
            "models": (names := [m.strip() for m in model.split(",") if m.strip()]),
            "model": names[0] if names else "",
            "endpoint": args.endpoint,
            # The *classifier*, and only the classifier — this field always
            # meant that, and the wire shape comes from `--preset`. Kept as the
            # seeded default because it is the one this deployment has live
            # evidence for: it can tell a burst limit from a weekly spend cap,
            # where the generic HTTP classifier cannot. Change it per role with
            # PUT /api/roles, which also accepts a whole `preset`.
            "provider": "claw-bay",
        }
        for name, model in roles.items()
        if model
    }
    stored_map = queue.get_setting(ROLE_MAP_KEY) or {}
    # MERGED, per role, not chosen wholesale. A stored route wins for the role
    # it names, because re-routing a role live is the point of storing it --
    # but a map holding only `reviewer` used to suppress the planner and
    # implementer the operator had just typed, and the run then failed its
    # first item with `no route for role 'planner'` after claiming it.
    # `--reroute` inverts it: the operator is saying the command line is the
    # correction, not the seed. Without it, changing a model on a database that
    # has ever run meant editing the settings table by hand or deleting the
    # queue, which are both worse than the problem.
    merged = {**stored_map, **from_cli} if args.reroute else {**from_cli, **stored_map}
    if merged != stored_map:
        queue.set_setting(ROLE_MAP_KEY, merged)
    overridden = sorted(
        name
        for name in from_cli
        if name in stored_map and stored_map[name].get("model") != from_cli[name]["model"]
    )
    if overridden:
        # Logged, not printed. This changes which model a run calls, so it
        # belongs on the same stream as the rest of the run's narration —
        # stdout is block-buffered into a pipe or a file, so the note arrived
        # out of order when it arrived at all, and was lost entirely when the
        # process was killed. Two runs were spent on a model the operator had
        # explicitly replaced on the command line (#153).
        was = {name: stored_map[name].get("model") for name in overridden}
        asked = {name: from_cli[name]["model"] for name in overridden}
        log.warning(
            "a stored role map overrides the command line: %s. "
            "The flags seed the map on first use only; after that the stored map wins. "
            "Pass --reroute to make the command line win, or PUT /api/roles.",
            "; ".join(f"{name}={was[name]} (you asked for {asked[name]})" for name in overridden),
        )
    filled = sorted(set(from_cli) - set(stored_map))
    if stored_map and filled:
        log.info("the stored role map had no route for %s; used the flags", ", ".join(filled))

    preset = _resolved_preset(args.preset)
    if preset is None:
        return 2
    _report_routes(merged, preset)

    def live_routes() -> dict[str, Chain]:
        return chains_from_map(
            queue.get_setting(ROLE_MAP_KEY) or {}, api_key=api_key, default_preset=preset.name
        )

    # Nothing claims work until every role this run needs can be routed. The
    # alternative is finding out on the first model call -- after the project
    # is running and the item is claimed, so the failure costs an attempt and
    # leaves failed work in the queue.
    unroutable = [
        name
        for name in roles
        if not (merged.get(name, {}).get("model") and merged.get(name, {}).get("endpoint"))
    ]
    if unroutable:
        print(f"no usable route for role(s): {', '.join(unroutable)}", file=sys.stderr)
        print(
            "The stored role map is incomplete and the command line did not fill it. "
            "PUT /api/roles to set them, or pass the flags for those roles.",
            file=sys.stderr,
        )
        return 2

    # Before anything claims, and before the first model call: a direct run
    # works in place and begins each attempt by discarding the working tree, so
    # a dirty checkout is uncommitted work about to be destroyed. Refusing is
    # the only safe default; --allow-dirty is how you say it is disposable.
    # Session mode does not apply — it works in its own worktree.
    if not session_mode and args.work and (Path(args.work) / ".git").exists():
        from .preflight import _is_clean_tree

        clean, why = _is_clean_tree(str(args.work))
        if not clean and not args.allow_dirty:
            print(f"refusing to start: {why}", file=sys.stderr)
            return 2
        if not clean:
            log.warning("--allow-dirty: %s", why)

    # Applies to BOTH modes, unlike the check above. A worktree cut from a base
    # that has fallen behind is the rdpapp failure exactly: session mode gives
    # each item a pristine worktree, and a pristine worktree of the wrong
    # lineage is still the wrong lineage (#180).
    if args.work and args.base and (Path(args.work) / ".git").exists():
        from .preflight import _base_is_current

        current, where = _base_is_current(str(args.work), args.base)
        if not current and not args.allow_stale_base:
            print(f"refusing to start: {where}", file=sys.stderr)
            return 2
        if not current:
            log.warning("--allow-stale-base: %s", where)
        else:
            log.info("base: %s", where)

    if demo_mode:
        from .demo import demo_transport

        transport = demo_transport(args.work)
        print("demo: fixed answers, no network, no model. This proves wiring, not quality.")
    else:
        transport = _http_transport(api_key)

    client = ModelClient(
        roles=live_routes(),
        transport=transport,
        on_event=emit,
        routes_provider=live_routes,
    )

    # Said out loud, every run. This was documented in three places and
    # enforced in none, so a reviewer could be the same model as the
    # implementer and nothing would mention it -- every review a model
    # grading its own work, invisibly.
    #
    # Against the implementer that actually runs: in session mode the agent
    # process writes the code, so comparing the reviewer to the configured
    # implementer route would be a verdict about a pairing that never happens.
    independent, why = client.reviewer_independence(
        implemented_by=args.agent if session_mode else ""
    )
    print(("reviewer: " if independent else "WARNING: ") + why)

    executor: Any
    if session_mode:
        from .session_executor import AgentSpec, SessionExecutor
        from .session_host import HttpSessionHost

        host_token = os.environ.get("AIDEVENV_TOKEN", "") or api_key
        executor = SessionExecutor(
            queue,
            HttpSessionHost(args.session_host, token=host_token),
            args.work,
            agent=AgentSpec(command=tuple(shlex.split(args.agent))),
            checks=checks,
            guard=guard,
            reviewer=client,
            github=GitHub(args.repo) if args.repo else None,
            base_branch=args.base,
            ui_base_url=args.session_host,
            context_budget=args.context_budget,
            follow_ups=artifacts,
            on_event=emit,
            push=not args.no_push,
            project_id=args.project,
        )
    else:
        # The flag wins, then the project's own setting, then the default.
        # Named in that order because a run someone typed a mode onto is a run
        # they meant to type it onto.
        project = queue.get_project(args.project)
        durability = args.durability or (project.durability if project else "") or None
        executor = Executor(
            queue,
            client,
            args.work,
            checks=checks,
            context_policy=ContextPolicy(
                budget=args.context_budget,
                fallback_budget=args.context_fallback_budget,
            ),
            durability=durability,
            github=GitHub(args.repo) if args.repo else None,
            base_branch=args.base,
            on_event=emit,
            push=not args.no_push,
            artifacts=artifacts,
            # Without this, `run --project X` set X running and then claimed
            # from `default` — which is nobody's project once more than one
            # exists, and reports "nothing to do" over a full queue.
            project_id=args.project,
            role_runner=role_runner,
            runner_step_limit=args.runner_step_limit,
            runner_command_timeout=args.runner_command_timeout,
        )
    # Typing `agent-harness run` IS the human deciding to start this project.
    # A project starts `stopped` so a restart never resumes on its own, but
    # applying that to an explicit command would make the CLI silently do
    # nothing -- correct by the letter of the rule and useless.
    state, _ = queue.control(project_id=args.project)
    if state != RUNNING:
        queue.set_control(RUNNING, reason="agent-harness run", project_id=args.project)
        print(f"project {args.project}: {state} -> running")

    if args.serve:
        print(f"serving; polling every {args.poll:.0f}s. Ctrl-C to stop.")
        try:
            outcomes = executor.serve(poll_seconds=args.poll)
        except KeyboardInterrupt:
            outcomes = []
            print("\nstopping after the current item")
        finally:
            queue.checkpoint()
    else:
        outcomes = executor.run(limit=args.limit)
        queue.checkpoint()
    if not outcomes:
        print("nothing to do")
        return 0
    for line in run_summary(outcomes):
        print(line)
    return 1 if any(_is_failure(o) for o in outcomes) else 0


def _is_failure(outcome: Any) -> bool:
    """Did this go wrong, as opposed to needing a person?

    An escalation is not a failure. Nothing malfunctioned, no attempt was
    wasted, and the item is waiting on a decision only a human can make — so
    it must not colour the exit status, or a queue full of well-formed
    questions reads to CI as a broken run.
    """
    from .outcomes import NEEDS_A_PERSON

    if outcome.stop is not None and outcome.stop.disposition in NEEDS_A_PERSON:
        return False
    return not outcome.ok


def run_summary(outcomes: Sequence[Any]) -> list[str]:
    """The lines a finished run prints, as data so they can be tested.

    `FAIL` and `ok` were the whole vocabulary, so an item that escalated —
    nothing went wrong, a person is needed — was announced as a failure.
    Measured on rdpapp R7: an agent correctly refused an impossible item, with
    citations and no attempt spent, and the run's last word on it was `FAIL R7`.
    """
    from .outcomes import NEEDS_A_PERSON

    lines = []
    waiting = [o for o in outcomes if o.stop and o.stop.disposition in NEEDS_A_PERSON]
    for outcome in outcomes:
        mark = "YOU" if outcome in waiting else ("ok " if outcome.ok else "FAIL")
        detail = outcome.pr_url or outcome.reason[:100]
        lines.append(f"  {mark} {outcome.item_id}: {' -> '.join(outcome.stages)}  {detail}")
    done = [o for o in outcomes if o.ok]
    lines.append(f"{len(done)}/{len(outcomes)} items completed")
    if waiting:
        lines.append(
            f"{len(waiting)} waiting on you, not on a retry: "
            + ", ".join(o.item_id for o in waiting)
        )
    return lines


def _role_chain(
    models: str,
    endpoint: str,
    api_key: str,
    preset: str = "",
) -> list[Any]:
    """One role's models, preferred first, from a comma-separated name.

    Every command that names a role comes through here. `run` had fallback
    chains and the single-call commands did not, because each built its own
    route: a `Route` holds one model, and only `run` split its flags on
    commas. Measured cost of that divergence: a surveyor retried one model
    returning 524 for fifteen minutes while another model on the *same
    endpoint* was answering 200, and the run only moved because a human
    noticed and restarted it by hand.

    "One model call" is why a chain matters more here, not less. `run` can
    lose an item and re-claim it under a lease; a single-call command that
    fails returns nothing and leaves nothing behind.
    """
    from .model_client import Route

    names = [m.strip() for m in models.split(",") if m.strip()]
    return [Route(name, endpoint, api_key=api_key, preset=preset) for name in names]


def _assessor(args: argparse.Namespace) -> Any:
    """The optional `assessor` role, over the same transport `run` uses."""
    from .adoption import ASSESSOR, ModelAssessor
    from .executor import _text_of
    from .model_client import ModelClient

    api_key = os.environ.get("HARNESS_API_KEY", "")
    client = ModelClient(
        roles={ASSESSOR: _role_chain(args.assessor_model, args.endpoint, api_key)},
        transport=_http_transport(api_key),
    )

    def ask(prompt: str) -> str:
        reply = client.call(ASSESSOR, [{"role": "user", "content": prompt}])
        return _text_of(reply.body)

    return ModelAssessor(ask)


def _survey(args: argparse.Namespace) -> int:
    """Read an existing project and propose a plan for a stated objective.

    Writes nothing unless `--out` is given, and refuses to write a plan the
    harness's own parser could not read. The parser is the gate on purpose: a
    generated plan that the queue reads differently from how it looks is worse
    than no plan, and catching it here costs one function call.
    """
    from .executor import _text_of
    from .model_client import ModelClient
    from .outputs import OutputBusy, claiming
    from .survey import SURVEYOR, survey

    if not (args.work / ".git").exists():
        print(f"{args.work} is not a git repository", file=sys.stderr)
        return 2
    if not args.surveyor:
        print(
            "--surveyor MODEL is required (comma-separated names are a "
            "fallback chain, tried in order)",
            file=sys.stderr,
        )
        return 2
    if not args.endpoint:
        print("--endpoint or $HARNESS_ENDPOINT is required", file=sys.stderr)
        return 2
    # Checked before the model call, not after it. Refusing to overwrite is
    # only useful if it happens before the minutes and the money are spent.
    if args.out is not None and args.out.exists() and not args.replace:
        print(
            f"refusing to overwrite {args.out}. A plan under review is the "
            f"thing this command exists to produce; pass --replace to "
            f"discard it, or point --out somewhere else.",
            file=sys.stderr,
        )
        return 2

    api_key = os.environ.get("HARNESS_API_KEY", "")
    client = ModelClient(
        roles={
            SURVEYOR: _role_chain(args.surveyor, args.endpoint, api_key, args.preset),
        },
        transport=_http_transport(api_key),
    )

    def ask(prompt: str) -> str:
        return str(_text_of(client.call(SURVEYOR, [{"role": "user", "content": prompt}]).body))

    try:
        with claiming(args.out):
            report = survey(
                args.objective,
                args.work,
                ask=ask,
                docs=list(args.doc),
                name=args.name,
                now=time.time(),
            )
    except OutputBusy as busy:
        print(str(busy), file=sys.stderr)
        return 2
    for line in report.lines():
        print(line)

    if not report.usable and not args.force:
        print(
            "refusing to write: the harness cannot read the plan it just generated. "
            "Re-run, name the roadmap with --doc, or pass --force.",
            file=sys.stderr,
        )
        return 1
    if args.out is None:
        print()
        print(report.markdown)
        print("(nothing was written; pass --out PATH to keep it)")
        return 0
    args.out.write_text(report.markdown)
    print(f"wrote {args.out}")
    print(f"Review it, then: agent-harness adopt {args.out} --project NAME --work {args.work}")
    return 0


def _adopt(args: argparse.Namespace) -> int:
    """Inspect an existing project, then reconcile only with explicit approval.

    The default is inspection: it reads the plan, the repository, the queue
    and — with `--repo` — the issues and pull requests, and prints what it
    would do. Nothing outside the harness changes until someone types
    `--approve`, and no completed work is dropped unless they also name it.
    """
    import json as _json

    from .adoption import DEFAULT_VERIFY_TIMEOUT, Adoption, GitHubAdoptionInspector
    from .github import GitHub
    from .plan import parse_plan_file
    from .work import WorkQueue

    decision = args.reject or args.revise
    if args.reconcile and not args.approve:
        print("--reconcile requires --approve", file=sys.stderr)
        return 2
    if args.approve_drop and not args.approve:
        print("--approve-drop requires --approve", file=sys.stderr)
        return 2
    if args.approve and decision:
        print("--approve and --reject/--revise are opposite decisions", file=sys.stderr)
        return 2
    if args.dry_run and (args.approve or decision):
        print(
            "--dry-run inspects and stores nothing, so there is no proposal to "
            "approve, reject or revise. Run it without --dry-run first.",
            file=sys.stderr,
        )
        return 2
    if args.assessor_model and not args.endpoint:
        print("--assessor-model needs --endpoint (or $HARNESS_ENDPOINT)", file=sys.stderr)
        return 2

    external = GitHubAdoptionInspector(GitHub(args.repo)) if args.repo else None
    adopter = Adoption(
        WorkQueue(args.db),
        args.work,
        external=external,
        assessor=_assessor(args) if args.assessor_model else None,
        verify_timeout=(
            DEFAULT_VERIFY_TIMEOUT if args.verify_timeout is None else args.verify_timeout
        ),
    )
    report = adopter.inspect(
        args.project,
        parse_plan_file(args.path),
        dry_run=args.dry_run,
        persist=not args.dry_run,
    )
    if decision:
        report = adopter.reject(args.project, reason=decision, revise=bool(args.revise))
    elif args.approve:
        report = adopter.approve(args.project, approved_drops=args.approve_drop)
        if args.reconcile:
            report = adopter.reconcile(args.project)

    print(report.summary())
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(_json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")
        print(f"report written to {args.report}")
    if args.reconcile:
        return 0
    unapproved = sorted(set(report.proposed_drops()) - set(report.approved_drops))
    print(
        "inspection only: no queue rows, issue edits or other external changes were made."
        + (
            f"\n{len(unapproved)} item(s) proposed as already delivered and NOT dropped: "
            + ", ".join(unapproved)
            if unapproved
            else ""
        )
        + "\nUse --approve --reconcile, and name every allowed drop with --approve-drop."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    # Imported here, as every other executor name in this module is: the CLI
    # starts for `--help` on a machine with no queue and no credentials, and
    # only the default value is needed to print it.
    from .executor import DEFAULT_CONTEXT_BUDGET
    from .session_executor import default_agent_command

    parser = argparse.ArgumentParser(prog="agent-harness", description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get("HARNESS_DB", "harness.sqlite"),
        help="SQLite path (default: $HARNESS_DB or ./harness.sqlite)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("HARNESS_LOG_LEVEL", "info"),
        choices=("debug", "info", "warning", "error"),
        help="How much the harness says about what it is doing (or "
        "$HARNESS_LOG_LEVEL). Defaults to info: this process spends money "
        "unattended, and silence is not a safe default for that.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="read event streams into the store")
    p_ingest.add_argument(
        "--events",
        action="append",
        type=Path,
        metavar="PATH",
        help="the harness's own JSONL event stream (repeatable)",
    )
    p_ingest.add_argument(
        "--adapter",
        choices=["oxidex"],
        help="read another tool's logs via an adapter",
    )
    p_ingest.add_argument(
        "--adapter-path", type=Path, metavar="DIR", help="log directory the adapter should read"
    )
    p_ingest.add_argument(
        "--watch",
        type=float,
        metavar="SECONDS",
        help="re-ingest every SECONDS instead of exiting",
    )

    p_plan = sub.add_parser("plan", help="sync a plan .md into a GitHub backlog")
    p_plan.add_argument("path", type=Path, help="the plan markdown file")
    # Not required with --dry-run: a dry run contacts GitHub for nothing and
    # writes nothing, so demanding an owner/name meant inventing a repository
    # that does not exist in order to ask a purely local question — what can
    # the parser read? (#148)
    p_plan.add_argument(
        "--repo",
        metavar="OWNER/NAME",
        help="the GitHub repository to sync into. Required unless --dry-run, "
        "which parses and reports without contacting anything.",
    )
    p_plan.add_argument(
        "--dry-run", action="store_true", help="report what would change without writing"
    )
    p_plan.add_argument(
        "--allow-duplicates",
        action="store_true",
        help="sync anyway when the plan states an id twice (the richest description wins)",
    )

    p_graph = sub.add_parser(
        "graph",
        help="inspect, export, rebuild or checkpoint the dependency graph",
    )
    p_graph.add_argument(
        "action",
        choices=("report", "export", "rebuild", "checkpoint"),
        help="report: why each item is or is not ready. export: a JSON backup that "
        "outlives this schema. rebuild: re-derive every edge from work.depends_on. "
        "checkpoint: fold the WAL back into the database file before a backup.",
    )
    p_graph.add_argument(
        "--project",
        metavar="ID",
        help="limit to one project (default: every project)",
    )
    p_graph.add_argument(
        "--out",
        type=Path,
        metavar="FILE",
        help="where `export` writes its JSON (default: stdout)",
    )

    p_survey = sub.add_parser(
        "survey", help="read an existing project and propose a plan for an objective"
    )
    p_survey.add_argument(
        "objective",
        help='what you want done, in prose: "review and generate a plan to upgrade to Node v22"',
    )
    p_survey.add_argument(
        "--work", type=Path, default=Path("."), help="the existing repository to read"
    )
    p_survey.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write the proposed plan here. Without this it is printed and nothing is "
        "written, which is the safe default for a command whose whole output is a "
        "proposal to argue with.",
    )
    p_survey.add_argument(
        "--doc",
        action="append",
        default=[],
        metavar="PATH",
        help="a document that states this project's direction, relative to the "
        "repository. Repeatable. Naming them beats the guesses: a plan built without "
        "the roadmap is confident and wrong, and a named file that is missing is "
        "reported rather than skipped silently.",
    )
    p_survey.add_argument("--name", default="Plan", help="title for the generated plan")
    p_survey.add_argument(
        "--surveyor",
        default=os.environ.get("HARNESS_SURVEYOR", ""),
        help="model for the surveyor role (or $HARNESS_SURVEYOR). Several, "
        "comma-separated, are a fallback chain in preference order — the "
        "first that answers does the work.",
    )
    p_survey.add_argument(
        "--replace",
        action="store_true",
        help="overwrite an existing --out. Without this an existing plan is "
        "kept: it is the artefact under review, and re-running would discard "
        "the version being argued over.",
    )
    p_survey.add_argument(
        "--endpoint",
        default=os.environ.get("HARNESS_ENDPOINT", ""),
        help="model API base url (or $HARNESS_ENDPOINT)",
    )
    p_survey.add_argument(
        "--preset", default="", help="route preset, as for run: claw-bay, chat-completions, generic"
    )
    p_survey.add_argument(
        "--force",
        action="store_true",
        help="write the plan even when the harness's own parser could not read it. "
        "The parser is the gate here; overriding it means executing a plan the "
        "queue will read differently from how it looks.",
    )

    p_adopt = sub.add_parser(
        "adopt", help="inspect an existing project and propose a reconciliation"
    )
    p_adopt.add_argument("path", type=Path, help="the plan markdown file")
    p_adopt.add_argument("--project", required=True, help="stable queue project id")
    p_adopt.add_argument(
        "--work", type=Path, default=Path("."), help="existing repository working tree"
    )
    p_adopt.add_argument(
        "--repo",
        default="",
        metavar="OWNER/NAME",
        help="optional GitHub repository to inspect read-only before approval",
    )
    p_adopt.add_argument(
        "--approve", action="store_true", help="approve this exact reconciliation proposal"
    )
    p_adopt.add_argument(
        "--approve-drop",
        action="append",
        default=[],
        metavar="ITEM",
        help="allow one proposed completed item to enter the queue as done; repeatable",
    )
    p_adopt.add_argument(
        "--reconcile",
        action="store_true",
        help="write approved queue rows and marker backfills; otherwise inspection is read-only",
    )
    p_adopt.add_argument(
        "--reject", default="", metavar="REASON", help="refuse this proposal, with a reason"
    )
    p_adopt.add_argument(
        "--revise",
        default="",
        metavar="REASON",
        help="send this proposal back to be inspected again, with a reason",
    )
    p_adopt.add_argument(
        "--dry-run",
        action="store_true",
        help="inspect and print, storing nothing at all — not even the proposal",
    )
    p_adopt.add_argument(
        "--report", type=Path, default=None, metavar="FILE", help="also write the report as JSON"
    )
    p_adopt.add_argument(
        "--assessor-model",
        default=os.environ.get("HARNESS_ASSESSOR", ""),
        metavar="MODEL",
        help="ask a model whether unverifiable items already exist (or "
        "$HARNESS_ASSESSOR). It can only propose; a drop still needs "
        "--approve-drop.",
    )
    p_adopt.add_argument(
        "--endpoint",
        default=os.environ.get("HARNESS_ENDPOINT", ""),
        help="model API endpoint for --assessor-model",
    )
    p_adopt.add_argument(
        "--verify-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="timeout for each item's `verify:` command; defaults to the project-check timeout",
    )

    p_init = sub.add_parser("init", help="create a first-run environment that needs no credentials")
    p_init.add_argument(
        "--demo",
        action="store_true",
        required=True,
        help="build the deterministic demo. Required, and the only mode there "
        "is: `init` with nothing to initialise would be a command that guesses "
        "what you wanted.",
    )
    p_init.add_argument(
        "--into",
        type=Path,
        default=Path("demo"),
        metavar="DIR",
        help="where to build it (default: ./demo). Must not already exist.",
    )

    p_doctor = sub.add_parser(
        "doctor", help="report what is configured and what is missing, spending nothing"
    )
    p_doctor.add_argument(
        "--project",
        default="",
        metavar="ID",
        help="limit to one project (default: every project)",
    )
    p_doctor.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    p_doctor.add_argument(
        "--probe-models",
        action="store_true",
        help="also ask each routed model a one-token question. OFF by default: "
        "it needs a network and a credential, and it spends, however little. "
        "Everything else doctor reports is answered without leaving the machine.",
    )
    p_doctor.add_argument(
        "--endpoint",
        default=os.environ.get("HARNESS_ENDPOINT", ""),
        help="model API base url for --probe-models (or $HARNESS_ENDPOINT)",
    )

    p_guard = sub.add_parser(
        "guard",
        help="show or set the commands this deployment refuses to run",
        description="What the harness will not run on an agent's behalf. With no "
        "arguments it prints the current policy and changes nothing. A refusal is "
        "TERMINAL for the item that triggers it (owner decision, 2026-08-05): the "
        "command is blocked, the item stops in `blocked` with disposition "
        "`blocked_by_policy`, and it is never handed back to the agent to retry.",
    )
    p_guard.add_argument(
        "--refuse",
        action="append",
        default=[],
        metavar="PATTERN",
        help="a command pattern to refuse, e.g. --refuse 'sh -c'. The first word "
        "matches the program (by basename too), the rest match anywhere in its "
        "arguments. Repeatable; together they REPLACE this deployment's list.",
    )
    p_guard.add_argument(
        "--no-defaults",
        action="store_true",
        help="drop the built-in default refusals. They are small and generic "
        "(privilege escalation, host lifecycle, force push) and dropping them is a "
        "deliberate widening.",
    )
    p_guard.add_argument(
        "--no-confine",
        action="store_true",
        help="allow a guarded command to name paths outside the item's worktree. "
        "This is the boundary that makes ~/.ssh, /etc and `rm -rf /` unreachable; "
        "turning it off is a deliberate widening.",
    )
    p_guard.add_argument(
        "--clear",
        action="store_true",
        help="forget this deployment's policy. The built-in default returns, and "
        "doctor goes back to reporting the guard as NOT CONFIGURED.",
    )

    p_run = sub.add_parser("run", help="execute claimed work items")
    p_run.add_argument(
        "--repo",
        default="",
        metavar="OWNER/NAME",
        help="GitHub repo for issues and pull requests. Required unless "
        "--no-push: with nothing to push, there is nothing to open a pull "
        "request against, and demanding a repo would mean inventing one.",
    )
    p_run.add_argument(
        "--work", type=Path, default=Path("."), help="the git working tree the agents change"
    )
    p_run.add_argument(
        "--plan", type=Path, help="plan .md to load work from (else the queue as-is)"
    )
    p_run.add_argument(
        "--events", type=Path, default=Path("events.jsonl"), help="where to append the event stream"
    )
    p_run.add_argument(
        "--hold-webhook",
        default=os.environ.get("HARNESS_HOLD_WEBHOOK", ""),
        metavar="URL",
        help="POST a JSON notice here when an item stops to ask a person something "
        "(or $HARNESS_HOLD_WEBHOOK). One URL is the whole configuration: what is on "
        "the other end is not this service's business. Delivery is best-effort and "
        "can never fail or stall the item — without it the question is still in "
        "`GET /api/holds` and the event stream.",
    )
    p_run.add_argument(
        "--artifacts",
        type=Path,
        default=None,
        metavar="DIR",
        help="where to keep a patch that could not be applied, for inspection "
        "or replay. Defaults to an `artifacts` directory beside --events; "
        "pass an empty string to keep nothing.",
    )
    p_run.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="CMD",
        help="shell-free check command, repeatable, e.g. "
        "--check 'pytest -q'. Run BEFORE the reviewer.",
    )
    p_run.add_argument(
        "--session-host",
        default=os.environ.get("AIDEVENV_URL", ""),
        metavar="URL",
        help="base URL of a session host (AIDevEnv). With this, each agent runs as a "
        "TERMINAL SESSION you can attach to, which is the point. Without it, the "
        "harness calls the model API directly and there is nothing to watch.",
    )
    p_run.add_argument(
        "--role-runner",
        default=os.environ.get("HARNESS_ROLE_RUNNER", ""),
        metavar="NAME",
        help="installed role runner for implementation (or $HARNESS_ROLE_RUNNER). "
        "Resolved by name through installed metadata; empty keeps the historical "
        "single-shot implementer while the loop path earns delivery evidence.",
    )
    p_run.add_argument(
        "--runner-step-limit",
        type=int,
        default=int(os.environ.get("HARNESS_RUNNER_STEP_LIMIT", "") or 80),
        metavar="N",
        help="whole-loop model-call ceiling (default 80, or $HARNESS_RUNNER_STEP_LIMIT).",
    )
    p_run.add_argument(
        "--runner-command-timeout",
        type=int,
        default=int(os.environ.get("HARNESS_RUNNER_COMMAND_TIMEOUT", "") or 300),
        metavar="SECONDS",
        help="timeout for one feedback command inside the role loop (default 300).",
    )
    p_run.add_argument(
        "--agent",
        # The executor's default, not a second one. They disagreed: the
        # executor's carried `--permission-mode acceptEdits` and the CLI's did
        # not, so `run --session-host` without this flag produced an agent that
        # could not write. It reported no changes and read as a model that had
        # considered the task and declined — measured, and it cost a run.
        default=" ".join(default_agent_command()),
        metavar="CMD",
        help="CLI agent to run per item (or $HARNESS_AGENT_COMMAND). "
        "`{prompt_file}` is substituted. Defaults to the agent command the "
        "session executor declares, which grants edit permission — an agent "
        "that cannot write reports no changes, which is indistinguishable "
        "from one that chose to make none.",
    )
    p_run.add_argument("--limit", type=int, help="stop after N items")
    p_run.add_argument(
        "--project",
        default="default",
        help="Which project's queue to work. Items are keyed by (project, id), "
        "so two projects may each have a T1.",
    )
    p_run.add_argument(
        "--serve",
        action="store_true",
        help="Keep running when the queue is empty, waiting for work, instead "
        "of exiting. Without this a plan synced an hour later is never picked up.",
    )
    p_run.add_argument(
        "--poll",
        type=float,
        default=15.0,
        help="Seconds between checks for new work when serving.",
    )
    p_run.add_argument("--base", default="main", help="branch to base work on")
    p_run.add_argument(
        "--no-push", action="store_true", help="commit locally but do not push or open PRs"
    )
    p_run.add_argument(
        "--endpoint",
        default=os.environ.get("HARNESS_ENDPOINT", ""),
        help="model API base url (or $HARNESS_ENDPOINT)",
    )
    p_run.add_argument(
        "--preset",
        default=os.environ.get("HARNESS_ROUTE_PRESET", DEFAULT_PRESET),
        metavar="NAME",
        help="route preset for roles that name none: the wire protocol, the "
        "authentication header, the response reader and a failure classifier, "
        "as one name (or $HARNESS_ROUTE_PRESET). A role's stored `preset` "
        "overrides this; its older `provider` field selects only the classifier. "
        "Register your own — nothing here needs editing to add a vendor.",
    )
    p_run.add_argument(
        "--planner",
        default=os.environ.get("HARNESS_PLANNER", ""),
        help="model for the planner role (or $HARNESS_PLANNER)",
    )
    p_run.add_argument(
        "--implementer",
        default=os.environ.get("HARNESS_IMPLEMENTER", ""),
        help="model for the implementer role (or $HARNESS_IMPLEMENTER)",
    )
    p_run.add_argument(
        "--reviewer",
        default=os.environ.get("HARNESS_REVIEWER", ""),
        help="model for the reviewer role (or $HARNESS_REVIEWER) — use a "
        "DIFFERENT vendor from the implementer, so a model does not grade "
        "its own work",
    )
    p_run.add_argument(
        "--dry-run", action="store_true", help="show what would run, call nothing, change nothing"
    )
    p_run.add_argument(
        "--otel",
        action="store_true",
        help="also project this run's events to OpenTelemetry spans. Needs "
        "$OTEL_EXPORTER_OTLP_ENDPOINT and the OpenTelemetry SDK; without "
        "either it says so once and changes nothing else. **Export only** — "
        "the event stream stays the single source of truth, and a span is "
        "never read back to answer a question the events could answer.",
    )
    p_run.add_argument(
        "--durability",
        default=os.environ.get("HARNESS_DURABILITY", ""),
        choices=("", "exit", "boundary", "sync"),
        help="how often an attempt is made durable, so a killed worker resumes "
        "rather than re-paying for the planner and the implementer. `exit` "
        "writes nothing until the attempt ends; `boundary` (the default) writes "
        "one row per stage; `sync` also records the intent to perform each "
        "external effect before it happens. Empty takes the project's setting, "
        "then the default. The pre-review git checkpoint is unaffected by all "
        "three (or $HARNESS_DURABILITY).",
    )
    p_run.add_argument(
        "--reroute",
        action="store_true",
        help="make the role flags win over the stored role map, and store them. "
        "Without this the flags SEED the map on first use and are ignored ever "
        "after, because a stored route is how `PUT /api/roles` re-routes a live "
        "deployment — so on a database that has run before, --implementer does "
        "nothing unless you pass this.",
    )
    p_run.add_argument(
        "--allow-dirty",
        action="store_true",
        help="run against a checkout that has uncommitted or untracked files. A "
        "direct run works IN PLACE and discards the working tree before each "
        "attempt — tracked changes are reverted and untracked files are deleted, "
        "neither recoverably — so a dirty checkout is refused by default. Pass "
        "this only when the tree is genuinely disposable.",
    )
    p_run.add_argument(
        "--allow-stale-base",
        action="store_true",
        help="run against a base branch that has fallen well behind its upstream. "
        "A stale base is the one wrong setting every later stage reports as a "
        "success — the agent works, the checks pass, the reviewer approves, and "
        "the commit lands on a lineage nobody develops on any more — so it is "
        "refused by default. Pass this when the base really is the line of work.",
    )
    p_run.add_argument(
        "--context-budget",
        type=int,
        default=int(os.environ.get("HARNESS_CONTEXT_BUDGET", "") or DEFAULT_CONTEXT_BUDGET),
        help="the most characters of repository the implementer may be shown "
        f"(default {DEFAULT_CONTEXT_BUDGET}, or $HARNESS_CONTEXT_BUDGET). A file "
        "larger than this cannot be supplied at all, and an item whose target "
        "does not fit is stopped before the implementer is paid rather than "
        "asked to change a file it cannot see. Raise it for a repository with "
        "large files; the ceiling that matters is the model's context window. "
        "It is a ceiling and not a target: an item is shown what is relevant "
        "to it and no more, so raising this does not make every item cost more.",
    )
    p_run.add_argument(
        "--context-fallback-budget",
        type=int,
        default=(
            int(os.environ["HARNESS_CONTEXT_FALLBACK_BUDGET"])
            if os.environ.get("HARNESS_CONTEXT_FALLBACK_BUDGET")
            else None
        ),
        help="how many of those characters the surrounding context — files the "
        "planner did not name — may use (default: the standard "
        f"{DEFAULT_CONTEXT_BUDGET}, or $HARNESS_CONTEXT_FALLBACK_BUDGET, never "
        "more than --context-budget). Targets are supplied whole up to the "
        "budget regardless; this is what keeps a budget raised for one large "
        "file from enlarging every other item's prompt.",
    )
    p_run.add_argument(
        "--demo",
        action="store_true",
        help="answer every model call from a fixed script instead of a network. "
        "Needs no credentials, no endpoint and no model, and proves the wiring "
        "only — the answers are written to succeed. Use with `init --demo`.",
    )

    p_serve = sub.add_parser(
        "serve", help="serve the JSON API and self-contained browser control plane"
    )
    p_serve.add_argument(
        "--audit-db",
        default="",
        help="Audit database. Defaults to audit.sqlite beside --db, or "
        "HARNESS_AUDIT_DB. Put it on a different volume to stop history "
        "sharing a fate with the queue.",
    )
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8099)
    p_serve.add_argument(
        "--session-host",
        default=os.environ.get("AIDEVENV_URL", ""),
        metavar="URL",
        help="optional execution adapter for terminal sessions. WITH this, the "
        "service can start work. WITHOUT it the packaged GUI and every read work "
        "in monitoring-only mode, while starting is refused because nothing can claim.",
    )
    p_serve.add_argument(
        "--agent",
        # The same resolved default as `run`, and for the same reason. This
        # one was a second literal, and it had drifted: it carried no
        # `--permission-mode`, so a supervised deployment started agents that
        # were refused every write and reported no changes.
        default=" ".join(default_agent_command()),
        metavar="CMD",
        help="CLI agent to run per item (or $HARNESS_AGENT_COMMAND). "
        "`{prompt_file}` is substituted.",
    )
    p_serve.add_argument(
        "--endpoint",
        default=os.environ.get("HARNESS_ENDPOINT", ""),
        help="model API base URL for the reviewer. Only the reviewer needs a model "
        "in this mode: the CLI agent does the implementing.",
    )
    p_serve.add_argument(
        "--preset",
        default=os.environ.get("HARNESS_ROUTE_PRESET", DEFAULT_PRESET),
        metavar="NAME",
        help="route preset for roles that name none. Same meaning as `run --preset`.",
    )
    p_serve.add_argument(
        "--reviewer",
        default="",
        help="model for the reviewer role. Seeds the stored role map when it has "
        "no reviewer; the stored map wins when it does, because re-routing a role "
        "live is the point of storing it.",
    )
    p_serve.add_argument(
        "--events",
        type=Path,
        default=None,
        metavar="PATH",
        help="where the fleet appends its event stream. Defaults to events.jsonl beside --db.",
    )
    p_serve.add_argument(
        "--hold-webhook",
        default=os.environ.get("HARNESS_HOLD_WEBHOOK", ""),
        metavar="URL",
        help="POST a JSON notice here when an item stops to ask a person something "
        "(or $HARNESS_HOLD_WEBHOOK). One URL is the whole configuration; the receiver "
        "decides what a question means to it. Delivery is best-effort and can never "
        "fail or stall the item.",
    )
    p_serve.add_argument(
        "--no-push", action="store_true", help="commit locally but do not push or open PRs"
    )
    p_serve.add_argument(
        "--poll",
        type=float,
        default=15.0,
        help="seconds a worker waits before asking for work again when the queue is dry",
    )
    p_serve.add_argument(
        "--root-path",
        default=os.environ.get("HARNESS_ROOT_PATH", ""),
        metavar="PREFIX",
        help="prefix this service is reached under when behind a proxy, e.g. "
        "/harness. Without it, browser links and OpenAPI advertise URLs clients "
        "cannot call.",
    )

    args = parser.parse_args(argv)

    # Once, here, before anything can want to log. Without this every
    # `log.info`/`log.warning` in the package went to a root logger with no
    # handler and was discarded -- including the lines that exist to explain
    # a lost claim, a drained project or a discarded result.
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    if args.command == "plan":
        return _plan(args)

    if args.command == "run":
        return _run(args)

    if args.command == "init":
        return _init(args)

    if args.command == "doctor":
        return _doctor(args)

    if args.command == "guard":
        return _guard(args)

    if args.command == "graph":
        return _graph(args)

    if args.command == "adopt":
        return _adopt(args)

    if args.command == "survey":
        return _survey(args)

    store = EventStore(args.db)

    if args.command == "ingest":
        sources = resolve_sources(args)
        if not sources:
            print("nothing to ingest: pass --events PATH and/or --adapter", file=sys.stderr)
            return 1
        if args.watch:
            import time

            while True:
                print(ingest(store, sources), flush=True)
                time.sleep(args.watch)
        print(ingest(store, sources))
        return 0

    token = os.environ.get("HARNESS_TOKEN")
    if not token:
        # Generating one beats defaulting to no auth: the service is
        # reachable over the network and must never come up open.
        token = secrets.token_urlsafe(24)
        print(f"HARNESS_TOKEN not set; generated one for this run:\n  {token}", file=sys.stderr)

    # Before anything is served. Readiness probes routes with it, so a preset
    # this build cannot resolve should be a refusal at startup rather than a
    # probe quietly asking the wrong URL.
    if _resolved_preset(args.preset) is None:
        return 2

    import uvicorn

    from .api import create_api
    from .audit import open_audit_store
    from .holds import webhook_hook
    from .maintenance import DEFAULT_RETENTION_DAYS, MaintenanceLoop
    from .work import WorkQueue

    # A separate file, deliberately. History must not share a fate with the
    # queue: the queue is migrated in place and is a reasonable thing to
    # delete and rebuild from the plan, and anything in that file goes with it.
    #
    # Defaults to sitting beside the queue so a single-file deployment still
    # works; point HARNESS_AUDIT_DB at a different volume to make the
    # separation physical as well as logical.
    audit_path = (
        args.audit_db
        or os.environ.get("HARNESS_AUDIT_DB")
        or (str(Path(args.db).with_name("audit.sqlite")))
    )
    audit = open_audit_store(
        audit_path,
        required=os.environ.get("HARNESS_AUDIT_REQUIRED", "").lower() in {"1", "true", "yes"},
        adopt_from=args.db,
    )
    if audit.degraded:
        print(
            f"WARNING: audit store at {audit_path} is DEGRADED; history is NOT "
            "being recorded. The harness will keep working.",
            file=sys.stderr,
        )
    else:
        print(f"audit: {audit_path} ({audit.count()} events)")

    # Started here rather than left to cron: retention that depends on an
    # external scheduler silently stops when nobody installs it, and the
    # symptom is a database that grows for months before anyone notices.
    # Configured before the fleet exists, because the operator's hook is the
    # one consumer that does not depend on this deployment being supervised.
    # `_fleet_for_serve` adds the event stream to it when there is one.
    queue_for_serve = WorkQueue(args.db, on_hold=webhook_hook(args.hold_webhook))
    if args.hold_webhook:
        print(f"holds: notices POSTed to {args.hold_webhook}")
    maintenance = MaintenanceLoop(
        audit,
        retention_days=int(os.environ.get("HARNESS_AUDIT_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)),
        # Reconciliation needs the queue: a pull request is only attributable
        # to an item because the queue recorded its URL.
        queue=queue_for_serve,
    )
    maintenance.start()

    fleet, reviewer_client, host, executor_roles = _fleet_for_serve(
        args, queue_for_serve, audit=audit
    )
    if fleet is None:
        print(
            "monitoring only: no --session-host, so no worker pool is attached and "
            "starting a project will be refused.",
            file=sys.stderr,
        )

    try:
        uvicorn.run(
            create_api(
                store,
                queue=queue_for_serve,
                token=token,
                root_path=args.root_path,
                audit=audit,
                fleet=fleet,
                model_client=reviewer_client,
                # Readiness probes it with a read. Passing the client rather
                # than the URL keeps the token out of the API layer.
                session_host=host,
                executor_roles=executor_roles,
                # The same default the workers route with, so a readiness
                # probe asks the URL the work will actually use.
                default_preset=args.preset,
            ),
            host=args.host,
            port=args.port,
            log_level="info",
        )
    finally:
        if fleet is not None:
            # Drain rather than kill. An agent stopped mid-item loses the
            # context that makes its work resumable, so in-flight work is
            # joined and only new claims stop.
            fleet.stop_all(reason="the harness process is stopping")
    return 0


def _fleet_for_serve(
    args: argparse.Namespace, queue: Any, *, audit: Any | None = None
) -> tuple[Any | None, Any | None, Any | None, Any | None]:
    """The supervised half of `serve`: a fleet the API's start action can use.

    Returns all-None for a monitoring-only deployment. That mode is
    supported on purpose — a dashboard over someone else's harness should not
    need a session host, a model key or a checkout — and the API already
    refuses to start a project when nothing can claim.

    **Nothing is started here.** Building the fleet creates no workers; only
    the API's start action does, and only after preflight passes.
    """
    if not args.session_host:
        return (None, None, None, None)

    import json as _json

    from .api import ROLE_MAP_KEY
    from .events import KINDS, MODEL_CALL, Event
    from .fleet import Fleet
    from .github import GitHub
    from .holds import fanout
    from .model_client import Chain, ModelClient, chains_from_map, effective_routes
    from .runtime import ExecutorRoles, session_executor_factory
    from .session_executor import AgentSpec
    from .session_host import HttpSessionHost

    api_key = os.environ.get("HARNESS_API_KEY", "")
    host_token = os.environ.get("AIDEVENV_TOKEN", "") or api_key

    # Seed the reviewer route from the flags when the stored map has none.
    # The stored map wins where it has an opinion: re-routing a role live
    # through PUT /api/roles is the reason it exists.
    stored = queue.get_setting(ROLE_MAP_KEY) or {}
    if args.reviewer and args.endpoint and "reviewer" not in stored:
        stored = {
            **stored,
            "reviewer": {
                "model": args.reviewer,
                "endpoint": args.endpoint,
                # The classifier only; the wire shape is `--preset`. See `run`.
                "provider": "claw-bay",
            },
        }
        queue.set_setting(ROLE_MAP_KEY, stored)

    preset = _resolved_preset(getattr(args, "preset", "") or DEFAULT_PRESET)
    if preset is None:
        # Refused here rather than on the first review, which would be after an
        # item had been claimed, implemented and checked.
        raise SystemExit(2)
    _report_routes(dict(stored), preset)

    def live_routes() -> dict[str, Chain]:
        return chains_from_map(
            queue.get_setting(ROLE_MAP_KEY) or {}, api_key=api_key, default_preset=preset.name
        )

    def routes_for(project_id: str) -> dict[str, Chain]:
        """One project's effective map, read live on every call.

        The project row is read here rather than closed over so that a role
        override written through the API reaches a worker that is already
        running — the same reason the global map is read per call.
        """
        project = queue.get_project(project_id)
        return effective_routes(
            live_routes(),
            chains_from_map(
                getattr(project, "roles", None) or {},
                api_key=api_key,
                default_preset=preset.name,
            ),
        )

    routes = live_routes()
    if "reviewer" not in routes:
        # Not fatal, and not silent: preflight blocks the start with exactly
        # this reason, so the fleet may as well exist and say why now.
        print(
            "warning: no reviewer is routed globally. Preflight will refuse to start "
            "any project that does not override one — set a global reviewer with "
            "--reviewer/--endpoint or PUT /api/roles.",
            file=sys.stderr,
        )
    events_path = args.events or Path(args.db).with_name("events.jsonl")
    events_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(event: dict[str, Any]) -> None:
        try:
            with events_path.open("a") as handle:
                handle.write(_json.dumps(event) + "\n")
        except OSError:
            # A broken convenience stream must not prevent the durable audit
            # sink below from recording the event, or stop the work itself.
            log.warning("events: could not append %s", events_path, exc_info=True)
        if audit is None:
            return
        # The JSONL stream remains tail-able, but the audit database is the
        # durable system of record used by every projection. Keep the sink
        # translation here, at the deployment boundary, so the core remains
        # generic about producers and their payloads.
        kind = event.get("kind")
        if kind not in KINDS:
            kind = MODEL_CALL
        known = {
            "ts",
            "kind",
            "source",
            "worker",
            "role",
            "model",
            "endpoint",
            "outcome",
            "error_class",
            "latency_s",
        }
        data = {k: v for k, v in event.items() if k not in known}
        try:
            audit.append(
                [
                    Event(
                        ts=float(event.get("ts", time.time())),
                        kind=kind,
                        source="serve",
                        worker=event.get("worker"),
                        role=event.get("role"),
                        model=event.get("model"),
                        endpoint=event.get("endpoint"),
                        outcome=event.get("outcome"),
                        error_class=event.get("error_class"),
                        latency_s=event.get("latency_s"),
                        data=data,
                    )
                ]
            )
        except Exception:  # telemetry is never load-bearing
            log.warning("audit: could not append live event", exc_info=True)

    # The question goes into the same stream as the work it stopped, next to
    # whatever the operator already configured (#188). Composed rather than
    # replaced, so `--hold-webhook` keeps working in a supervised deployment.
    queue.holds.on_hold = fanout(emit, queue.holds.on_hold)

    reviewer_client = ModelClient(
        roles=routes,
        transport=_http_transport(api_key),
        on_event=emit,
        routes_provider=live_routes,
    )

    host = HttpSessionHost(args.session_host, token=host_token)
    agent = AgentSpec(command=tuple(shlex.split(args.agent)))
    factory = session_executor_factory(
        queue,
        host=host,
        agent=agent,
        reviewer=reviewer_client,
        routes_for=routes_for,
        github_for=GitHub,
        ui_base_url=args.session_host,
        on_event=emit,
        push=not args.no_push,
    )
    print(f"fleet: `{args.agent}` as sessions on {args.session_host}")
    print(f"events: {events_path}")
    # The fleet emits into the same stream as the executors: a worker that
    # dies is recorded next to the work it was doing, not in a separate log.
    return (
        Fleet(queue, factory, poll_seconds=args.poll, on_event=emit),
        reviewer_client,
        host,
        # What this deployment will actually call, so the API can stop
        # advertising the two roles the agent process does instead.
        ExecutorRoles.for_session(agent),
    )


if __name__ == "__main__":
    sys.exit(main())
