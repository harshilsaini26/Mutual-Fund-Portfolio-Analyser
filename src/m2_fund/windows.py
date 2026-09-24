"""Return windows. MODULE_2.md §8.1.

§8.1 computes eighteen fields; eight of them need a benchmark or a risk-free
rate. The risk-free side is on record: `config/risk_free.yaml` carries every
91-day Treasury Bill auction since 2011 (S13), so Sharpe, Sortino and the
downside deviation that feeds Sortino are all computed here, with the rate that
produced them travelling beside as `risk_free_pct`.

The benchmark side is now on record too, for part of the warehouse. S12 loads
NSE's total-return series and resolves 1,573 of 19,598 schemes to an index, so
alpha, beta, tracking error, information ratio and the capture ratios are
computed where a fund has a benchmark and left `None` where it does not. That
is optionality of the ordinary kind, and it is why these fields exist at all
now — a field that is ALWAYS `None` claims to be optional when it is in fact
unavailable, which is what they were before an index series existed.

Two different `None`s remain, and they mean different things. Sharpe is `None`
for a window starting before 2011 because the risk-free record does not reach
it. Beta is `None` for an active fund because nothing says which index it is
measured against — its name does not carry one, and the disclosure that does is
not loaded for every AMC.

The three-window model of §3 — fund, manager, user — is not here either. There
is no manager or tenure data, and W_user needs the Zone B ledger. What remains
is the fund window. The longest is `since_first_nav`, not `since_inception`:
the launch date is known now (AMFI's scheme master, S2), but the NAV history
held can start after it, and the window is only as long as the prices are.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from statistics import median
from typing import NamedTuple

from src.common.contracts.market import NavPoint
from src.common.decimals import RATE_Q, annualise
from src.common.types import IndexId, SchemeId
from src.m0_data.config import risk_free_on
from src.m0_data.providers.market_data import MarketDataProvider
from src.m2_fund.paths import price_history, rolling_windows
from src.m2_fund.risk import (
    Drawdown,
    aligned,
    alpha_annual,
    annualised_vol,
    beta,
    capture,
    confidence_from_obs,
    daily_returns,
    downside_deviation,
    information_ratio,
    max_drawdown,
    returns_of,
    sharpe,
    sortino,
    tracking_error,
)

#: The fixed look-back windows, in years. `since_first_nav` is not here: its
#: start is a property of the series, not of the calendar.
WINDOW_YEARS = {"1y": 1, "3y": 3, "5y": 5}

#: A week: a window whose first price falls on the day after a holiday still
#: spans its period.
SPAN_SLACK_DAYS = 7


def spans(obs_days: int, key: str) -> bool:
    """Whether `obs_days` of prices cover the fixed window `key` (1y, 3y, 5y).

    `compute_return_window` builds a window from whatever prices fall inside it,
    so a fund with four years of history has a "5y" window four years long. That
    is right in a table that prints each window's days, and wrong anywhere the
    figure is labelled "5 years" (DECISIONS V1-74).
    """
    return obs_days + SPAN_SLACK_DAYS >= WINDOW_YEARS[key] * 365


#: §9: below twelve windows the distribution says nothing worth printing.
MIN_ROLLING_WINDOWS = 12

#: A series with fewer distinct prices than this has no return to read. The
#: shape a daily-IDCW plan takes when its declarations are not loaded: the
#: whole return was distributed rather than accrued, so `nav_adj` equals raw
#: NAV and the line is flat. Named here, and imported by the reporting layer,
#: so there is one answer to "is this computable?" rather than two.
MIN_DISTINCT_NAVS = 2


class NonPositiveNav(ValueError):
    """A NAV at or below zero. Invariant 5: raise rather than clamp.

    No real scheme prices at zero, so this means the series is corrupt. The
    arithmetic downstream would otherwise divide by it or take its log, and
    produce a number that looks like a return.
    """


def _reject_non_positive(navs: list[NavPoint]) -> None:
    """The one validation both entry points route through."""
    for p in navs:
        if p.nav <= 0:
            raise NonPositiveNav(f"{p.scheme_id} priced {p.nav} on {p.nav_date}")


class Rolling(NamedTuple):
    """Every window of one horizon, summarised. MODULE_2.md §9.

    Answers what a single 3y figure cannot: was that number typical, or one
    good year carrying the average. `worst` is the question people actually
    ask -- the worst three years this fund ever handed anyone who held it.
    """

    horizon_days: int
    windows: int
    worst: Decimal
    median: Decimal
    best: Decimal
    pct_positive: Decimal


@dataclass(frozen=True)
class ReturnWindow:
    """One fund, one window, no benchmark.

    `interpolated_pct` is carried rather than dropped because a filled NAV is
    not a fetched one (`NavPoint.is_interpolated`), and interpolation is a
    straight line — a straight line has no variance, so a window built largely
    from filled points understates its own volatility. Reporting the figure
    without the proportion would hide that.

    `obs_days` is the wall-clock span and `obs_count` the number of prices. A
    "3y" window over a fund with eighteen months of history is still labelled
    3y; these two are how a reader sees that.
    """

    window_key: str
    return_cum: Decimal
    return_ann: Decimal
    volatility_ann: Decimal
    drawdown: Drawdown
    obs_days: int
    obs_count: int
    interpolated_pct: Decimal
    confidence: str
    #: None when `config/risk_free.yaml` carries no observation on or before
    #: this window's start. Genuinely optional now, unlike the fields left out
    #: entirely for needing a benchmark that does not exist.
    risk_free_pct: Decimal | None = None
    sharpe: Decimal | None = None
    sortino: Decimal | None = None
    #: `benchmark_id` is set whenever the scheme has a benchmark. The statistics
    #: below it are None unless that index also overlaps this window by at
    #: least `MIN_PAIRED_OBS` days -- so an id with no beta means "too little
    #: data", and no id means "no benchmark". 1,573 of 19,598 schemes carry one
    #: (S12), so these are optional in the ordinary way, not the "absent
    #: because unavailable" case the module docstring describes.
    benchmark_id: str | None = None
    bench_return_ann: Decimal | None = None
    beta: Decimal | None = None
    tracking_error: Decimal | None = None
    alpha_ann: Decimal | None = None
    information_ratio: Decimal | None = None
    up_capture: Decimal | None = None
    down_capture: Decimal | None = None


#: Below this many paired observations a beta is noise dressed as a number.
#: About a month of trading days. §8.2's confidence tier describes how much to
#: trust a figure; this is the floor under which there is no figure to trust.
MIN_PAIRED_OBS = 20


class NonPositiveLevel(ValueError):
    """An index level at or below zero. Invariant 5, as `NonPositiveNav` is.

    A zero level makes the paired-return arithmetic a bare ZeroDivisionError
    naming nothing, and a negative one produces a return that looks like a
    number. `migrations/014_index.sql` has no CHECK on `level`, so this is
    where the series is refused.
    """


def _against(
    navs: list[NavPoint],
    benchmark: list[tuple[date, Decimal]] | None,
    fund_ann: Decimal,
    rf: Decimal | None,
    benchmark_id: str | None = None,
) -> dict[str, Decimal | None]:
    """The benchmark-relative half of a window, or `{}` when there is none.

    Empty rather than a dict of `None`s so the caller can tell "no benchmark"
    from "a benchmark that produced nothing", and so `ReturnWindow`'s defaults
    stay the single definition of absent.

    Everything here is measured over the dates BOTH series priced. The
    benchmark's own annualised return is taken across that intersection rather
    than from the index's own endpoints, because a fund cannot be held
    responsible for an index move on a day it did not trade.
    """
    if not benchmark:
        return {}
    common = aligned(navs, benchmark)
    if len(common) < MIN_PAIRED_OBS:
        return {}
    # Only the levels actually divided by are checked -- the list passed in is
    # the index's whole history, and re-validating 3,895 levels on each of four
    # windows buys nothing the arithmetic needs.
    for level_date, _, level in common:
        if level <= 0:
            raise NonPositiveLevel(
                f"{benchmark_id or 'benchmark'} priced {level} on {level_date}"
            )

    fund_rets, bench_rets = returns_of(common)
    span = (common[-1][0] - common[0][0]).days
    if span <= 0:
        return {}

    bench_ann = annualise(common[-1][2] / common[0][2], span).quantize(RATE_Q)
    b = beta(fund_rets, bench_rets)
    te = tracking_error(fund_rets, bench_rets)
    return {
        "bench_return_ann": bench_ann,
        "beta": b,
        "tracking_error": te,
        # Jensen's alpha needs both a beta and a risk-free rate. Without either
        # there is no alpha, rather than an alpha computed from an assumed one.
        "alpha_ann": (
            alpha_annual(fund_ann, bench_ann, b, rf)
            if b is not None and rf is not None
            else None
        ),
        "information_ratio": (
            information_ratio(fund_ann, bench_ann, te) if te is not None else None
        ),
        "up_capture": capture(fund_rets, bench_rets, rising=True),
        "down_capture": capture(fund_rets, bench_rets, rising=False),
    }

def window_start(as_of: date, key: str) -> date:
    """The start date of a fixed window ending at `as_of`.

    29 February has no counterpart in a non-leap year, so a 1y window from
    2024-02-29 would raise. It lands on the 28th instead, which is one day
    short and the only answer that exists.
    """
    years = WINDOW_YEARS[key]
    try:
        return as_of.replace(year=as_of.year - years)
    except ValueError:
        return as_of.replace(year=as_of.year - years, day=28)


def compute_return_window(
    navs: list[NavPoint],
    window_key: str,
    benchmark: list[tuple[date, Decimal]] | None = None,
    benchmark_id: str | None = None,
) -> ReturnWindow | None:
    """Assemble one window, or `None` when the series cannot support it.

    `None` rather than a zero-filled row: with fewer than two NAV points, or a
    series that never moves, there is no return to report and zeros would be
    indistinguishable from a fund that genuinely went nowhere. A flat series is
    what a daily-IDCW plan looks like when its declarations were never loaded —
    the whole return was distributed rather than accrued, so `nav_adj` equals
    raw NAV. Refusing here means no consumer can be handed 0.00%.

    This is also where the series is validated, once, for the arithmetic in
    `risk.py` that assumes positive prices.
    """
    if len(navs) < 2 or len({p.nav for p in navs}) < MIN_DISTINCT_NAVS:
        return None

    _reject_non_positive(navs)

    first, last = navs[0], navs[-1]
    obs_days = (last.nav_date - first.nav_date).days
    if obs_days <= 0:
        return None

    growth = last.nav / first.nav
    filled = sum(1 for p in navs if p.is_interpolated)
    rets = daily_returns(navs)
    vol = annualised_vol(rets)
    ann = annualise(growth, obs_days).quantize(RATE_Q)

    # The rate in force when the window STARTED, not today's: a Sharpe over
    # 2019-2022 judged against a 2026 yield is comparing a return to money
    # that cost something different at the time.
    rf = risk_free_on(first.nav_date)

    bench = _against(navs, benchmark, ann, rf, benchmark_id)

    return ReturnWindow(
        window_key=window_key,
        return_cum=(growth - 1).quantize(RATE_Q),
        return_ann=ann,
        volatility_ann=vol,
        drawdown=max_drawdown(navs),
        obs_days=obs_days,
        obs_count=len(navs),
        interpolated_pct=(Decimal(filled) * 100 / Decimal(len(navs))).quantize(RATE_Q),
        confidence=confidence_from_obs(obs_days),
        risk_free_pct=rf,
        sharpe=sharpe(ann, vol, rf) if rf is not None else None,
        sortino=(
            sortino(ann, downside_deviation(rets), rf)
            if rf is not None
            else None
        ),
        # The scheme's benchmark whether or not this window had enough overlap
        # to compare against it. Dropping it when the statistics come out empty
        # made "too little data" read the same as "no benchmark at all".
        benchmark_id=benchmark_id or None,
        **bench,
    )


def rolling_returns(
    navs: list[NavPoint], horizon_days: int, step_days: int = 30
) -> Rolling | None:
    """Annualised return of every `horizon_days` window, stepped by `step_days`.

    §9 steps by calendar month. 30 days is the same thing without pulling in
    `dateutil` for a distinction the distribution cannot feel -- and dateutil
    is not a declared dependency here, only a transitive accident (V0-19).

    `None` when the series yields fewer than twelve windows: §9's own floor,
    below which percentiles describe the sample rather than the fund.
    """
    if len(navs) < 2:
        return None
    _reject_non_positive(navs)

    # The windows are walked by `paths.rolling_windows`, which the fund page's
    # rolling chart shares. It yields only i < j: i == j means both ends
    # resolve to the SAME price, so the ratio is forced to 1.0 and the window
    # reports exactly 0.00% -- a fabricated figure. On a series sparse relative
    # to the horizon every window can land that way: points every 150 days
    # rising 100 -> 2050 reported 0.00% across all 49 windows before that guard.
    rets = [
        annualise(navs[j].nav / navs[i].nav, horizon_days).quantize(RATE_Q)
        for i, j in rolling_windows(
            [p.nav_date for p in navs], horizon_days, step_days
        )
    ]

    if len(rets) < MIN_ROLLING_WINDOWS:
        return None
    return Rolling(
        horizon_days=horizon_days,
        windows=len(rets),
        worst=min(rets),
        median=median(rets),
        best=max(rets),
        pct_positive=(
            Decimal(sum(1 for r in rets if r > 0)) * 100 / len(rets)
        ).quantize(RATE_Q),
    )


class NothingToCompute(ValueError):
    """The series holds no return to read. The message says why, and what
    loads the data that would -- it is shown to the user as it stands."""


@dataclass(frozen=True)
class FundWindows:
    """Every window for one scheme on one date: the fund page and the CLI.

    `windows` keeps a key for every window, `None` where the history cannot
    support it, so a short-history fund shows its gaps rather than fewer rows
    -- which would read as a fund examined and found unremarkable.
    """

    navs: list[NavPoint]  # adjusted, on or before as_of
    benchmark_id: IndexId | None
    windows: dict[str, ReturnWindow | None]
    staleness_days: int
    confidence: str  # the weakest window's: MODULE_6 §14.1 rule 3
    inception: date | None = None  # launch date, from AMFI's scheme master


def fund_windows(
    md: MarketDataProvider, scheme_id: SchemeId, as_of: date
) -> FundWindows:
    """Windows ending at the last NAV on or before `as_of`."""
    navs, by_date, index_id = price_history(md, scheme_id, as_of)
    if len(navs) < 2:
        raise NothingToCompute(
            f"{len(navs)} NAV points for {scheme_id} on or before {as_of}, so "
            f"there is nothing to compute. Load its history with "
            f"python -m jobs.backfill_scheme_nav --scheme {scheme_id}"
        )
    # A flat line is not a fund that made nothing: it is an IDCW plan whose
    # distributions are not on record, so nav_adj == nav and the return that
    # was paid out is invisible. 4,595 of 15,006 schemes with NAV are IDCW.
    if len({p.nav for p in navs}) < MIN_DISTINCT_NAVS:
        raise NothingToCompute(
            f"NAV for {scheme_id} takes one value across {len(navs):,} points, "
            f"so no return can be read from it. This is an IDCW plan whose "
            f"distributions are not loaded: the return paid out does not "
            f"appear. The Growth option of the same fund carries it."
        )

    end = navs[-1].nav_date
    # One level per NAV date: a comparison pairs same-day prices, so a level
    # on a day the fund did not price is never used.
    levels = list(by_date.items())
    windows: dict[str, ReturnWindow | None] = {
        key: compute_return_window(
            [p for p in navs if p.nav_date >= window_start(end, key)],
            key, levels, index_id,
        )
        for key in WINDOW_YEARS
    }
    windows["since_first_nav"] = compute_return_window(
        navs, "since_first_nav", levels, index_id
    )
    tiers = ["low", "medium", "high"]
    return FundWindows(
        navs=navs,
        benchmark_id=index_id,
        windows=windows,
        staleness_days=(as_of - end).days,
        confidence=min(
            (w.confidence for w in windows.values() if w),
            key=tiers.index, default="low",
        ),
        inception=md.inception(scheme_id),
    )
