"""`FakeRiskInputs` — M4's single façade over everything upstream.

MODULE_4.md §14.4. `PLAN.md` §8.2 rule 2: "M4 reaches everything via
`RiskInputs`", so this fake is an *adapter*, not a second data layer — it holds a
`LookThroughProvider` and delegates the whole M3 block to it, unchanged.

That delegation is the point. `BUILD_ORDER.md` R1's amendment says
`LookThroughProvider` must satisfy M4's needs as well as M6's; the four methods
in the M3 block below are one-line pass-throughs, which is the proof that it
does.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from src.common.contracts.market import IndexMeta, PricePoint, RfPoint
from src.common.fixtures import (
    FixtureError,
    FixtureStore,
    as_date,
    as_datetime,
    as_decimal,
    default_store,
    fixture_key,
)
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
from src.m0_data.providers.fake import FakeFundDataProvider
from src.m1_ledger.handoff import PositionContext
from src.m3_lookthrough.providers.fake import (
    FakeLookThroughDataProvider,
    FakeLookThroughProvider,
)
from src.m3_lookthrough.providers.lookthrough import Concentration, Exposure
from src.m4_risk.providers.inputs import PolicyComponent, RiskLimit, StressShock
from src.m4_risk.providers.risk import RiskSnapshot


class FakeRiskInputs:
    """Satisfies `RiskInputs`."""

    def __init__(
        self,
        store: FixtureStore | None = None,
        lookthrough: FakeLookThroughProvider | None = None,
    ) -> None:
        self._store = store or default_store()
        self._m = self._store.section("market")
        self._ltp = lookthrough or FakeLookThroughProvider(self._store)
        self._ltd = FakeLookThroughDataProvider(self._store)
        self._fd = FakeFundDataProvider(self._store)

    # --- from M3: pure delegation ------------------------------------------
    # Every method here forwards to LookThroughProvider with no arithmetic.
    # If a computation ever appears in this block, an M3 concern has leaked
    # into M4 and the boundary has moved.

    def exposures(
        self,
        user_id: UserId,
        as_of: date,
        weight_basis: WeightBasis = WeightBasis.DISCLOSED,
        scope: ExposureScope = "all",
    ) -> list[Exposure]:
        return self._ltp.exposures(user_id, as_of, weight_basis, scope)

    def sector_exposure(
        self, user_id: UserId, as_of: date, basis: ClassificationBasis
    ) -> dict[str, Decimal]:
        return self._ltp.sector_exposure(user_id, as_of, basis)

    def concentration(
        self, user_id: UserId, as_of: date, scope: ExposureScope
    ) -> Concentration:
        return self._ltp.concentration(user_id, as_of, scope)

    def max_pairwise_overlap(self, user_id: UserId, as_of: date) -> Decimal | None:
        return self._ltp.max_pairwise_overlap(user_id, as_of)

    # --- from M0 -----------------------------------------------------------

    def price(self, isin: Isin, on: date) -> PricePoint | None:
        return self._ltd.price(isin, on)

    def price_series(self, isin: Isin, start: date, end: date) -> list[PricePoint]:
        out = []
        for row in (self._m.get("prices") or {}).get(isin) or []:
            row_date = as_date(row["price_date"])
            assert row_date is not None
            if start <= row_date <= end:
                point = self._ltd.price(isin, row_date)
                if point:
                    out.append(point)
        return sorted(out, key=lambda p: p.price_date)

    def adjustment_factor(self, isin: Isin, frm: date, to: date) -> Decimal:
        return self._fd.adjustment_factor(isin, frm, to)

    def index_level(self, index_id: IndexId, on: date) -> Decimal | None:
        return self._fd.index_level(index_id, on)

    def index_meta(self, index_id: IndexId) -> IndexMeta:
        """`PLAN.md` §9.4: callers suppress the comparison when this is not TRI."""
        for row in self._store.table("market_data", "benchmarks"):
            if row["index_id"] == index_id:
                return IndexMeta(
                    index_id=IndexId(str(row["index_id"])),
                    index_name=str(row["index_name"]),
                    is_total_return=bool(row["is_total_return"]),
                    provider=row.get("provider"),
                    base_date=as_date(row.get("base_date")),
                    base_value=as_decimal(row.get("base_value")),
                )
        raise FixtureError(f"no benchmark index {index_id!r}")

    def risk_free_series(self, start: date, end: date) -> list[RfPoint]:
        out = []
        for row in self._store.table("market_data", "risk_free"):
            rate_date = as_date(row["rate_date"])
            annual = as_decimal(row["annual_rate"])
            assert rate_date is not None and annual is not None
            if start <= rate_date <= end:
                out.append(
                    RfPoint(
                        rate_date=rate_date,
                        tenor=str(row.get("tenor", "91D")),
                        annual_rate=annual,
                        daily_rate=as_decimal(row.get("daily_rate")),
                        is_interpolated=bool(row.get("is_interpolated", False)),
                    )
                )
        return sorted(out, key=lambda r: r.rate_date)

    def factor_series(
        self, factor_ids: list[FactorId], start: date, end: date
    ) -> dict[str, dict[date, Decimal]]:
        block = self._m.get("factor_series") or {}
        out: dict[str, dict[date, Decimal]] = {}
        for fid in factor_ids:
            series = block.get(fid)
            if series is None:
                continue
            points: dict[date, Decimal] = {}
            for when, value in series.items():
                d = as_date(when)
                v = as_decimal(value)
                assert d is not None and v is not None
                if start <= d <= end:
                    points[d] = v
            out[str(fid)] = points
        return out

    def sector_of(self, issuer_id: IssuerId, on: date) -> str | None:
        return self._fd.sector_of(issuer_id, on)

    def mcap_bucket(
        self, issuer_id: IssuerId, on: date, mcap_basis: date | None = None
    ) -> McapBucket | None:
        """DECISIONS D5: `mcap_basis` pins the AMFI list.

        Without it M4 would resolve against the list in force on `on` while M3
        used a pinned basis, and the two would disagree about which companies
        are large-cap for the same portfolio.
        """
        return self._fd.mcap_list_as_of(mcap_basis or on).bucket_for(issuer_id)

    def primary_isin(self, issuer_id: IssuerId) -> Isin | None:
        row = (self._store.section("holdings").get("issuers") or {}).get(issuer_id)
        if not row or not row.get("primary_isin"):
            return None
        return Isin(str(row["primary_isin"]))

    def trading_days(self, start: date, end: date) -> list[date]:
        out: list[date] = []
        for days in (self._m.get("trading_days") or {}).values():
            for v in days:
                d = as_date(v)
                if d is not None and start <= d <= end:
                    out.append(d)
        return sorted(set(out))

    # --- from M1 -----------------------------------------------------------

    def _contexts(self, user_id: UserId, as_of: date) -> list[PositionContext]:
        return self._ltd.position_contexts(user_id, as_of)

    def portfolio_value_before_flows(self, user_id: UserId, on: date) -> Decimal:
        return sum((pc.market_value for pc in self._contexts(user_id, on)), Decimal(0))

    def net_flow_on(self, user_id: UserId, on: date) -> Decimal:
        """Net external flow on one day, at portfolio scope.

        Portfolio scope, so switch legs are already excluded — `PLAN.md` §9.6.
        """
        total = Decimal(0)
        for when, amount in self._ltd.portfolio_cashflows(user_id, on, "portfolio"):
            if when == on:
                total += amount
        return total

    def portfolio_return(self, user_id: UserId, start: date, end: date) -> Decimal:
        row = (self._m.get("portfolio_returns") or {}).get(f"{start}|{end}")
        value = as_decimal(row)
        if value is None:
            raise FixtureError(f"no portfolio return fixture for {start}..{end}")
        return value

    def position_contexts_over(
        self, user_id: UserId, start: date, end: date
    ) -> list[PositionContext]:
        return self._contexts(user_id, end)

    # --- from M2 -----------------------------------------------------------

    def scheme_return(
        self, scheme_id: SchemeId, start: date, end: date
    ) -> Decimal | None:
        """NAV-based, using the adjusted series: raw NAV would understate IDCW plans."""
        series = self._fd.nav_series(scheme_id, start, end, adjusted=True)
        if len(series) < 2 or series[0].nav <= 0:
            return None
        return series[-1].nav / series[0].nav - Decimal(1)

    def category_median_return(
        self, cat: str, plan: str, start: date, end: date
    ) -> Decimal | None:
        return as_decimal(
            (self._m.get("category_median_returns") or {}).get(f"{cat}|{plan}")
        )

    def sebi_category(self, scheme_id: SchemeId) -> str:
        category = self._fd.scheme(scheme_id).sebi_category
        if category is None:
            raise FixtureError(f"no SEBI category for {scheme_id!r}")
        return category

    # --- limit evaluation (DECISIONS D9) -----------------------------------
    # Declared because MODULE_4.md §13's LIMIT_EVALUATORS calls all six on `ri`,
    # while §14.4 declares none of them.

    def _pct_of_total(self, user_id: UserId, as_of: date, iid: str) -> Decimal | None:
        for e in self._ltp.exposures(user_id, as_of):
            if e.issuer_id == iid:
                return e.exposure_pct
        return None

    def issuer_exposure_pct(
        self, user_id: UserId, as_of: date, issuer_id: IssuerId
    ) -> Decimal | None:
        return self._pct_of_total(user_id, as_of, str(issuer_id))

    def sector_exposure_pct(
        self, user_id: UserId, as_of: date, sector_id: str
    ) -> Decimal | None:
        by_sector = self._ltp.sector_exposure(user_id, as_of, ClassificationBasis.CURRENT)
        if sector_id not in by_sector:
            return None
        total = self._ltp.summary(user_id, as_of).total_value_inr
        if total <= 0:
            return None
        return by_sector[sector_id] / total * Decimal(100)

    def mcap_exposure_pct(
        self, user_id: UserId, as_of: date, bucket: str
    ) -> Decimal | None:
        basis = self._fd.mcap_list_as_of(as_of)
        total = self._ltp.summary(user_id, as_of).total_value_inr
        if total <= 0:
            return None
        matched = Decimal(0)
        for e in self._ltp.exposures(user_id, as_of):
            if e.is_synthetic:
                continue
            found = basis.bucket_for(e.issuer_id)
            if found is not None and found.value == bucket:
                matched += e.exposure_inr
        return matched / total * Decimal(100)

    def illiquid_exposure_pct(self, user_id: UserId, as_of: date) -> Decimal | None:
        return as_decimal(fixture_key(self._m.get("illiquid_pct") or {}, as_of))

    def current_drawdown(self, user_id: UserId, as_of: date) -> Decimal | None:
        return as_decimal(fixture_key(self._m.get("current_drawdown") or {}, as_of))

    def risk_snapshot(self, user_id: UserId, as_of: date) -> RiskSnapshot:
        """Provenance tail only — the metric fields are V4 work (DECISIONS SZ-11)."""
        row: dict[str, Any] = dict(
            fixture_key(self._m.get("risk_snapshot") or {}, as_of) or {}
        )
        if not row:
            raise FixtureError(f"no risk_snapshot fixture for {as_of}")
        computed_at = as_datetime(row.get("computed_at"))
        assert computed_at is not None
        return RiskSnapshot(
            user_id=UserId(user_id),
            as_of=as_of,
            scope="portfolio",
            scope_id="__PORTFOLIO__",
            lookback_days=int(row["lookback_days"]),
            obs_count=int(row["obs_count"]),
            rf_available=bool(row["rf_available"]),
            bm_available=bool(row["bm_available"]),
            cov_method=row.get("cov_method"),
            synthetic_basis=row.get("synthetic_basis"),
            confidence=str(row["confidence"]),
            caveats=list(row.get("caveats") or []),
            computed_at=computed_at,
        )

    # --- M4-owned config ---------------------------------------------------

    def policy_benchmark(self, user_id: UserId, on: date) -> list[PolicyComponent]:
        rows = (self._m.get("policy_benchmark") or {}).get(user_id) or []
        out = []
        for row in rows:
            weight = as_decimal(row["weight_pct"])
            valid_from = as_date(row["valid_from"])
            valid_to = as_date(row.get("valid_to"))
            assert weight is not None and valid_from is not None
            if valid_from <= on and (valid_to is None or on <= valid_to):
                out.append(
                    PolicyComponent(
                        user_id=UserId(user_id),
                        component_id=IndexId(str(row["component_id"])),
                        weight_pct=weight,
                        valid_from=valid_from,
                        is_auto=bool(row["is_auto"]),
                        valid_to=valid_to,
                    )
                )
        return out

    def active_limits(self, user_id: UserId) -> list[RiskLimit]:
        rows = (self._m.get("risk_limits") or {}).get(user_id) or []
        out = []
        for row in rows:
            if not row.get("is_active", True):
                continue
            threshold = as_decimal(row["threshold"])
            created_at = as_datetime(row["created_at"])
            assert threshold is not None and created_at is not None
            out.append(
                RiskLimit(
                    limit_id=str(row["limit_id"]),
                    user_id=UserId(user_id),
                    limit_type=str(row["limit_type"]),
                    threshold=threshold,
                    is_active=True,
                    created_at=created_at,
                    dimension_value=row.get("dimension_value"),
                    notes=row.get("notes"),
                )
            )
        return out

    def scenario_shocks(self, scenario_id: ScenarioId) -> list[StressShock]:
        rows = (self._m.get("stress_scenarios") or {}).get(scenario_id) or []
        out = []
        for row in rows:
            shock = as_decimal(row["shock_pct"])
            assert shock is not None
            out.append(
                StressShock(
                    scenario_id=ScenarioId(scenario_id),
                    shock_dimension=str(row["shock_dimension"]),
                    dimension_value=str(row["dimension_value"]),
                    shock_pct=shock,
                )
            )
        return out
