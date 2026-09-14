"""Storing the look-through. MODULE_3.md §4.2, §4.6, §5.5, §14.

The engine is a pure function; this is the only part of M3 touching a database,
so the arithmetic is tested without a connection and the storage without
re-deriving anything.

**These tables are derived, and that changes the write rule.** Invariant 2
forbids updating a fact row, but nothing here is a fact anyone stated — it is
recomputed from `txn` and `scheme_issuer_weight`. So a rebuild REPLACES;
appending would double-count on the second run, invisibly.

**Every aggregation is a Python `Decimal`** (invariant 1): the
contribution-to-exposure reconciliation is the obvious place for
`SUM(exposure_inr) GROUP BY issuer_id`, and it is not used.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from src.common.types import IssuerId, SchemeId, UserId
from src.m3_lookthrough.engine import (
    NO_DISCLOSURE,
    Exposure,
    LookThroughResult,
    PortfolioSummary,
)

#: `MODULE_6.md` states 45. It is defined HERE because `MODULE_3.md` §14.1 uses
#: it without declaring it, and M3 cannot import from M6 — the dependency runs
#: M3 -> M6, never back. M6 should import this one rather than keep its own.
STALENESS_WARN_DAYS = 45

#: The only basis that exists until `security_price` is built (§3.2). Stored
#: rather than assumed, so drift-adjusted rows can land beside these instead of
#: overwriting them — the primary key already separates them.
DISCLOSED = "disclosed"

TABLES = ("lookthrough_exposure", "lookthrough_contribution", "portfolio_summary")


def confidence_for(
    coverage_pct: Decimal, unresolved_pct: Decimal, worst_staleness_days: int | None
) -> str:
    """§14.1, verbatim, with one addition §14.1 does not cover.

    §14.2 rule 3 makes portfolio confidence *"the weakest link, not an
    average"*, so any one condition failing drops the whole result a level.

    **`None` means staleness is unknown, and unknown is not fresh.**
    `max(..., default=0)` turned it into "zero days old", so a portfolio whose
    age nobody knew scored `high` beside stored rows whose `staleness_days`
    were correctly NULL.
    """
    if (
        coverage_pct >= Decimal(98)
        and unresolved_pct <= Decimal(2)
        and worst_staleness_days is not None
        and worst_staleness_days <= STALENESS_WARN_DAYS
    ):
        return "high"
    return "medium" if coverage_pct >= Decimal(80) else "low"


def save_lookthrough(
    conn: sqlite3.Connection,
    user_id: UserId,
    as_of: date,
    result: LookThroughResult,
    as_of_by_scheme: dict[SchemeId, date] | None = None,
    weight_basis: str = DISCLOSED,
    computed_at: str | None = None,
) -> int:
    """Write §4.2's two tables and §4.6's summary. Returns exposure rows written.

    `as_of_by_scheme` carries each scheme's own disclosure date, because §5.5
    lets every scheme contribute from *its* latest disclosure. Absent, staleness
    is stored NULL rather than guessed — an unknown date is not today's.
    """
    dates = as_of_by_scheme or {}
    stamp = computed_at or datetime.now(UTC).isoformat()

    # §5.5: per issuer, the EARLIEST contributing disclosure date.
    earliest: dict[IssuerId, date] = {}
    for contribution in result.contributions:
        found = dates.get(contribution.scheme_id)
        if found is None:
            continue
        current = earliest.get(contribution.issuer_id)
        if current is None or found < current:
            earliest[contribution.issuer_id] = found

    ages = [(as_of - d).days for d in earliest.values()]
    if any(age < 0 for age in ages):
        # §5.5 says each scheme contributes from its latest disclosure ON OR
        # BEFORE `as_of`. A negative age means the caller paired a look-through
        # date with holdings from after it, and `CLAUDE.md` invariant 5 says
        # raise rather than clamp — a silently negative staleness reads as
        # fresher than fresh and scores `high`.
        raise ValueError(
            f"disclosure dated after the look-through date {as_of}: "
            f"{sorted(d for d in earliest.values() if (as_of - d).days < 0)}. "
            f"Pass `on_or_before=as_of` to weights.latest_as_of."
        )
    worst_staleness = max(ages) if ages else None
    summary = result.summary
    confidence = confidence_for(
        summary.coverage_pct, summary.unresolved_pct, worst_staleness
    )

    # Derived, so a rebuild replaces. `portfolio_summary` has no `weight_basis`
    # column — its key is (user, as_of) — so it is cleared without one.
    for table in TABLES:
        if table == "portfolio_summary":
            conn.execute(
                f"DELETE FROM {table} WHERE user_id = ? AND as_of = ?",
                (str(user_id), as_of.isoformat()),
            )
        else:
            conn.execute(
                f"DELETE FROM {table}"
                " WHERE user_id = ? AND as_of = ? AND weight_basis = ?",
                (str(user_id), as_of.isoformat(), weight_basis),
            )

    for exposure in result.exposures:
        holdings_as_of = earliest.get(exposure.issuer_id)
        conn.execute(
            "INSERT INTO lookthrough_exposure ("
            " user_id, as_of, weight_basis, issuer_id, exposure_inr, exposure_pct,"
            " via_funds, fund_inr, direct_inr, instrument_class, is_synthetic,"
            " holdings_as_of, staleness_days, coverage_pct, confidence, computed_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(user_id), as_of.isoformat(), weight_basis, str(exposure.issuer_id),
                exposure.exposure_inr, exposure.pct_of_portfolio,
                exposure.fund_count,
                # No `direct_holding` yet (§7), so all of it comes through funds.
                exposure.exposure_inr, Decimal(0),
                exposure.instrument_class, 1 if exposure.is_synthetic else 0,
                holdings_as_of.isoformat() if holdings_as_of else None,
                (as_of - holdings_as_of).days if holdings_as_of else None,
                summary.coverage_pct, confidence, stamp,
            ),
        )

    # Hoisted: `_position_value` scanned every contribution, once per
    # contribution, which is O(n^2) — ~16M comparisons for a 20-fund portfolio
    # inside an open transaction. The totals are loop-invariant.
    position_values = _position_values(result)
    for contribution in result.contributions:
        position_value = position_values.get(contribution.scheme_id, Decimal(0))
        conn.execute(
            "INSERT INTO lookthrough_contribution ("
            " user_id, as_of, weight_basis, issuer_id, scheme_id, depth,"
            " weight_in_fund, exposure_inr"
            ") VALUES (?,?,?,?,?,0,?,?)",
            (
                str(user_id), as_of.isoformat(), weight_basis,
                str(contribution.issuer_id), str(contribution.scheme_id),
                (
                    contribution.exposure_inr / position_value * 100
                    if position_value
                    else Decimal(0)
                ),
                contribution.exposure_inr,
            ),
        )

    conn.execute(
        "INSERT INTO portfolio_summary ("
        " user_id, as_of, total_value_inr, fund_value_inr, direct_value_inr,"
        " scheme_count, issuer_count, direct_issuer_count, coverage_pct,"
        " schemes_covered, unresolved_pct, worst_staleness_days, confidence,"
        " caveats, computed_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(user_id), as_of.isoformat(), summary.total_value_inr,
            summary.total_value_inr, Decimal(0),
            len({c.scheme_id for c in result.contributions}),
            summary.issuer_count, 0, summary.coverage_pct,
            len(
                {
                    c.scheme_id
                    for c in result.contributions
                    if c.issuer_id != NO_DISCLOSURE
                }
            ),
            summary.unresolved_pct, worst_staleness, confidence,
            json.dumps(result.caveats), stamp,
        ),
    )
    conn.commit()
    return len(result.exposures)


def load_exposures(
    conn: sqlite3.Connection,
    user_id: UserId,
    as_of: date,
    weight_basis: str = DISCLOSED,
) -> list[Exposure]:
    """Read them back largest first, which is the order every view wants.

    **Sorted in Python.** `ORDER BY exposure_inr DESC` on a `DECIMAL_TEXT`
    column sorts as TEXT, so `"5000"` precedes `"25000"`. The column is text by
    design (SZ-13), the SQL looks correct, and the wrong order is invisible
    until someone reads a top-20 list that is not the top 20 (invariant 1).

    §4.2's `ix_lte_size` index is declared on `exposure_inr DESC` and has the
    same problem; kept for the equality part of the key, never trusted for
    ordering.
    """
    rows = conn.execute(
        "SELECT issuer_id, exposure_inr, exposure_pct, instrument_class,"
        " is_synthetic, via_funds FROM lookthrough_exposure"
        " WHERE user_id = ? AND as_of = ? AND weight_basis = ?",
        (str(user_id), as_of.isoformat(), weight_basis),
    ).fetchall()
    rows.sort(key=lambda r: (-r[1], str(r[0])))
    return [
        Exposure(
            issuer_id=IssuerId(r[0]),
            exposure_inr=r[1],
            pct_of_portfolio=r[2],
            instrument_class=r[3] or "unknown",
            is_synthetic=bool(r[4]),
            fund_count=r[5],
        )
        for r in rows
    ]


def load_summary(
    conn: sqlite3.Connection, user_id: UserId, as_of: date
) -> StoredSummary:
    """§4.6, with the fields M3 fills. The rest are NULL until M1/M2 feed them."""
    row = conn.execute(
        "SELECT total_value_inr, coverage_pct, unresolved_pct, issuer_count,"
        " worst_staleness_days, confidence, caveats FROM portfolio_summary"
        " WHERE user_id = ? AND as_of = ?",
        (str(user_id), as_of.isoformat()),
    ).fetchone()
    if row is None:
        raise LookupError(f"no portfolio_summary for {user_id} at {as_of}")
    return StoredSummary(
        total_value_inr=row[0],
        coverage_pct=row[1],
        unresolved_pct=row[2],
        issuer_count=row[3],
        worst_staleness_days=row[4],
        confidence=row[5],
        caveats=json.loads(row[6]) if row[6] else [],
    )


def drop_lookthrough(conn: sqlite3.Connection) -> None:
    """Drop every derived look-through table. `CLAUDE.md` invariant 10.

    A real DROP rather than a DELETE, for the reason V1-13 gave: the claim is
    that these are reconstructible, and deleting rows leaves the schema behind
    and proves half of it.
    """
    for table in TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    # DROP is DDL, so this commit is a NO-OP under `sqlite3`'s legacy
    # isolation: a transaction opens before DML only, and the DROP above
    # already left `in_transaction` False. Measured, and it is why no test
    # holds this line where one holds every other commit in `src/`.
    #
    # Kept rather than deleted: it stops being a no-op the moment this
    # connection is opened with `isolation_level=None` or 3.12's
    # `autocommit`, and a reader who found a bare DROP with no commit
    # beside three functions that do commit would reasonably add one back.
    conn.commit()


def _position_values(result: LookThroughResult) -> dict[SchemeId, Decimal]:
    """What each scheme was worth, recovered from its own contributions.

    One pass over the contributions instead of one pass per contribution.
    Python `Decimal`, not `SUM` — invariant 1. The value is not carried on the
    result, and re-deriving it from the rows that came from it keeps
    `weight_in_fund` consistent with `exposure_inr` by construction.
    """
    totals: dict[SchemeId, Decimal] = defaultdict(Decimal)
    for contribution in result.contributions:
        totals[contribution.scheme_id] += contribution.exposure_inr
    return totals


@dataclass(frozen=True)
class StoredSummary:
    """The subset of §4.6 that M3 can fill today."""

    total_value_inr: Decimal
    coverage_pct: Decimal
    unresolved_pct: Decimal
    issuer_count: int
    worst_staleness_days: int | None
    confidence: str
    caveats: list[str]


__all__ = [
    "DISCLOSED",
    "STALENESS_WARN_DAYS",
    "PortfolioSummary",
    "StoredSummary",
    "confidence_for",
    "drop_lookthrough",
    "load_exposures",
    "load_summary",
    "save_lookthrough",
]
