"""A CAS, end to end into the ledger. MODULE_1.md §5.7.

    python -m jobs.import_cas --file statement.pdf --user USER-01
    python -m jobs.import_cas --file statement.txt --user USER-01 --as-of 2026-09-04

    decrypt -> parse -> resolve -> import -> save -> rebuild

**Two databases, opened for different reasons.** Zone A is read only, for
scheme resolution and NAVs; Zone B is written. They never share a connection,
and the dependency runs the way invariant 3 requires: M1 reads M0, never the
reverse.

**Idempotent.** `txn_id` is §5.6's deterministic hash so re-importing inserts
nothing, and `import_id` derives from the file's sha256 so the `cas_import` row
is replaced. Re-running is the normal case — every new CAS re-covers periods
already imported.

**Two secrets, both prompted for and neither stored.** §5.4 on the CAS password:
*"From user input at import time. Never stored."* The Zone B key gets the same
treatment, with no flag and no environment variable for either.

`--allow-unencrypted` is deliberately not on this command line: Zone B holds a
PAN and folio numbers, and the escape hatch belongs in `connect_ledger` for
tests.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from src.common.decimals import connect
from src.common.types import Isin, SchemeId, UserId
from src.m0_data.config import warehouse_path
from src.m0_data.providers.warehouse import WarehouseMarketDataProvider
from src.m1_ledger.cas import PARSER_VERSION, ImportReport, StagedTxn, import_cas
from src.m1_ledger.cas.pdf import decrypt_and_extract, file_id
from src.m1_ledger.db import apply_ledger_schema, connect_ledger, ledger_path
from src.m1_ledger.persist import rebuild, save_txns


def run(
    path: Path,
    user_id: UserId,
    as_of: date | None = None,
    password: str | None = None,
    allow_unencrypted: bool = False,
    key: str | None = None,
) -> dict[str, object]:
    """Import one statement and rebuild everything derived from it."""
    content = path.read_bytes()
    source_file_id = file_id(content)
    lines = _lines(path, content, password)

    warehouse = warehouse_path()
    if not warehouse.exists():
        raise RuntimeError(
            f"no Zone A warehouse at {warehouse}; run `python -m jobs.fetch_nav` "
            f"first — schemes cannot be resolved without one"
        )
    zone_a = connect(str(warehouse))
    # The provider addresses columns by name (`row["scheme_id"]`), which a bare
    # connection does not support. Missing this raises a TypeError deep inside
    # resolution rather than anywhere near here.
    zone_a.row_factory = sqlite3.Row
    market = WarehouseMarketDataProvider(zone_a)

    db_path = ledger_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not allow_unencrypted and key is None:
        key = getpass.getpass("Zone B ledger key: ")
    ledger = connect_ledger(
        str(db_path), key=key, allow_unencrypted=allow_unencrypted
    )
    try:
        apply_ledger_schema(ledger)

        # The real INSERT OR IGNORE set, not a stand-in for it.
        known = {
            r[0]
            for r in ledger.execute(
                "SELECT txn_id FROM txn WHERE user_id = ?", (str(user_id),)
            )
        }
        report = import_cas(
            user_id, lines, _resolver(market), known_txn_ids=known
        )

        import_id = _import_id(str(user_id), source_file_id)
        added = save_txns(
            ledger,
            report.txns,
            source_file_id=source_file_id,
            import_id=import_id,
        )
        _record_import(ledger, import_id, user_id, source_file_id, report)

        effective = as_of or _last_txn_date(ledger, user_id) or date.today()
        book = rebuild(ledger, user_id, effective, _navs(ledger, market, user_id,
                                                         effective))
        ledger.commit()

        return {
            "source_file_id": source_file_id[:12],
            "parsed": report.inserted + report.duplicate + report.unmatched,
            "inserted": added,
            "duplicate": report.duplicate,
            "unmatched": report.unmatched,
            "unparsed_lines": report.unparsed_lines,
            "status": report.status,
            "as_of": str(effective),
            "lots": len(book.all_lots()),
            "consumptions": len(book.all_consumptions()),
        }
    finally:
        ledger.close()
        market.conn.close()


def _lines(path: Path, content: bytes, password: str | None) -> list[str]:
    """A PDF needs decrypting; a rendered statement is already text.

    The text path exists because the golden fixture is a rendered statement and
    the tests must not need a PDF — not as a convenience for real use.
    """
    if path.suffix.lower() != ".pdf":
        return content.decode("utf-8").splitlines()
    secret = password if password is not None else getpass.getpass(
        f"Password for {path.name}: "
    )
    return decrypt_and_extract(content, secret)


def _resolver(market: WarehouseMarketDataProvider):  # type: ignore[no-untyped-def]
    def _resolve(s: StagedTxn) -> SchemeId | None:
        ref = market.resolve_scheme(
            Isin(s.scheme_raw_isin) if s.scheme_raw_isin else None,
            s.scheme_raw_name,
            None,
            s.txn_date,
        )
        return ref.scheme_id

    return _resolve


def _import_id(user_id: str, source_file_id: str) -> str:
    """Derived from the bytes, so re-importing replaces rather than duplicates."""
    return hashlib.sha256(f"{user_id}|{source_file_id}".encode()).hexdigest()


def _record_import(
    conn: sqlite3.Connection,
    import_id: str,
    user_id: UserId,
    source_file_id: str,
    report: ImportReport,
) -> None:
    """§4.2's `cas_import`. "Without this table you're guessing."

    **The row describes the statement, not the insert.** `report.txns` holds
    only rows that were new, so on a re-import it is empty — reading coverage
    off it would record a statement spanning zero folios and no dates, which is
    false about the file and useless for the question this table exists to
    answer. Folios and schemes come from the parse context, which the parser
    fills per scheme block regardless of what was already stored; the date range
    comes from the ledger rows carrying this `import_id`, which are the same
    rows on every re-run because `import_id` is derived from the file's bytes.

    `cas_type` stays NULL: the parser does not yet identify the registrar, and a
    guessed value in an audit table is worse than an absent one.
    """
    span = conn.execute(
        "SELECT min(txn_date), max(txn_date) FROM txn WHERE import_id = ?",
        (import_id,),
    ).fetchone()
    conn.execute(
        "INSERT OR REPLACE INTO cas_import ("
        " import_id, user_id, source_file_id, statement_from, statement_to,"
        " rows_parsed, rows_inserted, rows_duplicate, rows_unmatched,"
        " folios_seen, schemes_seen, parser_version, imported_at, status"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            import_id, str(user_id), source_file_id,
            span[0] if span else None,
            span[1] if span else None,
            report.inserted + report.duplicate + report.unmatched,
            report.inserted, report.duplicate, report.unmatched,
            len({b.folio for b in report.ctx.balances}),
            len({b.scheme_raw_isin for b in report.ctx.balances}),
            PARSER_VERSION, datetime.now(UTC).isoformat(), report.status,
        ),
    )
    conn.commit()


def _last_txn_date(conn: sqlite3.Connection, user_id: UserId) -> date | None:
    """Default `as_of` is the statement's own last position, not today.

    Reconciliation compares the ledger against `units_balance_rep` as printed,
    and a date after the statement ends would reconcile a position the
    statement never claimed.
    """
    row = conn.execute(
        "SELECT max(txn_date) FROM txn WHERE user_id = ?", (str(user_id),)
    ).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def _navs(
    conn: sqlite3.Connection,
    market: WarehouseMarketDataProvider,
    user_id: UserId,
    as_of: date,
) -> dict[str, dict[date, Decimal]]:
    """Raw NAVs per held scheme, for position valuation.

    `adjusted=False` on purpose. `nav_adj` exists for return math — it removes
    the drop an IDCW payout puts in the raw series — but a position's market
    value is units times the NAV the fund actually publishes. Valuing a holding
    on an adjusted series would overstate every IDCW plan.
    """
    rows = conn.execute(
        "SELECT DISTINCT scheme_id, min(txn_date) FROM txn"
        " WHERE user_id = ? AND scheme_id IS NOT NULL GROUP BY scheme_id",
        (str(user_id),),
    ).fetchall()
    series: dict[str, dict[date, Decimal]] = {}
    for scheme_id, first in rows:
        points = market.nav_series(
            SchemeId(scheme_id), date.fromisoformat(first), as_of, adjusted=False
        )
        if points:
            series[scheme_id] = {p.nav_date: p.nav for p in points}
    return series


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True, help="the CAS")
    parser.add_argument("--user", required=True, help="user_id")
    parser.add_argument("--as-of", help="YYYY-MM-DD; defaults to the last txn date")
    args = parser.parse_args()
    summary = run(
        args.file,
        UserId(args.user),
        date.fromisoformat(args.as_of) if args.as_of else None,
    )
    print(" | ".join(f"{k}={v}" for k, v in summary.items()))


if __name__ == "__main__":
    main()
