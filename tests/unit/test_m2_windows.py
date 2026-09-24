"""Return window assembly. MODULE_2.md §8.1.

These test the parts §8.1 keeps that this warehouse can feed, the absence it
declares rather than hides — no benchmark on any of 19,598 schemes — and the
one field that is genuinely optional: Sharpe needs a risk-free rate, and
`config/risk_free.yaml` records those back to 2011 and no further.
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
    rolling_returns,
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


def test_a_field_with_no_data_behind_it_is_still_not_carried() -> None:
    """A field that is always None claims to be optional when it is
    unavailable, so it is omitted until there is data behind it.

    Alpha, beta, tracking error and the captures WERE on this list. S12 put an
    index series and 1,573 resolved schemes behind them, so they are now
    carried and `None` only when a particular fund has no benchmark — which is
    optionality of the ordinary kind. `bm_available` stays off: a boolean with
    one reachable value answers a question nobody can ask differently.
    """
    w = compute_return_window(series(["100", "110"]), "1y")
    assert w is not None
    assert not hasattr(w, "bm_available")

    # Carried, and None because this window was given no benchmark.
    for optional in ("alpha_ann", "beta", "tracking_error", "up_capture",
                     "down_capture", "information_ratio", "benchmark_id"):
        assert hasattr(w, optional), f"{optional} is no longer carried"
        assert getattr(w, optional) is None, optional


def test_sharpe_is_none_for_a_window_older_than_the_record() -> None:
    """`config/risk_free.yaml` starts at the 91-day auction of 2011-04-06, the
    oldest DBIE publishes. A window opening before that gets no Sharpe, rather
    than one built on the 2011 rate stretched back over history it never
    applied to.

    This asserted an empty file until S13's series landed. The property it was
    really after -- optional because the record is finite, not absent like the
    benchmark fields -- is unchanged; it now has a real boundary to sit on.
    """
    old = [
        NavPoint(
            scheme_id=SCHEME,
            nav_date=date(2010, 1, 1) + timedelta(days=i * 30),
            nav=Decimal(100 + i),
            is_interpolated=False,
        )
        for i in range(13)
    ]

    w = compute_return_window(old, "1y")
    assert w is not None
    assert w.risk_free_pct is None
    assert w.sharpe is None and w.sortino is None


def test_sharpe_appears_once_a_rate_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """Excess return over volatility. With a 6% rate and a series that rose,
    the ratio must be finite and signed the obvious way."""
    import src.m2_fund.windows as mod

    monkeypatch.setattr(mod, "risk_free_on", lambda _d: Decimal("6"))
    w = compute_return_window(series([str(100 + i) for i in range(400)]), "1y")
    assert w is not None
    assert w.risk_free_pct == Decimal("6")
    assert w.sharpe == ((w.return_ann - Decimal("0.06")) / w.volatility_ann).quantize(
        Decimal("0.000001")
    )
    # Sortino punishes only the falls, and this series never falls, so it has
    # no downside to divide by. None, not infinity.
    assert w.sortino is None


def test_sortino_needs_something_to_have_fallen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same series with real down days does produce one, and a fund that
    fell less is scored better than Sharpe alone would say."""
    import src.m2_fund.windows as mod

    monkeypatch.setattr(mod, "risk_free_on", lambda _d: Decimal("6"))
    jagged = [str(100 + i + (5 if i % 3 else 0)) for i in range(400)]
    w = compute_return_window(series(jagged), "1y")
    assert w is not None
    assert w.sortino is not None
    assert w.sharpe is not None


def test_a_rate_with_no_volatility_to_divide_gives_no_sharpe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fund that barely moved has no risk-adjusted return; dividing by zero
    would assert an infinitely good one."""
    import src.m2_fund.windows as mod

    monkeypatch.setattr(mod, "risk_free_on", lambda _d: Decimal("6"))
    w = compute_return_window(series(["100", "100", "100.000001"]), "1y")
    assert w is not None
    assert w.volatility_ann == Decimal(0)
    assert w.sharpe is None


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


# --- rolling returns -------------------------------------------------------


def test_too_few_windows_is_no_summary() -> None:
    """MODULE_2.md §9's floor: below twelve windows the percentiles describe
    the sample rather than the fund."""
    navs = series([str(100 + i) for i in range(200)])
    assert rolling_returns(navs, horizon_days=100, step_days=30) is None


def test_a_rising_series_is_positive_in_every_window() -> None:
    navs = series([str(100 + i) for i in range(500)])
    r = rolling_returns(navs, horizon_days=100, step_days=30)
    assert r is not None
    assert r.windows == 14  # starts at day 0, 30, ... 390; 390 + 100 <= 499
    assert r.pct_positive == Decimal("100.000000")
    assert r.worst > 0
    assert r.worst <= r.median <= r.best


def test_a_falling_series_is_negative_in_every_window() -> None:
    """Catches a sign inversion that a rising series cannot see."""
    navs = series([str(600 - i) for i in range(500)])
    r = rolling_returns(navs, horizon_days=100, step_days=30)
    assert r is not None
    assert r.pct_positive == Decimal(0)
    assert r.best < 0


def test_rolling_validates_the_series_like_the_window_does() -> None:
    """Both entry points route through the same guard."""
    with pytest.raises(NonPositiveNav):
        rolling_returns(series(["0"] + [str(100 + i) for i in range(499)]), 100, 30)


def test_a_window_holding_one_price_is_not_a_zero_return() -> None:
    """The defect every dense-series test above is blind to.

    When both ends of a window bisect to the SAME price the ratio is forced to
    1.0 and the window reports exactly 0.00% -- a fabricated figure. On a
    series sparse relative to the horizon every window lands that way: these
    points rise 100 -> 2050, a 20x gain, and the whole block read 0.00% worst,
    0.00% median, 0.00% best before `i < j`.
    """
    sparse = series([str(100 + d) for d in range(0, 2100, 150)], step_days=150)
    assert rolling_returns(sparse, horizon_days=100, step_days=30) is None


def test_a_gapped_series_still_reports_the_windows_it_can_fill() -> None:
    """The refusal above must not throw away windows that do span two prices."""
    dense = series([str(100 + i) for i in range(500)])  # 14 windows, over the floor
    r = rolling_returns(dense, horizon_days=100, step_days=30)
    assert r is not None
    assert r.worst > 0


def test_a_fixed_window_counts_only_when_the_prices_span_it() -> None:
    """DECISIONS V1-74. A fund with four years of prices has a "5y" window four
    years long; under a "5 years" label that is the whole history misnamed."""
    from src.m2_fund.windows import spans

    assert spans(1826, "5y")
    assert spans(1822, "5y")  # its first price fell after a long weekend
    assert not spans(1500, "5y")
    assert spans(365, "1y") and not spans(300, "1y")
