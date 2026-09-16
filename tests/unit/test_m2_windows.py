"""Return window assembly. MODULE_2.md §8.1.

These test the parts §8.1 keeps that this warehouse can feed, and the two
absences it declares rather than hides: no benchmark on any of 19,598 schemes,
and no risk-free series at all.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from src.common.contracts.market import NavPoint
from src.common.types import SchemeId
from src.m2_fund.windows import (
    NonPositiveNav,
    compute_return_window,
    window_start,
)

SCHEME = SchemeId("TEST-01")
START = date(2024, 1, 1)


def series(
    values: list[str], *, step_days: int = 1, interpolated: set[int] | None = None
) -> list[NavPoint]:
    filled = interpolated or set()
    return [
        NavPoint(
            scheme_id=SCHEME,
            nav_date=START + timedelta(days=i * step_days),
            nav=Decimal(v),
            is_interpolated=i in filled,
        )
        for i, v in enumerate(values)
    ]


# --- the window itself -----------------------------------------------------


def test_a_doubling_over_a_year_reports_both_returns() -> None:
    navs = [
        NavPoint(SCHEME, START, Decimal(100), False),
        NavPoint(SCHEME, START + timedelta(days=365), Decimal(200), False),
    ]
    w = compute_return_window(navs, "1y")
    assert w is not None
    assert w.return_cum == Decimal("1.000000")
    assert w.return_ann == Decimal("1.000000")
    assert w.obs_days == 365
    assert w.confidence == "medium"


def test_a_series_that_never_moves_is_refused_not_reported_as_zero() -> None:
    """The shape a daily-IDCW plan takes when its declarations are not loaded.

    The whole return was distributed rather than accrued, so `nav_adj` -- which
    equals raw NAV when no events are on record -- is a flat line. Reporting
    0.00% would be indistinguishable from a fund that genuinely went nowhere,
    and 9,187 of this warehouse's schemes are IDCW options against an empty
    `scheme_idcw`. Refusing is the only honest answer.
    """
    assert compute_return_window(series(["100"] * 400), "1y") is None


def test_a_barely_moving_series_still_computes() -> None:
    """The refusal above must not swallow a fund that really is this quiet.

    One paisa of movement is a real return, and a liquid Growth fund looks
    almost like this. Catches sign and division errors that a strongly rising
    series would hide.
    """
    w = compute_return_window(series(["100"] * 200 + ["100.01"] * 200), "1y")
    assert w is not None
    assert w.return_cum > 0
    assert w.volatility_ann > 0
    assert w.drawdown.depth == Decimal(0)


def test_fewer_than_two_points_is_no_window_rather_than_a_zero_row() -> None:
    """Zeros would be indistinguishable from a fund that went nowhere."""
    assert compute_return_window(series(["100"]), "1y") is None
    assert compute_return_window([], "1y") is None


def test_a_series_spanning_no_time_is_no_window() -> None:
    """Two points on one day: a ratio exists, an annual rate does not."""
    same_day = [
        NavPoint(SCHEME, START, Decimal(100), False),
        NavPoint(SCHEME, START, Decimal(110), False),
    ]
    assert compute_return_window(same_day, "1y") is None


def test_obs_count_and_obs_days_are_different_things() -> None:
    """250 trading days can span a calendar year. Conflating them would let a
    fund with a long gap in its series look like it had a full history."""
    w = compute_return_window(series(["100", "110", "120"], step_days=180), "1y")
    assert w is not None
    assert w.obs_count == 3
    assert w.obs_days == 360


# --- what the data cannot support ------------------------------------------


def test_no_benchmark_relative_field_is_carried_as_a_silent_none() -> None:
    """A field that is always None claims to be optional when it is
    unavailable. They are omitted until there is data behind them."""
    w = compute_return_window(series(["100", "110"]), "1y")
    assert w is not None
    for absent in (
        "alpha_ann", "beta", "tracking_error", "sharpe", "sortino",
        "downside_dev_ann",   # only feeds sortino, which needs the missing rf
        "bm_available", "rf_available",  # booleans with one reachable value
    ):
        assert not hasattr(w, absent), f"{absent} is carried but can never be computed"


def test_a_non_positive_nav_raises_rather_than_dividing() -> None:
    """Invariant 5. Validated once here, so risk.py can assume positive prices."""
    with pytest.raises(NonPositiveNav):
        compute_return_window(series(["0", "100"]), "1y")


# --- interpolation ---------------------------------------------------------


def test_interpolated_share_travels_with_the_window() -> None:
    """A filled NAV is not a fetched one. Interpolation is a straight line and
    a straight line has no variance, so a window built largely from filled
    points understates its own volatility — the proportion has to be visible."""
    navs = series(["100", "105", "110", "115"], interpolated={1, 2})
    w = compute_return_window(navs, "1y")
    assert w is not None
    assert w.interpolated_pct == Decimal("50.000000")


def test_a_fully_fetched_series_reports_zero_interpolation() -> None:
    w = compute_return_window(series(["100", "110"]), "1y")
    assert w is not None
    assert w.interpolated_pct == Decimal(0)


# --- window bounds ---------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected"),
    [("1y", date(2025, 6, 1)), ("3y", date(2023, 6, 1)), ("5y", date(2021, 6, 1))],
)
def test_window_start_subtracts_whole_years(key: str, expected: date) -> None:
    assert window_start(date(2026, 6, 1), key) == expected


def test_a_leap_day_start_lands_on_the_28th() -> None:
    """29 February has no counterpart in a non-leap year. One day short is the
    only answer that exists; raising would make a whole window unavailable."""
    assert window_start(date(2024, 2, 29), "1y") == date(2023, 2, 28)
