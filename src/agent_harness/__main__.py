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
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from .ingest import ingest
from .sources import Source, harness_source
from .store import EventStore


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
    unresolved = plan.unresolved_dependencies()
    if unresolved:
        # Not fatal: a dependency on work tracked elsewhere is legitimate.
        # Silence is not, because a typo would block an item forever.
        print("warning: dependencies naming unknown items:", file=sys.stderr)
        for item_id, missing in sorted(unresolved.items()):
            print(f"  {item_id} -> {', '.join(missing)}", file=sys.stderr)

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


def _http_transport(api_key: str) -> Any:
    """A real transport, built only when `run` needs one.

    Imported lazily and constructed here rather than in model_client,
    because the client's whole design is that it does not own the HTTP —
    a caller with its own client should be able to keep it.
    """
    import httpx

    from .model_client import Response

    client = httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0))

    def transport(route: Any, messages: Any, options: Any) -> Any:
        payload = {"model": route.model, "messages": list(messages)}
        payload.update({k: v for k, v in options.items() if k != "role"})
        response = client.post(
            f"{route.endpoint.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {route.api_key or api_key}",
                "content-type": "application/json",
            },
            json=payload,
        )
        return Response(response.status_code, dict(response.headers), response.text)

    return transport


def _run(args: argparse.Namespace) -> int:
    """Execute work items. This is the command that spends money and writes
    code, so it says exactly what it will do before doing any of it."""
    import json as _json
    import shlex

    from . import providers
    from .executor import Checks, Executor
    from .github import GitHub
    from .model_client import ModelClient, Route
    from .work import WorkQueue, WorkRecord

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

    def emit(event: dict[str, Any]) -> None:
        with events_path.open("a") as handle:
            handle.write(_json.dumps(event) + "\n")

    client = ModelClient(
        roles={
            name: Route(model, args.endpoint, providers.CLAW_BAY, api_key=api_key)
            for name, model in roles.items()
        },
        transport=_http_transport(api_key),
        on_event=emit,
    )

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
            github=GitHub(args.repo),
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
            github=GitHub(args.repo),
            base_branch=args.base,
            on_event=emit,
            push=not args.no_push,
        )
    outcomes = executor.run(limit=args.limit)
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

    p_run = sub.add_parser("run", help="execute claimed work items")
    p_run.add_argument(
        "--repo",
        required=True,
        metavar="OWNER/NAME",
        help="GitHub repo for issues and pull requests",
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
    p_run.add_argument("--base", default="main", help="branch to base work on")
    p_run.add_argument(
        "--no-push", action="store_true", help="commit locally but do not push or open PRs"
    )
    p_run.add_argument(
        "--endpoint",
        default=os.environ.get("HARNESS_ENDPOINT", ""),
        help="model API base url (or $HARNESS_ENDPOINT)",
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
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8099)
    p_serve.add_argument(
        "--root-path",
        default=os.environ.get("HARNESS_ROOT_PATH", ""),
        metavar="PREFIX",
        help="prefix this service is reached under when behind a proxy, e.g. "
        "/api/harness. Without it, Swagger UI tells clients to call URLs that 404.",
    )

    args = parser.parse_args(argv)

    if args.command == "plan":
        return _plan(args)

    if args.command == "run":
        return _run(args)

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

    import uvicorn

    from .api import create_api
    from .work import WorkQueue

    uvicorn.run(
        create_api(store, queue=WorkQueue(args.db), token=token, root_path=args.root_path),
        host=args.host,
        port=args.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
