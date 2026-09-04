"""`PositionContext` — the M1 to M2 handoff.

MODULE_1.md §12.1, transcribed. Slice Zero — dataclasses only, no logic.

Consumers read `position_context` ONLY. Never `txn`, `lot`, or `lot_consumption`:

    def load_contexts(user_id: str, as_of: date) -> list[PositionContext]: ...
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.common.types import SchemeId, UserId


@dataclass(frozen=True)
class LotSummary:
    """One open lot. FIFO is statutory for Indian MF units — no LIFO, no
    specific-identification. Tie-break order: acquisition_date, book_date, lot_id.

    `acquisition_date` is not `book_date`: mergers and segregations preserve the
    acquisition date while the book date moves. Collapsing them produces wrong tax.
    """

    lot_id: str
    acquisition_date: date
    units_remaining: Decimal
    cost_per_unit: Decimal
    unrealised_gain: Decimal
    holding_days: int
    gain_type_if_sold_today: str
    days_to_ltcg: int | None
    exit_load_free_on: date | None


@dataclass(frozen=True)
class PositionContext:
    """The complete state of one user's position in one scheme, as of a date.

    Three fields carry more weight than the rest:

    `cashflows` is passed, never recomputed. XIRR is a ledger concept; if a
    downstream module rebuilt cashflows from its own view there would be two
    divergent XIRRs in one product and a permanent support burden explaining
    which is right. One owner per number.

    `confidence` propagates. If reconciliation failed, every statistic derived
    from this position must be visibly degraded rather than silently wrong. Data
    quality is a property of the fact, not a global banner.

    `folio` is nullable (`PLAN.md` §9.7). Folios aggregate for display; lots and
    tax stay folio-scoped. Consumers must not assume it is populated.
    """

    # identity
    user_id: UserId
    scheme_id: SchemeId
    folio: str | None
    as_of: date

    # exposure
    units: Decimal
    nav: Decimal
    market_value: Decimal
    weight_in_portfolio: Decimal

    # temporal scope
    first_purchase: date
    last_purchase: date
    holding_days: int
    is_active_sip: bool
    cashflows: list[tuple[date, Decimal]]

    # cost basis
    invested_gross: Decimal
    invested_net: Decimal
    avg_cost_nav: Decimal
    unrealised_pnl: Decimal
    realised_pnl_todate: Decimal
    idcw_received_todate: Decimal

    # lots
    lots: list[LotSummary]

    # plan facts — drive fee comparisons downstream
    plan: str
    option: str

    # data quality — travels WITH the object
    reconciled: bool
    reconcile_delta_units: Decimal
    confidence: str
    flags: list[str]
