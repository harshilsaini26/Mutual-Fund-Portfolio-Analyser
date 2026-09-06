"""A CAS from file to rebuilt ledger. `jobs/import_cas.py`, MODULE_1.md §5.7.

The two halves of the ledger have been finished and disconnected for a while:
`import_cas` produced an in-memory `ImportReport`, and `persist.save_txns` /
`rebuild` wrote a database nothing filled. This exercises the join.

The part worth asserting hardest is idempotence. `import_cas.known_txn_ids`
existed as a stand-in for `INSERT OR IGNORE` while Zone B did not exist, so the
duplicate count was a claim about a database rather than an observation of one.
The job passes the real set now, and these tests check that the report and the
table agree — a report saying "0 inserted" while rows appear would be worse
than either failing.

**What this does not prove.** The statement is the one
`scripts/build_v0_cas.py` renders, so the rows were computed by this repository
and reading them back does not re-verify the arithmetic — that is
`scripts/verify_v0_ledger.py`'s job, and it imports nothing from
`src.m1_ledger`. What is established here is the wiring: that a file on disk
reaches `txn`, and that everything derived rebuilds from it.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from jobs.import_cas import run
from src.common.decimals import connect
from src.common.fixtures import load_yaml
from src.common.types import UserId
from src.m0_data.schema.apply import apply_migrations
from src.m1_ledger.db import apply_ledger_schema, connect_ledger, ledger_path
from src.m1_ledger.persist import derived_fingerprint
from src.m1_ledger.txn import load_transactions

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "v0_ledger"
USER = UserId("USER-01")
AS_OF = date(2026, 9, 4)


@pytest.fixture
def warehouse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Zone A warehouse holding just the three golden schemes and their NAVs.

    Built rather than borrowed: pointing at the real warehouse would make the
    test depend on whatever was last loaded into it, and a test that passes
    because of yesterday's job is not a test.
    """
    db = tmp_path / "canonical.db"
    apply_migrations(str(db))
    conn = connect(str(db))
    series = load_yaml(FIXTURES / "nav_series.yaml")["series"]
    for scheme_id, block in series.items():
        conn.execute(
            "INSERT INTO scheme (scheme_id, isin, scheme_name, plan, option)"
            " VALUES (?,?,?,?,?)",
            (scheme_id, scheme_id, f"Golden {scheme_id}",
             block.get("plan", "direct"), "growth"),
        )
        for on, nav in block["navs"].items():
            value = Decimal(str(nav))
            conn.execute(
                "INSERT INTO nav_daily (scheme_id, nav_date, nav, nav_adj)"
                " VALUES (?,?,?,?)",
                (scheme_id, on, value, value),
            )
    conn.commit()
    conn.close()
    monkeypatch.setenv("MF_WAREHOUSE", str(db))
    return db


@pytest.fixture
def statement(tmp_path: Path) -> Path:
    """The golden statement, with the fixture's own annotations stripped.

    `cas_statement.txt` carries `>>>` marker lines that label the traps for a
    human reader. They are not part of a statement and the gate test drops them
    too; a real CAS has no such thing.
    """
    text = (FIXTURES / "cas_statement.txt").read_text(encoding="utf-8")
    out = tmp_path / "statement.txt"
    out.write_text(
        "\n".join(
            ln for ln in text.splitlines() if not ln.lstrip().startswith(">>>")
        ),
        encoding="utf-8",
    )
    return out


@pytest.fixture
def ledger_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "personal.db"
    monkeypatch.setenv("MF_LEDGER", str(db))
    return db


def _open(path: Path) -> sqlite3.Connection:
    conn = connect_ledger(str(path), allow_unencrypted=True)
    apply_ledger_schema(conn)
    return conn


def test_the_statement_reaches_the_ledger_and_everything_rebuilds(
    warehouse: Path, statement: Path, ledger_db: Path
) -> None:
    """The whole path: file -> decrypt/read -> parse -> resolve -> save -> rebuild."""
    summary = run(statement, USER, AS_OF, allow_unencrypted=True)
    expected = len(load_transactions(FIXTURES / "transactions.csv"))

    assert summary["unparsed_lines"] == 0, summary
    assert summary["unmatched"] == 0, summary
    assert summary["status"] == "ok"
    assert summary["inserted"] == expected

    conn = _open(ledger_db)
    try:
        assert _count(conn, "txn") == expected
        assert _count(conn, "lot") > 0
        assert _count(conn, "lot_consumption") > 0
        assert _count(conn, "position") > 0
        assert _count(conn, "reconciliation") > 0
    finally:
        conn.close()


def test_ledger_path_is_where_the_job_wrote(
    warehouse: Path, statement: Path, ledger_db: Path
) -> None:
    """`MF_LEDGER` is read at call time, so the test's temp path is honoured.

    Worth asserting rather than assuming: a path resolved at import would send
    a real import into the developer's own ledger.
    """
    run(statement, USER, AS_OF, allow_unencrypted=True)
    assert ledger_path() == ledger_db
    assert ledger_db.exists()


def test_reimporting_the_same_statement_inserts_nothing(
    warehouse: Path, statement: Path, ledger_db: Path
) -> None:
    """§5.6's hash, now reported from the database rather than approximated.

    The second run must say `inserted=0` AND leave the row count unchanged.
    Checking both is the point — a report that disagrees with the table is a
    worse failure than either alone, and it is exactly what `known_txn_ids`
    standing in for the database could have hidden.
    """
    first = run(statement, USER, AS_OF, allow_unencrypted=True)
    conn = _open(ledger_db)
    try:
        before = _count(conn, "txn")
    finally:
        conn.close()

    second = run(statement, USER, AS_OF, allow_unencrypted=True)
    assert second["inserted"] == 0
    assert second["duplicate"] == first["inserted"]

    conn = _open(ledger_db)
    try:
        assert _count(conn, "txn") == before
    finally:
        conn.close()


def test_a_reimport_does_not_move_the_derived_tables(
    warehouse: Path, statement: Path, ledger_db: Path
) -> None:
    """Invariant 5 through the job, not just through `rebuild` directly.

    Re-importing rebuilds from the same `txn` rows, so every derived table must
    land where it was. If it moved, either the import was not idempotent or the
    rebuild was not deterministic, and the fingerprint does not care which.
    """
    run(statement, USER, AS_OF, allow_unencrypted=True)
    conn = _open(ledger_db)
    try:
        before = derived_fingerprint(conn)
    finally:
        conn.close()

    run(statement, USER, AS_OF, allow_unencrypted=True)
    conn = _open(ledger_db)
    try:
        assert derived_fingerprint(conn) == before
    finally:
        conn.close()


def test_the_import_is_recorded_once_and_describes_itself(
    warehouse: Path, statement: Path, ledger_db: Path
) -> None:
    """§4.2's `cas_import`: "without this table you're guessing".

    `import_id` is derived from the file's sha256, so re-importing replaces the
    row rather than adding a second one — the audit trail says what the latest
    attempt did, and does not grow by one every time cron fires.
    """
    run(statement, USER, AS_OF, allow_unencrypted=True)
    run(statement, USER, AS_OF, allow_unencrypted=True)

    conn = _open(ledger_db)
    try:
        rows = conn.execute(
            "SELECT rows_parsed, rows_inserted, rows_duplicate, rows_unmatched,"
            " folios_seen, schemes_seen, parser_version, status,"
            " statement_from, statement_to, cas_type FROM cas_import"
        ).fetchall()
        assert len(rows) == 1, "a re-import added a second audit row"
        (parsed, inserted, duplicate, unmatched, folios, schemes,
         version, status, first, last, cas_type) = rows[0]

        assert parsed == inserted + duplicate + unmatched
        assert inserted == 0 and duplicate > 0, "the second run's counts"
        assert folios == 3 and schemes == 3
        assert version and status == "ok"
        assert first < last
        assert cas_type is None, "the parser does not identify the registrar yet"
    finally:
        conn.close()


def test_positions_are_valued_on_raw_nav_not_the_adjusted_series(
    warehouse: Path, statement: Path, ledger_db: Path
) -> None:
    """A holding is worth units times the NAV the fund publishes.

    `nav_adj` removes the drop an IDCW payout puts in the raw series so return
    math works; using it to value a position would overstate every IDCW plan.
    The golden fixture sets both columns equal, so this asserts the wiring
    reaches a NAV at all and that market value is the product — not that the
    two series differ.
    """
    run(statement, USER, AS_OF, allow_unencrypted=True)
    conn = _open(ledger_db)
    try:
        rows = conn.execute(
            "SELECT units, nav, market_value FROM position WHERE nav IS NOT NULL"
        ).fetchall()
        assert rows, "no position was valued; the NAV lookup found nothing"
        for units, nav, market_value in rows:
            assert isinstance(nav, Decimal)
            assert market_value == units * nav
    finally:
        conn.close()


def test_the_job_refuses_without_a_warehouse(
    statement: Path, ledger_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolution needs Zone A. Failing loudly beats importing everything unresolved.

    Without a warehouse every scheme resolves to None, the rows quarantine, and
    the import "succeeds" with a ledger that reconciles against nothing.
    """
    monkeypatch.setenv("MF_WAREHOUSE", str(tmp_path / "absent.db"))
    with pytest.raises(RuntimeError, match="no Zone A warehouse"):
        run(statement, USER, AS_OF, allow_unencrypted=True)


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
