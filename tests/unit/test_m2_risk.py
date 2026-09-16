"""M2's return and risk arithmetic. MODULE_2.md §8.

Written against hand-computable series rather than fixtures, because every
figure here is a number a reader must be able to check by hand — that is the
whole argument for §8's gate being "matches hand-computed CAGR" rather than
"matches the previous run".

The flat-series tests exist because a rising series hides sign errors: a
drawdown computed with the comparison inverted still returns something
plausible on data that only goes up.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from src.common.contracts.market import NavPoint
from src.common.types import SchemeId
from src.m2_fund.risk import (
    annualise,
    annualised_vol,
    confidence_from_obs,
    daily_returns,
    max_drawdown,
)

SCHEME = SchemeId("TEST-01")
START = date(2024, 1, 1)


def series(values: list[str], *, interpolated: set[int] | None = None) -> list[NavPoint]:
    """A NAV series on consecutive days, one point per value."""
    filled = interpolated or set()
    return [
        NavPoint(
            scheme_id=SCHEME,
            nav_date=START + timedelta(days=i),
            nav=Decimal(v),
            is_interpolated=i in filled,
        )
        for i, v in enumerate(values)
    ]


# --- returns ---------------------------------------------------------------


def test_daily_returns_is_one_shorter_than_its_input() -> None:
    assert len(daily_returns(series(["100", "110", "121"]))) == 2


def test_daily_returns_are_ratios_not_differences() -> None:
    """A 100 -> 110 step is 0.1, not 10. The units are the whole point."""
    assert daily_returns(series(["100", "110"]))[0] == Decimal("0.1")


# --- annualisation ---------------------------------------------------------


def test_doubling_over_exactly_a_year_annualises_to_100_percent() -> None:
    """§19 step 5's gate: matches a hand-computed CAGR."""
    assert annualise(Decimal(2), 365) == Decimal("1.000000")


def test_annualising_returns_a_rate_not_a_growth_ratio() -> None:
    """50% over two years is 22.47% a year, not 122.47%."""
    assert annualise(Decimal("1.5"), 730) == Decimal("0.224745")


def test_no_change_over_any_span_annualises_to_zero() -> None:
    assert annualise(Decimal(1), 1000) == Decimal(0)


def test_a_sub_year_window_still_annualises() -> None:
    """§8.1 annualises regardless; §8.2's confidence tier is what flags it.

    10% in 30 days is a wildly misleading 216% a year, and the number is
    produced anyway so that the tier beside it can say how much to trust it.
    """
    assert annualise(Decimal("1.1"), 30) > Decimal(2)


def test_annualising_over_zero_days_raises() -> None:
    with pytest.raises(ValueError, match="cannot annualise"):
        annualise(Decimal(2), 0)


# --- volatility ------------------------------------------------------------


def test_a_flat_series_has_no_volatility() -> None:
    assert annualised_vol(daily_returns(series(["100"] * 10))) == Decimal(0)


def test_volatility_needs_two_returns_to_exist() -> None:
    """One observation has no dispersion. Zero, not a crash and not a nan."""
    assert annualised_vol([Decimal("0.01")]) == Decimal(0)


def test_volatility_is_annualised_on_trading_days_not_calendar_days() -> None:
    """sqrt(252), not sqrt(365). Using 365 overstates it by about 18%."""
    daily = [Decimal("0.01"), Decimal("-0.01")] * 10
    vol = annualised_vol(daily)
    sample_sd = Decimal("0.010259")  # n-1 over this alternating series
    assert abs(vol - sample_sd * Decimal(252).sqrt()) < Decimal("0.001")


# --- drawdown --------------------------------------------------------------


def test_a_rising_series_never_draws_down() -> None:
    dd = max_drawdown(series(["100", "110", "120"]))
    assert dd.depth == Decimal(0)
    assert dd.recovery_days is None


def test_drawdown_depth_is_negative_and_finds_the_trough() -> None:
    dd = max_drawdown(series(["100", "120", "60", "80"]))
    assert dd.depth == Decimal("-0.500000")  # 120 -> 60
    assert dd.peak == START + timedelta(days=1)
    assert dd.trough == START + timedelta(days=2)


def test_recovery_is_measured_against_the_peak_before_the_trough() -> None:
    """A fund that falls, regains its old peak, then climbs higher HAS
    recovered. Comparing against the later high would report that it never
    did, which is the easy way to write this wrong."""
    dd = max_drawdown(series(["100", "50", "100", "200"]))
    assert dd.recovery == START + timedelta(days=2)
    assert dd.recovery_days == 1


def test_an_unrecovered_fall_reports_no_recovery_date() -> None:
    dd = max_drawdown(series(["100", "50", "60"]))
    assert dd.recovery is None
    assert dd.recovery_days is None


def test_drawdown_invariants_hold_on_a_jagged_series() -> None:
    dd = max_drawdown(series(["100", "90", "130", "70", "85", "60", "140"]))
    assert dd.depth <= 0
    assert dd.peak <= dd.trough
    assert dd.recovery is None or dd.recovery >= dd.trough
    assert dd.duration_days >= 0


def test_max_drawdown_on_an_empty_series_raises() -> None:
    with pytest.raises(ValueError, match="at least one"):
        max_drawdown([])


# --- confidence ------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "tier"),
    [(0, "low"), (364, "low"), (365, "medium"), (1094, "medium"), (1095, "high")],
)
def test_confidence_boundaries(days: int, tier: str) -> None:
    """§8.2's thresholds, at the exact day they flip."""
    assert confidence_from_obs(days) == tier
