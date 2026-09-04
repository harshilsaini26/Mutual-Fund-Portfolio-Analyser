"""`FakeMarketDataFeed` and `FakeMarketIntelligence`.

MODULE_5.md §14.4 and §14.1. The feed is M5's market-wide view of M0; the
intelligence fake is canned M5 output so M6 can be built before M5 exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from src.common.contracts.entity import Constituent, Holding, SchemeWeight
from src.common.contracts.market import IndexPoint, PricePoint
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
    IndexId,
    Isin,
    IssuerId,
    McapBucket,
    SchemeId,
    SectorId,
    UniverseId,
    UniverseScope,
    UserId,
)
from src.m0_data.providers.fake import FakeFundDataProvider
from src.m3_lookthrough.providers.fake import FakeLookThroughDataProvider
from src.m5_market.providers.intelligence import (
    Announcement,
    CompanyView,
    Contributor,
    Conviction,
    IssuerFlow,
    ManualWatch,
    Rotation,
    SectorContext,
    SectorDetail,
    SectorRow,
    WatchEntry,
)


def _d(row: Mapping[str, Any], key: str) -> Decimal | None:
    """Optional Decimal field. Module-level so it never closes over a loop var."""
    return as_decimal(row.get(key))


def _i(row: Mapping[str, Any], key: str) -> int | None:
    """Optional int field."""
    v = row.get(key)
    return int(v) if v is not None else None


class FakeMarketDataFeed:
    """M5's view of M0 — fixture-backed. Satisfies `MarketDataFeed`."""

    def __init__(self, store: FixtureStore | None = None) -> None:
        self._store = store or default_store()
        self._h = self._store.section("holdings")
        self._m = self._store.section("market")
        self._fd = FakeFundDataProvider(self._store)
        self._ltd = FakeLookThroughDataProvider(self._store)

    # --- holdings across ALL schemes ---------------------------------------

    def schemes_with_disclosure(
        self, as_of: date, status_ok_only: bool
    ) -> list[SchemeId]:
        """`status_ok_only` drops quarantined files.

        A misparsed quantity column entering the flow engine becomes a
        fabricated industry-wide buy, so this filter is not optional in
        practice — SBI-SC is quarantined in the fixture precisely to prove it.
        """
        out = []
        for scheme_id in self._h.get("disclosure_quality") or {}:
            if status_ok_only:
                try:
                    q = self._fd.disclosure_quality(SchemeId(scheme_id), as_of)
                except FixtureError:
                    continue
                if q.validation_status.value == "quarantined":
                    continue
            out.append(SchemeId(scheme_id))
        return sorted(out)

    def holdings(self, scheme_id: SchemeId, as_of: date) -> list[Holding]:
        return self._fd.holdings(scheme_id, as_of)

    def all_scheme_weights(
        self, issuer_id: IssuerId, as_of: date, equity_only: bool
    ) -> list[SchemeWeight]:
        """Every scheme in the parsed universe holding this issuer.

        `equity_only` is True for conviction scoring: a fund holding a
        company's NCD is not expressing equity conviction in it (§8.4).
        """
        out = []
        for scheme_id in self.schemes_with_disclosure(as_of, status_ok_only=True):
            try:
                rows = self._fd.holdings(scheme_id, as_of)
            except FixtureError:
                continue
            weight = Decimal(0)
            qty = Decimal(0)
            mv = Decimal(0)
            found = False
            for r in rows:
                if r.issuer_id != issuer_id:
                    continue
                if equity_only and r.instrument_class.value != "equity":
                    continue
                found = True
                weight += r.pct_normalised
                qty += r.quantity or Decimal(0)
                mv += r.market_value
            if found:
                out.append(
                    SchemeWeight(
                        scheme_id=scheme_id,
                        issuer_id=issuer_id,
                        as_of_date=as_of,
                        weight=weight,
                        quantity=qty or None,
                        market_value=mv,
                    )
                )
        return out

    def prev_disclosure_date(self, as_of: date) -> date | None:
        seen: set[date] = set()
        for dates in (self._h.get("disclosure_dates") or {}).values():
            for v in dates:
                d = as_date(v)
                if d is not None and d < as_of:
                    seen.add(d)
        return max(seen) if seen else None

    def all_active_schemes(self, as_of: date) -> list[SchemeId]:
        return sorted(
            SchemeId(str(r["scheme_id"]))
            for r in self._store.table("market_data", "schemes")
            if r.get("status") == "active"
        )

    def scheme_aum(self, scheme_id: SchemeId, as_of: date) -> Decimal | None:
        block = (self._m.get("scheme_aum_market") or {}).get(scheme_id)
        return as_decimal(fixture_key(block, as_of)) if block else None

    def total_industry_aum(self, as_of: date) -> Decimal | None:
        row = fixture_key(self._m.get("coverage") or {}, as_of)
        return as_decimal(row.get("total_aum_inr")) if row else None

    def coverage_pct(self, as_of: date) -> Decimal:
        """`PLAN.md` §4.10 — travels onto every cross-scheme figure."""
        row = fixture_key(self._m.get("coverage") or {}, as_of)
        value = as_decimal(row.get("coverage_pct")) if row else None
        if value is None:
            raise FixtureError(f"no coverage stat for {as_of}")
        return value

    # --- prices and indices ------------------------------------------------

    def price(self, isin: Isin, on: date) -> PricePoint | None:
        return self._ltd.price(isin, on)

    def price_series(self, isin: Isin, start: date, end: date) -> list[PricePoint]:
        out = []
        for row in (self._m.get("prices") or {}).get(isin) or []:
            d = as_date(row["price_date"])
            assert d is not None
            if start <= d <= end:
                point = self._ltd.price(isin, d)
                if point:
                    out.append(point)
        return sorted(out, key=lambda p: p.price_date)

    def adjustment_factor(self, isin: Isin, frm: date, to: date) -> Decimal:
        return self._fd.adjustment_factor(isin, frm, to)

    def moving_average(self, isin: Isin, on: date, window: int) -> Decimal | None:
        block = (self._m.get("moving_averages") or {}).get(isin)
        by_date = fixture_key(block, on) if block else None
        return as_decimal(by_date.get(window)) if by_date else None

    def index_constituents(self, index_id: IndexId, as_of: date) -> list[Constituent]:
        block = (self._m.get("index_constituents") or {}).get(index_id)
        rows = fixture_key(block, as_of) if block else None
        out = []
        for row in rows or []:
            out.append(
                Constituent(
                    index_id=str(index_id),
                    as_of_date=as_of,
                    isin=Isin(str(row["isin"])),
                    issuer_id=IssuerId(str(row["issuer_id"])),
                    weight_pct=as_decimal(row.get("weight_pct")),
                    free_float_mcap_inr=as_decimal(row.get("free_float_mcap_inr")),
                )
            )
        return out

    def index_series(self, index_id: IndexId, start: date, end: date) -> list[IndexPoint]:
        return self._fd.index_series(index_id, start, end)

    def index_sector_weights(self, as_of: date) -> dict[str, Decimal]:
        block = fixture_key(self._m.get("index_sector_weights") or {}, as_of) or {}
        out = {}
        for k, v in block.items():
            value = as_decimal(v)
            assert value is not None
            out[str(k)] = value
        return out

    # --- entities ----------------------------------------------------------

    def sector_of(self, issuer_id: IssuerId, on: date) -> str | None:
        return self._fd.sector_of(issuer_id, on)

    def mcap_bucket(
        self, issuer_id: IssuerId, on: date, mcap_basis: date | None = None
    ) -> McapBucket | None:
        return self._fd.mcap_list_as_of(mcap_basis or on).bucket_for(issuer_id)

    def market_cap(self, issuer_id: IssuerId, on: date) -> Decimal | None:
        return self._fd.mcap_list_as_of(on).mcap_for(issuer_id)

    def free_float_mcap(self, issuer_id: IssuerId, on: date) -> Decimal | None:
        return as_decimal((self._m.get("free_float_mcap") or {}).get(issuer_id))

    def primary_isin(self, issuer_id: IssuerId) -> Isin | None:
        row = (self._h.get("issuers") or {}).get(issuer_id)
        if not row or not row.get("primary_isin"):
            return None
        return Isin(str(row["primary_isin"]))

    def is_synthetic(self, issuer_id: IssuerId) -> bool:
        row = (self._h.get("issuers") or {}).get(issuer_id)
        return bool(row.get("synthetic", False)) if row else False

    def adv_20d(self, issuer_id: IssuerId, on: date) -> Decimal | None:
        return as_decimal((self._m.get("adv_20d") or {}).get(issuer_id))

    def beta(self, issuer_id: IssuerId, on: date) -> Decimal | None:
        return as_decimal((self._m.get("betas") or {}).get(issuer_id))

    def trading_days(self, start: date, end: date) -> list[date]:
        out: list[date] = []
        for days in (self._m.get("trading_days") or {}).values():
            for v in days:
                d = as_date(v)
                if d is not None and start <= d <= end:
                    out.append(d)
        return sorted(set(out))

    # --- user config -------------------------------------------------------

    def manual_watchlist(self, user_id: UserId) -> list[ManualWatch]:
        block = self._store.section("derived").get("manual_watchlist") or {}
        rows = block.get(user_id) or []
        out = []
        for row in rows:
            added = as_datetime(row["added_at"])
            assert added is not None
            out.append(
                ManualWatch(
                    user_id=UserId(user_id),
                    issuer_id=IssuerId(str(row["issuer_id"])),
                    added_at=added,
                    notes=row.get("notes"),
                )
            )
        return out


class FakeMarketIntelligence:
    """Canned M5 outputs, so M6 can be built first.

    Satisfies `MarketIntelligence`. Every market-facing method defaults
    `scope` to `watch`: that default is how §3.1 rule 2 — the over-monitoring
    constraint — is enforced in the API rather than left to each caller.
    """

    def __init__(self, store: FixtureStore | None = None) -> None:
        self._store = store or default_store()
        self._d = self._store.section("derived")
        self._feed = FakeMarketDataFeed(self._store)

    def _watch_ids(self, user_id: UserId, as_of: date) -> set[str]:
        return {str(w.issuer_id) for w in self.watch_universe(user_id, as_of)}

    def sector_dashboard(
        self,
        as_of: date,
        universe_id: UniverseId,
        scope: UniverseScope = "watch",
    ) -> list[SectorRow]:
        rows = fixture_key(self._d.get("sector_rows") or {}, as_of) or []
        out = []
        for row in rows:
            coverage = as_decimal(row["coverage_pct"])
            assert coverage is not None
            out.append(
                SectorRow(
                    sector_id=SectorId(str(row["sector_id"])),
                    as_of=as_of,
                    universe_id=UniverseId(str(row["universe_id"])),
                    constituent_count=int(row["constituent_count"]),
                    priced_count=int(row["priced_count"]),
                    coverage_pct=coverage,
                    return_1d=_d(row, "return_1d"),
                    median_return_1d=_d(row, "median_return_1d"),
                    index_level=_d(row, "index_level"),
                    advancers=_i(row, "advancers"),
                    decliners=_i(row, "decliners"),
                    unchanged=_i(row, "unchanged"),
                    pct_above_50dma=_d(row, "pct_above_50dma"),
                    pct_above_200dma=_d(row, "pct_above_200dma"),
                    total_mcap_inr=_d(row, "total_mcap_inr"),
                    total_traded_value_inr=_d(row, "total_traded_value_inr"),
                    ret_1w=_d(row, "ret_1w"),
                    ret_1m=_d(row, "ret_1m"),
                    ret_3m=_d(row, "ret_3m"),
                    ret_6m=_d(row, "ret_6m"),
                    ret_1y=_d(row, "ret_1y"),
                    ret_ytd=_d(row, "ret_ytd"),
                    rel_strength_1m=_d(row, "rel_strength_1m"),
                    rel_strength_3m=_d(row, "rel_strength_3m"),
                    rel_strength_1y=_d(row, "rel_strength_1y"),
                    volatility_1y=_d(row, "volatility_1y"),
                    max_dd_1y=_d(row, "max_dd_1y"),
                    rank_1m=_i(row, "rank_1m"),
                    rank_3m=_i(row, "rank_3m"),
                    rank_1y=_i(row, "rank_1y"),
                )
            )
        return out

    def sector_detail(self, sector_id: SectorId, as_of: date) -> SectorDetail:
        rows = self.sector_dashboard(as_of, UniverseId("NSE500"), scope="market")
        match = next((r for r in rows if r.sector_id == sector_id), None)
        if match is None:
            raise FixtureError(f"no sector row for {sector_id!r} on {as_of}")
        flows = [
            f
            for f in self.flow_leaders(as_of, "in", scope="market")
            if self._feed.sector_of(f.issuer_id, as_of) == sector_id
        ]
        conviction = [
            c
            for c in self.conviction_map(as_of, scope="market")
            if self._feed.sector_of(c.issuer_id, as_of) == sector_id
        ]
        return SectorDetail(
            sector_id=sector_id,
            as_of=as_of,
            row=match,
            constituents=[
                IssuerId(k)
                for k, v in (self._store.section("holdings").get("sectors") or {}).items()
                if v == sector_id
            ],
            top_flows=flows,
            top_conviction=conviction,
            coverage_pct=match.coverage_pct,
            caveats=[
                f"Based on {self._feed.coverage_pct(as_of)}% of industry AUM. "
                "Flows from unparsed AMCs are not included and are not estimated."
            ],
        )

    def flow_leaders(
        self,
        as_of: date,
        direction: str,
        limit: int = 20,
        sector: SectorId | None = None,
        scope: UniverseScope = "watch",
    ) -> list[IssuerFlow]:
        rows = fixture_key(self._d.get("issuer_flows") or {}, as_of) or []
        out = []
        for row in rows:
            prev = as_date(row["prev_as_of_date"])
            coverage = as_decimal(row["coverage_pct"])
            assert prev is not None and coverage is not None
            out.append(
                IssuerFlow(
                    as_of_date=as_of,
                    prev_as_of_date=prev,
                    issuer_id=IssuerId(str(row["issuer_id"])),
                    schemes_holding_curr=int(row["schemes_holding_curr"]),
                    schemes_holding_prev=int(row["schemes_holding_prev"]),
                    schemes_entering=int(row["schemes_entering"]),
                    schemes_exiting=int(row["schemes_exiting"]),
                    schemes_adding=int(row["schemes_adding"]),
                    schemes_trimming=int(row["schemes_trimming"]),
                    coverage_pct=coverage,
                    schemes_eligible=int(row["schemes_eligible"]),
                    schemes_total=int(row["schemes_total"]),
                    caveats=[
                        f"Based on {row['schemes_eligible']} of {row['schemes_total']} "
                        f"schemes ({coverage}% of industry AUM). Flows from unparsed "
                        "AMCs are not included and are not estimated."
                    ],
                    net_qty_adj=as_decimal(row.get("net_qty_adj")),
                    net_value_inr=as_decimal(row.get("net_value_inr")),
                    gross_bought_inr=as_decimal(row.get("gross_bought_inr")),
                    gross_sold_inr=as_decimal(row.get("gross_sold_inr")),
                    total_mf_holding_inr=as_decimal(row.get("total_mf_holding_inr")),
                    pct_of_free_float=as_decimal(row.get("pct_of_free_float")),
                    net_flow_as_pct_adv=as_decimal(row.get("net_flow_as_pct_adv")),
                )
            )

        if sector is not None:
            out = [f for f in out if self._feed.sector_of(f.issuer_id, as_of) == sector]
        if direction == "in":
            out = [f for f in out if (f.net_value_inr or Decimal(0)) > 0]
        elif direction == "out":
            out = [f for f in out if (f.net_value_inr or Decimal(0)) < 0]
        out.sort(key=lambda f: (-(f.net_value_inr or Decimal(0)), f.issuer_id))
        return out[:limit]

    def flow_contributors(self, as_of: date, issuer_id: IssuerId) -> list[Contributor]:
        block = fixture_key(self._d.get("flow_contributors") or {}, as_of) or {}
        rows = block.get(issuer_id) or []
        out = []
        for row in rows:
            factor = as_decimal(row["adj_factor"])
            assert factor is not None
            out.append(
                Contributor(
                    as_of_date=as_of,
                    issuer_id=issuer_id,
                    scheme_id=SchemeId(str(row["scheme_id"])),
                    action=str(row["action"]),
                    adj_factor=factor,
                    qty_delta_adj=as_decimal(row.get("qty_delta_adj")),
                    value_delta_inr=as_decimal(row.get("value_delta_inr")),
                    weight_prev=as_decimal(row.get("weight_prev")),
                    weight_curr=as_decimal(row.get("weight_curr")),
                )
            )
        return out

    def conviction_map(
        self,
        as_of: date,
        min_schemes: int = 3,
        scope: UniverseScope = "watch",
    ) -> list[Conviction]:
        rows = fixture_key(self._d.get("conviction") or {}, as_of) or []
        out = []
        for row in rows:
            if int(row["schemes_holding"]) < min_schemes:
                continue
            out.append(
                Conviction(
                    issuer_id=IssuerId(str(row["issuer_id"])),
                    as_of_date=as_of,
                    schemes_holding=int(row["schemes_holding"]),
                    schemes_high_conviction=int(row.get("schemes_high_conviction", 0)),
                    coverage_pct=as_decimal(row.get("coverage_pct")),
                    consensus_direction=str(row.get("consensus_direction", "stable")),
                    max_weight_pct=as_decimal(row.get("max_weight_pct")),
                    max_weight_scheme_id=(
                        SchemeId(str(row["max_weight_scheme_id"]))
                        if row.get("max_weight_scheme_id")
                        else None
                    ),
                    median_weight_pct=as_decimal(row.get("median_weight_pct")),
                    wtd_avg_weight_pct=as_decimal(row.get("wtd_avg_weight_pct")),
                    dispersion=as_decimal(row.get("dispersion")),
                    conviction_score=as_decimal(row.get("conviction_score")),
                )
            )
        return out

    def company_page(self, issuer_id: IssuerId, as_of: date) -> CompanyView:
        block = (self._d.get("company_snapshots") or {}).get(issuer_id)
        row = fixture_key(block, as_of) if block else None
        if row is None:
            raise FixtureError(f"no company snapshot for {issuer_id!r} on {as_of}")
        # Bhavcopy lands 18:30 IST; a snapshot without an explicit stamp is
        # assumed computed then rather than left unset.
        computed_at = as_datetime(row.get("computed_at")) or as_datetime(
            f"{as_of}T18:30:00"
        )
        assert computed_at is not None

        return CompanyView(
            issuer_id=issuer_id,
            as_of=as_of,
            classification_basis=ClassificationBasis.AS_OF_HOLDING,
            computed_at=computed_at,
            primary_isin=(
                Isin(str(row["primary_isin"])) if row.get("primary_isin") else None
            ),
            sector_id=(SectorId(str(row["sector_id"])) if row.get("sector_id") else None),
            price=_d(row, "price"),
            price_chg_1d=_d(row, "price_chg_1d"),
            ret_1w=_d(row, "ret_1w"),
            ret_1m=_d(row, "ret_1m"),
            ret_3m=_d(row, "ret_3m"),
            ret_6m=_d(row, "ret_6m"),
            ret_1y=_d(row, "ret_1y"),
            mcap_inr=_d(row, "mcap_inr"),
            mcap_bucket=row.get("mcap_bucket"),
            mcap_basis=as_date(row.get("mcap_basis")),
            high_52w=_d(row, "high_52w"),
            low_52w=_d(row, "low_52w"),
            pct_from_52w_high=_d(row, "pct_from_52w_high"),
            dma_50=_d(row, "dma_50"),
            dma_200=_d(row, "dma_200"),
            volatility_1y=_d(row, "volatility_1y"),
            beta_1y=_d(row, "beta_1y"),
            adv_20d_inr=_d(row, "adv_20d_inr"),
            rel_strength_vs_sector_3m=_d(row, "rel_strength_vs_sector_3m"),
        )

    def user_sector_context(
        self, user_id: UserId, as_of: date, basis: ClassificationBasis
    ) -> list[SectorContext]:
        """`basis` is required and has no default — `CLAUDE.md` invariant 6."""
        block = (self._d.get("user_sector_context") or {}).get(user_id)
        rows = fixture_key(block, as_of) if block else None
        out = []
        for row in rows or []:
            inr = as_decimal(row["your_exposure_inr"])
            pct = as_decimal(row["your_exposure_pct"])
            coverage = as_decimal(row["coverage_pct"])
            assert inr is not None and pct is not None and coverage is not None
            out.append(
                SectorContext(
                    user_id=UserId(user_id),
                    as_of=as_of,
                    sector_id=SectorId(str(row["sector_id"])),
                    your_exposure_inr=inr,
                    your_exposure_pct=pct,
                    classification_basis=basis,
                    coverage_pct=coverage,
                    index_weight_pct=_d(row, "index_weight_pct"),
                    active_tilt_pp=_d(row, "active_tilt_pp"),
                    mf_industry_weight_pct=_d(row, "mf_industry_weight_pct"),
                    vs_industry_pp=_d(row, "vs_industry_pp"),
                    sector_ret_1w=_d(row, "sector_ret_1w"),
                    sector_ret_1m=_d(row, "sector_ret_1m"),
                    sector_ret_3m=_d(row, "sector_ret_3m"),
                    contribution_1m=_d(row, "contribution_1m"),
                    mf_flow_1m_inr=_d(row, "mf_flow_1m_inr"),
                )
            )
        return out

    def watch_universe(self, user_id: UserId, as_of: date) -> list[WatchEntry]:
        block = (self._d.get("watch_universe") or {}).get(user_id)
        rows = fixture_key(block, as_of) if block else None
        out = []
        for row in rows or []:
            inr = as_decimal(row["exposure_inr"])
            pct = as_decimal(row["exposure_pct"])
            assert inr is not None and pct is not None
            out.append(
                WatchEntry(
                    user_id=UserId(user_id),
                    as_of=as_of,
                    issuer_id=IssuerId(str(row["issuer_id"])),
                    exposure_inr=inr,
                    exposure_pct=pct,
                    via_funds=int(row["via_funds"]),
                    direct=bool(row["direct"]),
                    in_watchlist=bool(row["in_watchlist"]),
                    relevance_rank=(
                        int(row["relevance_rank"]) if row.get("relevance_rank") else None
                    ),
                    added_at=as_datetime(row.get("added_at")),
                )
            )
        return out

    def rotation(self, as_of: date, universe_id: UniverseId) -> list[Rotation]:
        rows = fixture_key(self._d.get("rotation") or {}, as_of) or []
        return [
            Rotation(
                as_of=as_of,
                sector_id=SectorId(str(row["sector_id"])),
                universe_id=UniverseId(str(row["universe_id"])),
                quadrant=str(row["quadrant"]),
                rs_ratio=as_decimal(row.get("rs_ratio")),
                rs_momentum=as_decimal(row.get("rs_momentum")),
                tail_json=row.get("tail_json"),
            )
            for row in rows
            if row["universe_id"] == universe_id
        ]

    def announcements(self, issuer_id: IssuerId, limit: int = 20) -> list[Announcement]:
        """Headline and URL only — never the body (`PLAN.md` §3.2)."""
        rows = (self._d.get("announcements") or {}).get(issuer_id) or []
        out = []
        for row in rows:
            announced = as_datetime(row["announced_at"])
            assert announced is not None
            out.append(
                Announcement(
                    announcement_id=str(row["announcement_id"]),
                    issuer_id=issuer_id,
                    announced_at=announced,
                    headline=str(row["headline"]),
                    url=str(row["url"]),
                    source=str(row["source"]),
                    category=row.get("category"),
                )
            )
        out.sort(key=lambda a: a.announced_at, reverse=True)
        return out[:limit]
