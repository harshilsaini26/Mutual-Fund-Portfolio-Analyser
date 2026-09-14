"""`FundDataProvider` — M2's only interface into M0.

MODULE_2.md §5.1. M2 needs more than `MarketDataProvider` gives M1, so a second
narrow protocol rather than a widening of the first.

Two amendments, recorded in DECISIONS:

  D6  +`index_level()`. §13's `segment_position()` calls it but §5.1 declares
      only `index_series()`; regime segmentation needs a point lookup.
  D3  `sector()` renamed `sector_of()`, matching the other two protocols.

Slice Zero — Protocol stub. `FakeFundDataProvider` reading YAML fixtures ships
with it: M2's test suite must run without a warehouse.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Protocol

from src.common.contracts.entity import Holding
from src.common.contracts.market import IndexPoint, NavPoint
from src.common.contracts.quality import DisclosureQuality
from src.common.contracts.scheme import ManagerRow, McapList, SchemeRow, Tenure, TerPoint
from src.common.types import IndexId, Isin, IssuerId, ManagerId, Plan, SchemeId


class FundDataProvider(Protocol):
    """M2's only interface into M0. No raw SQL against Zone A."""

    # --- holdings ----------------------------------------------------------

    def holdings(self, scheme_id: SchemeId, as_of: date) -> list[Holding]: ...

    def disclosure_dates(
        self,
        scheme_id: SchemeId,
        start: date | None = None,
        end: date | None = None,
    ) -> list[date]: ...

    def disclosure_quality(
        self, scheme_id: SchemeId, as_of: date
    ) -> DisclosureQuality: ...

    def prev_disclosure(self, scheme_id: SchemeId, before: date) -> date | None: ...

    # --- classification (point-in-time) ------------------------------------

    def mcap_list_as_of(self, on: date) -> McapList:
        """The whole point is point-in-time (§6.2).

        Applying today's classification retroactively produces phantom drift, or
        masks real drift. Every point in a drift series uses its own
        contemporaneous basis.
        """
        ...

    def sector_of(self, issuer_id: IssuerId, on: date) -> str | None:
        """Renamed from `sector()` per DECISIONS D3."""
        ...

    def adjustment_factor(self, isin: Isin, frm: date, to: date) -> Decimal: ...

    # --- NAV / index / rates ------------------------------------------------

    def nav_series(
        self,
        scheme_id: SchemeId,
        start: date,
        end: date,
        adjusted: bool = True,
    ) -> list[NavPoint]: ...

    def index_series(
        self, index_id: IndexId, start: date, end: date
    ) -> list[IndexPoint]: ...

    def index_level(self, index_id: IndexId, on: date) -> Decimal | None:
        """Added per DECISIONS D6 — required by §13 `segment_position()`."""
        ...

    def benchmark_for(self, scheme_id: SchemeId) -> IndexId | None: ...

    def risk_free(self, on: date) -> Decimal | None: ...

    # --- scheme attributes --------------------------------------------------

    def scheme(self, scheme_id: SchemeId) -> SchemeRow: ...

    def ter_series(self, scheme_id: SchemeId) -> list[TerPoint]: ...

    def aum(self, scheme_id: SchemeId, on: date) -> Decimal | None: ...

    def schemes_in_category(
        self, sebi_category: str, plan: Plan, as_of: date
    ) -> list[SchemeId]:
        """Peer groups never mix plans — Direct and Regular are not comparable."""
        ...

    def category_universe_count(self, sebi_category: str, plan: Plan, as_of: date) -> int:
        """Denominator for peer coverage: schemes in the category per AMFI master."""
        ...

    # --- managers -----------------------------------------------------------

    def tenures(self, scheme_id: SchemeId) -> list[Tenure]: ...

    def manager_schemes(self, manager_id: ManagerId) -> list[Tenure]: ...

    def manager(self, manager_id: ManagerId) -> ManagerRow: ...
