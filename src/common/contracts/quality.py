"""Data-quality and coverage types.

`PLAN.md` §4.10: never silently drop data. A number that is quietly incomplete
is worse than a number that is visibly missing. Every type here exists so the
gap travels with the fact rather than being reconstructed downstream.

Slice Zero — dataclasses only, no logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.common.types import ValidationStatus


@dataclass(frozen=True)
class DisclosureQuality:
    """MODULE_2.md §5.1 — the M0 to M2 direction.

    Describes one parsed holdings file. See `DataQuality` for the M2 to M3
    direction; the two are deliberately distinct types with overlapping fields.

    NOTE (DECISIONS D8): the spec names this field `unresolved_mv_pct` here and
    `unresolved_pct` on `DataQuality`. Standardised on `unresolved_pct` — two
    spellings of one percentage in one module is a live footgun in a codebase
    where a wrong percentage is a silent error.
    """

    holdings_as_of: date
    unresolved_pct: Decimal
    weight_residual: Decimal
    validation_status: ValidationStatus
    row_count: int


@dataclass(frozen=True)
class DataQuality:
    """MODULE_2.md §14.1 — the M2 to M3 direction.

    Adds the gating fields `DisclosureQuality` does not carry.
    `usable_for_lookthrough` is the flag M3 §14.3 keys its inclusion table off:

      quarantined              -> exclude the scheme, reduce coverage_pct
      unresolved_pct > 2%      -> include, plus an explicit __UNRESOLVED__ block
      staleness_days > 45      -> include, surface a caveat
      usable_for_lookthrough=0 -> exclude from look-through, still show position value
    """

    holdings_as_of: date
    staleness_days: int
    unresolved_pct: Decimal
    weight_residual: Decimal
    validation_status: ValidationStatus
    usable_for_lookthrough: bool
    confidence: str


@dataclass(frozen=True)
class CoverageStat:
    """MODULE_0.md §9.4 `coverage_stat`.

    Every figure derived from cross-scheme aggregation must carry this
    (`PLAN.md` §4.10). Flows from unparsed AMCs are not included and are never
    estimated.
    """

    as_of_date: date
    parsed_aum_inr: Decimal | None
    total_aum_inr: Decimal | None
    coverage_pct: Decimal
