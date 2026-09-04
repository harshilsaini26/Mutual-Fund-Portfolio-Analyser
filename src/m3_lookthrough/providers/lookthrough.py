"""`LookThroughProvider` — what M3 exposes to M4, M5 and M6.

MODULE_3.md §15.1. One amendment against the spec as written:

  D2  +`max_pairwise_overlap()`. `RiskInputs` (MODULE_4.md §14.4) declares it but
      `LookThroughProvider` does not, so M4's adapter would have to compute a
      reduction over M3's own overlap matrix. `BUILD_ORDER.md` R1 requires M4's
      M3-block to be pure delegation; this is the widening R1 exists to prevent.

R3 keeps the `Exposure` dataclass stable: M4 depends on its shape.

Slice Zero — Protocol stub and result dataclasses, no logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from src.common.types import (
    ClassificationBasis,
    ExposureScope,
    IndexId,
    IssuerId,
    SchemeId,
    UserId,
    WeightBasis,
)


@dataclass(frozen=True)
class Exposure:
    """MODULE_3.md §15.2. One issuer, aggregated across every route to it.

    Joined on `issuer_id`, never `isin`: a Reliance equity line and a Reliance
    NCD are one exposure.

    Synthetics (`__UNRESOLVED__`, `__NO_DISCLOSURE__`, `__CASH__`, `__DERIV__`)
    appear here as visible line items and are excluded from concentration
    denominators (§8.2) — included in exposure, excluded from the denominator.

    `holdings_as_of` is the EARLIEST contributing disclosure date: the worst-case
    staleness, which is the honest number to display (§5.5).
    """

    issuer_id: IssuerId
    issuer_name: str
    exposure_inr: Decimal
    exposure_pct: Decimal
    via_funds: int
    fund_inr: Decimal
    direct_inr: Decimal
    instrument_class: str | None
    is_synthetic: bool
    holdings_as_of: date | None
    staleness_days: int | None


@dataclass(frozen=True)
class Contribution:
    """MODULE_3.md §15.2. One route from one scheme to one issuer.

    `scheme_id` is `__DIRECT__` for directly-held equity. `depth` is 0 for a
    direct holding, 1 for a fund, 2 for a fund-of-funds underlying — recursion
    is capped at depth 2 and cycle-guarded.
    """

    issuer_id: IssuerId
    scheme_id: SchemeId
    depth: int
    weight_in_fund: Decimal | None
    exposure_inr: Decimal


@dataclass(frozen=True)
class Concentration:
    """MODULE_3.md §8.1 / `portfolio_concentration`.

    Synthetics are filtered out of the pool before any of these are computed —
    §8.2, the denominator trap. Counting `__CASH__` as an issuer would flatter
    every concentration figure.
    """

    scope: ExposureScope
    issuer_count: int
    hhi: Decimal | None = None
    effective_n: Decimal | None = None
    top1_pct: Decimal | None = None
    top5_pct: Decimal | None = None
    top10_pct: Decimal | None = None
    top20_pct: Decimal | None = None
    gini: Decimal | None = None
    largest_issuer_id: IssuerId | None = None
    largest_issuer_pct: Decimal | None = None


@dataclass(frozen=True)
class Overlap:
    """MODULE_3.md §9 / `fund_overlap`. One unordered scheme pair.

    `scheme_a` is the lexicographically smaller id, so each pair is stored once.

    `aligned` is False when the two schemes disclosed on different dates.
    Pairwise comparison across mismatched dates is a strictly stronger
    requirement than portfolio aggregation, so it carries its own flag (§9.3)
    rather than inheriting the portfolio's staleness caveat.
    """

    scheme_a: SchemeId
    scheme_b: SchemeId
    overlap_pct: Decimal
    common_issuers: int
    union_issuers: int
    as_of_a: date
    as_of_b: date
    as_of_gap_days: int
    aligned: bool
    overlap_equity_pct: Decimal | None
    jaccard: Decimal | None
    overlap_value_inr: Decimal | None


@dataclass(frozen=True)
class Redundancy:
    """MODULE_3.md §10 / `fund_redundancy`.

    `PLAN.md` §9.8: the 0.5/0.3/0.2 blend is arbitrary, so `blended_score` is a
    SORT KEY ONLY, never a headline number. The three signals are shown
    separately; `signals_available` says how many were computable.
    """

    scheme_a: SchemeId
    scheme_b: SchemeId
    signals_available: int
    confidence: str
    overlap_pct: Decimal | None
    return_corr_36m: Decimal | None
    style_distance: Decimal | None
    blended_score: Decimal | None


@dataclass(frozen=True)
class Marginal:
    """MODULE_3.md §11 / `fund_marginal_contribution`.

    Answers what this scheme adds that nothing else in the portfolio provides.
    A negative `hhi_delta` means diversifying.
    """

    scheme_id: SchemeId
    position_inr: Decimal
    new_issuers: int | None
    new_exposure_inr: Decimal | None
    new_exposure_pct: Decimal | None
    hhi_with: Decimal | None
    hhi_without: Decimal | None
    hhi_delta: Decimal | None
    effective_n_delta: Decimal | None
    style_shift_pp: Decimal | None
    fee_cost_inr: Decimal | None


@dataclass(frozen=True)
class Tilt:
    """MODULE_3.md §12 / `portfolio_tilt`. One bucket of one dimension.

    `classification_basis` is recorded on every row: the same exposure produces
    different tilts depending on which classification vintage is applied, and
    nothing in the data forces a choice.

    `benchmark_pct` and `active_tilt_pp` require `index_constituent`, which
    `BUILD_ORDER.md` R4 promotes to a V1 dependency. Without it this renders
    absolute exposure with no comparison — a materially weaker screen.
    """

    dimension: str  # sector|industry|mcap|instrument_class|credit_rating
    dimension_value: str
    exposure_inr: Decimal
    exposure_pct: Decimal
    classification_basis: ClassificationBasis
    coverage_pct: Decimal
    benchmark_pct: Decimal | None
    active_tilt_pp: Decimal | None
    benchmark_index_id: IndexId | None
    mcap_basis: date | None


@dataclass(frozen=True)
class PortfolioSummary:
    """MODULE_3.md §13 / `portfolio_summary`.

    `portfolio_xirr` comes from M1 cashflows at portfolio scope — never an
    average of per-scheme XIRRs. Per `PLAN.md` §9.6 switch legs are excluded at
    portfolio level as internal transfers.

    `worst_staleness_days` derives from the earliest contributing disclosure
    date, not the average (§5.5).
    """

    user_id: UserId
    as_of: date
    total_value_inr: Decimal
    fund_value_inr: Decimal
    direct_value_inr: Decimal
    coverage_pct: Decimal
    unresolved_pct: Decimal
    confidence: str
    caveats: list[str]
    computed_at: datetime

    invested_net_inr: Decimal | None
    unrealised_pnl_inr: Decimal | None
    realised_pnl_todate: Decimal | None
    portfolio_xirr: Decimal | None
    portfolio_twrr_ann: Decimal | None
    blended_ter: Decimal | None
    annual_fee_inr: Decimal | None

    scheme_count: int | None
    folio_count: int | None
    issuer_count: int | None
    direct_issuer_count: int | None
    schemes_covered: int | None
    schemes_stale: int | None
    schemes_quarantined: int | None
    worst_staleness_days: int | None


@dataclass(frozen=True)
class LookThroughResult:
    """MODULE_3.md §5.1.

    `holdings_dates` added per DECISIONS D7 — §12 `tilts()` reads it to pick the
    AMFI basis via `min(lt.holdings_dates)`, but §5.1 as written omits the field.

    Closure is asserted at runtime, not only in tests: the sum of exposures
    equals the sum of position values plus direct holdings, within one rupee.
    """

    exposures: list[Exposure]
    contributions: list[Contribution]
    summary: PortfolioSummary
    caveats: list[str]
    holdings_dates: list[date]


class LookThroughProvider(Protocol):
    """What M3 exposes to M4, M5 and M6.

    Determinism requirement (§15.3): `exposures()` returns rows in DESCENDING
    `exposure_inr`, tie-broken by `issuer_id`. M6 caches on payload hashes;
    non-deterministic ordering would invalidate caches on every call for no
    reason.

    `exposures`, `sector_exposure`, `concentration` and `max_pairwise_overlap`
    together form the surface `RiskInputs` delegates to — `BUILD_ORDER.md` R1's
    amendment. Do not narrow them without an ADR.
    """

    def lookthrough(
        self,
        user_id: UserId,
        as_of: date,
        weight_basis: WeightBasis = WeightBasis.DISCLOSED,
    ) -> LookThroughResult: ...

    def exposures(
        self,
        user_id: UserId,
        as_of: date,
        weight_basis: WeightBasis = WeightBasis.DISCLOSED,
        scope: ExposureScope = "all",
    ) -> list[Exposure]:
        """Descending `exposure_inr`, tie-broken by `issuer_id`. Always."""
        ...

    def contributions(
        self,
        user_id: UserId,
        as_of: date,
        issuer_id: IssuerId,
        weight_basis: WeightBasis = WeightBasis.DISCLOSED,
    ) -> list[Contribution]: ...

    def concentration(
        self,
        user_id: UserId,
        as_of: date,
        scope: ExposureScope = "equity",
    ) -> Concentration: ...

    def overlap_matrix(self, user_id: UserId, as_of: date) -> list[Overlap]: ...

    def pairwise_overlap(
        self,
        user_id: UserId,
        as_of: date,
        scheme_a: SchemeId,
        scheme_b: SchemeId,
    ) -> Overlap | None: ...

    def max_pairwise_overlap(self, user_id: UserId, as_of: date) -> Decimal | None:
        """Added per DECISIONS D2 — consumed by M4's `overlap_max` risk limit."""
        ...

    def redundancy(self, user_id: UserId, as_of: date) -> list[Redundancy]: ...

    def marginal(self, user_id: UserId, as_of: date, scheme_id: SchemeId) -> Marginal: ...

    def tilts(
        self,
        user_id: UserId,
        as_of: date,
        dimension: str,
        basis: ClassificationBasis,
    ) -> list[Tilt]:
        """`basis` is required and has no default — `CLAUDE.md` invariant 6."""
        ...

    def summary(self, user_id: UserId, as_of: date) -> PortfolioSummary: ...

    def sector_exposure(
        self, user_id: UserId, as_of: date, basis: ClassificationBasis
    ) -> dict[str, Decimal]:
        """Exists specifically for M4's Brinson attribution, which needs sector
        weights in a plain dict keyed by the canonical taxonomy.
        """
        ...
