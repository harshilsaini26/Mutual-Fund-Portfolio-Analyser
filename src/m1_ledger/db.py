"""Zone B connections and schema. MODULE_1.md §4, `PLAN.md` §6.3.

Zone B holds transactions, units, folios and a PAN. §6.3 puts it under the
strictest handling in the project: encrypted at rest, and it *"never leaves the
device unencrypted."*

**Encryption is on.** `sqlcipher3` is installed (SQLCipher 4.12.0), and
`PRAGMA key` is applied before any other statement on the connection — which is
what SQLCipher requires, since the first read has to decrypt page 1. Verified:
the file does not begin with `SQLite format 3`, the stdlib `sqlite3` cannot open
it, and a wrong key fails the page HMAC rather than returning garbage.

This is the one runtime dependency the project has taken. V0-19 avoided a native
dependency for **Zone A**, which holds public market data and can be rebuilt from
the archive at any time. Zone B is the opposite: a PAN, folio numbers and a
postal address, which `PLAN.md` §6.3 says *"never leaves the device
unencrypted."* The cost is worth paying exactly here and nowhere else.

**An unencrypted Zone B database is refused, not silently opened** — no driver,
or no key, and `connect_ledger` raises. A fallback that quietly produced a
plaintext file holding a PAN is the class of failure this project keeps finding:
the write succeeds and the result is quietly wrong. `allow_unencrypted=True`
remains for tests and for a ledger holding nothing real, and it says so at every
call site; it is no longer on any command line.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from src.common.decimals import register_decimal_sqlite
from src.m0_data.schema.apply import MigrationError, unsafe_decimal_columns

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations" / "zone_b"


def ledger_path() -> Path:
    """Zone B on disk. §13.2, and gitignored in full along with the rest of /data.

    Lives here rather than in `m0_data.config` beside `warehouse_path`: M0 has
    no business knowing where the personal ledger is, and the dependency only
    runs the other way. `data_root()` is shared infrastructure, so that much is
    borrowed.
    """
    import os

    from src.m0_data.config import data_root

    env = os.environ.get("MF_LEDGER")
    return Path(env) if env else data_root() / "ledger" / "personal.db"


class EncryptionUnavailable(RuntimeError):
    """No SQLCipher driver, and the caller did not accept a plaintext database."""


def sqlcipher_module() -> Any | None:
    """The first importable SQLCipher binding, or None.

    Checked at call time rather than import time so that installing the driver
    takes effect without touching this file.
    """
    for name in ("sqlcipher3", "pysqlcipher3.dbapi2", "sqlcipher3.dbapi2"):
        try:
            module = __import__(name, fromlist=["connect"])
        except ImportError:
            continue
        if hasattr(module, "connect"):
            return module
    return None


def connect_ledger(
    path: str,
    *,
    key: str | None = None,
    allow_unencrypted: bool = False,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open Zone B with Decimal handling and, where possible, encryption.

    `detect_types=PARSE_DECLTYPES` is what makes the `DECIMAL_TEXT` converter
    fire; without it every money column reads back as `str` and the first
    arithmetic on one introduces a float.

    Raises `EncryptionUnavailable` rather than degrading. The two ways to open
    a database are therefore "encrypted" and "explicitly, visibly not" — there
    is no third that happens by accident.

    `check_same_thread=False` is an explicit opt-out with one caller: M6's HTTP
    API, which serves requests on an event loop running in a thread that did not
    open this connection. It serialises every use behind a lock. Nothing else
    should pass it.
    """
    register_decimal_sqlite()
    driver = sqlcipher_module()
    if driver is not None:
        # Per-module registries: registering on stdlib `sqlite3` does nothing
        # for a `sqlcipher3` connection. See `register_decimal_sqlite`.
        register_decimal_sqlite(driver)

    if driver is not None and key:
        conn: sqlite3.Connection = driver.connect(
            path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=check_same_thread,
        )
        # Must precede every other statement on the connection, including the
        # schema read SQLCipher itself performs to validate the key.
        #
        # PRAGMA takes no bound parameters, so the key is interpolated — and a
        # key containing an apostrophe would otherwise end the string literal
        # and change the statement. Doubling is SQL's own escape for a quote
        # inside a quoted literal.
        conn.execute("PRAGMA key = '{}'".format(key.replace("'", "''")))
        # Forces page 1 to be decrypted, so a wrong key fails HERE rather than
        # at some later query that looks like a schema problem.
        conn.execute("SELECT count(*) FROM sqlite_master")
        return conn

    if not allow_unencrypted:
        reason = (
            "no SQLCipher driver is installed"
            if driver is None
            else "no key was supplied"
        )
        raise EncryptionUnavailable(
            f"refusing to open Zone B at {path!r} unencrypted: {reason}. "
            f"PLAN.md §6.3 requires encryption at rest. Pass "
            f"allow_unencrypted=True only for a database holding no real data."
        )

    return sqlite3.connect(
        path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=check_same_thread,
    )


def apply_ledger_schema(
    conn: sqlite3.Connection, *, force: bool = False
) -> list[str]:
    """Run the Zone B migrations that have not run yet. Returns names applied.

    Deliberately not `m0_data.schema.apply.apply_migrations`: that opens its own
    Zone A connection, and Zone B's has to come from `connect_ledger` so the key
    is applied first. The part worth reusing is the decimal-safety check, and
    that takes a connection.

    `force` re-runs every migration regardless of what `schema_migration` says.
    That exists for one specific reason: `persist.drop_derived` DROPs the
    derived tables to prove they are reconstructible (invariant 5), and the
    migration ledger still records the migration that created them — so a
    plain re-apply would skip the file and leave the tables missing. Every
    statement in the Zone B DDL is `CREATE ... IF NOT EXISTS`, so re-running is
    a no-op for whatever survived. It never drops or rewrites anything, and it
    is not a way to edit an applied migration: forward-only still holds.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migration ("
        "  name TEXT PRIMARY KEY,"
        "  applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    done = {r[0] for r in conn.execute("SELECT name FROM schema_migration")}
    applied: list[str] = []
    for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
        if path.name in done and not force:
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT OR IGNORE INTO schema_migration (name) VALUES (?)", (path.name,)
        )
        applied.append(path.name)
    conn.commit()

    bad = unsafe_decimal_columns(conn)
    if bad:
        listed = ", ".join(f"{t}.{c} declared {d}" for t, c, d in bad)
        raise MigrationError(f"unsafe decimal columns in Zone B: {listed}")
    return applied
