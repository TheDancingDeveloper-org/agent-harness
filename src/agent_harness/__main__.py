"""CLI.

    agent-harness plan   PLAN.md --repo owner/name   # plan -> GitHub backlog
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

    p_serve = sub.add_parser("serve", help="run the dashboard")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8099)
    p_serve.add_argument(
        "--baseline",
        metavar="TOTAL:DAYS:LABEL",
        help="a prior measurement to compare against, e.g. 27662:8:'pre-classification'. "
        "Treated as an unclassified TOTAL unless --baseline-classified is passed, "
        "so the panel will not imply a per-class delta that does not exist.",
    )
    p_serve.add_argument(
        "--baseline-classified",
        action="store_true",
        help="the baseline has a per-class breakdown of its own",
    )

    args = parser.parse_args(argv)

    if args.command == "plan":
        return _plan(args)

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

    from .app import Baseline, create_app

    baseline = None
    if args.baseline:
        parts = args.baseline.split(":")
        if len(parts) < 2:
            raise SystemExit("--baseline must be TOTAL:DAYS[:LABEL]")
        baseline = Baseline(
            total=int(parts[0]),
            days=float(parts[1]),
            window=parts[2] if len(parts) > 2 else "prior measurement",
            classified=args.baseline_classified,
        )

    uvicorn.run(
        create_app(store, token=token, baseline=baseline),
        host=args.host,
        port=args.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
