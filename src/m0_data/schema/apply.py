"""Apply the Zone A migrations. `PLAN.md` §8.1: numbered, forward-only.

Zone A is SQLite rather than the DuckDB of `MODULE_0.md` §4.1 — DECISIONS V0-19.
The consequence that matters is SZ-13's: SQLite gives a `DECIMAL` column NUMERIC
affinity and silently rewrites a decimal string as a REAL, so every money, NAV
and weight column is declared `DECIMAL_TEXT` instead. `applied_schema_is_safe()`
below asserts that rather than trusting it, because the failure is invisible —
the write succeeds and the value is quietly wrong.

Migrations are forward-only and never edited once applied. To change a table,
add the next numbered file.

**A migration that REBUILDS a table must wrap itself in `BEGIN`/`COMMIT`.**
Most files here are a single `CREATE TABLE IF NOT EXISTS` or `ALTER TABLE ADD
COLUMN`, which SQLite applies atomically on its own, so nothing below needs a
transaction and the pattern to copy looks safe. A rebuild is not one statement:
SQLite cannot `ALTER` a CHECK onto a column, so the shape is copy-to-scratch,
drop, rename, and a process killed between any two of those leaves the table
half swapped. Statement order cannot fix it -- guarding one window widens the
next -- and SQLite has transactional DDL, so `BEGIN`/`COMMIT` around the whole
rebuild is the answer. `013_scheme_aum_basis.sql` is the worked example, and
DECISIONS V1-55 is what it cost to learn.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.common.decimals import UNSAFE_DECIMAL_TYPES, connect

MIGRATIONS = Path(__file__).resolve().parents[3] / "migrations"

#: Name segments that suggest a column holds a Decimal. Used ONLY to judge an
#: undeclared column — matching on names is too crude for anything else, as
#: `nav_date` (a DATE) and `is_interpolated` (which contains "ter") both show.
DECIMAL_NAME_TOKENS = frozenset(
    {"nav", "amount", "aum", "ter", "value", "weight", "pct", "qty", "price"}
)


class MigrationError(RuntimeError):
    """A migration file is missing, out of order, or produced an unsafe schema."""


def migration_files(directory: Path = MIGRATIONS) -> list[Path]:
    """Numbered `.sql` files in order. Gaps are an error, not a warning.

    A missing number means a migration was deleted rather than superseded, and
    the schema on disk can no longer be reproduced from the repository.
    """
    files = sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
    if not files:
        raise MigrationError(f"no migrations found in {directory}")
    for expected, path in enumerate(files, start=1):
        found = int(path.name[:3])
        if found != expected:
            raise MigrationError(f"migration {expected:03d} missing; found {path.name}")
    return files


def apply_migrations(db_path: str, directory: Path = MIGRATIONS) -> list[str]:
    """Run every migration that has not run yet. Returns the names applied.

    Each file is idempotent on its own (`CREATE TABLE IF NOT EXISTS`), but
    `schema_migration` is still recorded: it is the only thing that says which
    version produced the database, and a rebuild that silently re-ran an old
    file would be indistinguishable from one that did not.
    """
    conn = connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration ("
            "  name TEXT PRIMARY KEY,"
            "  applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migration")}
        applied: list[str] = []
        for path in migration_files(directory):
            if path.name in done:
                continue
            try:
                conn.executescript(path.read_text(encoding="utf-8"))
            except sqlite3.Error as exc:
                # NAMED. Without this the raw sqlite error propagates with no
                # reference to the file, and since every job calls this at
                # startup, one row a migration cannot accept stops the whole
                # system with a message that mentions neither the migration nor
                # the table it was reading -- measured with a single bad
                # `scheme_aum.basis`: `CHECK constraint failed`, from a NAV
                # backfill, naming a scratch table that no longer exists.
                raise MigrationError(f"{path.name} failed: {exc}") from exc
            conn.execute("INSERT INTO schema_migration(name) VALUES (?)", (path.name,))
            # Committed WITH its migration, not at the end of the loop. Deferred,
            # a later migration failing rolled back the marker for every one
            # before it while their DDL stood -- so the next run re-applied an
            # already-applied file. Harmless for a `CREATE TABLE IF NOT EXISTS`,
            # not harmless for a rebuild: re-running one against a table a later
            # migration had widened would copy the columns it knew about and
            # silently drop the rest.
            conn.commit()
            applied.append(path.name)
        conn.commit()
        return applied
    finally:
        conn.close()


def _looks_decimal(column_name: str) -> bool:
    """Whole underscore-separated segments, not substrings.

    `nav_date` splits to {nav, date} and would still match on `nav`, so the
    caller uses this only where the declared type is absent entirely.
    """
    return bool(set(column_name.lower().split("_")) & DECIMAL_NAME_TOKENS)


def unsafe_decimal_columns(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Every column that looks like a Decimal but is not declared `DECIMAL_TEXT`.

    SZ-13 in assertion form. The affinity rules resolve in order and only the
    text rule gives TEXT affinity, so `DECIMAL`, `NUMERIC` and `DECIMAL(18,6)`
    all end up NUMERIC and lose both exactness and trailing zeros on write.

    Returns (table, column, declared_type) so a failure names the column.
    """
    bad: list[tuple[str, str, str]] = []
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (table,) in tables:
        for _cid, name, decl, _nn, _dflt, _pk in conn.execute(
            f"PRAGMA table_info({table})"
        ):
            declared = (decl or "").upper()
            base = declared.split("(")[0].strip()
            # Declared DECIMAL / NUMERIC / REAL is unsafe whatever the column
            # is called: SQLite gives all of them NUMERIC or REAL affinity.
            if base in UNSAFE_DECIMAL_TYPES:
                bad.append((table, name, declared or "<undeclared>"))
            # An UNDECLARED column takes BLOB affinity and stores whatever it
            # is given, so a Decimal survives by luck rather than by design.
            # Only flagged when the name suggests a Decimal — the hints cannot
            # judge a declared type, only the absence of one.
            elif not base and _looks_decimal(name):
                bad.append((table, name, "<undeclared>"))
    return bad


def assert_schema_is_decimal_safe(db_path: str) -> None:
    """Raise if any column would silently store a Decimal as a REAL."""
    conn = connect(db_path)
    try:
        bad = unsafe_decimal_columns(conn)
    finally:
        conn.close()
    if bad:
        listed = ", ".join(f"{t}.{c} declared {d}" for t, c, d in bad)
        raise MigrationError(f"unsafe decimal columns: {listed}")


def main() -> None:
    from src.m0_data.config import warehouse_path

    path = warehouse_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    applied = apply_migrations(str(path))
    assert_schema_is_decimal_safe(str(path))
    print(f"warehouse {path}")
    print(f"applied: {', '.join(applied) if applied else 'nothing new'}")


if __name__ == "__main__":
    main()
