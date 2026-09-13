"""`scheme_issuer_weight` materialisation. MODULE_0.md §9.3.

Collapses a scheme's current holdings — several instruments of one issuer — to
one row per issuer. `MODULE_3.md` §2.3: *"the exposure unit is the issuer, not
the ISIN"*, because a fund holding Reliance equity and another holding a
Reliance NCD are the same corporate exposure.

**The aggregation is in Python, deliberately.** `SUM(pct_normalised)` in SQL
would be the obvious way to write this and it is forbidden by `CLAUDE.md`
invariant 1: SQLite coerces to float over a text-affinity column, and every
weight downstream — the closure, the concentration denominator, the overlap —
would inherit a float. This is the exact function where that would have
happened.

No drift-adjusted basis: §9.3 computes one by repricing at a `price_date`, and
`security_price` is not built. The column is stored NULL rather than filled with
a copy of the disclosed weight, which would be a second basis that silently is
not one.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal

from src.common.types import IssuerId, SchemeId
from src.m0_data.derive.scheme_family import disclosure_scheme_for
from src.m3_lookthrough.engine import IssuerWeight

#: The tier a disclosure read from the AMC's own statutory file carries, and
#: migration 011's default -- so a warehouse written before `source_tier`
#: existed reads as what it is rather than as an unknown.
SOURCE_OF_RECORD = "amc_direct"


def current_holdings(
    conn: sqlite3.Connection, scheme_id: SchemeId, as_of: date
) -> list[tuple[str, Decimal, str, Decimal | None]]:
    """The current revision's holdings: (issuer_id, pct_normalised, class, qty).

    `is_current` is what makes this the *current* revision — `CLAUDE.md`
    invariant 2 appends a new revision rather than updating, so without the
    filter a restated disclosure would be counted twice.

    **The scheme is resolved to its share-class family first** (V1-37). A
    disclosure describes a scheme, not an ISIN: Direct and Regular, Growth and
    IDCW hold one pool of assets and differ only in fees and payout. Keying
    holdings on the single ISIN a file happened to be loaded against told a
    Regular-plan holder `__NO_DISCLOSURE__` for a portfolio already in the
    warehouse — and Regular is what distributors sell.
    """
    source = SchemeId(disclosure_scheme_for(conn, str(scheme_id), as_of.isoformat()))
    rows = conn.execute(
        "SELECT h.issuer_id, h.pct_normalised, h.instrument_class, h.quantity"
        " FROM holding h"
        " JOIN holding_disclosure d"
        "   ON d.scheme_id = h.scheme_id AND d.as_of_date = h.as_of_date"
        "  AND d.revision = h.revision"
        " WHERE h.scheme_id = ? AND h.as_of_date = ? AND d.is_current = 1",
        (str(source), as_of),
    ).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def materialise_weights(
    conn: sqlite3.Connection, scheme_id: SchemeId, as_of: date
) -> int:
    """§9.3. One row per issuer for this scheme-date. Returns rows written.

    Idempotent: `INSERT OR REPLACE` on the natural key, so re-running after a
    restatement overwrites rather than accumulating.
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
        # §9.3 writes `klass[issuer] = r.instrument_class`, i.e. last row wins.
        # Taking the largest holding's class instead makes the answer
        # independent of row order, which matters because an issuer held as
        # both equity and debt would otherwise flip with the sheet's ordering.
        #
        # Compared on ABSOLUTE weight, and `largest` is a plain dict rather than
        # one defaulting to zero. A short leg has a negative `pct_normalised`
        # (V1-07: HDFC writes Eternal Limited's short at -0.001%), so a
        # zero-default meant an issuer held ONLY short never cleared the bar,
        # never got a class, and raised KeyError below. Absolute size is also
        # the right comparison: a large short is the dominant position in that
        # issuer, not the smallest.
        if issuer_id not in largest or abs(pct) > abs(largest[issuer_id]):
            largest[issuer_id] = pct
            klass[issuer_id] = instrument_class

    stamp = datetime.now(UTC).isoformat()
    # Delete before insert, not `INSERT OR REPLACE` alone. Replace updates the
    # issuers the new revision still has and leaves behind any it dropped: a
    # restatement that sells a holding left the old row in place and the
    # scheme's weights summed to 120, which `assert_weights_sum_to_100` then
    # blamed on MODULE_0's normalise_weights. These rows are derived, so
    # rebuilding them means replacing the set, not merging into it.
    conn.execute(
        "DELETE FROM scheme_issuer_weight WHERE scheme_id = ? AND as_of_date = ?",
        (str(scheme_id), as_of),
    )
    for issuer_id, weight in disclosed.items():
        conn.execute(
            "INSERT INTO scheme_issuer_weight ("
            " scheme_id, as_of_date, issuer_id, weight_disclosed, weight_drift_adj,"
            " instrument_class, quantity, built_at"
            ") VALUES (?,?,?,?,NULL,?,?,?)",
            (
                str(scheme_id),
                as_of,
                issuer_id,
                weight,
                klass[issuer_id],
                quantity[issuer_id] if issuer_id in has_quantity else None,
                stamp,
            ),
        )
    conn.commit()
    return len(disclosed)


def load_issuer_weights(
    conn: sqlite3.Connection, scheme_id: SchemeId, as_of: date
) -> list[IssuerWeight]:
    """Read the materialised weights back, ready for `compute_lookthrough`."""
    rows = conn.execute(
        "SELECT issuer_id, weight_disclosed, instrument_class"
        " FROM scheme_issuer_weight WHERE scheme_id = ? AND as_of_date = ?"
        " ORDER BY issuer_id",
        (str(scheme_id), as_of),
    ).fetchall()
    return [IssuerWeight(IssuerId(r[0]), r[1], r[2]) for r in rows]


def latest_disclosure(
    conn: sqlite3.Connection, scheme_id: SchemeId, on_or_before: date | None = None
) -> tuple[date, str] | None:
    """The disclosure a scheme contributes from, and which tier it came from.

    §5.5 says *its latest disclosure on or before `as_of`*, and that was the
    whole rule while every disclosure came from an AMC's own file. V1-43 ended
    that: the coverage tier reads an aggregator's page, which carries no ISIN
    column, and four of the cascade's rules key on ISIN structure. Same fund,
    same month, measured — **0.00% unresolved from the workbook against 19-23%
    from the page.** The range is not vagueness: the page's figure falls as the
    entity master grows and the workbook's does not, because a name match
    improves with the master while an ISIN match was never waiting on it.

    So "latest" stopped being sufficient on its own. Taking `max(as_of_date)`
    moved PPFAS Flexi Cap onto the worse data the moment an August page was
    loaded beside a July workbook, and nothing downstream could see it happen.

    **The rule: the AMC's own file wins while it is not itself stale.**
    `STALENESS_WARN_DAYS` is the system's existing definition of too old — the
    same threshold `confidence_for` uses — so no new number is invented here.
    Inside it, better resolution beats fresher dates; outside it, the aggregator
    wins, because at that point age is the larger problem and `confidence_for`
    is already saying so through `unresolved_pct`. Both costs stay priced; only
    the choice between them moved here.

    It is here rather than in the job that writes the data because a guard in
    one writer is a guard the next writer has to remember. Every reader goes
    through this query.

    `on_or_before` implements §5.5's bound. Without it this returned the newest
    disclosure whatever its date, so a look-through computed for an earlier date
    would use holdings from the future and store a negative `staleness_days`
    that `confidence_for` reads as fresher than fresh.
    """
    from src.m3_lookthrough.persist import STALENESS_WARN_DAYS

    sql = (
        "SELECT as_of_date, source_tier FROM holding_disclosure"
        " WHERE scheme_id = ? AND is_current = 1"
    )
    # Same family resolution as `current_holdings`, and for the same reason:
    # asking whether a scheme has a disclosure has to mean the same thing as
    # asking for its holdings, or a share class reports a date and then returns
    # nothing.
    params: list[object] = [
        disclosure_scheme_for(
            conn, str(scheme_id), on_or_before.isoformat() if on_or_before else None
        )
    ]
    if on_or_before is not None:
        sql += " AND as_of_date <= ?"
        params.append(on_or_before)

    # Ordered in Python. `as_of_date` is a DATE column so SQL would sort it
    # correctly, but the tie-break below is not expressible as an ORDER BY and
    # splitting the decision across two languages is how it gets lost.
    found = [
        (
            r[0] if isinstance(r[0], date) else date.fromisoformat(str(r[0])),
            str(r[1] or SOURCE_OF_RECORD),
        )
        for r in conn.execute(sql, tuple(params)).fetchall()
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
    reference = on_or_before or newest
    if (reference - best).days <= STALENESS_WARN_DAYS:
        return best, tier
    return newest, newest_tier


def latest_as_of(
    conn: sqlite3.Connection, scheme_id: SchemeId, on_or_before: date | None = None
) -> date | None:
    """§5.5's date alone, for callers that do not care where it came from.

    Kept as the narrow entry point because every existing caller wants a date
    and widening them all to a tuple would be churn for nothing. A caller that
    needs to SAY which tier it is reporting asks `latest_disclosure`.
    """
    found = latest_disclosure(conn, scheme_id, on_or_before)
    return found[0] if found else None
