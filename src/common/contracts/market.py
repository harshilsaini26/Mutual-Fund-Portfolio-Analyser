"""Price, NAV, index and rate points. Shared by M0, M2, M4, M5.

Slice Zero — dataclasses only, no logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.common.types import IndexId, SchemeId

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
class IndexPoint:
    """MODULE_0.md `index_level`."""

    index_id: IndexId
    level_date: date
    level: Decimal
