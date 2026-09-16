"""`FakeLookThroughDataProvider` and `FakeLookThroughProvider`.

MODULE_3.md §15.4 and §15.1. The first lets M3 itself be built against
fixtures; the second let M6 be built before M3 existed.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from src.common.contracts.entity import DirectHolding, IssuerWeight
from src.common.contracts.market import PricePoint
from src.common.types import (
    CashflowScope,
    ClassificationBasis,
    ExposureScope,
    IndexId,
    InstrumentClass,
    Isin,
    IssuerId,
    SchemeId,
    UserId,
    WeightBasis,
)
from src.m1_ledger.handoff import LotSummary, PositionContext
from src.m3_lookthrough.providers.lookthrough import (
    Concentration,
    Contribution,
    Exposure,
    LookThroughResult,
    Marginal,
    Overlap,
    PortfolioSummary,
    Redundancy,
    Tilt,
)

from tests.fakes.loader import (
    FixtureError,
    FixtureStore,
    as_date,
    as_datetime,
    as_decimal,
    default_store,
    fixture_key,
)
from tests.fakes.m0 import FakeFundDataProvider


class FakeLookThroughDataProvider:
    """M3's view of M0 and M1 — fixture-backed. Satisfies `LookThroughDataProvider`."""

    def __init__(self, store: FixtureStore | None = None) -> None:
        self._store = store or default_store()
        self._h = self._store.section("holdings")
        self._l = self._store.section("ledger")
        self._m = self._store.section("market")
        self._fd = FakeFundDataProvider(self._store)
        self._path: set[SchemeId] = set()

    # --- from M0 -----------------------------------------------------------

    def scheme_issuer_weights(
        self, scheme_id: SchemeId, as_of: date, basis: str
    ) -> tuple[list[IssuerWeight], str]:
        """Collapses holding rows to one row per issuer.

        Returns the basis actually used, which may differ from the one asked
        for: drift adjustment needs priceable quantities, and this fixture has
        none, so a drift request falls back to disclosed and says so. M3
        surfaces that as a caveat rather than quietly returning a different
        number than was requested.
        """
        rows = self._fd.holdings(scheme_id, as_of)

        disclosed: dict[IssuerId, Decimal] = {}
        qty: dict[IssuerId, Decimal] = {}
        klass: dict[IssuerId, InstrumentClass] = {}
        for r in rows:
            disclosed[r.issuer_id] = (
                disclosed.get(r.issuer_id, Decimal(0)) + r.pct_normalised
            )
            if r.quantity is not None:
                qty[r.issuer_id] = qty.get(r.issuer_id, Decimal(0)) + r.quantity
            klass[r.issuer_id] = r.instrument_class

        weights = [
            IssuerWeight(
                scheme_id=SchemeId(scheme_id),
                as_of_date=as_of,
                issuer_id=iid,
                weight_disclosed=w,
                instrument_class=klass[iid],
                weight_drift_adj=None,
                quantity=qty.get(iid),
            )
            for iid, w in disclosed.items()
        ]
        weights.sort(key=lambda w: (-w.weight_disclosed, w.issuer_id))
        return weights, "disclosed"

    def issuer_of(self, isin: Isin) -> IssuerId | None:
        found = (self._h.get("isin_to_issuer") or {}).get(isin)
        return IssuerId(str(found)) if found else None

    def issuer_name(self, issuer_id: IssuerId) -> str:
        row = (self._h.get("issuers") or {}).get(issuer_id)
        if row is None:
            raise FixtureError(f"no issuer {issuer_id!r}")
        return str(row["name"])

    def class_of(self, issuer_id: IssuerId) -> str | None:
        for scheme_block in (self._h.get("holdings") or {}).values():
            for rows in scheme_block.values():
                for r in rows or []:
                    if r["issuer_id"] == issuer_id:
                        return str(r["instrument_class"])
        return None

    def price(self, isin: Isin, on: date) -> PricePoint | None:
        for row in (self._m.get("prices") or {}).get(isin) or []:
            if as_date(row["price_date"]) == on:
                close = as_decimal(row["close"])
                assert close is not None
                return PricePoint(
                    isin=Isin(isin),
                    price_date=on,
                    exchange=str(row.get("exchange", "NSE")),
                    close=close,
                    open=as_decimal(row.get("open")),
                    high=as_decimal(row.get("high")),
                    low=as_decimal(row.get("low")),
                    prev_close=as_decimal(row.get("prev_close")),
                    close_adj=as_decimal(row.get("close_adj")),
                    volume=int(row["volume"]) if row.get("volume") else None,
                    traded_value=as_decimal(row.get("traded_value")),
                    trades_count=int(row["trades_count"])
                    if row.get("trades_count")
                    else None,
                )
        return None

    def classification(
        self,
        issuer_id: IssuerId,
        dimension: str,
        on: date,
        mcap_basis: date | None,
    ) -> str | None:
        if dimension == "mcap":
            basis_date = mcap_basis or on
            bucket = self._fd.mcap_list_as_of(basis_date).bucket_for(issuer_id)
            return bucket.value if bucket else None
        if dimension in ("sector", "industry"):
            return self._fd.sector_of(issuer_id, on)
        if dimension == "instrument_class":
            return self.class_of(issuer_id)
        return None

    def amfi_mcap_effective_date(self, on: date) -> date:
        return self._fd.mcap_list_as_of(on).effective_date

    def benchmark_weights(self, dimension: str, on: date) -> dict[str, Decimal]:
        if dimension != "sector":
            return {}
        block = fixture_key(self._m.get("index_sector_weights") or {}, on)
        if not block:
            return {}
        out = {}
        for k, v in block.items():
            value = as_decimal(v)
            assert value is not None
            out[str(k)] = value
        return out

    def resolve_underlying_scheme(
        self, isin: Isin | None, raw_name: str
    ) -> SchemeId | None:
        if not isin:
            return None
        ref = self._fd.resolve_scheme(isin, raw_name, None, date.today())
        return ref.scheme_id

    def scheme_name(self, scheme_id: SchemeId) -> str:
        return self._fd.scheme(scheme_id).scheme_name

    def ter(self, scheme_id: SchemeId, on: date) -> Decimal | None:
        try:
            return self._fd.ter(scheme_id, on)
        except FixtureError:
            return None

    # --- from M1 -----------------------------------------------------------

    def _contexts_raw(self) -> list[dict[str, Any]]:
        return self._store.table("ledger", "position_contexts")

    def position_contexts(self, user_id: UserId, as_of: date) -> list[PositionContext]:
        out = []
        for row in self._contexts_raw():
            out.append(_build_position_context(row, UserId(user_id), as_of))
        return out

    def position_value(
        self, user_id: UserId, scheme_id: SchemeId, as_of: date
    ) -> Decimal:
        for pc in self.position_contexts(user_id, as_of):
            if pc.scheme_id == scheme_id:
                return pc.market_value
        raise FixtureError(f"no position in {scheme_id!r} for {user_id!r}")

    def direct_holdings(self, user_id: UserId, as_of: date) -> list[DirectHolding]:
        out = []
        for row in self._store.table("ledger", "direct_holdings"):
            row_as_of = as_date(row["as_of"])
            qty = as_decimal(row["quantity"])
            assert row_as_of is not None and qty is not None
            out.append(
                DirectHolding(
                    user_id=str(user_id),
                    isin=Isin(str(row["isin"])),
                    broker=str(row["broker"]),
                    as_of=row_as_of,
                    quantity=qty,
                    source=str(row["source"]),
                    avg_cost=as_decimal(row.get("avg_cost")),
                    first_purchase=as_date(row.get("first_purchase")),
                    notes=row.get("notes"),
                )
            )
        return out

    def portfolio_cashflows(
        self, user_id: UserId, as_of: date, scope: CashflowScope
    ) -> list[tuple[date, Decimal]]:
        """`PLAN.md` §9.6: 'portfolio' excludes switch legs, 'scheme' includes them.

        The two answers are genuinely different here, by design — the fixture
        carries a switch pair that nets to zero at portfolio scope.
        """
        block = (self._l.get("portfolio_cashflows") or {}).get(scope)
        if block is None:
            raise FixtureError(f"no portfolio_cashflows for scope {scope!r}")
        out = []
        for when, amount in block:
            d = as_date(when)
            a = as_decimal(amount)
            assert d is not None and a is not None
            out.append((d, a))
        return out

    # --- recursion support -------------------------------------------------

    def recursion_path(self) -> set[SchemeId]:
        return set(self._path)

    @contextlib.contextmanager
    def recursion_guard(self, scheme_id: SchemeId) -> Iterator[None]:
        """Depth 2, cycle-guarded. A FoF holding itself must not loop."""
        if scheme_id in self._path:
            raise FixtureError(f"FoF cycle re-entering {scheme_id!r}")
        if len(self._path) >= 2:
            raise FixtureError(f"FoF recursion past depth 2 at {scheme_id!r}")
        self._path.add(scheme_id)
        try:
            yield
        finally:
            self._path.discard(scheme_id)


def _build_position_context(
    row: dict[str, Any], user_id: UserId, as_of: date
) -> PositionContext:
    def d(key: str) -> Decimal:
        value = as_decimal(row[key])
        assert value is not None, f"{key} is required on a PositionContext"
        return value

    cashflows = []
    for when, amount in row.get("cashflows") or []:
        cf_date = as_date(when)
        cf_amount = as_decimal(amount)
        assert cf_date is not None and cf_amount is not None
        cashflows.append((cf_date, cf_amount))

    lots = []
    for lot in row.get("lots") or []:
        acq = as_date(lot["acquisition_date"])
        units = as_decimal(lot["units_remaining"])
        cost = as_decimal(lot["cost_per_unit"])
        gain = as_decimal(lot["unrealised_gain"])
        assert acq is not None and units is not None
        assert cost is not None and gain is not None
        lots.append(
            LotSummary(
                lot_id=str(lot["lot_id"]),
                acquisition_date=acq,
                units_remaining=units,
                cost_per_unit=cost,
                unrealised_gain=gain,
                holding_days=int(lot["holding_days"]),
                gain_type_if_sold_today=str(lot["gain_type_if_sold_today"]),
                days_to_ltcg=int(lot["days_to_ltcg"])
                if lot.get("days_to_ltcg")
                else None,
                exit_load_free_on=as_date(lot.get("exit_load_free_on")),
            )
        )
    # FIFO ordering is statutory: acquisition_date, then book date, then lot_id.
    lots.sort(key=lambda x: (x.acquisition_date, x.lot_id))

    first_purchase = as_date(row["first_purchase"])
    last_purchase = as_date(row["last_purchase"])
    assert first_purchase is not None and last_purchase is not None

    return PositionContext(
        user_id=user_id,
        scheme_id=SchemeId(str(row["scheme_id"])),
        folio=row.get("folio"),
        as_of=as_of,
        units=d("units"),
        nav=d("nav"),
        market_value=d("market_value"),
        weight_in_portfolio=d("weight_in_portfolio"),
        first_purchase=first_purchase,
        last_purchase=last_purchase,
        holding_days=int(row["holding_days"]),
        is_active_sip=bool(row["is_active_sip"]),
        cashflows=cashflows,
        invested_gross=d("invested_gross"),
        invested_net=d("invested_net"),
        avg_cost_nav=d("avg_cost_nav"),
        unrealised_pnl=d("unrealised_pnl"),
        realised_pnl_todate=d("realised_pnl_todate"),
        idcw_received_todate=d("idcw_received_todate"),
        lots=lots,
        plan=str(row["plan"]),
        option=str(row["option"]),
        reconciled=bool(row["reconciled"]),
        reconcile_delta_units=d("reconcile_delta_units"),
        confidence=str(row["confidence"]),
        flags=list(row.get("flags") or []),
    )


class FakeLookThroughProvider:
    """Canned M3 outputs, so M4/M5/M6 can be built first.

    Satisfies `LookThroughProvider`.
    """

    def __init__(self, store: FixtureStore | None = None) -> None:
        self._store = store or default_store()
        self._d = self._store.section("derived")
        self._lt = self._d.get("lookthrough") or {}

    def _exposures(self) -> list[Exposure]:
        out = []
        for row in self._lt.get("exposures") or []:
            inr = as_decimal(row["exposure_inr"])
            pct = as_decimal(row["exposure_pct"])
            fund = as_decimal(row["fund_inr"])
            direct = as_decimal(row["direct_inr"])
            assert inr is not None and pct is not None
            assert fund is not None and direct is not None
            out.append(
                Exposure(
                    issuer_id=IssuerId(str(row["issuer_id"])),
                    issuer_name=str(row["issuer_name"]),
                    exposure_inr=inr,
                    exposure_pct=pct,
                    via_funds=int(row["via_funds"]),
                    fund_inr=fund,
                    direct_inr=direct,
                    instrument_class=row.get("instrument_class"),
                    is_synthetic=bool(row["is_synthetic"]),
                    holdings_as_of=as_date(row.get("holdings_as_of")),
                    staleness_days=int(row["staleness_days"])
                    if row.get("staleness_days")
                    else None,
                )
            )
        # §15.3 determinism: descending exposure_inr, tie-broken by issuer_id.
        # Sorted here rather than trusted from the fixture, so the fake cannot
        # be the reason a caching test passes.
        out.sort(key=lambda e: (-e.exposure_inr, e.issuer_id))
        return out

    def lookthrough(
        self,
        user_id: UserId,
        as_of: date,
        weight_basis: WeightBasis = WeightBasis.DISCLOSED,
    ) -> LookThroughResult:
        dates = [
            d for d in (as_date(v) for v in self._lt.get("holdings_dates") or []) if d
        ]
        return LookThroughResult(
            exposures=self._exposures(),
            contributions=[],
            summary=self.summary(user_id, as_of),
            caveats=list(self._lt.get("caveats") or []),
            holdings_dates=dates,
        )

    def exposures(
        self,
        user_id: UserId,
        as_of: date,
        weight_basis: WeightBasis = WeightBasis.DISCLOSED,
        scope: ExposureScope = "all",
    ) -> list[Exposure]:
        rows = self._exposures()
        if scope == "equity":
            return [e for e in rows if e.instrument_class == "equity"]
        if scope == "debt":
            return [e for e in rows if e.instrument_class == "debt"]
        return rows

    def contributions(
        self,
        user_id: UserId,
        as_of: date,
        issuer_id: IssuerId,
        weight_basis: WeightBasis = WeightBasis.DISCLOSED,
    ) -> list[Contribution]:
        return []

    def concentration(
        self,
        user_id: UserId,
        as_of: date,
        scope: ExposureScope = "equity",
    ) -> Concentration:
        row = (self._d.get("concentration") or {}).get(scope)
        if row is None:
            raise FixtureError(f"no concentration fixture for scope {scope!r}")
        return Concentration(
            scope=scope,
            issuer_count=int(row["issuer_count"]),
            hhi=as_decimal(row.get("hhi")),
            effective_n=as_decimal(row.get("effective_n")),
            top1_pct=as_decimal(row.get("top1_pct")),
            top5_pct=as_decimal(row.get("top5_pct")),
            top10_pct=as_decimal(row.get("top10_pct")),
            top20_pct=as_decimal(row.get("top20_pct")),
            gini=as_decimal(row.get("gini")),
            largest_issuer_id=IssuerId(str(row["largest_issuer_id"]))
            if row.get("largest_issuer_id")
            else None,
            largest_issuer_pct=as_decimal(row.get("largest_issuer_pct")),
        )

    def overlap_matrix(self, user_id: UserId, as_of: date) -> list[Overlap]:
        out = []
        for row in self._store.table("derived", "overlaps"):
            pct = as_decimal(row["overlap_pct"])
            a = as_date(row["as_of_a"])
            b = as_date(row["as_of_b"])
            assert pct is not None and a is not None and b is not None
            out.append(
                Overlap(
                    scheme_a=SchemeId(str(row["scheme_a"])),
                    scheme_b=SchemeId(str(row["scheme_b"])),
                    overlap_pct=pct,
                    common_issuers=int(row["common_issuers"]),
                    union_issuers=int(row["union_issuers"]),
                    as_of_a=a,
                    as_of_b=b,
                    as_of_gap_days=int(row["as_of_gap_days"]),
                    aligned=bool(row["aligned"]),
                    overlap_equity_pct=as_decimal(row.get("overlap_equity_pct")),
                    jaccard=as_decimal(row.get("jaccard")),
                    overlap_value_inr=as_decimal(row.get("overlap_value_inr")),
                )
            )
        return out

    def pairwise_overlap(
        self,
        user_id: UserId,
        as_of: date,
        scheme_a: SchemeId,
        scheme_b: SchemeId,
    ) -> Overlap | None:
        wanted = tuple(sorted([str(scheme_a), str(scheme_b)]))
        for o in self.overlap_matrix(user_id, as_of):
            if tuple(sorted([str(o.scheme_a), str(o.scheme_b)])) == wanted:
                return o
        return None

    def max_pairwise_overlap(self, user_id: UserId, as_of: date) -> Decimal | None:
        return as_decimal(self._d.get("max_pairwise_overlap"))

    def redundancy(self, user_id: UserId, as_of: date) -> list[Redundancy]:
        out = []
        for row in self._store.table("derived", "redundancy"):
            out.append(
                Redundancy(
                    scheme_a=SchemeId(str(row["scheme_a"])),
                    scheme_b=SchemeId(str(row["scheme_b"])),
                    signals_available=int(row["signals_available"]),
                    confidence=str(row["confidence"]),
                    overlap_pct=as_decimal(row.get("overlap_pct")),
                    return_corr_36m=as_decimal(row.get("return_corr_36m")),
                    style_distance=as_decimal(row.get("style_distance")),
                    blended_score=as_decimal(row.get("blended_score")),
                )
            )
        return out

    def marginal(self, user_id: UserId, as_of: date, scheme_id: SchemeId) -> Marginal:
        row = (self._d.get("marginal") or {}).get(scheme_id)
        if row is None:
            raise FixtureError(f"no marginal fixture for {scheme_id!r}")
        position = as_decimal(row["position_inr"])
        assert position is not None
        return Marginal(
            scheme_id=SchemeId(scheme_id),
            position_inr=position,
            new_issuers=int(row["new_issuers"]) if row.get("new_issuers") else None,
            new_exposure_inr=as_decimal(row.get("new_exposure_inr")),
            new_exposure_pct=as_decimal(row.get("new_exposure_pct")),
            hhi_with=as_decimal(row.get("hhi_with")),
            hhi_without=as_decimal(row.get("hhi_without")),
            hhi_delta=as_decimal(row.get("hhi_delta")),
            effective_n_delta=as_decimal(row.get("effective_n_delta")),
            style_shift_pp=as_decimal(row.get("style_shift_pp")),
            fee_cost_inr=as_decimal(row.get("fee_cost_inr")),
        )

    def tilts(
        self,
        user_id: UserId,
        as_of: date,
        dimension: str,
        basis: ClassificationBasis,
    ) -> list[Tilt]:
        rows = (self._d.get("tilts") or {}).get(dimension) or []
        out = []
        for row in rows:
            inr = as_decimal(row["exposure_inr"])
            pct = as_decimal(row["exposure_pct"])
            coverage = as_decimal(row["coverage_pct"])
            assert inr is not None and pct is not None and coverage is not None
            out.append(
                Tilt(
                    dimension=dimension,
                    dimension_value=str(row["dimension_value"]),
                    exposure_inr=inr,
                    exposure_pct=pct,
                    classification_basis=basis,
                    coverage_pct=coverage,
                    benchmark_pct=as_decimal(row.get("benchmark_pct")),
                    active_tilt_pp=as_decimal(row.get("active_tilt_pp")),
                    benchmark_index_id=IndexId(str(row["benchmark_index_id"]))
                    if row.get("benchmark_index_id")
                    else None,
                    mcap_basis=as_date(row.get("mcap_basis")),
                )
            )
        return out

    def summary(self, user_id: UserId, as_of: date) -> PortfolioSummary:
        row = self._lt.get("summary") or {}
        coverage = as_decimal(self._lt.get("coverage_pct"))
        unresolved = as_decimal(self._lt.get("unresolved_pct"))

        def d(key: str) -> Decimal | None:
            return as_decimal(row.get(key))

        def i(key: str) -> int | None:
            v = row.get(key)
            return int(v) if v is not None else None

        total = d("total_value_inr")
        fund = d("fund_value_inr")
        direct = d("direct_value_inr")
        assert total is not None and fund is not None and direct is not None
        assert coverage is not None and unresolved is not None

        computed_at = as_datetime(row.get("computed_at")) or datetime(2026, 8, 1, 3, 15)
        return PortfolioSummary(
            user_id=UserId(user_id),
            as_of=as_of,
            total_value_inr=total,
            fund_value_inr=fund,
            direct_value_inr=direct,
            coverage_pct=coverage,
            unresolved_pct=unresolved,
            confidence=str(row.get("confidence", "medium")),
            caveats=list(self._lt.get("caveats") or []),
            computed_at=computed_at,
            invested_net_inr=d("invested_net_inr"),
            unrealised_pnl_inr=d("unrealised_pnl_inr"),
            realised_pnl_todate=d("realised_pnl_todate"),
            portfolio_xirr=d("portfolio_xirr"),
            portfolio_twrr_ann=d("portfolio_twrr_ann"),
            blended_ter=d("blended_ter"),
            annual_fee_inr=d("annual_fee_inr"),
            scheme_count=i("scheme_count"),
            folio_count=i("folio_count"),
            issuer_count=i("issuer_count"),
            direct_issuer_count=i("direct_issuer_count"),
            schemes_covered=i("schemes_covered"),
            schemes_stale=i("schemes_stale"),
            schemes_quarantined=i("schemes_quarantined"),
            worst_staleness_days=i("worst_staleness_days"),
        )

    def sector_exposure(
        self, user_id: UserId, as_of: date, basis: ClassificationBasis
    ) -> dict[str, Decimal]:
        block = (self._d.get("sector_exposure") or {}).get(basis.value) or {}
        out = {}
        for k, v in block.items():
            value = as_decimal(v)
            assert value is not None
            out[str(k)] = value
        return out
