"""The `Fake*` providers satisfy their Protocols and honour the invariants.

`BUILD_ORDER.md` R1 step 2. A fake that diverges from its Protocol, or that
quietly relaxes an invariant, makes every test written against it worthless —
so the conformance checks matter as much as the behaviour ones.
"""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.fixtures import DecimalSafeLoader, FixtureError, FixtureStore, load_yaml
from src.common.types import (
    ClassificationBasis,
    IndexId,
    Isin,
    IssuerId,
    SchemeId,
    UniverseId,
    UserId,
)
from src.m0_data.providers.fake import FakeFundDataProvider, FakeMarketDataProvider
from src.m0_data.providers.fund_data import FundDataProvider
from src.m0_data.providers.market_data import MarketDataProvider
from src.m2_fund.providers.characteristics import SchemeCharacteristics
from src.m2_fund.providers.fake import FakeSchemeCharacteristics
from src.m3_lookthrough.providers.data import LookThroughDataProvider
from src.m3_lookthrough.providers.fake import (
    FakeLookThroughDataProvider,
    FakeLookThroughProvider,
)
from src.m3_lookthrough.providers.lookthrough import LookThroughProvider
from src.m4_risk.providers.fake import FakeRiskInputs
from src.m4_risk.providers.inputs import RiskInputs
from src.m5_market.providers.fake import FakeMarketDataFeed, FakeMarketIntelligence
from src.m5_market.providers.feed import MarketDataFeed
from src.m5_market.providers.intelligence import MarketIntelligence

USER = UserId("USER-01")
AS_OF = date(2026, 7, 31)

PAIRS = [
    (FakeMarketDataProvider, MarketDataProvider),
    (FakeFundDataProvider, FundDataProvider),
    (FakeSchemeCharacteristics, SchemeCharacteristics),
    (FakeLookThroughDataProvider, LookThroughDataProvider),
    (FakeLookThroughProvider, LookThroughProvider),
    (FakeRiskInputs, RiskInputs),
    (FakeMarketDataFeed, MarketDataFeed),
    (FakeMarketIntelligence, MarketIntelligence),
]


# --- conformance -----------------------------------------------------------


@pytest.mark.parametrize(("fake", "proto"), PAIRS, ids=[f.__name__ for f, _ in PAIRS])
def test_fake_implements_every_protocol_method(fake: type, proto: type) -> None:
    """Every method the Protocol declares exists on the fake, with a
    signature that accepts everything the Protocol promises callers can pass.
    """
    missing = []
    mismatched = []
    for name, proto_method in inspect.getmembers(proto, inspect.isfunction):
        if name.startswith("_"):
            continue
        impl = getattr(fake, name, None)
        if impl is None:
            missing.append(name)
            continue
        proto_params = set(inspect.signature(proto_method).parameters)
        impl_params = set(inspect.signature(impl).parameters)
        if not proto_params <= impl_params:
            mismatched.append(f"{name}: cannot accept {proto_params - impl_params}")

    assert not missing, f"{fake.__name__} is missing {missing}"
    assert not mismatched, f"{fake.__name__} signature drift: {mismatched}"


@pytest.mark.parametrize(("fake", "proto"), PAIRS, ids=[f.__name__ for f, _ in PAIRS])
def test_fake_constructs_and_shares_one_store(fake: type, proto: type) -> None:
    """All fakes read the same fixture set, so they cannot contradict each other."""
    store = FixtureStore(Path(__file__).resolve().parents[1] / "fixtures" / "slice_zero")
    instance = fake(store)
    assert instance is not None


# --- the loader: no float may enter ----------------------------------------


def test_loader_produces_decimal_never_float() -> None:
    """PyYAML's default resolver would make `142.839104` a float.

    `DecimalSafeLoader` is the single point where that is prevented, so it is
    checked directly rather than only through its consumers.
    """
    import io

    import yaml

    data = yaml.load(
        io.StringIO("a: 142.839104\nb: '0.1'\nc: 1\nd: 2026-07-31\n"),
        Loader=DecimalSafeLoader,
    )
    assert isinstance(data["a"], Decimal)
    assert data["a"] == Decimal("142.839104")
    assert isinstance(data["b"], str)
    assert isinstance(data["c"], int)
    assert isinstance(data["d"], date)


@pytest.mark.parametrize("literal", [".inf", "-.inf", ".nan"])
def test_loader_rejects_non_finite(literal: str) -> None:
    """None of these is a valid money, unit, NAV or weight.

    Letting one through would propagate silently through an aggregation and
    surface as a blank cell three layers downstream.
    """
    import io

    import yaml

    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.load(io.StringIO(f"v: {literal}\n"), Loader=DecimalSafeLoader)


def test_loader_rescues_real_fixture_values_a_plain_loader_would_float() -> None:
    """The loader does real work on the committed fixtures, not just a toy input.

    Loading each fixture twice — once with PyYAML's `SafeLoader`, once with
    `DecimalSafeLoader` — finds every path a naive load turns into a float, then
    asserts the safe loader returns a Decimal of the same value at that exact
    path.

    Checking only `DecimalSafeLoader` output would be vacuous: it converts
    floats by construction, so it can never report one.
    """
    import yaml

    root = Path(__file__).resolve().parents[1] / "fixtures" / "slice_zero"
    files = sorted(root.glob("*.yaml"))
    assert files, "no fixtures found"

    # Paths are lists of the real key objects, not strings: YAML resolves bare
    # dates to `datetime.date`, so a stringified path cannot index back in.
    def float_paths(node: object, path: list[object]) -> list[list[object]]:
        if isinstance(node, float):
            return [path]
        if isinstance(node, dict):
            return [p for k, v in node.items() for p in float_paths(v, [*path, k])]
        if isinstance(node, list):
            return [p for i, v in enumerate(node) for p in float_paths(v, [*path, i])]
        return []

    def at(node: object, path: list[object]) -> object:
        for step in path:
            node = node[step]  # type: ignore[index]
        return node

    total = 0
    for f in files:
        naive = yaml.safe_load(f.read_text(encoding="utf-8"))
        safe = load_yaml(f)
        for path in float_paths(naive, []):
            total += 1
            rescued = at(safe, path)
            where = f"{f.name}:{'.'.join(str(p) for p in path)}"
            assert isinstance(rescued, Decimal), (
                f"{where} is {type(rescued).__name__} under DecimalSafeLoader"
            )
            assert rescued == Decimal(str(at(naive, path)))

    assert total > 0, (
        "no fixture value would have become a float, so this proves nothing — "
        "the fixtures need at least one unquoted decimal"
    )


# --- M0 behaviour ----------------------------------------------------------


def test_resolution_never_falls_back_from_isin_to_name() -> None:
    """§11.3. An ISIN that matches nothing is unresolved, not a name lookup.

    Falling through would resolve a Direct-plan ISIN onto the Regular-plan
    scheme by name — the ~1%/year silent error.
    """
    m = FakeMarketDataProvider()
    ref = m.resolve_scheme(Isin("INF-DOES-NOT-EXIST"), "HDFC Top 100 Fund", None, AS_OF)
    assert ref.scheme_id is None
    assert ref.confidence.value == "unresolved"


def test_merger_chain_is_followed_only_after_the_merger_date() -> None:
    """A transaction before the merger belongs to the predecessor."""
    m = FakeMarketDataProvider()
    after = m.resolve_scheme(Isin("INF090I01_29"), "", None, date(2026, 7, 31))
    before = m.resolve_scheme(Isin("INF090I01_29"), "", None, date(2020, 1, 1))

    assert after.scheme_id == "HDFC-TOP100-DIR"
    assert after.merged_from == "FRANKLIN-BLUE-DIR"
    assert before.scheme_id == "FRANKLIN-BLUE-DIR"
    assert before.merged_from is None


def test_merger_ratio_is_not_unity() -> None:
    """Per-unit figures must be rescaled by it, so a 1:1 fixture proves nothing."""
    links = FakeMarketDataProvider().merger_chain(SchemeId("FRANKLIN-BLUE-DIR"))
    assert len(links) == 1
    assert (links[0].ratio_num, links[0].ratio_den) == (731, 1000)


def test_ter_is_point_in_time() -> None:
    """TER changes; a single number would be wrong for any window spanning it."""
    m = FakeMarketDataProvider()
    assert m.ter(SchemeId("HDFC-TOP100-DIR"), date(2023, 6, 1)) == Decimal("1.1800")
    assert m.ter(SchemeId("HDFC-TOP100-DIR"), date(2025, 6, 1)) == Decimal("1.0400")


def test_missing_nav_raises_rather_than_inventing_one() -> None:
    """§11.4 guarantees NAV coverage for held schemes.

    A fake that returned zero here would let a test pass on a portfolio the
    real system cannot price.
    """
    with pytest.raises(FixtureError):
        FakeMarketDataProvider().nav(SchemeId("HDFC-TOP100-DIR"), date(2010, 1, 1))


def test_mcap_classification_is_point_in_time() -> None:
    """The same issuer sits in different buckets under different AMFI lists.

    Applying today's list to a historical portfolio manufactures phantom drift.
    """
    f = FakeFundDataProvider()
    assert (
        f.mcap_list_as_of(date(2026, 7, 31)).bucket_for(IssuerId("ISS-PERSISTENT"))
        is not None
    )
    now = f.mcap_list_as_of(date(2026, 7, 31)).bucket_for(IssuerId("ISS-PERSISTENT"))
    then = f.mcap_list_as_of(date(2026, 3, 1)).bucket_for(IssuerId("ISS-PERSISTENT"))
    assert now != then, "fixture must contain a reclassification to be useful"


def test_weights_normalise_but_raw_does_not() -> None:
    """MODULE_0.md §7.3, and the reason aggregation never reads `pct_to_nav`."""
    f = FakeFundDataProvider()
    for scheme_id in (SchemeId("HDFC-TOP100-DIR"), SchemeId("AXIS-MID-DIR")):
        rows = f.holdings(scheme_id, AS_OF)
        assert sum((r.pct_normalised for r in rows), Decimal(0)) == Decimal("100.000000")

    raw = sum(
        (
            r.pct_to_nav
            for r in f.holdings(SchemeId("AXIS-MID-DIR"), AS_OF)
            if r.pct_to_nav
        ),
        Decimal(0),
    )
    assert raw == Decimal("100.140000")


# --- M3 behaviour ----------------------------------------------------------


def test_issuer_weights_collapse_multiple_instruments() -> None:
    """Join on `issuer_id`, never `isin`.

    The fixture holds Reliance twice — equity and an NCD. One exposure.
    """
    weights, _ = FakeLookThroughDataProvider().scheme_issuer_weights(
        SchemeId("HDFC-TOP100-DIR"), AS_OF, "disclosed"
    )
    reliance = [w for w in weights if w.issuer_id == "ISS-RELIANCE"]
    assert len(reliance) == 1
    assert reliance[0].weight_disclosed == Decimal("9.550000")  # 8.448 + 1.102


def test_drift_basis_falls_back_and_says_so() -> None:
    """The returned basis is the one used, not the one asked for."""
    _, basis = FakeLookThroughDataProvider().scheme_issuer_weights(
        SchemeId("HDFC-TOP100-DIR"), AS_OF, "drift_adj"
    )
    assert basis == "disclosed"


def test_closure_holds_within_one_rupee() -> None:
    """MODULE_3.md: sum of exposures == position values + direct, +/- Rs 1.

    Asserted at runtime in the real engine, not only in tests.
    """
    lt = FakeLookThroughProvider()
    exposures = lt.exposures(USER, AS_OF)
    summary = lt.summary(USER, AS_OF)

    total = sum((e.exposure_inr for e in exposures), Decimal(0))
    expected = summary.fund_value_inr + summary.direct_value_inr
    assert abs(total - expected) <= Decimal(1)
    assert total != expected, (
        "fixture rounds to exactly equal; a tolerance bug would go unnoticed"
    )


def test_exposures_are_deterministically_ordered() -> None:
    """§15.3: descending exposure_inr, tie-broken by issuer_id.

    M6 caches on payload hashes; unstable ordering would bust every cache.
    """
    rows = FakeLookThroughProvider().exposures(USER, AS_OF)
    keys = [(-e.exposure_inr, e.issuer_id) for e in rows]
    assert keys == sorted(keys)


def test_unresolved_and_no_disclosure_are_present_not_dropped() -> None:
    """`PLAN.md` §4.10. Missing mass stays visible as a line item."""
    ids = {e.issuer_id for e in FakeLookThroughProvider().exposures(USER, AS_OF)}
    assert "__UNRESOLVED__" in ids
    assert "__NO_DISCLOSURE__" in ids


def test_quarantined_scheme_is_excluded_but_its_value_is_not() -> None:
    """§14.3: exclude from look-through, still show the position value."""
    chars = FakeSchemeCharacteristics()
    assert chars.quality(SchemeId("SBI-SC-DIR"), AS_OF).usable_for_lookthrough is False

    lt = FakeLookThroughProvider()
    no_disc = next(
        e for e in lt.exposures(USER, AS_OF) if e.issuer_id == "__NO_DISCLOSURE__"
    )
    ledger_value = FakeLookThroughDataProvider().position_value(
        USER, SchemeId("SBI-SC-DIR"), AS_OF
    )
    assert no_disc.exposure_inr == ledger_value


def test_concentration_excludes_synthetics_from_the_denominator() -> None:
    """§8.2, the denominator trap.

    Cash is ~49% of this portfolio. Counting it as an issuer would drag top1
    from 45% to ~22% and make a concentrated portfolio look diversified.
    """
    lt = FakeLookThroughProvider()
    conc = lt.concentration(USER, AS_OF, "equity")
    exposures = lt.exposures(USER, AS_OF)

    real = [e for e in exposures if not e.is_synthetic]
    assert conc.issuer_count == len(real)
    assert conc.largest_issuer_id == "ISS-HDFCBANK"

    # The same issuer, two denominators. `exposure_pct` is of the whole
    # portfolio — cash included — while `top1_pct` is of the equity pool alone.
    # If synthetics leaked into the concentration denominator the two would
    # converge, and a 45%-concentrated portfolio would read as 22%.
    largest = next(e for e in exposures if e.issuer_id == "ISS-HDFCBANK")
    assert conc.top1_pct is not None
    assert largest.exposure_pct == Decimal("21.858421")
    assert conc.top1_pct == Decimal("45.181322")
    assert conc.top1_pct > largest.exposure_pct * Decimal(2)


def test_cashflow_scope_changes_the_answer() -> None:
    """`PLAN.md` §9.6: switch legs at scheme scope, excluded at portfolio scope.

    They net to zero, so the totals agree while the shapes differ — which is
    exactly why both must be labelled.
    """
    d = FakeLookThroughDataProvider()
    portfolio = d.portfolio_cashflows(USER, AS_OF, "portfolio")
    scheme = d.portfolio_cashflows(USER, AS_OF, "scheme")

    assert len(scheme) > len(portfolio)
    assert sum(a for _, a in scheme) == sum(a for _, a in portfolio)


def test_recursion_guard_blocks_cycles_and_depth() -> None:
    """FoF recursion: depth 2, cycle-guarded."""
    d = FakeLookThroughDataProvider()
    with d.recursion_guard(SchemeId("A")):
        assert d.recursion_path() == {SchemeId("A")}
        with pytest.raises(FixtureError):
            with d.recursion_guard(SchemeId("A")):
                pass
        with d.recursion_guard(SchemeId("B")), pytest.raises(FixtureError):
            with d.recursion_guard(SchemeId("C")):
                pass
    assert d.recursion_path() == set()


# --- M4 behaviour ----------------------------------------------------------


def test_risk_inputs_m3_block_is_pure_delegation() -> None:
    """`BUILD_ORDER.md` R1's amendment, proved at runtime.

    Each of the four M3-block methods must return exactly what
    `LookThroughProvider` returns — no arithmetic in the adapter.
    """
    ltp = FakeLookThroughProvider()
    ri = FakeRiskInputs(lookthrough=ltp)

    assert ri.exposures(USER, AS_OF) == ltp.exposures(USER, AS_OF)
    assert ri.concentration(USER, AS_OF, "equity") == ltp.concentration(
        USER, AS_OF, "equity"
    )
    assert ri.max_pairwise_overlap(USER, AS_OF) == ltp.max_pairwise_overlap(USER, AS_OF)
    assert ri.sector_exposure(USER, AS_OF, ClassificationBasis.CURRENT) == (
        ltp.sector_exposure(USER, AS_OF, ClassificationBasis.CURRENT)
    )


def test_index_meta_exposes_pri_so_callers_can_suppress() -> None:
    """`PLAN.md` §9.4. A PRI index overstates alpha by the dividend yield."""
    ri = FakeRiskInputs()
    assert ri.index_meta(IndexId("NIFTY100-TRI")).is_total_return is True
    assert ri.index_meta(IndexId("NIFTY100-PRI")).is_total_return is False


def test_mcap_basis_pins_the_amfi_list() -> None:
    """DECISIONS SZ-05. Without the parameter, M4 and M3 would disagree."""
    ri = FakeRiskInputs()
    pinned = ri.mcap_bucket(
        IssuerId(IssuerId("ISS-PERSISTENT")), AS_OF, mcap_basis=date(2026, 1, 1)
    )
    default = ri.mcap_bucket(IssuerId(IssuerId("ISS-PERSISTENT")), AS_OF)
    assert pinned != default


def test_limit_evaluator_methods_all_resolve() -> None:
    """DECISIONS SZ-09: the six methods §13 calls but §14.4 never declared."""
    ri = FakeRiskInputs()
    assert (
        ri.issuer_exposure_pct(USER, AS_OF, IssuerId(IssuerId("ISS-HDFCBANK")))
        is not None
    )
    assert ri.sector_exposure_pct(USER, AS_OF, "Financial Services") is not None
    assert ri.mcap_exposure_pct(USER, AS_OF, "large") is not None
    assert ri.illiquid_exposure_pct(USER, AS_OF) is not None
    assert ri.current_drawdown(USER, AS_OF) is not None
    assert ri.risk_snapshot(USER, AS_OF).confidence == "medium"


def test_risk_snapshot_confidence_matches_observation_count() -> None:
    """§14.3: high needs 756 observations. 588 is medium, and says so."""
    snap = FakeRiskInputs().risk_snapshot(USER, AS_OF)
    assert snap.obs_count < 756
    assert snap.confidence == "medium"
    assert snap.caveats, "a degraded snapshot must carry a caveat"


# --- M5 behaviour ----------------------------------------------------------


def test_quarantined_scheme_never_enters_a_flow_calculation() -> None:
    """A misparsed quantity column becomes a fabricated industry-wide buy."""
    feed = FakeMarketDataFeed()
    ok_only = feed.schemes_with_disclosure(AS_OF, status_ok_only=True)
    everything = feed.schemes_with_disclosure(AS_OF, status_ok_only=False)

    assert "SBI-SC-DIR" in everything
    assert "SBI-SC-DIR" not in ok_only


def test_conviction_is_equity_only() -> None:
    """§8.4. A fund holding a company's NCD is not expressing equity conviction."""
    feed = FakeMarketDataFeed()
    with_debt = feed.all_scheme_weights(
        IssuerId(IssuerId("ISS-RELIANCE")), AS_OF, equity_only=False
    )
    equity = feed.all_scheme_weights(
        IssuerId(IssuerId("ISS-RELIANCE")), AS_OF, equity_only=True
    )

    total_with = sum((w.weight for w in with_debt), Decimal(0))
    total_eq = sum((w.weight for w in equity), Decimal(0))
    assert total_with > total_eq, "the NCD line should be excluded from equity-only"


def test_every_flow_result_carries_coverage_and_a_caveat() -> None:
    """§14.3 and `PLAN.md` §4.10: unparsed AMCs are never estimated."""
    flows = FakeMarketIntelligence().flow_leaders(AS_OF, "in", scope="market")
    assert flows
    for f in flows:
        assert f.coverage_pct < Decimal(100)
        assert f.caveats
        assert "not estimated" in f.caveats[0]


def test_watch_scope_is_the_default() -> None:
    """§3.1 rule 2 is enforced by the API's default, not by each caller."""
    sig = inspect.signature(MarketIntelligence.flow_leaders)
    assert sig.parameters["scope"].default == "watch"
    sig = inspect.signature(MarketIntelligence.conviction_map)
    assert sig.parameters["scope"].default == "watch"


def test_announcements_carry_headline_and_url_only() -> None:
    """`PLAN.md` §3.2 puts news aggregation out of scope on licensing grounds."""
    items = FakeMarketIntelligence().announcements(IssuerId("ISS-CUMMINS"))
    assert items
    fields = {f for f in items[0].__dataclass_fields__}
    assert "headline" in fields and "url" in fields
    assert "body" not in fields and "text" not in fields


def test_rotation_and_sector_dashboard_load() -> None:
    mi = FakeMarketIntelligence()
    assert mi.rotation(AS_OF, UniverseId("NSE500"))
    assert mi.sector_dashboard(AS_OF, UniverseId("NSE500"), scope="market")
    assert mi.user_sector_context(USER, AS_OF, ClassificationBasis.CURRENT)
    assert (
        mi.company_page(IssuerId(IssuerId("ISS-CUMMINS")), AS_OF).mcap_basis is not None
    )
