"""CLI: ingest logs, then serve the dashboard.

    agent-harness ingest --logs ~/.oxidex/logs --db harness.sqlite
    agent-harness serve  --db harness.sqlite --port 8099

Ingest is safe to re-run and safe to run on a timer: it is idempotent, so a
cron loop and a one-off backfill produce the same store.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

from .ingest import ingest
from .store import EventStore

DEFAULT_LOGS = Path(os.environ.get("OXIDEX_HOME", Path.home() / ".oxidex")) / "logs"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-harness", description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get("HARNESS_DB", "harness.sqlite"),
        help="SQLite path (default: $HARNESS_DB or ./harness.sqlite)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="read harness logs into the store")
    p_ingest.add_argument("--logs", type=Path, default=DEFAULT_LOGS)
    p_ingest.add_argument(
        "--watch", type=float, metavar="SECONDS", help="re-ingest every SECONDS instead of exiting"
    )

    p_serve = sub.add_parser("serve", help="run the dashboard")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8099)

    args = parser.parse_args(argv)
    store = EventStore(args.db)

    if args.command == "ingest":
        if not args.logs.exists():
            print(f"{args.logs} does not exist — nothing to ingest.", file=sys.stderr)
            print(
                "On a host that is not the fleet host this is expected, not an error.",
                file=sys.stderr,
            )
            return 1
        if args.watch:
            import time

            while True:
                print(ingest(store, args.logs), flush=True)
                time.sleep(args.watch)
        print(ingest(store, args.logs))
        return 0

    token = os.environ.get("HARNESS_TOKEN")
    if not token:
        # Generating one beats defaulting to no auth: the service is
        # reachable over the network and must never come up open.
        token = secrets.token_urlsafe(24)
        print(f"HARNESS_TOKEN not set; generated one for this run:\n  {token}", file=sys.stderr)

    import uvicorn

    from .app import create_app

    uvicorn.run(create_app(store, token=token), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
