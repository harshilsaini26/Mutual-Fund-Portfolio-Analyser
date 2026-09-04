"""`RiskInputs` — M4's view of everything upstream.

MODULE_4.md §14.4. `PLAN.md` §8.2 rule 2: "M4 reaches everything via
`RiskInputs`." The real implementation is an adapter holding a
`LookThroughProvider`, a `MarketDataProvider` and so on — not a second data layer.

Three amendments against the spec as written, all recorded in DECISIONS:

  D1  `lookthrough()` removed, `exposures()` used instead. The spec declares
      `lookthrough(user_id, as_of) -> list[Exposure]` here and
      `lookthrough(user_id, as_of, weight_basis) -> LookThroughResult` on
      `LookThroughProvider`. One name, two return types, across a module
      boundary. M3's `exposures()` already returns exactly what M4 wants, so the
      adapter becomes a pass-through instead of a rename.

  D3  `sector_of()`, matching `MarketDataFeed`. One operation, one name.

  D5  `mcap_bucket()` gains `mcap_basis`. Without it M4 silently uses today's
      AMFI list while M3 uses a pinned basis — `PLAN.md` §9.3's look-ahead bias,
      in the flattering direction.

  D9  Six methods added that §13's `LIMIT_EVALUATORS` calls on `ri` but §14.4
      never declares: `issuer_exposure_pct`, `sector_exposure_pct`,
      `mcap_exposure_pct`, `illiquid_exposure_pct`, `current_drawdown`, and
      `risk_snapshot` — the last of which the spec places on `RiskProvider`.
      Without them the limit engine does not typecheck against its own inputs.

After D1 and D2, the M3 block below is a pure delegation to
`LookThroughProvider`, which is `BUILD_ORDER.md` R1's amendment. Keep it that way.

Slice Zero — Protocol stub, no implementation. `FakeRiskInputs` reading YAML
fixtures ships alongside it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from src.common.contracts.market import IndexMeta, PricePoint, RfPoint
from src.common.types import (
    ClassificationBasis,
    ExposureScope,
    FactorId,
    IndexId,
    Isin,
    IssuerId,
    McapBucket,
    ScenarioId,
    SchemeId,
    UserId,
    WeightBasis,
)
from src.m1_ledger.handoff import PositionContext
from src.m3_lookthrough.providers.lookthrough import Concentration, Exposure
from src.m4_risk.providers.risk import RiskSnapshot


@dataclass(frozen=True)
class PolicyComponent:
    """MODULE_4.md `policy_benchmark`. One weighted leg of the user's benchmark.

    `is_auto` marks a component derived from the current fund mix rather than
    chosen by the user. An auto benchmark drifts with the portfolio, which makes
    attribution against it close to circular — surfaced as a caveat, not hidden.
    """

    user_id: UserId
    component_id: IndexId
    weight_pct: Decimal
    valid_from: date
    is_auto: bool
    valid_to: date | None


@dataclass(frozen=True)
class RiskLimit:
    """MODULE_4.md §13 / `risk_limit`. A threshold the USER set.

    The system reports breaches descriptively; it never proposes a limit or
    suggests an action on one (`PLAN.md` §3.3).

    `limit_type` is one of: issuer_max, sector_max, mcap_min, mcap_max, hhi_max,
    effective_n_min, illiquid_max, overlap_max, drawdown_max, vol_max.
    `mcap_min` and `effective_n_min` breach on the low side.
    """

    limit_id: str
    user_id: UserId
    limit_type: str
    threshold: Decimal
    is_active: bool
    created_at: datetime
    dimension_value: str | None
    notes: str | None


@dataclass(frozen=True)
class StressShock:
    """MODULE_4.md §11 / `stress_shock`. One dimension's move within a scenario."""

    scenario_id: ScenarioId
    shock_dimension: str  # sector|mcap_bucket|factor|rates|broad_market
    dimension_value: str
    shock_pct: Decimal


class RiskInputs(Protocol):
    """M4's single façade over M3, M0, M1, M2 and its own config."""

    # --- from M3 (pure delegation to LookThroughProvider) ------------------

    def exposures(
        self,
        user_id: UserId,
        as_of: date,
        weight_basis: WeightBasis = WeightBasis.DISCLOSED,
        scope: ExposureScope = "all",
    ) -> list[Exposure]:
        """Was `lookthrough()` in the spec — see DECISIONS D1."""
        ...

    def sector_exposure(
        self, user_id: UserId, as_of: date, basis: ClassificationBasis
    ) -> dict[str, Decimal]: ...

    def concentration(
        self, user_id: UserId, as_of: date, scope: ExposureScope
    ) -> Concentration:
        """`scope` is passed explicitly here, unlike M3's default of 'equity'.

        DECISIONS D9: always pass it. Falling through to the default would
        silently switch the denominator under a risk limit.
        """
        ...

    def max_pairwise_overlap(self, user_id: UserId, as_of: date) -> Decimal | None: ...

    # --- from M0 -----------------------------------------------------------

    def price(self, isin: Isin, on: date) -> PricePoint | None: ...

    def price_series(self, isin: Isin, start: date, end: date) -> list[PricePoint]: ...

    def adjustment_factor(self, isin: Isin, frm: date, to: date) -> Decimal: ...

    def index_level(self, index_id: IndexId, on: date) -> Decimal | None: ...

    def index_meta(self, index_id: IndexId) -> IndexMeta:
        """`PLAN.md` §9.4: suppress the comparison if `is_total_return` is False."""
        ...

    def risk_free_series(self, start: date, end: date) -> list[RfPoint]: ...

    def factor_series(
        self, factor_ids: list[FactorId], start: date, end: date
    ) -> dict[str, dict[date, Decimal]]: ...

    def sector_of(self, issuer_id: IssuerId, on: date) -> str | None: ...

    def mcap_bucket(
        self, issuer_id: IssuerId, on: date, mcap_basis: date | None = None
    ) -> McapBucket | None:
        """`mcap_basis` added per DECISIONS D5 — one basis across an aggregation."""
        ...

    def primary_isin(self, issuer_id: IssuerId) -> Isin | None: ...

    def trading_days(self, start: date, end: date) -> list[date]: ...

    # --- from M1 -----------------------------------------------------------

    def portfolio_value_before_flows(self, user_id: UserId, on: date) -> Decimal: ...

    def net_flow_on(self, user_id: UserId, on: date) -> Decimal: ...

    def portfolio_return(self, user_id: UserId, start: date, end: date) -> Decimal: ...

    def position_contexts_over(
        self, user_id: UserId, start: date, end: date
    ) -> list[PositionContext]: ...

    # --- from M2 -----------------------------------------------------------

    def scheme_return(
        self, scheme_id: SchemeId, start: date, end: date
    ) -> Decimal | None: ...

    def category_median_return(
        self, cat: str, plan: str, start: date, end: date
    ) -> Decimal | None: ...

    def sebi_category(self, scheme_id: SchemeId) -> str: ...

    # --- limit evaluation (DECISIONS D9) -----------------------------------
    # Called by §13's LIMIT_EVALUATORS but absent from §14.4 as written.

    def issuer_exposure_pct(
        self, user_id: UserId, as_of: date, issuer_id: IssuerId
    ) -> Decimal | None: ...

    def sector_exposure_pct(
        self, user_id: UserId, as_of: date, sector_id: str
    ) -> Decimal | None: ...

    def mcap_exposure_pct(
        self, user_id: UserId, as_of: date, bucket: str
    ) -> Decimal | None: ...

    def illiquid_exposure_pct(self, user_id: UserId, as_of: date) -> Decimal | None: ...

    def current_drawdown(self, user_id: UserId, as_of: date) -> Decimal | None: ...

    def risk_snapshot(self, user_id: UserId, as_of: date) -> RiskSnapshot:
        """The `vol_max` evaluator reads `.volatility_ann` off this.

        Declared here per D9 even though the spec places it on `RiskProvider`;
        the limit engine takes `ri` and calls it directly.
        """
        ...

    # --- M4-owned config ---------------------------------------------------

    def policy_benchmark(self, user_id: UserId, on: date) -> list[PolicyComponent]: ...

    def active_limits(self, user_id: UserId) -> list[RiskLimit]: ...

    def scenario_shocks(self, scenario_id: ScenarioId) -> list[StressShock]: ...
