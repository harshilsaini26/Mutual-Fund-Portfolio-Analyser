"""`MarketDataFeed` — M5's view of M0.

MODULE_5.md §14.4, transcribed. Two amendments, both recorded in DECISIONS:

  D3  `sector_of()` — already this name here; kept, and `FundDataProvider`
      renamed to match.

  D5  `mcap_bucket()` gains `mcap_basis`, so M5 can pin the same AMFI list M3
      pinned. Without it a sector dashboard and a portfolio tilt can disagree
      about which companies are large-cap.

Unlike `FundDataProvider`, this reaches holdings across ALL schemes — it is the
flow engine's foundation, and the only protocol in the corpus that is
deliberately market-wide rather than portfolio-scoped.

Slice Zero — Protocol stub, no implementation. `FakeMarketDataFeed` reading YAML
fixtures ships alongside it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Protocol

from src.common.contracts.entity import Constituent, Holding, SchemeWeight
from src.common.contracts.market import IndexPoint, PricePoint
from src.common.types import (
    IndexId,
    Isin,
    IssuerId,
    McapBucket,
    SchemeId,
    UserId,
)
from src.m5_market.providers.intelligence import ManualWatch


class MarketDataFeed(Protocol):
    """M5's only route into M0."""

    # --- holdings across ALL schemes — the flow engine's foundation --------

    def schemes_with_disclosure(
        self, as_of: date, status_ok_only: bool
    ) -> list[SchemeId]:
        """`status_ok_only` excludes quarantined files.

        A quarantined disclosure must never enter a flow calculation: a
        misparsed quantity column becomes a fabricated industry-wide buy.
        """
        ...

    def holdings(self, scheme_id: SchemeId, as_of: date) -> list[Holding]: ...

    def all_scheme_weights(
        self, issuer_id: IssuerId, as_of: date, equity_only: bool
    ) -> list[SchemeWeight]:
        """`equity_only` is True for conviction scoring (§8.4).

        A fund holding a company's NCD is not expressing equity conviction in it.
        """
        ...

    def prev_disclosure_date(self, as_of: date) -> date | None: ...

    def all_active_schemes(self, as_of: date) -> list[SchemeId]: ...

    def scheme_aum(self, scheme_id: SchemeId, as_of: date) -> Decimal | None: ...

    def total_industry_aum(self, as_of: date) -> Decimal | None: ...

    def coverage_pct(self, as_of: date) -> Decimal:
        """Travels onto every cross-scheme result — `PLAN.md` §4.10."""
        ...

    # --- prices and indices ------------------------------------------------

    def price(self, isin: Isin, on: date) -> PricePoint | None: ...

    def price_series(self, isin: Isin, start: date, end: date) -> list[PricePoint]: ...

    def adjustment_factor(self, isin: Isin, frm: date, to: date) -> Decimal: ...

    def moving_average(self, isin: Isin, on: date, window: int) -> Decimal | None: ...

    def index_constituents(self, index_id: IndexId, as_of: date) -> list[Constituent]:
        """Point-in-time membership — `BUILD_ORDER.md` R4."""
        ...

    def index_series(
        self, index_id: IndexId, start: date, end: date
    ) -> list[IndexPoint]: ...

    def index_sector_weights(self, as_of: date) -> dict[str, Decimal]: ...

    # --- entities ----------------------------------------------------------

    def sector_of(self, issuer_id: IssuerId, on: date) -> str | None: ...

    def mcap_bucket(
        self, issuer_id: IssuerId, on: date, mcap_basis: date | None = None
    ) -> McapBucket | None:
        """`mcap_basis` added per DECISIONS D5."""
        ...

    def market_cap(self, issuer_id: IssuerId, on: date) -> Decimal | None: ...

    def free_float_mcap(self, issuer_id: IssuerId, on: date) -> Decimal | None: ...

    def primary_isin(self, issuer_id: IssuerId) -> Isin | None: ...

    def is_synthetic(self, issuer_id: IssuerId) -> bool:
        """True for `__UNRESOLVED__`, `__CASH__`, `__DERIV__` and friends.

        Synthetics are excluded from concentration denominators and from
        conviction scoring, but never dropped from exposure.
        """
        ...

    def adv_20d(self, issuer_id: IssuerId, on: date) -> Decimal | None: ...

    def beta(self, issuer_id: IssuerId, on: date) -> Decimal | None: ...

    def trading_days(self, start: date, end: date) -> list[date]: ...

    # --- user config -------------------------------------------------------

    def manual_watchlist(self, user_id: UserId) -> list[ManualWatch]: ...
