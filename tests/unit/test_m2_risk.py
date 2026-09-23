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
from itertools import pairwise
from pathlib import Path

import pytest
from src.common.contracts.market import NavPoint
from src.common.decimals import RATE_Q, annualise
from src.common.types import SchemeId
from src.m2_fund.risk import (
    TRADING_DAYS,
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
    """50% over two years is 22.47% a year, not 122.47%.

    Quantised here, not by `annualise`: it returns full precision so M1's
    ledger output is not silently rounded to M2's display scale.
    """
    assert annualise(Decimal("1.5"), 730).quantize(RATE_Q) == Decimal("0.224745")


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


# --- risk-free rate, hand-entered ---------------------------------------------


def test_the_rate_in_force_is_the_latest_on_or_before(tmp_path: Path) -> None:
    """S13's source bot-blocks automated clients, so the rate arrives by hand.
    A window is judged against the rate that applied when it started."""
    from src.m0_data.config import risk_free_on

    cfg = tmp_path / "rf.yaml"
    cfg.write_text(
        "observations:\n  2020-01-01: 5.0\n  2022-01-01: 4.0\n  2024-01-01: 7.0\n",
        encoding="utf-8",
    )

    assert risk_free_on(date(2019, 12, 31), cfg) is None   # before any observation
    assert risk_free_on(date(2020, 1, 1), cfg) == Decimal("5.0")
    assert risk_free_on(date(2021, 6, 1), cfg) == Decimal("5.0")   # carried forward
    assert risk_free_on(date(2024, 6, 1), cfg) == Decimal("7.0")


def test_no_file_and_no_observations_are_both_just_empty(tmp_path: Path) -> None:
    """Not an error: every M2 figure but Sharpe and Sortino is unaffected."""
    from src.m0_data.config import risk_free_on, risk_free_rates

    missing = tmp_path / "absent.yaml"
    empty = tmp_path / "empty.yaml"
    empty.write_text("observations: {}\n", encoding="utf-8")

    assert risk_free_rates(missing) == []
    assert risk_free_rates(empty) == []
    assert risk_free_on(date(2024, 1, 1), empty) is None


def test_the_cache_keys_on_the_path(tmp_path: Path) -> None:
    """`risk_free_rates` is memoised -- parsing 772 rows once per window per
    scheme is 50 minutes of re-reading one unchanged file. Two files must still
    give two answers, which is the way that optimisation goes wrong."""
    from src.m0_data.config import risk_free_on

    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    a.write_text("observations:\n  2020-01-01: 5.0\n", encoding="utf-8")
    b.write_text("observations:\n  2020-01-01: 9.0\n", encoding="utf-8")

    assert risk_free_on(date(2021, 1, 1), a) == Decimal("5.0")
    assert risk_free_on(date(2021, 1, 1), b) == Decimal("9.0")
    assert risk_free_on(date(2021, 1, 1), a) == Decimal("5.0")   # still, from cache


# --- the shipped series -------------------------------------------------------


def test_the_shipped_file_carries_the_auction_series() -> None:
    """The file is the feature. Emptied, Sharpe silently becomes `None` on
    every window in the warehouse and nothing else fails -- so this asserts the
    data is there, not merely that the reader works.

    The cadence check is the one that matters: a quarterly sample of this
    series is out by up to 4.50pp at a window start, because the 91-day cut-off
    went 7.24% -> 12.02% inside Q3 2013. Weekly observations cost 20KB and
    carry no such error.
    """
    from src.m0_data.config import risk_free_rates

    rates = risk_free_rates()
    assert len(rates) > 700, "the shipped auction series is missing or truncated"
    assert rates[0][0] == date(2011, 4, 6), "no longer starts at S13's first auction"

    gaps = [(b - a).days for (a, _), (b, _) in pairwise(rates)]
    assert max(gaps) <= 31, "a gap over a month: resampled coarser than weekly"
    assert all(Decimal(1) < v < Decimal(15) for _, v in rates), "a yield outside 1-15%"


def test_every_fixed_window_open_today_has_a_rate() -> None:
    """§8.1's three windows, as a reader would actually ask for them. This is
    what "Sharpe is on" means: not that the function exists, but that the rate
    it needs is on record for every window the warehouse can open today."""
    from src.m0_data.config import risk_free_on
    from src.m2_fund.windows import WINDOW_YEARS, window_start

    today = date.today()
    for key in WINDOW_YEARS:
        assert risk_free_on(window_start(today, key)) is not None, key


def test_a_window_older_than_the_record_still_gets_no_sharpe() -> None:
    """The boundary is a real date now, not a hypothetical. 2 of the 15,006
    schemes with NAV start before it, and they must get `None` rather than the
    2011 rate stretched backwards over history it did not apply to."""
    from src.m0_data.config import risk_free_on

    assert risk_free_on(date(2011, 4, 5)) is None
    assert risk_free_on(date(2011, 4, 6)) is not None


def test_sharpe_and_sortino_come_out_of_a_real_window() -> None:
    """End to end, over dates the shipped file actually covers.

    The series falls before it rises, because a monotonic one has zero downside
    deviation and Sortino is correctly `None` for it -- a rising-only fixture
    would assert nothing about Sortino at all.
    """
    from src.m2_fund.windows import compute_return_window

    start = date(2022, 1, 3)
    path = ["100", "97", "94", "99", "103", "108", "112", "119", "126", "134"]
    navs = [
        NavPoint(
            scheme_id=SCHEME,
            nav_date=start + timedelta(days=i * 90),
            nav=Decimal(v),
            is_interpolated=False,
        )
        for i, v in enumerate(path)
    ]

    w = compute_return_window(navs, "3y")
    assert w is not None
    assert w.risk_free_pct == Decimal("3.657")   # the auction of 2021-12-29
    assert w.sharpe is not None
    assert w.sortino is not None
    # It beat a 3.69% bill comfortably, and punishing only the falls flatters it.
    assert w.sharpe > 0
    assert w.sortino > w.sharpe


# --- against a benchmark ------------------------------------------------------


def levels(
    values: list[str], *, start: date = START, step: int = 1
) -> list[tuple[date, Decimal]]:
    return [(start + timedelta(days=i * step), Decimal(v)) for i, v in enumerate(values)]


def test_a_fund_that_is_the_index_has_beta_one_and_no_tracking_error() -> None:
    """The defining case, and the one a sign error still looks plausible on."""
    from src.m2_fund.risk import beta, paired_returns, tracking_error

    navs = series(["100", "102", "99", "104", "103"])
    index = levels(["500", "510", "495", "520", "515"])   # the same moves, scaled
    fund, bench = paired_returns(navs, index)

    assert beta(fund, bench) == Decimal("1.000000")
    assert tracking_error(fund, bench) == Decimal("0.000000")


def test_a_fund_that_moves_twice_as_hard_has_beta_two() -> None:
    from src.m2_fund.risk import beta, paired_returns

    index = levels(["100", "101", "99", "102"])
    navs = series(["100", "102", "98", "104"])
    fund, bench = paired_returns(navs, index)
    got = beta(fund, bench)
    assert got is not None
    assert abs(got - Decimal(2)) < Decimal("0.05")


def test_only_dates_both_series_priced_are_compared() -> None:
    """A fund that did not price on a day the index fell would otherwise carry
    the index's fall against its own previous move — tracking error it did not
    have. The intersection is the comparison."""
    from src.m2_fund.risk import aligned, paired_returns

    navs = series(["100", "101", "102"])                       # 3 consecutive days
    index = [(START, Decimal("50")), (START + timedelta(days=2), Decimal("52"))]

    assert [d for d, _, _ in aligned(navs, index)] == [START, START + timedelta(days=2)]
    fund, bench = paired_returns(navs, index)
    assert len(fund) == len(bench) == 1


def test_alpha_is_what_is_left_after_paying_for_the_market() -> None:
    """`fund - (rf + beta*(bench - rf))`. A fund that returned exactly what its
    beta entitled it to has zero alpha, however large the return."""
    from src.m2_fund.risk import alpha_annual

    # beta 1, so alpha is simply fund minus benchmark.
    assert alpha_annual(Decimal("0.12"), Decimal("0.10"), Decimal(1), Decimal(6)) == (
        Decimal("0.020000")
    )
    # beta 2 on a 10% benchmark over a 6% bill entitles it to 6 + 2*4 = 14%.
    assert alpha_annual(Decimal("0.14"), Decimal("0.10"), Decimal(2), Decimal(6)) == (
        Decimal("0.000000")
    )


def test_an_index_funds_alpha_is_about_minus_its_fee() -> None:
    """The property that says the whole chain is wired correctly.

    An index fund holds the index and charges a fee, so it must return the
    index minus that fee: beta 1, tracking error near zero, and an alpha of
    about minus the TER. Measured on ICICI Prudential Nifty 50 Index Fund
    Direct against the real Nifty 50 TRI, alpha came out -0.23% to -0.33% over
    1y/3y/5y against a published TER near 0.20% -- which is the arithmetic
    here, on a series built to the same shape.
    """
    from src.m2_fund.risk import alpha_annual, beta, paired_returns, tracking_error

    # The index compounds 0.05%/day; the fund does the same less a 0.20%/yr fee.
    fee_daily = Decimal("0.002") / TRADING_DAYS
    index_vals, fund_vals = [Decimal(1000)], [Decimal(100)]
    for i in range(60):
        move = Decimal("0.0005") if i % 3 else Decimal("-0.0004")
        index_vals.append(index_vals[-1] * (1 + move))
        fund_vals.append(fund_vals[-1] * (1 + move - fee_daily))

    navs = series([str(v) for v in fund_vals])
    index = levels([str(v) for v in index_vals])
    fund, bench = paired_returns(navs, index)

    got_beta, got_te = beta(fund, bench), tracking_error(fund, bench)
    assert got_beta is not None and got_te is not None
    assert abs(got_beta - 1) < Decimal("0.01")
    assert got_te < Decimal("0.001")

    span = (navs[-1].nav_date - navs[0].nav_date).days
    fund_ann = annualise(navs[-1].nav / navs[0].nav, span)
    bench_ann = annualise(index[-1][1] / index[0][1], span)
    alpha = alpha_annual(fund_ann, bench_ann, Decimal(1), Decimal(6))
    assert Decimal("-0.004") < alpha < Decimal("-0.001"), f"alpha {alpha} is not ~-0.2%"


def test_capture_compounds_rather_than_averaging() -> None:
    """A ratio of arithmetic means describes a portfolio nobody holds."""
    from src.m2_fund.risk import capture

    bench = [Decimal("0.02"), Decimal("-0.01"), Decimal("0.03"), Decimal("-0.02")]
    half = [b / 2 for b in bench]
    up = capture(half, bench, rising=True)
    down = capture(half, bench, rising=False)
    assert up is not None and down is not None
    assert Decimal("0.45") < up < Decimal("0.55")
    assert Decimal("0.45") < down < Decimal("0.55")


def test_a_flat_benchmark_has_no_beta_rather_than_an_infinite_one() -> None:
    from src.m2_fund.risk import beta

    assert beta([Decimal("0.01")] * 5, [Decimal(0)] * 5) is None


def test_too_little_overlap_keeps_the_benchmark_and_computes_nothing() -> None:
    """Below a month of paired days a beta is noise dressed as a number, so none
    is reported -- but the scheme still HAS a benchmark, and the id says so.

    This asserted `benchmark_id is None` until review. It had pinned the defect:
    "too little data" read exactly the same as "no benchmark at all".
    """
    from src.m2_fund.windows import compute_return_window

    navs = series(["100", "101", "102", "103"])
    w = compute_return_window(navs, "1y", levels(["50", "51", "52", "53"]), "NSE:X_TRI")
    assert w is not None
    assert w.beta is None and w.tracking_error is None
    assert w.benchmark_id == "NSE:X_TRI"


def test_a_window_with_no_benchmark_at_all_leaves_every_field_none() -> None:
    from src.m2_fund.windows import compute_return_window

    w = compute_return_window(series(["100", "110"]), "1y")
    assert w is not None
    for field in ("benchmark_id", "beta", "tracking_error", "alpha_ann",
                  "up_capture", "down_capture", "information_ratio"):
        assert getattr(w, field) is None, field


# --- review fixes: the benchmark path ------------------------------------------


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_a_non_positive_benchmark_level_raises_naming_the_index(bad: str) -> None:
    """NAVs were validated and index levels were not, though the same arithmetic
    divides by both. A zero level was a bare ZeroDivisionError naming nothing;
    a negative one was a return that looked like a number. Invariant 5."""
    from src.m2_fund.windows import NonPositiveLevel, compute_return_window

    navs = series([str(100 + i) for i in range(30)])
    values = [str(1000 + i) for i in range(30)]
    values[10] = bad
    with pytest.raises(NonPositiveLevel, match="NSE:X_TRI"):
        compute_return_window(navs, "1y", levels(values), "NSE:X_TRI")


def test_returns_of_an_aligned_series_is_what_paired_returns_gives() -> None:
    """`_against` now builds the intersection once and reuses it. The two
    routes to a pair of return series must agree exactly."""
    from src.m2_fund.risk import aligned, paired_returns, returns_of

    navs = series(["100", "102", "99", "104"])
    index = levels(["500", "510", "495", "520"])
    assert returns_of(aligned(navs, index)) == paired_returns(navs, index)


def test_capture_treats_a_mismatched_pair_as_its_siblings_do() -> None:
    """`beta` and `tracking_error` gave None for mismatched lengths and `capture`
    raised. A caller guarding on one was unprotected calling the next."""
    from src.m2_fund.risk import beta, capture, tracking_error

    fund, bench = [Decimal("0.01")] * 3, [Decimal("0.01")] * 2
    assert beta(fund, bench) is None
    assert tracking_error(fund, bench) is None
    assert capture(fund, bench, rising=True) is None
    assert capture(fund, bench, rising=False) is None


def test_capture_survives_a_zero_denominator_no_market_produces() -> None:
    """Two falling-day returns of -200% multiply back to exactly 1, so the
    denominator is zero. The review called this guard unreachable; working the
    fix showed otherwise, and this pins the case that reaches it. No real price
    series gets here -- levels are validated positive upstream."""
    from src.m2_fund.risk import capture

    falls = [Decimal("-2"), Decimal("-2")]
    assert capture([Decimal("-0.5"), Decimal("-0.5")], falls, rising=False) is None
