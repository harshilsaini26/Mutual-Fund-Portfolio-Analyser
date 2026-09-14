"""Shared test helpers. DECISIONS V1-58.

The suite had no `conftest.py` at all, and every sqlite fixture was redefined
per file — which is how it came to share one blind spot: **a test that writes
through a helper and reads back on the SAME connection is testing the
transaction, not the database.** Uncommitted rows are visible to the connection
that wrote them, so such a test passes whether or not anything was committed.

Measured when this was written: deleting the `conn.commit()` from any of twelve
functions in `src/` left `tests/unit` fully green. The only two the suite could
hold were in `src/m0_data/schema/apply.py`, and the only thing that made them
different was that `test_m0_migrations.py` closes and reopens.

So `reopen` lives here rather than being hand-rolled per file, and a write-path
test that asserts through it cannot pass on an uncommitted write.
"""

from __future__ import annotations

import sqlite3

from src.common.decimals import connect


def reopen(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Close `conn` and return a NEW connection to the same file.

    Takes the path from the connection rather than from the caller, so a test
    never restates the filename its own fixture chose — two of the first tests
    to need this hard-coded `tmp_path / "w.db"`, which is knowledge owned by
    the fixture three lines above them.
    """
    path = next(
        str(row[2]) for row in conn.execute("PRAGMA database_list") if row[1] == "main"
    )
    conn.close()
    return connect(path)
