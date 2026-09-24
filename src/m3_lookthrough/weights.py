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
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal

from src.common.types import IssuerId, SchemeId
from src.m0_data.derive.scheme_family import (
    disclosed_scheme_ids,
    disclosure_scheme_for,
)
from src.m3_lookthrough.engine import IssuerWeight
from src.m3_lookthrough.persist import STALENESS_WARN_DAYS

#: Tier of a disclosure read from the AMC's own statutory file; migration 011's
#: default, so a warehouse written before `source_tier` existed reads as this.
SOURCE_OF_RECORD = "amc_direct"


def current_holdings(
    conn: sqlite3.Connection, scheme_id: SchemeId, as_of: date
) -> list[tuple[str, Decimal, str, Decimal | None, str | None]]:
    """The current revision's holdings: (issuer_id, pct, class, qty, isin).

    `isin` is carried for one reason: on a `__MFUNIT__` row it names the fund
    being held, and that is the only handle fund-of-funds expansion has. It
    does not survive `materialise_weights` — `scheme_issuer_weight` has no
    isin column — so anything nested has to happen before the collapse.

    Resolved to the share-class family first: a disclosure describes a scheme,
    not an ISIN, and Direct and Regular hold one pool of assets (V1-37).

    **No ORDER BY, deliberately.** One was added here to make the caller's
    class tie-break deterministic and then removed: it put an ordering contract
    on shared read infrastructure for one consumer's benefit, contradicting the
    rule `latest_disclosure` states below, and `EXPLAIN QUERY PLAN` showed it
    buying a `USE TEMP B-TREE FOR ORDER BY` for an order the primary-key index
    already supplied. The tie-break is a total key instead — see
    `materialise_weights`.
    """
    source = SchemeId(disclosure_scheme_for(conn, str(scheme_id), as_of.isoformat()))
    return [
        (r[0], r[1], r[2], r[3], r[4])
        for r in conn.execute(
            "SELECT h.issuer_id, h.pct_normalised, h.instrument_class, h.quantity,"
            " h.isin"
            " FROM holding h"
            " JOIN holding_disclosure d"
            "   ON d.scheme_id = h.scheme_id AND d.as_of_date = h.as_of_date"
            "  AND d.revision = h.revision"
            " WHERE h.scheme_id = ? AND h.as_of_date = ? AND d.is_current = 1",
            (str(source), as_of),
        )
    ]


#: MODULE_3.md §6.3 rule 1. Beyond this the remaining exposure is bucketed
#: rather than expanded: real structures rarely nest deeper, and a longer chain
#: usually means bad data rather than a real holding.
MAX_FOF_DEPTH = 2

#: The synthetic issuer a unit of another fund resolves to.
MFUNIT = "__MFUNIT__"

Holding = tuple[str, Decimal, str, Decimal | None, str | None]


def expand_fund_units(
    conn: sqlite3.Connection,
    scheme_id: SchemeId,
    as_of: date,
    *,
    visited: frozenset[str] = frozenset(),
    depth: int = 0,
) -> list[Holding]:
    """This scheme's holdings with `__MFUNIT__` rows replaced by what they hold.

    MODULE_3.md §6. A fund-of-funds reports one opaque block, and without this
    the product does nothing at all for whoever holds one: of the 39 schemes
    carrying a `__MFUNIT__` bucket, six are 99-100% units and contribute zero
    real issuers.

    **Weights, not amounts.** §6.2 threads rupee amounts through the engine.
    Here the weights are materialised per scheme before `compute_lookthrough`
    runs, so a sub-weight scaled by its parent's weight is the same operation,
    scale-invariant, and the engine needs no change. A holding of weight `w`
    is replaced by rows summing to `w`, so closure is untouched.

    **No provider.** §6.2 calls `ltp.recursion_path()` and
    `ltp.recursion_guard()` on a provider nothing ever implemented (V1-73
    deleted its Protocol). `visited` is the same cycle guard in one argument.

    A unit stays bucketed, never guessed at, when any of these hold (§6.3, and
    invariant 4 -- 17 of the 53 funds held as units are in this state):

      - the row carries no ISIN, so there is nothing to resolve
      - the depth cap is reached
      - the target is already on the path, i.e. a cycle
      - the target has no current disclosure of its own
    """
    rows = current_holdings(conn, scheme_id, as_of)
    if depth >= MAX_FOF_DEPTH:
        return rows

    # Seeded with the scheme being expanded, so a fund holding ITSELF is
    # caught by the same check as a longer cycle. Without it the self-holding
    # expands one level before the guard sees it and reports 24% of its own
    # equity twice over.
    visited = visited | {str(scheme_id)}

    out: list[Holding] = []
    for row in rows:
        issuer_id, pct, _klass, _qty, isin = row
        if issuer_id != MFUNIT or not isin or isin in visited:
            out.append(row)
            continue

        target = SchemeId(isin)
        target_as_of = latest_as_of(conn, target)
        if target_as_of is None:
            out.append(row)
            continue

        inner = expand_fund_units(
            conn,
            target,
            target_as_of,
            visited=visited | {isin},
            depth=depth + 1,
        )
        if not inner:
            out.append(row)
            continue

        # Scaled by this holding's own weight, so the replacement sums to what
        # it replaced. Quantity is dropped: units of the underlying fund are
        # not units of its holdings, and carrying the inner count up would be
        # a number with no meaning at this level.
        for in_issuer, in_pct, in_klass, _in_qty, in_isin in inner:
            out.append((in_issuer, in_pct * pct / 100, in_klass, None, in_isin))

    return out


def materialise_weights(
    conn: sqlite3.Connection, scheme_id: SchemeId, as_of: date
) -> int:
    """One row per issuer for this scheme-date. Returns rows written.

    **Does not commit** — the caller owns the transaction. Committing per
    scheme cost 0.86s of a 1.92s run across 192 schemes, one fsync each.

    That also makes a rebuild ATOMIC rather than incremental: a failure part-way
    loses every scheme done so far, where the old per-scheme commit kept them.
    Cheap here because the whole loop is under a second and this function is
    idempotent, but a caller that runs it over a much larger set inherits the
    trade. `rebuild_weights` below is the supported way to drive it.

    `weight_drift_adj` stays NULL: §9.3's drift basis needs `security_price`,
    which is not built, and copying the disclosed weight into it would be a
    second basis that silently is not one.
    """
    rows = expand_fund_units(conn, scheme_id, as_of)
    if not rows:
        return 0

    disclosed: dict[str, Decimal] = defaultdict(Decimal)
    quantity: dict[str, Decimal] = defaultdict(Decimal)
    has_quantity: set[str] = set()
    klass: dict[str, str] = {}
    largest: dict[str, tuple[Decimal, str]] = {}

    for issuer_id, pct, instrument_class, qty, _isin in rows:
        disclosed[issuer_id] += pct
        if qty is not None:
            quantity[issuer_id] += qty
            has_quantity.add(issuer_id)
        # Largest holding's class, not the last row's, so an issuer held as both
        # equity and debt cannot flip with sheet order. Compared on ABSOLUTE
        # weight: a short leg's pct is negative, and an issuer held only short
        # still needs a class (V1-07).
        #
        # A TOTAL key, so an exact tie does not fall through to whatever order
        # the rows arrived in. `(abs, class)` rather than `abs` alone is what
        # lets `current_holdings` stay an unordered read: the alternative was an
        # ORDER BY there, which put this function's contract inside another
        # function's SQL with only prose joining them.
        key = (abs(pct), instrument_class)
        if issuer_id not in largest or key > largest[issuer_id]:
            largest[issuer_id] = key
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

    **A quarantined disclosure is never a candidate** (MODULE_3 §5.4, V1-66). It
    failed a check that decides whether its numbers can be believed — V2's AUM
    witness is how a 100x unit error is caught — so the scheme uses its last
    disclosure that passed, whose age then shows as staleness, or none at all
    and its value shows as `__NO_DISCLOSURE__`. A warning does not block.
    """
    sql = (
        "SELECT as_of_date, source_tier FROM holding_disclosure"
        " WHERE scheme_id = ? AND is_current = 1"
        " AND validation_status <> 'quarantined'"
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


def rebuild_weights(
    conn: sqlite3.Connection,
) -> tuple[dict[SchemeId, list[IssuerWeight]], dict[SchemeId, date]]:
    """Re-materialise every disclosed scheme's weights, and COMMIT once.

    Here rather than inline in `scripts/show_lookthrough.py`'s `main()` because
    the commit is the only thing that persists the rebuild and it had no owner
    a test could name. **Not because a script is unreachable** — this suite
    already imports from `jobs/` and `scripts/` in four places, and the earlier
    version of this docstring said otherwise. What hid the defect was that
    every test read back on the connection that wrote, where uncommitted rows
    are visible either way; see `tests/conftest.py`.

    Rebuilt unconditionally, not "only if absent": guarding on absence meant a
    restated disclosure never refreshed its weights and every later report used
    the withdrawn revision. Replacing the set is idempotent.

    It commits, so it cannot be composed into a caller's larger transaction.
    That is a real cost and `PROGRESS.md` carries it: the module that owns M3's
    other commits is `persist.py`, and this arguably belongs there or in a job.
    """
    weights: dict[SchemeId, list[IssuerWeight]] = {}
    as_ofs: dict[SchemeId, date] = {}
    for found_id in disclosed_scheme_ids(conn):
        scheme_id = SchemeId(found_id)
        as_of = latest_as_of(conn, scheme_id)
        if as_of is None:
            # Nothing usable: every current disclosure is quarantined (V1-66).
            # Its old weights go too, or the set is not replaced and a reader
            # that skips `latest_disclosure` finds the failed figures.
            conn.execute(
                "DELETE FROM scheme_issuer_weight WHERE scheme_id = ?", (found_id,)
            )
            continue
        materialise_weights(conn, scheme_id, as_of)
        found = load_issuer_weights(conn, scheme_id, as_of)
        if found:
            weights[scheme_id] = found
            as_ofs[scheme_id] = as_of
    conn.commit()
    # Keyed as well by every share class a disclosure serves (V1-37), since the
    # engine reads a holding by the one HELD: here, so no caller has to remember.
    members = [
        SchemeId(r[0])
        for r in conn.execute(
            "SELECT DISTINCT s.scheme_id FROM scheme s JOIN scheme d"
            "   ON d.amc_id = s.amc_id AND d.scheme_family = s.scheme_family"
            " JOIN holding_disclosure h ON h.scheme_id = d.scheme_id AND h.is_current = 1"
            " ORDER BY s.scheme_id"
        )
    ]
    return served(conn, weights, as_ofs, members)


def served(
    conn: sqlite3.Connection,
    weights: dict[SchemeId, list[IssuerWeight]],
    as_ofs: dict[SchemeId, date],
    held: Iterable[SchemeId],
) -> tuple[dict[SchemeId, list[IssuerWeight]], dict[SchemeId, date]]:
    """`rebuild_weights`'s result, plus each held share class its fund's weights.

    Weights are keyed by the share class that DISCLOSED; the engine reads them
    by the one HELD. So V1-37's family reached the lookup but never a holder: a
    Regular plan of a fund disclosed against its Direct plan -- HDFC Flexi Cap
    among them -- was booked entirely to `__NO_DISCLOSURE__`.
    """
    weights, as_ofs = dict(weights), dict(as_ofs)
    for scheme_id in held:
        source = SchemeId(disclosure_scheme_for(conn, str(scheme_id)))
        if scheme_id not in weights and source in weights:
            weights[scheme_id], as_ofs[scheme_id] = weights[source], as_ofs[source]
    return weights, as_ofs
