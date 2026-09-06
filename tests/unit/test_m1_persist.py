"""Zone B persistence. MODULE_1.md §4, `PLAN.md` §8.3 invariant 5.

Invariant 5 — *"a full rebuild reproduces byte-identical derived tables"* — has
until now been tested as two in-memory `LotBook.fingerprint()` calls. That
proves the engine is deterministic and nothing about the storage layer: it never
dropped a table, never round-tripped a Decimal through SQLite, and never showed
the derived rows are actually droppable. SZ-13 is the reason that gap matters —
a `DECIMAL` column takes NUMERIC affinity and SQLite silently rewrites a decimal
string as a REAL, and no pure-Python test can see it.

These tests assert against the **database**.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.decimals import UNSAFE_DECIMAL_TYPES
from src.common.fixtures import load_yaml
from src.common.types import UserId
from src.m0_data.schema.apply import MigrationError
from src.m1_ledger.db import (
    EncryptionUnavailable,
    apply_ledger_schema,
    connect_ledger,
)
from src.m1_ledger.persist import (
    DERIVED_TABLES,
    _cell,
    _fy,
    derived_fingerprint,
    drop_derived,
    load_txns,
    rebuild,
    save_txns,
)
from src.m1_ledger.txn import Txn, load_transactions

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "v0_ledger"
USER = UserId("USER-01")
AS_OF = date(2026, 9, 4)
PINNED = "2026-09-06T00:00:00+00:00"


@pytest.fixture
def txns() -> list[Txn]:
    return load_transactions(FIXTURES / "transactions.csv")


@pytest.fixture
def navs() -> dict[str, dict[date, Decimal]]:
    raw = load_yaml(FIXTURES / "nav_series.yaml")["series"]
    return {
        k: {d: Decimal(str(v)) for d, v in b["navs"].items()} for k, b in raw.items()
    }


@pytest.fixture
def conn(tmp_path: Path, txns: list[Txn]) -> sqlite3.Connection:
    db = connect_ledger(str(tmp_path / "personal.db"), allow_unencrypted=True)
    apply_ledger_schema(db)
    save_txns(db, txns, source_file_id="sha256:golden", ingested_at=PINNED)
    return db


# --- the encryption boundary -------------------------------------------------


def test_zone_b_refuses_to_open_unencrypted_by_default(tmp_path: Path) -> None:
    """`PLAN.md` §6.3: Zone B never leaves the device unencrypted.

    SQLCipher is not installed here, and the failure mode this guards against is
    a fallback that quietly writes a plaintext file holding a PAN and folio
    numbers. Refusing is the whole design — `allow_unencrypted=True` is the only
    way past it, and it says so at every call site.
    """
    with pytest.raises(EncryptionUnavailable, match="unencrypted"):
        connect_ledger(str(tmp_path / "personal.db"))


def test_the_schema_has_no_column_that_would_store_a_decimal_as_a_real(
    conn: sqlite3.Connection,
) -> None:
    """SZ-13, asserted against the applied schema rather than the DDL text.

    `apply_ledger_schema` already raises on an unsafe column; this checks the
    same property from the other side, so a future migration that reintroduces
    `DECIMAL` fails here even if the applier is changed.
    """
    for table in ("txn", *DERIVED_TABLES):
        for row in conn.execute(f"PRAGMA table_info({table})"):
            declared = (row[2] or "").upper()
            assert declared not in UNSAFE_DECIMAL_TYPES, (
                f"{table}.{row[1]} declared {declared}: SQLite would give it "
                f"NUMERIC affinity and silently store a REAL"
            )


# --- Decimals survive the round trip ----------------------------------------


def test_every_decimal_round_trips_through_sqlite_unchanged(
    conn: sqlite3.Connection, txns: list[Txn]
) -> None:
    """The SZ-13 guard, on real values rather than a constructed one.

    Exact equality AND type: a value that came back as `float` would still
    compare equal for many inputs, so the type assertion is what actually
    catches the affinity bug. `typeof()` confirms SQLite stored text.
    """
    loaded = {t.txn_id: t for t in load_txns(conn, USER)}
    assert len(loaded) == len(txns)

    for original in txns:
        back = loaded[original.txn_id]
        for field_name in ("units", "nav", "amount", "stamp_duty", "stt",
                           "exit_load", "units_balance_rep"):
            want = getattr(original, field_name)
            got = getattr(back, field_name)
            assert got == want, f"{original.txn_ref}.{field_name}: {got} != {want}"
            if want is not None:
                assert isinstance(got, Decimal), (
                    f"{original.txn_ref}.{field_name} came back "
                    f"{type(got).__name__}, not Decimal"
                )

    stored = conn.execute(
        "SELECT typeof(units), typeof(amount) FROM txn WHERE units IS NOT NULL"
    ).fetchall()
    assert stored and all(t == ("text", "text") for t in stored)


def test_trailing_zeros_survive(conn: sqlite3.Connection) -> None:
    """`4821.442100` must not come back `4821.4421`.

    Scale is information: a NAV quoted to six decimals and one quoted to four
    are different statements about precision, and REAL loses the distinction
    along with the exactness.
    """
    conn.execute(
        "UPDATE txn SET nav = ? WHERE txn_id = (SELECT txn_id FROM txn LIMIT 1)",
        (Decimal("4821.442100"),),
    )
    conn.commit()
    got = conn.execute(
        "SELECT nav FROM txn WHERE nav IS NOT NULL ORDER BY nav LIMIT 1"
    ).fetchone()
    assert str(Decimal("4821.442100")) == "4821.442100"
    assert any(
        str(r[0]) == "4821.442100"
        for r in conn.execute("SELECT nav FROM txn WHERE nav IS NOT NULL")
    ), f"trailing zeros lost; sample {got}"


# --- invariant 5, against the database ---------------------------------------


def test_invariant_5_drop_and_rebuild_reproduces_the_derived_tables(
    conn: sqlite3.Connection, navs: dict[str, dict[date, Decimal]]
) -> None:
    """`PLAN.md` §8.3 invariant 5 and CLAUDE.md invariant 10, on real tables.

    Import, rebuild, **DROP every derived table**, rebuild again. The drop is a
    real `DROP TABLE`, not a `DELETE`: the claim is that these tables are
    reconstructible from `txn` alone, and deleting rows would leave the schema
    behind and prove only half of it.
    """
    rebuild(conn, USER, AS_OF, navs, rebuilt_at=PINNED)
    first = derived_fingerprint(conn)
    counts = {t: _count(conn, t) for t in DERIVED_TABLES}
    assert all(counts.values()), f"a derived table came back empty: {counts}"

    drop_derived(conn)
    for table in DERIVED_TABLES:
        assert not _table_exists(conn, table), f"{table} survived the drop"

    # `force`: the migration ledger still records the file that created these
    # tables, so a plain re-apply would skip it and leave them missing.
    apply_ledger_schema(conn, force=True)
    rebuild(conn, USER, AS_OF, navs, rebuilt_at=PINNED)

    assert derived_fingerprint(conn) == first
    assert {t: _count(conn, t) for t in DERIVED_TABLES} == counts


def test_the_rebuilt_rows_are_byte_identical_timestamps_included(
    conn: sqlite3.Connection, navs: dict[str, dict[date, Decimal]]
) -> None:
    """Stronger than the fingerprint, and it needs the stamp pinned to be fair.

    `derived_fingerprint` excludes `rebuilt_at` and `checked_at` because two
    rebuilds at different times are the same rebuild. Pinning the stamp removes
    that excuse and compares whole rows — so a column the fingerprint happens
    not to cover cannot drift unnoticed.
    """
    rebuild(conn, USER, AS_OF, navs, rebuilt_at=PINNED)
    before = {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in DERIVED_TABLES}

    rebuild(conn, USER, AS_OF, navs, rebuilt_at=PINNED)
    after = {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in DERIVED_TABLES}

    for table in DERIVED_TABLES:
        assert after[table] == before[table], f"{table} differs across rebuilds"


def test_a_rebuild_without_market_data_is_still_a_valid_rebuild(
    conn: sqlite3.Connection,
) -> None:
    """NAV-dependent columns go NULL, not zero.

    A position whose value is unknown is not a position worth nothing, and
    writing 0 would make it aggregate as one.
    """
    rebuild(conn, USER, AS_OF, None, rebuilt_at=PINNED)
    rows = conn.execute(
        "SELECT units, nav, market_value, unrealised_pnl FROM position"
    ).fetchall()
    assert rows
    for units, nav, market_value, unrealised in rows:
        assert units is not None
        assert nav is None and market_value is None and unrealised is None


def test_txn_is_the_only_input_a_rebuild_reads(
    conn: sqlite3.Connection, navs: dict[str, dict[date, Decimal]]
) -> None:
    """CLAUDE.md invariant 10, stated as a property rather than a comment.

    Wiping the derived tables and rebuilding from an untouched `txn` must give
    the same answer as the first rebuild. If any derived value depended on a
    previous derived value, this would drift.
    """
    rebuild(conn, USER, AS_OF, navs, rebuilt_at=PINNED)
    expected = derived_fingerprint(conn)

    for table in DERIVED_TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    assert all(_count(conn, t) == 0 for t in DERIVED_TABLES)

    rebuild(conn, USER, AS_OF, navs, rebuilt_at=PINNED)
    assert derived_fingerprint(conn) == expected


def test_reimporting_the_same_transactions_inserts_nothing(
    conn: sqlite3.Connection, txns: list[Txn]
) -> None:
    """§5.6's hash is the whole dedup mechanism. `PLAN.md` §7's V0 gate.

    Already proven in memory; asserted here through `INSERT OR IGNORE`, which
    is where it actually has to hold.
    """
    before = _count(conn, "txn")
    added = save_txns(conn, txns, source_file_id="sha256:golden", ingested_at=PINNED)
    assert added == 0
    assert _count(conn, "txn") == before


# --- gaps found by mutation testing ------------------------------------------
#
# Each of these was written because a mutant survived a green suite. The claim
# each one makes was true of the code and untested, which is the same thing as
# being unprotected.


def test_transactions_load_in_a_deterministic_order(
    tmp_path: Path, txns: list[Txn]
) -> None:
    """`load_txns` orders by (txn_date, txn_seq, txn_id), not by insertion.

    Deleting the `ORDER BY` survived a green suite: SQLite returns rowid order,
    which for a single import *is* insertion order, so the rebuild stayed
    deterministic by accident. It would stop being accidental the moment a
    second statement was imported covering an earlier period — the lot engine's
    FIFO reads this order, and getting it from rowid means the ledger depends
    on which CAS happened to be imported first.

    Saving in reverse is what makes the assertion mean anything.
    """
    db = connect_ledger(str(tmp_path / "personal.db"), allow_unencrypted=True)
    apply_ledger_schema(db)
    save_txns(db, list(reversed(txns)), source_file_id="x", ingested_at=PINNED)

    loaded = load_txns(db, USER)
    keys = [(t.txn_date, t.txn_seq, t.txn_id) for t in loaded]
    assert keys == sorted(keys), "load_txns returned rows out of order"
    assert len(loaded) == len(txns)


def test_derived_decimal_columns_read_back_as_decimal(
    conn: sqlite3.Connection, navs: dict[str, dict[date, Decimal]]
) -> None:
    """SZ-13 applies to the derived tables too, not just to `txn`.

    Dropping the type tag from `derived_fingerprint`'s `_cell` survived, because
    nothing asserted the derived side ever produces a `Decimal` rather than a
    `float` that happens to render the same. Checking the values directly is
    what closes it.
    """
    rebuild(conn, USER, AS_OF, navs, rebuilt_at=PINNED)
    checks = {
        "lot": ("units_original", "units_remaining", "cost_per_unit", "cost_total"),
        "lot_consumption": ("units_consumed", "cost_allocated", "gain_amount"),
        "position": ("units", "market_value", "realised_pnl_todate"),
        "reconciliation": ("units_computed", "delta_units"),
    }
    for table, columns in checks.items():
        rows = conn.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
        assert rows, f"{table} is empty"
        for row in rows:
            for column, value in zip(columns, row, strict=True):
                assert value is None or isinstance(value, Decimal), (
                    f"{table}.{column} came back {type(value).__name__}"
                )


def test_the_fingerprint_actually_discriminates(
    conn: sqlite3.Connection, navs: dict[str, dict[date, Decimal]]
) -> None:
    """A rebuild comparing two fingerprints proves nothing if both are blind.

    Widening `_TIMESTAMP_COLUMNS` to exclude `units_remaining` survived the
    drop-and-rebuild test, because both sides of that comparison used the same
    weakened hash. The check was checking itself. Changing a derived value and
    requiring the hash to move is the assertion that was missing.
    """
    rebuild(conn, USER, AS_OF, navs, rebuilt_at=PINNED)
    before = derived_fingerprint(conn)

    for column in ("units_remaining", "cost_total"):
        conn.execute(
            f"UPDATE lot SET {column} = ? WHERE lot_id = "
            f"(SELECT lot_id FROM lot ORDER BY lot_id LIMIT 1)",
            (Decimal("999.999999"),),
        )
        conn.commit()
        assert derived_fingerprint(conn) != before, (
            f"the fingerprint does not cover lot.{column}"
        )
        rebuild(conn, USER, AS_OF, navs, rebuilt_at=PINNED)
        assert derived_fingerprint(conn) == before


def test_the_financial_year_runs_april_to_march(
    conn: sqlite3.Connection, navs: dict[str, dict[date, Decimal]]
) -> None:
    """`fy` is written on every realised gain and was asserted nowhere.

    Moving the boundary off April survived a green suite. India's FY runs 1
    April to 31 March, so 31-Mar-2027 is `2026-27` and 1-Apr-2027 is `2027-28`
    — an off-by-one here files a realised gain in the wrong tax year, which is
    the kind of wrong that looks perfectly plausible on a report.
    """
    assert _fy(date(2026, 4, 1)) == "2026-27"
    assert _fy(date(2027, 3, 31)) == "2026-27"
    assert _fy(date(2027, 4, 1)) == "2027-28"
    assert _fy(date(2026, 12, 31)) == "2026-27"

    rebuild(conn, USER, AS_OF, navs, rebuilt_at=PINNED)
    written = {r[0] for r in conn.execute("SELECT fy FROM lot_consumption")}
    assert written, "no realised gains to check"
    assert all(len(f) == 7 and f[4] == "-" for f in written), written


def test_a_migration_that_would_store_a_decimal_as_a_real_is_refused(
    conn: sqlite3.Connection,
) -> None:
    """The post-migration safety check fires, rather than merely existing.

    Replacing it with `bad = []` survived, because the real schema is safe and
    the check never had anything to find. Giving it something to find is the
    only way to prove it looks.
    """
    conn.execute("CREATE TABLE unsafe_probe (id TEXT PRIMARY KEY, nav DECIMAL)")
    conn.commit()
    with pytest.raises(MigrationError, match="unsafe decimal"):
        apply_ledger_schema(conn)


def test_a_float_and_a_decimal_do_not_hash_alike(
    conn: sqlite3.Connection, navs: dict[str, dict[date, Decimal]]
) -> None:
    """`_cell` must not hash a REAL the same as the Decimal it was rewritten from.

    `Decimal("1.5")` and `1.5` render identically under `str`, so without type
    information a derived column silently rewritten as a REAL would hash the
    same as the correct value and the rebuild comparison would pass on wrong
    data. This asserts the discrimination, which is the property that matters.

    **Recorded from mutation testing:** removing the `D:` prefix specifically
    survives, and correctly so — every other branch tags itself, so a float
    still renders `FLOAT:1.5` and a string `str:1.5`, and the discrimination
    holds without it. The prefix is redundant belt-and-braces rather than the
    load-bearing part, and the load-bearing part is the float branch.

    The schema makes a REAL unreachable today — every Decimal column is
    `DECIMAL_TEXT` — so this is defence in depth, tested at the unit rather
    than through a database that cannot currently produce the condition.
    """
    assert _cell(Decimal("1.5")) != _cell(1.5)
    assert _cell(Decimal("1.50")) != _cell(Decimal("1.5"))  # scale is information
    assert _cell(None) != _cell("")

    rebuild(conn, USER, AS_OF, navs, rebuilt_at=PINNED)
    assert derived_fingerprint(conn)


def test_one_transaction_consumes_a_lot_at_most_once(
    conn: sqlite3.Connection, navs: dict[str, dict[date, Decimal]]
) -> None:
    """Why `consumption_id` is unique on (lot_id, close_txn_id) alone.

    Mutation testing flagged that dropping `sequence_in_txn` from the id
    survives. It survives because it is an **equivalent mutant**: the lot
    engine walks each book's FIFO lots once per closing transaction, so a lot
    contributes to a given transaction at most once and the pair is already
    unique. `sequence_in_txn` orders the rows within a transaction; it does not
    distinguish them.

    Recorded as a property rather than left as an unexplained survivor, because
    the next person to read that mutation report should not have to re-derive
    it — and if the engine ever did consume a lot twice in one transaction,
    this fails and the id genuinely needs the sequence.
    """
    rebuild(conn, USER, AS_OF, navs, rebuilt_at=PINNED)
    pairs = conn.execute(
        "SELECT lot_id, close_txn_id, count(*) FROM lot_consumption"
        " GROUP BY lot_id, close_txn_id HAVING count(*) > 1"
    ).fetchall()
    assert not pairs, f"a lot contributed twice to one transaction: {pairs}"

    total = _count(conn, "lot_consumption")
    distinct = conn.execute(
        "SELECT count(*) FROM (SELECT DISTINCT lot_id, close_txn_id"
        " FROM lot_consumption)"
    ).fetchone()[0]
    assert total == distinct


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )
