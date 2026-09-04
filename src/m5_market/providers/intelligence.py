"""`MarketIntelligence` — what M5 exposes to M6.

MODULE_5.md §14.1, transcribed. Slice Zero — Protocol stub and result
dataclasses, no logic.

Every market-facing method takes `scope`, defaulting to `watch`. That default is
the mechanism implementing §3.1 rule 2: the tool filters the market through the
user's actual exposure rather than showing them everything. `market` is opt-in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from src.common.types import (
    ClassificationBasis,
    Isin,
    IssuerId,
    SchemeId,
    SectorId,
    UniverseId,
    UniverseScope,
    UserId,
)


@dataclass(frozen=True)
class SectorRow:
    """MODULE_5.md §6 / `sector_daily` + `sector_performance`, joined.

    `median_return_1d` sits beside the cap-weighted `return_1d` deliberately:
    the gap between them is breadth. A sector index up 2% on one mega-cap is a
    different fact from one up 2% across forty names.

    `coverage_pct` is the share of constituents that had a price that day.
    """

    sector_id: SectorId
    as_of: date
    universe_id: UniverseId
    constituent_count: int
    priced_count: int
    coverage_pct: Decimal

    return_1d: Decimal | None
    median_return_1d: Decimal | None
    index_level: Decimal | None
    advancers: int | None
    decliners: int | None
    unchanged: int | None
    pct_above_50dma: Decimal | None
    pct_above_200dma: Decimal | None
    total_mcap_inr: Decimal | None
    total_traded_value_inr: Decimal | None

    ret_1w: Decimal | None
    ret_1m: Decimal | None
    ret_3m: Decimal | None
    ret_6m: Decimal | None
    ret_1y: Decimal | None
    ret_ytd: Decimal | None
    rel_strength_1m: Decimal | None
    rel_strength_3m: Decimal | None
    rel_strength_1y: Decimal | None
    volatility_1y: Decimal | None
    max_dd_1y: Decimal | None
    rank_1m: int | None
    rank_3m: int | None
    rank_1y: int | None


@dataclass(frozen=True)
class SectorDetail:
    """MODULE_5.md §6 — one sector's full page.

    `top_flows` and `top_conviction` are the sector's own slices of the flow and
    conviction engines, not separate computations.
    """

    sector_id: SectorId
    as_of: date
    row: SectorRow
    constituents: list[IssuerId]
    top_flows: list[IssuerFlow]
    top_conviction: list[Conviction]
    coverage_pct: Decimal
    caveats: list[str]


@dataclass(frozen=True)
class IssuerFlow:
    """MODULE_5.md §7 / `mf_flow_issuer`. Month-over-month across all parsed schemes.

    §14.3: coverage travels on every result. `caveats` typically carries
    "Based on N of M schemes (X% of industry AUM). Flows from unparsed AMCs are
    not included and are not estimated."

    `net_qty_adj` is corporate-action adjusted — an unadjusted quantity delta
    across a bonus issue is a fabricated flow.
    """

    as_of_date: date
    prev_as_of_date: date
    issuer_id: IssuerId
    schemes_holding_curr: int
    schemes_holding_prev: int
    schemes_entering: int
    schemes_exiting: int
    schemes_adding: int
    schemes_trimming: int
    coverage_pct: Decimal
    schemes_eligible: int
    schemes_total: int
    caveats: list[str]

    net_qty_adj: Decimal | None
    net_value_inr: Decimal | None
    gross_bought_inr: Decimal | None
    gross_sold_inr: Decimal | None
    total_mf_holding_inr: Decimal | None
    pct_of_free_float: Decimal | None
    net_flow_as_pct_adv: Decimal | None


@dataclass(frozen=True)
class Contributor:
    """MODULE_5.md §7 / `mf_flow_contributor`. One scheme's share of one flow.

    `adj_factor` is stored so the flow can be re-derived and audited — `PLAN.md`
    §4.2, show your working.
    """

    as_of_date: date
    issuer_id: IssuerId
    scheme_id: SchemeId
    action: str  # entry|exit|add|trim
    adj_factor: Decimal
    qty_delta_adj: Decimal | None
    value_delta_inr: Decimal | None
    weight_prev: Decimal | None
    weight_curr: Decimal | None


@dataclass(frozen=True)
class Conviction:
    """MODULE_5.md §8 / `manager_conviction`. Equity only.

    `dispersion` is the underrated field (§8.3). High aggregate buying with low
    dispersion is broad consensus; the same buying with high dispersion is a few
    managers taking large positions while others stayed out. Most flow reporting
    collapses those into one number. `consensus_direction == 'divided'` fires
    when dispersion exceeds the median weight.

    `conviction_score` is log-damped by breadth: one fund at 9% is not thirty
    funds averaging 3%.
    """

    issuer_id: IssuerId
    as_of_date: date
    schemes_holding: int
    schemes_high_conviction: int = 0
    coverage_pct: Decimal | None = None
    consensus_direction: str = "stable"  # accumulating|distributing|stable|divided
    max_weight_pct: Decimal | None = None
    max_weight_scheme_id: SchemeId | None = None
    median_weight_pct: Decimal | None = None
    wtd_avg_weight_pct: Decimal | None = None
    dispersion: Decimal | None = None
    conviction_score: Decimal | None = None


@dataclass(frozen=True)
class CompanyView:
    """MODULE_5.md §11 / `company_snapshot`.

    `classification_basis` is required: a company promoted to large-cap should
    not retroactively restate what the user held in 2023.
    """

    issuer_id: IssuerId
    as_of: date
    classification_basis: ClassificationBasis
    computed_at: datetime

    primary_isin: Isin | None
    sector_id: SectorId | None
    price: Decimal | None
    price_chg_1d: Decimal | None
    ret_1w: Decimal | None
    ret_1m: Decimal | None
    ret_3m: Decimal | None
    ret_6m: Decimal | None
    ret_1y: Decimal | None
    mcap_inr: Decimal | None
    mcap_bucket: str | None
    mcap_basis: date | None
    high_52w: Decimal | None
    low_52w: Decimal | None
    pct_from_52w_high: Decimal | None
    dma_50: Decimal | None
    dma_200: Decimal | None
    volatility_1y: Decimal | None
    beta_1y: Decimal | None
    adv_20d_inr: Decimal | None
    rel_strength_vs_sector_3m: Decimal | None


@dataclass(frozen=True)
class SectorContext:
    """MODULE_5.md §10 / `user_sector_context`. The relevance layer.

    This is the type that makes market data personal: the user's own weight
    beside the index weight and beside what the whole parsed MF industry holds.
    Bloomberg knows the market and nothing about you; a broker app knows your
    holdings and shows a pie chart. This joins them.
    """

    user_id: UserId
    as_of: date
    sector_id: SectorId
    your_exposure_inr: Decimal
    your_exposure_pct: Decimal
    classification_basis: ClassificationBasis
    coverage_pct: Decimal

    index_weight_pct: Decimal | None
    active_tilt_pp: Decimal | None
    mf_industry_weight_pct: Decimal | None
    vs_industry_pp: Decimal | None
    sector_ret_1w: Decimal | None
    sector_ret_1m: Decimal | None
    sector_ret_3m: Decimal | None
    contribution_1m: Decimal | None
    mf_flow_1m_inr: Decimal | None


@dataclass(frozen=True)
class WatchEntry:
    """MODULE_5.md §9 / `user_watch_universe`. One issuer in the relevance filter.

    `in_watchlist` entries may carry zero exposure — a manually watched company
    the user does not own yet. Exposure-derived entries carry `relevance_rank`.
    """

    user_id: UserId
    as_of: date
    issuer_id: IssuerId
    exposure_inr: Decimal
    exposure_pct: Decimal
    via_funds: int
    direct: bool
    in_watchlist: bool
    relevance_rank: int | None
    added_at: datetime | None


@dataclass(frozen=True)
class ManualWatch:
    """MODULE_5.md `user_watchlist_manual`."""

    user_id: UserId
    issuer_id: IssuerId
    added_at: datetime
    notes: str | None


@dataclass(frozen=True)
class Rotation:
    """MODULE_5.md §12 / `sector_rotation`. One RRG point.

    `tail_json` carries the prior 8 weeks so the RRG tail renders without a
    second query. Quadrant is derived from the two axes, not stored separately
    as a judgement.
    """

    as_of: date
    sector_id: SectorId
    universe_id: UniverseId
    quadrant: str  # leading|weakening|lagging|improving
    rs_ratio: Decimal | None
    rs_momentum: Decimal | None
    tail_json: str | None


@dataclass(frozen=True)
class Announcement:
    """MODULE_5.md §13 / `corporate_announcement`.

    HEADLINE AND URL ONLY — never the body. `PLAN.md` §3.2 puts news aggregation
    out of scope on licensing grounds; this is a pointer to the exchange filing,
    not a reproduction of it.
    """

    announcement_id: str
    issuer_id: IssuerId
    announced_at: datetime
    headline: str
    url: str
    source: str  # nse|bse
    category: str | None  # results|board|dividend|allotment|other


class MarketIntelligence(Protocol):
    """What M5 exposes to M6.

    `scope` defaults to `watch` on every market-facing method. That is not a
    convenience — it is how §3.1 rule 2 (the over-monitoring constraint) is
    enforced in the API rather than left to each caller.
    """

    def sector_dashboard(
        self,
        as_of: date,
        universe_id: UniverseId,
        scope: UniverseScope = "watch",
    ) -> list[SectorRow]: ...

    def sector_detail(self, sector_id: SectorId, as_of: date) -> SectorDetail: ...

    def flow_leaders(
        self,
        as_of: date,
        direction: str,
        limit: int = 20,
        sector: SectorId | None = None,
        scope: UniverseScope = "watch",
    ) -> list[IssuerFlow]: ...

    def flow_contributors(
        self, as_of: date, issuer_id: IssuerId
    ) -> list[Contributor]: ...

    def conviction_map(
        self,
        as_of: date,
        min_schemes: int = 3,
        scope: UniverseScope = "watch",
    ) -> list[Conviction]: ...

    def company_page(self, issuer_id: IssuerId, as_of: date) -> CompanyView: ...

    def user_sector_context(
        self, user_id: UserId, as_of: date, basis: ClassificationBasis
    ) -> list[SectorContext]:
        """`basis` is required and has no default — `CLAUDE.md` invariant 6."""
        ...

    def watch_universe(self, user_id: UserId, as_of: date) -> list[WatchEntry]: ...

    def rotation(self, as_of: date, universe_id: UniverseId) -> list[Rotation]: ...

    def announcements(
        self, issuer_id: IssuerId, limit: int = 20
    ) -> list[Announcement]: ...
