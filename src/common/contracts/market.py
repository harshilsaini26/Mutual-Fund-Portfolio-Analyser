"""Price, NAV, index and rate points. Shared by M0, M2, M4, M5.

Slice Zero — dataclasses only, no logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.common.types import IndexId, Isin, SchemeId

# `PLAN.md` §8.2 rule 1: Decimal for every money, unit, NAV and weight field.
# No `float` appears in any dataclass in this package — tests/unit asserts it.


@dataclass(frozen=True)
class NavPoint:
    """MODULE_0.md §11.2.

    `is_interpolated` is never hidden: a NAV we filled is not a NAV we fetched.
    """

    scheme_id: SchemeId
    nav_date: date
    nav: Decimal
    is_interpolated: bool


@dataclass(frozen=True)
class IdcwEvent:
    """MODULE_0.md `scheme_idcw`. Drives the `nav_adj` total-return series."""

    scheme_id: SchemeId
    record_date: date
    amount_per_unit: Decimal


@dataclass(frozen=True)
class PricePoint:
    """MODULE_0.md `security_price`.

    `close_adj` is corporate-action adjusted; `close` is not. Return math uses
    the adjusted series.
    """

    isin: Isin
    price_date: date
    exchange: str
    close: Decimal
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    prev_close: Decimal | None
    close_adj: Decimal | None
    volume: int | None
    traded_value: Decimal | None
    trades_count: int | None


@dataclass(frozen=True)
class IndexPoint:
    """MODULE_0.md `index_level`."""

    index_id: IndexId
    level_date: date
    level: Decimal


@dataclass(frozen=True)
class IndexMeta:
    """MODULE_0.md `benchmark_index`.

    `PLAN.md` §9.4 is decided: TRI only. `is_total_return` is non-optional so a
    price-return index cannot be compared by accident — that error runs in our
    favour by roughly the dividend yield, which is the worst kind.
    """

    index_id: IndexId
    index_name: str
    is_total_return: bool
    provider: str | None
    base_date: date | None
    base_value: Decimal | None


@dataclass(frozen=True)
class RfPoint:
    """MODULE_0.md `risk_free_rate`."""

    rate_date: date
    tenor: str
    annual_rate: Decimal
    daily_rate: Decimal | None
    is_interpolated: bool
