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

import shutil
import sqlite3
import tempfile
from pathlib import Path

from src.common.decimals import connect
from src.m0_data.schema.apply import apply_migrations
from src.m1_ledger.db import connect_ledger


def _main_path(conn: sqlite3.Connection) -> str:
    """The file a connection is open on, from the connection itself.

    Taken from the connection rather than from the caller, so a test never
    restates the filename its own fixture chose — the first two tests to need
    this hard-coded `tmp_path / "w.db"`, which the fixture three lines above
    them owns.
    """
    return next(
        str(row[2]) for row in conn.execute("PRAGMA database_list") if row[1] == "main"
    )


def reopen(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Close `conn` and return a NEW connection to the same Zone A file."""
    path = _main_path(conn)
    conn.close()
    return connect(path)


def reopen_ledger(conn: sqlite3.Connection, key: str) -> sqlite3.Connection:
    """`reopen` for Zone B, which is SQLCipher and needs its key again.

    A separate function rather than a flag: `connect` and `connect_ledger` are
    different drivers with different adapter registries (see
    `src/common/decimals.py`), and a test that reopened Zone B through the
    wrong one would read every money column back as `str`.
    """
    path = _main_path(conn)
    conn.close()
    return connect_ledger(path, key=key)


#: A migrated, empty Zone A warehouse, built once per process. `migrated()`
#: copies it; nothing writes to it.
_TEMPLATE: Path | None = None


def migrated(path: Path | str) -> str:
    """An empty migrated warehouse at `path`, by COPY rather than by migration.

    `apply_migrations` ran 228 times across `tests/unit` for 94s of a 261s run
    — 36% of the suite spent re-applying the same 13 files. The result is a
    244 KiB file that is identical every time, so it is built once and copied:
    1.0ms against 209.5ms, measured.

    Not used by `test_m0_migrations.py`, which is about migrations and has to
    run them, nor by the two idempotency assertions in `test_m0_amfi.py` that
    read `apply_migrations`' return value.
    """
    global _TEMPLATE
    if _TEMPLATE is None or not _TEMPLATE.exists():
        template = Path(tempfile.mkdtemp(prefix="mf-schema-")) / "migrated.db"
        apply_migrations(str(template))
        _TEMPLATE = template
    shutil.copy(_TEMPLATE, path)
    return str(path)
