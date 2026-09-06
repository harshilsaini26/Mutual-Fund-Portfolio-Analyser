"""The ledger, in the database. MODULE_1.md §4, `PLAN.md` §8.3 invariant 5.

Until now the ledger ran entirely in memory from fixtures, which made invariant
5 — *"a full rebuild reproduces byte-identical derived tables"* — an in-memory
comparison of two `LotBook`s. That is weaker than the invariant intends in the
one way that matters: it never dropped a table, never wrote a Decimal through
SQLite, and never proved the derived rows are actually droppable. SZ-13 already
showed the storage layer has a trap in it that a pure-Python test cannot see.

`rebuild()` is the whole point: **`txn` is the only input**. Every other table
here is derived and may be dropped at any moment, which is CLAUDE.md invariant
10. If a number cannot be reconstructed from `txn` alone, it does not belong in
a derived table.

**Determinism, and where the timestamps go.** `rebuilt_at` and `checked_at`
record when a rebuild ran, so two runs of the same rebuild differ in exactly
those columns and in nothing else. `derived_fingerprint` therefore hashes the
content and excludes them — and `rebuild()` takes `rebuilt_at` so a test can
pin it and compare whole rows, timestamps included. The two checks answer
different questions and both are worth having.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from src.common.types import SchemeId, UserId
from src.m1_ledger.lots import LotBook, build_book
from src.m1_ledger.reconcile import reconcile_all
from src.m1_ledger.txn import Txn, drop_reversed

#: Tables `rebuild()` owns entirely. Dropping every one of them and rebuilding
#: must reproduce the same content — that is invariant 5.
DERIVED_TABLES = ("lot_consumption", "lot", "position", "reconciliation")

#: Columns that record *when* a rebuild ran rather than what it produced.
_TIMESTAMP_COLUMNS = frozenset({"rebuilt_at", "checked_at"})

#: The tax engine is V2 (`PLAN.md` §7). `tax_rule_id` is NOT NULL because §4.4
#: is right that a realised gain must record which rule produced it — so V0
#: writes a placeholder that is obviously not a rule id, rather than a plausible
#: one that would later be mistaken for a real lookup.
UNVERSIONED_TAX_RULE = "v0-unversioned"


def ensure_user(conn: sqlite3.Connection, user_id: UserId) -> None:
    """`txn.user_id` references `app_user`, so the row has to exist first."""
    conn.execute(
        "INSERT OR IGNORE INTO app_user (user_id, created_at) VALUES (?, ?)",
        (str(user_id), datetime.now(UTC).isoformat()),
    )


def save_txns(
    conn: sqlite3.Connection,
    txns: list[Txn],
    *,
    source_file_id: str,
    import_id: str | None = None,
    ingested_at: str | None = None,
) -> int:
    """Insert transactions, ignoring ones already present. Returns rows added.

    `INSERT OR IGNORE` on `txn_id` is all the dedup logic there is, because
    §5.6's hash already makes a transaction identify itself. That is what makes
    re-import idempotent, and it matters because every new CAS re-covers
    periods already imported.

    `txn_ref` and `reverses_txn_ref` are fixture handles and are not persisted
    (§4.3 stores `reverses_txn_id`, a real foreign key). The ref is mapped to
    the target's `txn_id` on the way in and reconstructed from it on the way
    out, so a round trip through the database preserves the link without
    preserving the handle.
    """
    if not txns:
        return 0
    ensure_user(conn, txns[0].user_id)
    stamp = ingested_at or datetime.now(UTC).isoformat()

    by_ref = {t.txn_ref: t.txn_id for t in txns}
    reversed_ids = {
        by_ref[t.reverses_txn_ref]
        for t in txns
        if t.reverses_txn_ref and t.reverses_txn_ref in by_ref
    }

    before = conn.total_changes
    for t in txns:
        conn.execute(
            "INSERT OR IGNORE INTO txn ("
            " txn_id, user_id, folio, scheme_id, scheme_raw_name, txn_date,"
            " txn_seq, txn_type, units, nav, amount, stamp_duty, stt, exit_load,"
            " units_balance_rep, switch_group_id, reverses_txn_id, is_reversed,"
            " import_id, source_file_id, ingested_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t.txn_id, str(t.user_id), t.folio,
                str(t.scheme_id) if t.scheme_id else None,
                t.scheme_raw_name, t.txn_date.isoformat(), t.txn_seq, t.txn_type,
                t.units, t.nav, t.amount, t.stamp_duty, t.stt, t.exit_load,
                t.units_balance_rep, t.switch_group_id,
                by_ref.get(t.reverses_txn_ref or ""),
                1 if t.txn_id in reversed_ids else 0,
                import_id, source_file_id, stamp,
            ),
        )
    conn.commit()
    return conn.total_changes - before


def load_txns(conn: sqlite3.Connection, user_id: UserId) -> list[Txn]:
    """Read the ledger's only input back, in a deterministic order.

    Ordered by `(txn_date, txn_seq, txn_id)` rather than by insertion: the lot
    engine's FIFO depends on it, and `rowid` order would make a rebuild depend
    on which statement happened to be imported first.
    """
    rows = conn.execute(
        "SELECT txn_id, user_id, folio, scheme_id, scheme_raw_name, txn_date,"
        " txn_seq, txn_type, units, nav, amount, stamp_duty, stt, exit_load,"
        " units_balance_rep, switch_group_id, reverses_txn_id"
        " FROM txn WHERE user_id = ? ORDER BY txn_date, txn_seq, txn_id",
        (str(user_id),),
    ).fetchall()
    return [
        Txn(
            txn_ref=r[0],  # the txn_id IS the handle once the fixture is gone
            user_id=UserId(r[1]),
            folio=r[2],
            scheme_id=SchemeId(r[3]) if r[3] else None,
            scheme_raw_name=r[4],
            txn_date=date.fromisoformat(r[5]),
            txn_seq=r[6],
            txn_type=r[7],
            units=r[8], nav=r[9], amount=r[10],
            stamp_duty=r[11], stt=r[12], exit_load=r[13],
            units_balance_rep=r[14],
            switch_group_id=r[15],
            reverses_txn_ref=r[16],
        )
        for r in rows
    ]


def drop_derived(conn: sqlite3.Connection) -> None:
    """Drop every derived table. CLAUDE.md invariant 10.

    A real DROP, not a DELETE: the invariant is that these tables are
    reconstructible, and deleting rows would leave the schema behind and prove
    only half of it. `apply_ledger_schema` recreates them.
    """
    for table in DERIVED_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()


def rebuild(
    conn: sqlite3.Connection,
    user_id: UserId,
    as_of: date,
    navs: dict[str, dict[date, Decimal]] | None = None,
    rebuilt_at: str | None = None,
) -> LotBook:
    """Recompute every derived table from `txn`. The only writer of them.

    `navs` is optional because the NAV-dependent fields of `position` —
    market value, unrealised P&L, portfolio weight — are the only things it
    supplies, and a rebuild with no market data is still a valid rebuild of
    everything the transactions determine. When it is absent those columns are
    NULL rather than zero: a position whose value is unknown is not a position
    worth nothing.
    """
    stamp = rebuilt_at or datetime.now(UTC).isoformat()
    txns = load_txns(conn, user_id)
    live = drop_reversed(txns)
    book = build_book(live)

    for table in DERIVED_TABLES:
        conn.execute(f"DELETE FROM {table}")

    _write_lots(conn, book, user_id, stamp)
    _write_consumptions(conn, book, user_id, stamp)
    results = reconcile_all(book, live, navs or {}, as_of)
    _write_positions(conn, book, live, results, user_id, as_of, navs, stamp)
    _write_reconciliations(conn, results, user_id, stamp)
    conn.commit()
    return book


def derived_fingerprint(conn: sqlite3.Connection) -> str:
    """Content hash of every derived table, straight out of the database.

    Excludes `rebuilt_at` and `checked_at` — two rebuilds of the same ledger at
    different times are the same rebuild. Everything else is included, in a
    fixed column and row order, so a changed value anywhere moves the hash.

    Reading through the `DECIMAL_TEXT` converter matters here: hashing the
    stored text would pass even if SQLite had rewritten a decimal as a REAL,
    which is the exact failure SZ-13 describes. Hashing `repr(Decimal)` means
    a value that came back as a float breaks the hash.
    """
    h = hashlib.sha256()
    for table in sorted(DERIVED_TABLES):
        columns = [
            r[1]
            for r in conn.execute(f"PRAGMA table_info({table})")
            if r[1] not in _TIMESTAMP_COLUMNS
        ]
        if not columns:
            continue
        selected = ", ".join(columns)
        rows = conn.execute(
            f"SELECT {selected} FROM {table} ORDER BY {selected}"
        ).fetchall()
        h.update(f"--{table}:{selected}\n".encode())
        for row in rows:
            h.update("|".join(_cell(v) for v in row).encode() + b"\n")
    return h.hexdigest()


def _cell(value: Any) -> str:
    """A value's identity for hashing, with its type. See `derived_fingerprint`."""
    if value is None:
        return "\x00"
    if isinstance(value, Decimal):
        return f"D:{value}"
    if isinstance(value, float):  # pragma: no cover - the SZ-13 failure
        return f"FLOAT:{value!r}"
    return f"{type(value).__name__}:{value}"


def _write_lots(
    conn: sqlite3.Connection, book: LotBook, user_id: UserId, stamp: str
) -> None:
    for lot in sorted(book.all_lots(), key=lambda x: x.lot_id):
        conn.execute(
            "INSERT INTO lot ("
            " lot_id, user_id, folio, scheme_id, open_txn_id, acquisition_date,"
            " book_date, units_original, units_remaining, cost_per_unit,"
            " cost_total, grandfathered_nav, origin, is_closed, rebuilt_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                lot.lot_id, str(user_id), lot.folio, str(lot.scheme_id),
                lot.open_txn_id, lot.acquisition_date.isoformat(),
                lot.book_date.isoformat(), lot.units_original, lot.units_remaining,
                lot.cost_per_unit, lot.cost_total, lot.grandfathered_nav,
                lot.origin, 1 if lot.is_closed else 0, stamp,
            ),
        )


def _write_consumptions(
    conn: sqlite3.Connection, book: LotBook, user_id: UserId, stamp: str
) -> None:
    for c in sorted(
        book.all_consumptions(), key=lambda x: (x.close_txn_id, x.sequence_in_txn)
    ):
        sale_date = c.acquisition_date + _days(c.holding_days)
        conn.execute(
            "INSERT INTO lot_consumption ("
            " consumption_id, user_id, lot_id, close_txn_id, units_consumed,"
            " sale_nav, proceeds_gross, proceeds_net, cost_allocated,"
            " cost_basis_method, holding_days, gain_type, gain_amount,"
            " tax_class_at_sale, tax_rule_id, fy, sequence_in_txn, rebuilt_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _consumption_id(c.lot_id, c.close_txn_id, c.sequence_in_txn),
                str(user_id), c.lot_id, c.close_txn_id, c.units_consumed,
                c.sale_nav, c.proceeds_gross, c.proceeds_net, c.cost_allocated,
                c.cost_basis_method, c.holding_days, c.gain_type, c.gain_amount,
                c.tax_class_at_sale, UNVERSIONED_TAX_RULE, _fy(sale_date),
                c.sequence_in_txn, stamp,
            ),
        )


def _write_positions(
    conn: sqlite3.Connection,
    book: LotBook,
    txns: list[Txn],
    results: list[Any],
    user_id: UserId,
    as_of: date,
    navs: dict[str, dict[date, Decimal]] | None,
    stamp: str,
) -> None:
    by_key = {(r.folio, str(r.scheme_id)): r for r in results}
    keys = sorted({(lot.folio, str(lot.scheme_id)) for lot in book.all_lots()})

    for folio, scheme_id in keys:
        lots = [
            x for x in book.all_lots()
            if x.folio == folio and str(x.scheme_id) == scheme_id
        ]
        units = sum((x.units_remaining for x in lots if not x.is_closed), Decimal(0))
        cost = sum((x.cost_remaining for x in lots if not x.is_closed), Decimal(0))
        scoped = [
            t for t in txns if t.folio == folio and str(t.scheme_id) == scheme_id
        ]
        inflow = [t for t in scoped if (t.units or Decimal(0)) > 0]
        realised = sum(
            (
                c.gain_amount
                for c in book.all_consumptions()
                if any(x.lot_id == c.lot_id for x in lots)
            ),
            Decimal(0),
        )
        nav, nav_date = _nav_on_or_before(navs, scheme_id, as_of)
        recon = by_key.get((folio, scheme_id))
        conn.execute(
            "INSERT INTO position ("
            " user_id, folio, scheme_id, as_of, units, nav, nav_date,"
            " market_value, invested_gross, invested_net, avg_cost_nav,"
            " unrealised_pnl, realised_pnl_todate, first_purchase, last_purchase,"
            " reconciled, reconcile_delta_units, confidence, rebuilt_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(user_id), folio, scheme_id, as_of.isoformat(), units,
                nav, nav_date.isoformat() if nav_date else None,
                (units * nav) if nav is not None else None,
                sum((t.amount or Decimal(0) for t in inflow), Decimal(0)),
                sum((t.amount or Decimal(0) for t in scoped), Decimal(0)),
                (cost / units) if units else None,
                (units * nav - cost) if nav is not None else None,
                realised,
                min((t.txn_date for t in inflow), default=None) and
                min(t.txn_date for t in inflow).isoformat(),
                max((t.txn_date for t in inflow), default=None) and
                max(t.txn_date for t in inflow).isoformat(),
                1 if recon and recon.status == "ok" else 0,
                recon.delta_units if recon else None,
                recon.confidence if recon else "low",
                stamp,
            ),
        )


def _write_reconciliations(
    conn: sqlite3.Connection, results: list[Any], user_id: UserId, stamp: str
) -> None:
    for r in sorted(results, key=lambda x: (x.folio, str(x.scheme_id))):
        conn.execute(
            "INSERT INTO reconciliation ("
            " recon_id, user_id, folio, scheme_id, as_of, units_computed,"
            " units_reported, delta_units, delta_value_pct, status, diagnosis,"
            " checked_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _recon_id(str(user_id), r.folio, str(r.scheme_id), r.as_of),
                str(user_id), r.folio, str(r.scheme_id), r.as_of.isoformat(),
                r.units_computed, r.units_reported, r.delta_units,
                r.delta_value_pct, r.status, json.dumps(r.diagnosis), stamp,
            ),
        )


def _nav_on_or_before(
    navs: dict[str, dict[date, Decimal]] | None, scheme_id: str, on: date
) -> tuple[Decimal | None, date | None]:
    series = (navs or {}).get(scheme_id)
    if not series:
        return None, None
    usable = [d for d in series if d <= on]
    if not usable:
        return None, None
    latest = max(usable)
    return series[latest], latest


def _days(count: int) -> Any:
    from datetime import timedelta

    return timedelta(days=count)


def _fy(on: date) -> str:
    """India's financial year runs April to March. `2026-27`."""
    start = on.year if on.month >= 4 else on.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def _consumption_id(lot_id: str, close_txn_id: str, sequence: int) -> str:
    """Deterministic, so a rebuild reproduces the same primary keys."""
    return hashlib.sha256(f"{lot_id}|{close_txn_id}|{sequence}".encode()).hexdigest()


def _recon_id(user_id: str, folio: str, scheme_id: str, as_of: date) -> str:
    return hashlib.sha256(
        f"{user_id}|{folio}|{scheme_id}|{as_of.isoformat()}".encode()
    ).hexdigest()
