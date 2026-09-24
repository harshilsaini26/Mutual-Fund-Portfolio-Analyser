"""What M1 exposes for display. `MODULE_6.md` Appendix B, the M1 edge.

`MODULE_6.md` §1.3 rule 2 forbids M6 from reading `position` directly, so this is
the way in. It carries no lots or cashflows: M6 computes nothing — it needs a
row to put in a table — and loading every lot to render a holdings list would be
reaching for data with no use.

**Every column M1 has not filled comes back `None`, never zero.** `position` is
written by `rebuild()` and several of its columns wait on engines that have not
run: `unrealised_pnl` needs the lot engine's cost basis, `weight_in_portfolio`
needs the whole portfolio valued on one date. A NULL renders as an em dash
(`MODULE_6.md` §9.3) and says "not computed"; a zero would say "computed, and the
answer is nothing", which for a P&L figure is a materially different claim.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.common.types import SchemeId, UserId


@dataclass(frozen=True)
class PositionRow:
    """One holding, as a table row.

    `folio` is nullable per `PLAN.md` §9.7 — folios aggregate for display while
    lots and tax stay folio-scoped, so a consumer must not assume it is
    populated.

    `reconciled` and `confidence` travel WITH the row rather than as a page-level
    banner. `MODULE_1.md` §12.1: data quality is a property of the fact. A folio
    that failed reconciliation must be visibly degraded in the row it appears
    in, not hidden behind a summary that says "some data may be stale".
    """

    scheme_id: SchemeId
    folio: str | None
    units: Decimal
    nav: Decimal | None
    nav_date: date | None
    market_value: Decimal | None
    invested_net: Decimal | None
    unrealised_pnl: Decimal | None
    realised_pnl_todate: Decimal | None
    weight_in_portfolio: Decimal | None
    reconciled: bool
    confidence: str


class SqlitePositionProvider:
    """Read-only over Zone B's `position`. Nothing here derives a number."""

    def __init__(self, ledger: sqlite3.Connection) -> None:
        self._ledger = ledger

    def positions(self, user_id: UserId, as_of: date) -> list[PositionRow]:
        """Every holding on this date, largest first.

        **Sorted in Python.** `market_value` is `DECIMAL_TEXT`, so `ORDER BY` it
        sorts as text and "5000" outranks "25000" — V1-18's trap, and a holdings
        table in the wrong order is exactly the kind of wrong that looks right.
        Positions with no market value sort last rather than as zero: unvalued
        is not worthless.
        """
        rows = self._ledger.execute(
            "SELECT scheme_id, folio, units, nav, nav_date, market_value,"
            " invested_net, unrealised_pnl, realised_pnl_todate,"
            " weight_in_portfolio, reconciled, confidence"
            " FROM position WHERE user_id = ? AND as_of = ?",
            (str(user_id), as_of.isoformat()),
        ).fetchall()
        found = [
            PositionRow(
                scheme_id=SchemeId(r[0]),
                folio=r[1],
                units=r[2],
                nav=r[3],
                nav_date=date.fromisoformat(r[4]) if r[4] else None,
                market_value=r[5],
                invested_net=r[6],
                unrealised_pnl=r[7],
                realised_pnl_todate=r[8],
                weight_in_portfolio=r[9],
                reconciled=bool(r[10]),
                confidence=r[11],
            )
            for r in rows
        ]
        found.sort(
            key=lambda p: (
                p.market_value is None,
                -(p.market_value or Decimal(0)),
                str(p.scheme_id),
            )
        )
        return found

    def latest_as_of(self, user_id: UserId) -> date | None:
        """The newest date `rebuild()` wrote for this user.

        `max()` over an ISO date string is chronological, and `as_of` is TEXT
        rather than `DECIMAL_TEXT` — invariant 1's ban is on aggregating
        decimals in SQL, not on ordering dates.
        """
        row = self._ledger.execute(
            "SELECT max(as_of) FROM position WHERE user_id = ?", (str(user_id),)
        ).fetchone()
        return date.fromisoformat(row[0]) if row and row[0] else None


__all__ = ["PositionRow", "SqlitePositionProvider"]
