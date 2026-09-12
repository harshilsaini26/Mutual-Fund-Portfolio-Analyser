"""Run the look-through API. MODULE_6.md §15.3.

    python -m jobs.serve

Prompts for the Zone B ledger key, opens both databases, and serves on
**127.0.0.1 only**. The key is prompted rather than read from an environment
variable on purpose: an env var lands in shell history, process listings and
crash dumps, and this is the key to the one file in the project that is
encrypted because it holds the user's actual money.

The API has no authentication (§15.3) and that is safe only while it is bound to
loopback. `PLAN.md` §6.5 puts remote access behind Tailscale rather than behind a
wider bind; changing that is a decision needing its own review.
"""

from __future__ import annotations

import argparse
import getpass
import sys

import uvicorn
from src.common.decimals import connect
from src.m0_data.config import warehouse_path
from src.m0_data.schema.apply import apply_migrations
from src.m1_ledger.db import apply_ledger_schema, connect_ledger, ledger_path
from src.m6_views.api.app import BIND_HOST, BIND_PORT, create_app
from src.m6_views.registry import seed_view_definitions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=BIND_PORT)
    parser.add_argument("--user", default="USER-01")
    args = parser.parse_args()

    db = ledger_path()
    if not db.exists():
        sys.exit(
            f"no ledger at {db}. Import a CAS first:\n"
            f"    python -m jobs.import_cas --file statement.pdf "
            f"--user {args.user}"
        )

    # Every other job in `jobs/` does this first, and this one needs it more
    # than most: `view_definition` arrives in migration 006, and seeding the
    # catalogue into a warehouse that predates it fails with "no such table" —
    # which is what happened the first time this was run against the real one.
    apply_migrations(str(warehouse_path()))
    warehouse = connect(str(warehouse_path()), check_same_thread=False)
    ledger = connect_ledger(
        str(db),
        key=getpass.getpass("Zone B ledger key: "),
        # The event loop serves requests from a thread that is not this one.
        # `app.DB_LOCK` serialises every use; see that module's docstring.
        check_same_thread=False,
    )
    apply_ledger_schema(ledger)

    # §4.1: the catalogue is seeded from code on every start, so a view added or
    # removed in `VIEW_DEFS` is reflected without a migration. The registry
    # consistency check already ran at import of `builders`.
    seeded = seed_view_definitions(warehouse)

    print(f"\n{seeded} views registered")
    print(f"  http://{BIND_HOST}:{args.port}/api/views")
    print(f"  http://{BIND_HOST}:{args.port}/api/docs\n")
    uvicorn.run(
        create_app(ledger, warehouse), host=BIND_HOST, port=args.port
    )


if __name__ == "__main__":
    main()
