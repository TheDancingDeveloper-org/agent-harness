"""CLI.

    agent-harness plan   PLAN.md --repo owner/name   # plan -> GitHub backlog
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
import sys
import time
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
    import shlex

    from .executor import Checks, Executor
    from .github import GitHub
    from .model_client import Chain, ModelClient, chains_from_map
    from .work import RUNNING, WorkQueue, WorkRecord

    # With a session host the CLI agent does the implementing, so only the
    # reviewer needs a model. Demanding three would be asking for two that are
    # never called.
    session_mode = bool(args.session_host)
    roles = {"planner": args.planner, "implementer": args.implementer, "reviewer": args.reviewer}
    if session_mode:
        roles = {"reviewer": args.reviewer}
    missing = [name for name, model in roles.items() if not model]
    if missing:
        print(f"no model configured for: {', '.join(missing)}", file=sys.stderr)
        print(
            "Every role needs one, and the reviewer should be a DIFFERENT vendor "
            "from the implementer — otherwise some share of reviews is a model "
            "grading its own work.",
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
    if not api_key and not args.dry_run:
        print("HARNESS_API_KEY is not set", file=sys.stderr)
        return 2

    queue = WorkQueue(args.db)
    if args.plan:
        from .plan import parse_plan_file

        plan = parse_plan_file(args.plan)
        added = queue.add(
            [
                WorkRecord(item_id=i.id, title=i.title, brief=i.brief(), depends_on=i.depends_on)
                for i in plan.deduplicated()
                if not i.done
            ]
        )
        print(f"loaded {added} new items from {args.plan}")

    checks = Checks(commands=[shlex.split(c) for c in args.check])
    counts = queue.counts()
    print(f"queue: {counts or 'empty'}")
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

    def emit(event: dict[str, Any]) -> None:
        with events_path.open("a") as handle:
            handle.write(_json.dumps(event) + "\n")
        # Also say it out loud. Every stage transition used to go only to the
        # JSONL file, so a run printed a header and then nothing at all --
        # and a legitimate 210-second backoff after a gateway error looked
        # exactly like a wedged process to the person watching.
        log.info("%s", _describe(event))

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
    }
    stored_map = queue.get_setting(ROLE_MAP_KEY) or {}
    # MERGED, per role, not chosen wholesale. A stored route wins for the role
    # it names, because re-routing a role live is the point of storing it --
    # but a map holding only `reviewer` used to suppress the planner and
    # implementer the operator had just typed, and the run then failed its
    # first item with `no route for role 'planner'` after claiming it.
    merged = {**from_cli, **stored_map}
    if merged != stored_map:
        queue.set_setting(ROLE_MAP_KEY, merged)
    overridden = sorted(
        name
        for name in from_cli
        if name in stored_map and stored_map[name].get("model") != from_cli[name]["model"]
    )
    if overridden:
        print(
            "note: a stored role map is in force and overrides the command line "
            f"for: {', '.join(overridden)}. "
            "PUT /api/roles to change it, or delete the database to reseed."
        )
    filled = sorted(set(from_cli) - set(stored_map))
    if stored_map and filled:
        print(f"note: the stored role map had no route for {', '.join(filled)}; used the flags.")

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

    client = ModelClient(
        roles=live_routes(),
        transport=_http_transport(api_key),
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
            reviewer=client,
            github=GitHub(args.repo) if args.repo else None,
            base_branch=args.base,
            ui_base_url=args.session_host,
            on_event=emit,
            push=not args.no_push,
        )
    else:
        executor = Executor(
            queue,
            client,
            args.work,
            checks=checks,
            github=GitHub(args.repo) if args.repo else None,
            base_branch=args.base,
            on_event=emit,
            push=not args.no_push,
            artifacts=artifacts,
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
    for outcome in outcomes:
        mark = "ok " if outcome.ok else "FAIL"
        detail = outcome.pr_url or outcome.reason[:100]
        print(f"  {mark} {outcome.item_id}: {' -> '.join(outcome.stages)}  {detail}")
    failed = [o for o in outcomes if not o.ok]
    print(f"{len(outcomes) - len(failed)}/{len(outcomes)} items completed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
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
    p_plan.add_argument("--repo", required=True, metavar="OWNER/NAME")
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
        "--agent",
        default="claude -p {prompt_file}",
        metavar="CMD",
        help="CLI agent to run per item. `{prompt_file}` is substituted.",
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
    p_run.add_argument("--planner", default="", help="model for the planner role")
    p_run.add_argument("--implementer", default="", help="model for the implementer role")
    p_run.add_argument(
        "--reviewer",
        default="",
        help="model for the reviewer role — use a DIFFERENT vendor "
        "from the implementer, so a model does not grade its own work",
    )
    p_run.add_argument(
        "--dry-run", action="store_true", help="show what would run, call nothing, change nothing"
    )

    p_serve = sub.add_parser(
        "serve", help="serve the JSON API (headless — the GUI is the session host's)"
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
        help="base URL of a session host. WITH this, the API can start work: a "
        "worker pool is attached and each agent runs as a terminal session you can "
        "attach to. WITHOUT it the service is monitoring-only — every read works "
        "and starting a project is refused, because starting would mark it running "
        "with nothing able to claim.",
    )
    p_serve.add_argument(
        "--agent",
        default="claude -p {prompt_file}",
        metavar="CMD",
        help="CLI agent to run per item. `{prompt_file}` is substituted.",
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
        "/api/harness. Without it, Swagger UI tells clients to call URLs that 404.",
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

    if args.command == "graph":
        return _graph(args)

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
    queue_for_serve = WorkQueue(args.db)
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
    import shlex

    from .api import ROLE_MAP_KEY
    from .events import KINDS, MODEL_CALL, Event
    from .fleet import Fleet
    from .github import GitHub
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
