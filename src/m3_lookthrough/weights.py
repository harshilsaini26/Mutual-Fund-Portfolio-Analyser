"""`scheme_issuer_weight` materialisation. MODULE_0.md §9.3.

Collapses a scheme's holdings to one row per issuer — the exposure unit is the
issuer, not the ISIN (MODULE_3.md §2.3).

Aggregated in Python, never `SUM(pct_normalised)`: CLAUDE.md invariant 1 —
SQLite coerces a text-affinity column to float and every weight downstream
would inherit it.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal

from src.common.types import IssuerId, SchemeId
from src.m0_data.derive.scheme_family import disclosure_scheme_for
from src.m3_lookthrough.engine import IssuerWeight
from src.m3_lookthrough.persist import STALENESS_WARN_DAYS

#: Tier of a disclosure read from the AMC's own statutory file; migration 011's
#: default, so a warehouse written before `source_tier` existed reads as this.
SOURCE_OF_RECORD = "amc_direct"


def current_holdings(
    conn: sqlite3.Connection, scheme_id: SchemeId, as_of: date
) -> list[tuple[str, Decimal, str, Decimal | None]]:
    """The current revision's holdings: (issuer_id, pct_normalised, class, qty).

    Resolved to the share-class family first: a disclosure describes a scheme,
    not an ISIN, and Direct and Regular hold one pool of assets (V1-37).
    """
    source = SchemeId(disclosure_scheme_for(conn, str(scheme_id), as_of.isoformat()))
    return [
        (r[0], r[1], r[2], r[3])
        for r in conn.execute(
            "SELECT h.issuer_id, h.pct_normalised, h.instrument_class, h.quantity"
            " FROM holding h"
            " JOIN holding_disclosure d"
            "   ON d.scheme_id = h.scheme_id AND d.as_of_date = h.as_of_date"
            "  AND d.revision = h.revision"
            " WHERE h.scheme_id = ? AND h.as_of_date = ? AND d.is_current = 1",
            (str(source), as_of),
        )
    ]


def materialise_weights(
    conn: sqlite3.Connection, scheme_id: SchemeId, as_of: date
) -> int:
    """One row per issuer for this scheme-date. Returns rows written.

    **Does not commit** — the caller owns the transaction. Committing per
    scheme cost 0.86s of a 1.92s run across 192 schemes, one fsync each.

    `weight_drift_adj` stays NULL: §9.3's drift basis needs `security_price`,
    which is not built, and copying the disclosed weight into it would be a
    second basis that silently is not one.
    """
    rows = current_holdings(conn, scheme_id, as_of)
    if not rows:
        return 0

    disclosed: dict[str, Decimal] = defaultdict(Decimal)
    quantity: dict[str, Decimal] = defaultdict(Decimal)
    has_quantity: set[str] = set()
    klass: dict[str, str] = {}
    largest: dict[str, Decimal] = {}

    for issuer_id, pct, instrument_class, qty in rows:
        disclosed[issuer_id] += pct
        if qty is not None:
            quantity[issuer_id] += qty
            has_quantity.add(issuer_id)
        # Largest holding's class, not the last row's, so an issuer held as both
        # equity and debt cannot flip with sheet order. Compared on ABSOLUTE
        # weight: a short leg's pct is negative, and an issuer held only short
        # still needs a class (V1-07).
        if issuer_id not in largest or abs(pct) > abs(largest[issuer_id]):
            largest[issuer_id] = pct
            klass[issuer_id] = instrument_class

    # DELETE then insert, not `INSERT OR REPLACE` alone: replace leaves behind
    # any issuer the new revision dropped, and the scheme's weights then sum
    # to 120. These rows are derived, so rebuilding replaces the set.
    conn.execute(
        "DELETE FROM scheme_issuer_weight WHERE scheme_id = ? AND as_of_date = ?",
        (str(scheme_id), as_of),
    )
    stamp = datetime.now(UTC).isoformat()
    conn.executemany(
        "INSERT INTO scheme_issuer_weight ("
        " scheme_id, as_of_date, issuer_id, weight_disclosed, weight_drift_adj,"
        " instrument_class, quantity, built_at"
        ") VALUES (?,?,?,?,NULL,?,?,?)",
        [
            (
                str(scheme_id),
                as_of,
                issuer_id,
                weight,
                klass[issuer_id],
                quantity[issuer_id] if issuer_id in has_quantity else None,
                stamp,
            )
            for issuer_id, weight in disclosed.items()
        ],
    )
    return len(disclosed)


def load_issuer_weights(
    conn: sqlite3.Connection, scheme_id: SchemeId, as_of: date
) -> list[IssuerWeight]:
    """Read the materialised weights back, ready for `compute_lookthrough`."""
    return [
        IssuerWeight(IssuerId(r[0]), r[1], r[2])
        for r in conn.execute(
            "SELECT issuer_id, weight_disclosed, instrument_class"
            " FROM scheme_issuer_weight WHERE scheme_id = ? AND as_of_date = ?"
            " ORDER BY issuer_id",
            (str(scheme_id), as_of),
        )
    ]


def latest_disclosure(
    conn: sqlite3.Connection, scheme_id: SchemeId, on_or_before: date | None = None
) -> tuple[date, str] | None:
    """The disclosure a scheme contributes from, and the tier it came from.

    §5.5 says "latest on or before `as_of`", which was enough while every
    disclosure came from an AMC's own file. The coverage tier reads an
    aggregator page with no ISIN column: same fund, same month, 0.00%
    unresolved from the workbook against 19-23% from the page (V1-43).

    So the AMC's file wins while it is not itself stale, by
    `STALENESS_WARN_DAYS` — measured against the newest CANDIDATE, never the
    caller's bound or the clock, which would make the answer a fact about when
    you asked rather than about the data (V1-47). Here rather than in the
    writers, because every reader goes through this query.
    """
    sql = (
        "SELECT as_of_date, source_tier FROM holding_disclosure"
        " WHERE scheme_id = ? AND is_current = 1"
    )
    # Same family resolution as `current_holdings`, or a share class reports a
    # date and then returns no holdings.
    params: list[object] = [
        disclosure_scheme_for(
            conn, str(scheme_id), on_or_before.isoformat() if on_or_before else None
        )
    ]
    if on_or_before is not None:
        sql += " AND as_of_date <= ?"
        params.append(on_or_before)

    # Ordered in Python: the tie-break below is not expressible as an ORDER BY,
    # and splitting the decision across two languages is how it gets lost.
    found = [
        (
            r[0] if isinstance(r[0], date) else date.fromisoformat(str(r[0])),
            str(r[1] or SOURCE_OF_RECORD),
        )
        for r in conn.execute(sql, tuple(params))
    ]
    if not found:
        return None

    newest, newest_tier = max(found, key=lambda pair: pair[0])
    if newest_tier == SOURCE_OF_RECORD:
        return newest, newest_tier

    of_record = [pair for pair in found if pair[1] == SOURCE_OF_RECORD]
    if not of_record:
        return newest, newest_tier

    best, tier = max(of_record, key=lambda pair: pair[0])
    if (newest - best).days <= STALENESS_WARN_DAYS:
        return best, tier
    return newest, newest_tier


def latest_as_of(
    conn: sqlite3.Connection, scheme_id: SchemeId, on_or_before: date | None = None
) -> date | None:
    """§5.5's date alone, for callers that do not care which tier it came from."""
    found = latest_disclosure(conn, scheme_id, on_or_before)
    return found[0] if found else None
