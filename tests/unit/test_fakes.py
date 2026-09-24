"""`FakeMarketDataProvider` satisfies its Protocol and honours the invariants.

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
from src.common.types import (
    Isin,
    SchemeId,
)
from src.m0_data.providers.market_data import MarketDataProvider

from tests.fakes.loader import DecimalSafeLoader, FixtureError, FixtureStore
from tests.fakes.m0 import FakeMarketDataProvider

AS_OF = date(2026, 7, 31)

PAIRS = [
    (FakeMarketDataProvider, MarketDataProvider),
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
