"""The swap: `WarehouseMarketDataProvider` against `FakeMarketDataProvider`.

`BUILD_ORDER.md` R1's whole payoff is that M0 can be built late because the
interface was frozen first. That only holds if the real implementation actually
behaves like the fake M1's entire test suite was written against — so the
important tests here parametrise **one body over both providers**, loaded with
the same data, rather than asserting agreement in prose.

Where they diverge, the divergence is named and tested. There is one, and it is
deliberate: V0-21.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from src.common.decimals import connect
from src.common.types import Confidence, Isin, Plan, SchemeId
from src.m0_data.derive.nav_adj import build_all_nav_adj
from src.m0_data.load import load_parse_result
from src.m0_data.parse.nav.amfi import parse_navall
from src.m0_data.providers.market_data import MarketDataProvider
from src.m0_data.providers.warehouse import WarehouseMarketDataProvider

from tests.conftest import migrated
from tests.fakes.loader import FixtureStore
from tests.fakes.m0 import FakeMarketDataProvider

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SAMPLE = FIXTURES / "m0" / "navall_sample.txt"
AS_OF = date(2026, 9, 4)

HDFC_DIRECT = SchemeId("INF179K01UT0")
HDFC_REGULAR = SchemeId("INF179K01608")
HDFC_IDCW_PAYOUT = SchemeId("INF179K01VL5")


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """A real warehouse, loaded from the real AMFI slice."""
    db = tmp_path_factory.mktemp("m0") / "canonical.db"
    migrated(db)
    conn = connect(str(db))
    conn.row_factory = None
    parsed = parse_navall(SAMPLE.read_text(encoding="utf-8").splitlines())
    load_parse_result(conn, parsed, "file-1", AS_OF)
    build_all_nav_adj(conn)
    conn.commit()
    import sqlite3

    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def real(warehouse: Any) -> WarehouseMarketDataProvider:
    return WarehouseMarketDataProvider(warehouse)


@pytest.fixture(scope="module")
def fake() -> FakeMarketDataProvider:
    """A fake loaded with the SAME schemes, so the two are comparable."""
    return FakeMarketDataProvider(FixtureStore(FIXTURES / "m0"))


@pytest.fixture(params=["real", "fake"])
def provider(request: pytest.FixtureRequest) -> MarketDataProvider:
    """One body, both implementations. This is the point of the module."""
    return request.getfixturevalue(request.param)  # type: ignore[no-any-return]


# --- the contract, satisfied by both ----------------------------------------


def test_resolving_on_isin_gives_the_scheme_and_high_confidence(
    provider: MarketDataProvider,
) -> None:
    """§11.3 step 1. The CAS carries ISIN reliably; this is the ~99% path."""
    ref = provider.resolve_scheme(Isin("INF179K01UT0"), "", None, AS_OF)
    assert ref.scheme_id == HDFC_DIRECT
    assert ref.confidence == Confidence.HIGH
    assert ref.matched_by == "isin"
    assert ref.plan == "direct"
    assert ref.option == "growth"


def test_an_unknown_isin_is_unresolved_and_never_falls_back_to_the_name(
    provider: MarketDataProvider,
) -> None:
    """§11.3: "Never resolve on name when ISIN is present."

    Falling through would match `HDFC Flexi Cap Fund` — which is two schemes,
    Direct and Regular, 10.13% apart. That substitution is V0-05, and it is the
    silent ~1%/year error the whole ordering exists to prevent.
    """
    ref = provider.resolve_scheme(
        Isin("INF000X01ZZ9"), "HDFC Flexi Cap Fund", None, AS_OF
    )
    assert ref.scheme_id is None
    assert ref.confidence == Confidence.UNRESOLVED
    assert ref.matched_by == "none"


def test_an_ambiguous_name_resolves_to_nothing(provider: MarketDataProvider) -> None:
    """DECISIONS V0-21, and the reason the fake was narrowed to match.

    `HDFC Flexi Cap Fund` is four schemes in the sample — two plans times two
    options. Returning any one of them would be a guess, and `CLAUDE.md`
    invariant 5 says raise or return nothing rather than guess.
    """
    ref = provider.resolve_scheme(None, "HDFC Flexi Cap Fund", None, AS_OF)
    assert ref.scheme_id is None
    assert ref.confidence == Confidence.UNRESOLVED


def test_nav_returns_the_published_value_exactly(provider: MarketDataProvider) -> None:
    """A Decimal in, the same Decimal out, through SQLite and through YAML."""
    point = provider.nav(HDFC_DIRECT, AS_OF)
    assert point.nav == Decimal("2271.324")
    assert point.nav_date == AS_OF
    assert point.scheme_id == HDFC_DIRECT
    assert isinstance(point.nav, Decimal)


def test_nav_carries_the_last_published_value_forward(
    provider: MarketDataProvider,
) -> None:
    """Funds do not price on weekends. An as-of date is not a trading date.

    `nav_date` reports which day the figure came from, so staleness is visible
    rather than inferred.
    """
    point = provider.nav(HDFC_DIRECT, date(2026, 9, 6))  # a Sunday
    assert point.nav_date == AS_OF
    assert point.nav == Decimal("2271.324")


def test_a_scheme_with_no_nav_raises_rather_than_returning_zero(
    provider: MarketDataProvider,
) -> None:
    """A plausible zero is worse than a crash — `CLAUDE.md` invariant 5.

    Zero NAV would read as a total loss and propagate silently into every
    return figure derived from it.
    """
    with pytest.raises((KeyError, Exception)):
        provider.nav(SchemeId("INF000X01ZZ9"), AS_OF)


def test_nav_series_is_ordered_and_bounded(provider: MarketDataProvider) -> None:
    series = provider.nav_series(HDFC_DIRECT, date(2026, 1, 1), AS_OF, adjusted=False)
    assert series
    assert [p.nav_date for p in series] == sorted(p.nav_date for p in series)
    assert all(date(2026, 1, 1) <= p.nav_date <= AS_OF for p in series)


def test_nav_series_defaults_to_the_adjusted_column(
    provider: MarketDataProvider,
) -> None:
    """§9.1: return math on raw NAV is wrong for IDCW plans, so adjusted wins.

    With no distributions loaded the two series coincide — which is exactly why
    the default matters: the day an IDCW event lands, code that never chose a
    column keeps working.
    """
    adjusted = provider.nav_series(HDFC_DIRECT, date(2026, 1, 1), AS_OF)
    raw = provider.nav_series(HDFC_DIRECT, date(2026, 1, 1), AS_OF, adjusted=False)
    assert [p.nav for p in adjusted] == [p.nav for p in raw]


def test_no_idcw_events_is_an_empty_list_not_an_error(
    provider: MarketDataProvider,
) -> None:
    assert provider.idcw_events(HDFC_DIRECT, date(2020, 1, 1), AS_OF) == []


def test_an_unmerged_scheme_has_an_empty_merger_chain(
    provider: MarketDataProvider,
) -> None:
    assert provider.merger_chain(HDFC_DIRECT) == []


# --- the real provider on its own -------------------------------------------


def test_the_two_plans_are_separate_schemes_with_separate_navs(
    real: WarehouseMarketDataProvider,
) -> None:
    """§4.4: one ISIN, one scheme_id, no exceptions.

    Same fund, same option, same date — and a 10.13% NAV gap. This is V0-05
    reproduced from the authoritative source, and the warehouse now keeps the
    two apart by construction rather than by care.
    """
    direct = real.nav(HDFC_DIRECT, AS_OF)
    regular = real.nav(HDFC_REGULAR, AS_OF)
    assert direct.nav == Decimal("2271.324")
    assert regular.nav == Decimal("2062.377")
    assert direct.scheme_id != regular.scheme_id


def test_sibling_plan_finds_the_other_share_class(
    real: WarehouseMarketDataProvider,
) -> None:
    """The V0-05 detector, as data.

    Given the Direct scheme it returns the Regular one, so a caller can compare
    the two series and see the ~1%/year gap instead of mistaking one for the
    other.
    """
    assert real.sibling_plan(HDFC_DIRECT, Plan("regular")) == HDFC_REGULAR
    assert real.sibling_plan(HDFC_REGULAR, Plan("direct")) == HDFC_DIRECT


def test_an_amfi_code_covering_two_schemes_resolves_to_neither(
    real: WarehouseMarketDataProvider,
) -> None:
    """One code, two schemes — the payout and reinvestment variants of a row.

    §11.3 treats an AMFI code as a unique key. It is not: code 118954 describes
    both `INF179K01VL5` (payout) and `INF179K01VM3` (reinvestment). Returning
    either would be the same coin flip V0-21 refuses on names.
    """
    ref = real.resolve_scheme(None, "", "118954", AS_OF)
    assert ref.scheme_id is None

    unique = real.resolve_scheme(None, "", "118955", AS_OF)
    assert unique.scheme_id == HDFC_DIRECT
    assert unique.confidence == Confidence.MEDIUM


def test_the_payout_variant_is_priced_and_its_sibling_is_not(
    real: WarehouseMarketDataProvider,
) -> None:
    """A NAV the file did not publish must not be invented.

    A reinvestment NAV diverges from its payout sibling from the first
    distribution onward, so copying one onto the other is not an approximation,
    it is a different fund's series.
    """
    assert real.nav(HDFC_IDCW_PAYOUT, AS_OF).nav == Decimal("91.839")
    with pytest.raises(KeyError):
        real.nav(SchemeId("INF179K01VM3"), AS_OF)


def test_unloaded_attributes_raise_rather_than_returning_a_default(
    real: WarehouseMarketDataProvider,
) -> None:
    """V0.4 loads NAV and the scheme master, and nothing else.

    `tax_class` defaulting to equity would understate tax (MODULE_1.md §10.2);
    `ter` has no table until V2 (V0-18). Both say so and raise.
    """
    for call in (
        lambda: real.tax_class(HDFC_DIRECT, AS_OF),
        lambda: real.ter(HDFC_DIRECT, AS_OF),
        lambda: real.exit_load_period(HDFC_DIRECT),
    ):
        with pytest.raises(KeyError):
            call()


def test_the_real_provider_satisfies_the_frozen_protocol() -> None:
    """Structural check. The Protocol was frozen in Slice Zero and has not moved.

    `MarketDataProvider` is not `@runtime_checkable`, so this asserts the method
    set explicitly — which also catches a method quietly renamed on one side.
    """
    required = {
        name for name in vars(MarketDataProvider)
        if not name.startswith("_") and callable(getattr(MarketDataProvider, name))
    }
    assert required
    for name in required:
        assert hasattr(WarehouseMarketDataProvider, name), f"missing {name}"
        assert hasattr(FakeMarketDataProvider, name), f"fake missing {name}"
