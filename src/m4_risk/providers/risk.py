"""`RiskProvider` — what M4 exposes to M6.

MODULE_4.md §14.1, transcribed. Slice Zero — Protocol stub and result
dataclasses, no logic.

`RiskSnapshot` here carries its provenance tail only. The metric fields are
marked V4 below: `BUILD_ORDER.md` R3 requires only `Exposure` to stay stable, and
freezing V4's metric surface before V0 is built would be guessing. Adding them
later needs no ADR.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from src.common.types import (
    AnalysisScope,
    FactorId,
    IssuerId,
    ScenarioId,
    UserId,
)


@dataclass(frozen=True)
class RiskSnapshot:
    """MODULE_4.md §14.2 — provenance tail only (see module docstring).

    Every result carries method and confidence. `caveats` flows into M6's
    `ViewEnvelope` unchanged and MUST be rendered.

    Confidence floors (§14.3): high at 756 observations, medium at 252, low at
    60. Below 60 the snapshot is suppressed rather than shown.

    `PLAN.md` §9.5 is decided: historical simulation only, no parametric VaR.
    Indian equity returns are fat-tailed and the normal assumption understates
    tail risk consistently. This type will never carry a `var_parametric` field;
    `skewness` and `excess_kurtosis` exist to explain why the familiar number is
    not offered.
    """

    user_id: UserId
    as_of: date
    scope: AnalysisScope
    scope_id: str
    lookback_days: int

    obs_count: int
    rf_available: bool
    bm_available: bool
    cov_method: str | None
    synthetic_basis: str | None  # constant_weight|drift|actual_history
    confidence: str
    caveats: list[str]
    computed_at: datetime

    # V4: metric fields land here — volatility_ann, downside_dev_ann, beta_vs_bm,
    # tracking_error_ann, information_ratio, sharpe, sortino, calmar,
    # var_95_hist, var_99_hist, cvar_95_hist, cvar_99_hist, var_95_inr,
    # var_horizon_days, skewness, excess_kurtosis, jarque_bera_p, max_drawdown,
    # dd_peak_date, dd_trough_date, dd_recovery_date, dd_duration_days,
    # dd_recovery_days, current_dd. Field names and types are fixed by the
    # `risk_snapshot` DDL (MODULE_4.md §4); only the timing is deferred.


@dataclass(frozen=True)
class DrawdownPoint:
    """MODULE_4.md §6. One point on the underwater curve.

    Derived, not stored — the DDL keeps only the summary on `risk_snapshot`.
    """

    point_date: date
    value: Decimal
    running_peak: Decimal
    drawdown_pct: Decimal


@dataclass(frozen=True)
class CorrelationCell:
    """MODULE_4.md §8 / `correlation_matrix`. One unordered entity pair.

    `method` records whether this came from a sample estimate, Ledoit-Wolf
    shrinkage, or a factor model — they are not interchangeable, and a sample
    covariance on 60 observations across 40 schemes is not a usable matrix.
    """

    scope_type: str  # scheme|sector|issuer
    entity_a: str
    entity_b: str
    lookback_days: int
    obs_count: int
    method: str  # sample|ledoit_wolf|factor
    correlation: Decimal | None
    covariance: Decimal | None


@dataclass(frozen=True)
class AttributionRun:
    """MODULE_4.md §9 / `attribution_run`. Brinson-Fachler, one period.

    `residual_pct_of_active` is the RELIABILITY SIGNAL, not a footnote. Holdings
    are monthly; a fund that traded heavily between disclosures produces a large
    unexplained residual, and the attribution is then describing a portfolio that
    did not exist. Confidence is keyed off it: high under 15%, medium under 30%.
    """

    run_id: str
    user_id: UserId
    period_start: date
    period_end: date
    level: str  # fund|sector|issuer
    benchmark_ref: str
    method: str  # brinson_fachler|fund_selection
    unexplained_residual: Decimal
    coverage_pct: Decimal
    confidence: str
    computed_at: datetime

    linking: str | None  # none|carino
    portfolio_return: Decimal | None
    benchmark_return: Decimal | None
    active_return: Decimal | None
    total_allocation: Decimal | None
    total_selection: Decimal | None
    total_interaction: Decimal | None
    residual_pct_of_active: Decimal | None
    snapshots_used: int | None
    avg_staleness_days: Decimal | None


@dataclass(frozen=True)
class FactorExposure:
    """MODULE_4.md §10 / `factor_exposure_holdings`.

    `basis` says whether fundamentals were available. `PLAN.md` §9.2 and §9.3:
    if point-in-time fundamentals are not achievable cheaply, ship size and
    momentum only — both are computable from price data alone.
    """

    scope: AnalysisScope
    scope_id: str
    factor_id: FactorId
    coverage_pct: Decimal
    basis: str  # price_only|with_fundamentals
    universe_id: str
    exposure_z: Decimal | None


@dataclass(frozen=True)
class StressResult:
    """MODULE_4.md §11 / `stress_result`.

    `coverage_pct` is the share of exposure the scenario could price directly.
    `method` distinguishes a real replay from beta-propagated estimates; the two
    do not carry the same weight and are never blended silently.
    """

    scenario_id: ScenarioId
    portfolio_value_pre: Decimal
    portfolio_value_post: Decimal
    impact_inr: Decimal
    impact_pct: Decimal
    coverage_pct: Decimal
    method: str  # direct_replay|beta_propagated|mixed
    confidence: str
    computed_at: datetime


@dataclass(frozen=True)
class StressContribution:
    """MODULE_4.md §11 / `stress_contribution`. One segment's share of an impact.

    `priced_directly` is False where the shock was propagated by beta rather
    than replayed from real history.
    """

    scenario_id: ScenarioId
    segment_type: str  # scheme|sector|issuer
    segment_id: str
    exposure_inr: Decimal
    impact_inr: Decimal
    priced_directly: bool
    shock_applied: Decimal | None
    pct_of_total_impact: Decimal | None


@dataclass(frozen=True)
class LiquidityRow:
    """MODULE_4.md §12 / `liquidity_profile`. One issuer within one scope."""

    scope: AnalysisScope
    scope_id: str
    issuer_id: IssuerId
    exposure_inr: Decimal
    participation_rate: Decimal
    adv_20d_inr: Decimal | None
    days_to_liquidate: Decimal | None
    liquidity_bucket: str | None  # liquid|moderate|illiquid|untradeable
    free_float_pct: Decimal | None


@dataclass(frozen=True)
class Breach:
    """MODULE_4.md §13 / `risk_breach`.

    A breach is a description, never an instruction (`PLAN.md` §3.3). "Your
    top-10 concentration is 41% against a limit you set at 35%" — not "reduce
    your banking exposure". The user set the limit; the system reports the fact.
    """

    breach_id: str
    limit_id: str
    user_id: UserId
    as_of: date
    actual_value: Decimal
    threshold: Decimal
    excess: Decimal
    status: str  # open|resolved|acknowledged
    first_breached: date
    resolved_on: date | None
    acknowledged_at: datetime | None


class RiskProvider(Protocol):
    """What M4 exposes to M6."""

    def risk_snapshot(
        self,
        user_id: UserId,
        as_of: date,
        scope: AnalysisScope = "portfolio",
        scope_id: str = "__PORTFOLIO__",
        lookback_days: int = 756,
    ) -> RiskSnapshot: ...

    def drawdown_series(
        self, user_id: UserId, as_of: date, lookback_days: int
    ) -> list[DrawdownPoint]: ...

    def correlation_matrix(
        self, user_id: UserId, as_of: date, scope_type: str = "scheme"
    ) -> list[CorrelationCell]: ...

    def attribution(
        self, user_id: UserId, start: date, end: date, level: str = "fund"
    ) -> AttributionRun: ...

    def factor_exposure(
        self, user_id: UserId, as_of: date, method: str = "holdings"
    ) -> list[FactorExposure]: ...

    def stress_results(self, user_id: UserId, as_of: date) -> list[StressResult]: ...

    def stress_detail(
        self, user_id: UserId, as_of: date, scenario_id: ScenarioId
    ) -> list[StressContribution]: ...

    def liquidity(
        self, user_id: UserId, as_of: date, scope: AnalysisScope = "scheme"
    ) -> list[LiquidityRow]: ...

    def breaches(
        self, user_id: UserId, as_of: date, status: str = "open"
    ) -> list[Breach]: ...
