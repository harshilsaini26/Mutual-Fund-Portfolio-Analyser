"""Run the look-through API. MODULE_6.md §15.3.

    python -m jobs.serve                 # opens the browser at the portal
    python -m jobs.serve --no-browser

Prompts for the Zone B ledger key, opens both databases, and serves on
**127.0.0.1 only**. The key is prompted rather than read from an environment
variable on purpose: an env var lands in shell history, process listings and
crash dumps, and this is the key to the one file in the project that is
encrypted because it holds the user's actual money.

**No ledger yet is not an error.** Fund pages, search and every market view need
only the warehouse, so a first-time user with no CAS imported gets them on an
empty in-memory ledger that holds no real data (DECISIONS V1-72). The portfolio
views say what is missing, as they always have.

The API has no authentication (§15.3) and that is safe only while it is bound to
loopback. `PLAN.md` §6.5 puts remote access behind Tailscale rather than behind a
wider bind; changing that is a decision needing its own review.
"""

from __future__ import annotations

import argparse
import getpass
import sqlite3
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path

import uvicorn
from src.common.decimals import connect
from src.m0_data.config import warehouse_path
from src.m0_data.schema.apply import apply_migrations
from src.m1_ledger.db import apply_ledger_schema, connect_ledger, ledger_path
from src.m6_views.api.app import BIND_HOST, BIND_PORT, create_app
from src.m6_views.registry import seed_view_definitions


def open_ledger(
    db: Path, ask_key: Callable[[], str] = lambda: getpass.getpass("Zone B ledger key: ")
) -> tuple[sqlite3.Connection, bool]:
    """The ledger to serve, and whether it holds a portfolio.

    With no ledger file, an empty in-memory one: nothing to decrypt, nothing
    written, and no key asked for. `allow_unencrypted` is the explicit, visible
    opt-out `connect_ledger` requires, and it is safe here because the database
    exists only in memory and never holds real data.
    """
    if not db.exists():
        ledger = connect_ledger(
            ":memory:", allow_unencrypted=True, check_same_thread=False
        )
        apply_ledger_schema(ledger)
        return ledger, False
    ledger = connect_ledger(
        str(db),
        key=ask_key(),
        # The event loop serves requests from a thread that is not this one.
        # `app.DB_LOCK` serialises every use; see that module's docstring.
        check_same_thread=False,
    )
    apply_ledger_schema(ledger)
    return ledger, True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=BIND_PORT)
    parser.add_argument("--user", default="USER-01")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the portal in the browser")
    args = parser.parse_args()

    # Every other job in `jobs/` does this first, and this one needs it more
    # than most: `view_definition` arrives in migration 006, and seeding the
    # catalogue into a warehouse that predates it fails with "no such table" —
    # which is what happened the first time this was run against the real one.
    apply_migrations(str(warehouse_path()))
    warehouse = connect(str(warehouse_path()), check_same_thread=False)
    ledger, has_portfolio = open_ledger(ledger_path())

    # §4.1: the catalogue is seeded from code on every start, so a view added or
    # removed in `VIEW_DEFS` is reflected without a migration. The registry
    # consistency check already ran at import of `builders`.
    seeded = seed_view_definitions(warehouse)

    url = f"http://{BIND_HOST}:{args.port}/"
    print(f"\n{seeded} views registered")
    print(f"  {url}")
    if not has_portfolio:
        print(
            "  No portfolio yet: search any fund by name. To add yours:\n"
            f"    python -m jobs.import_cas --file statement.pdf --user {args.user}"
        )
    print()
    if not args.no_browser:
        # After a moment, so the server is listening when the page asks.
        threading.Timer(1.5, webbrowser.open, [url]).start()
    uvicorn.run(create_app(ledger, warehouse), host=BIND_HOST, port=args.port)


if __name__ == "__main__":
    main()
