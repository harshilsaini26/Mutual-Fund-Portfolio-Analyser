"""Return windows. MODULE_2.md §8.1.

§8.1 computes eighteen fields; eight of them need a benchmark or a risk-free
rate, and this warehouse has neither — `benchmark_id` is populated on 0 of
19,598 schemes and no risk-free series exists. Alpha, beta, tracking error,
information ratio, Sharpe, Sortino and the capture ratios are therefore absent
rather than `None`: a field that is always `None` claims to be optional when it
is in fact unavailable.

Downside deviation is absent for the same reason once removed — it exists only
to feed Sortino, which needs the risk-free rate (S13) nobody fetches.

The three-window model of §3 — fund, manager, user — is not here either. There
is no manager or tenure data, `inception_date` is empty for every scheme, and
W_user needs the Zone B ledger. What remains is the fund window, and the
longest one is named `since_first_nav` rather than `inception` because the
inception date is exactly the thing we do not have.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from statistics import median
from typing import NamedTuple

from src.common.contracts.market import NavPoint
from src.common.decimals import RATE_Q, annualise
from src.m0_data.config import risk_free_on
from src.m2_fund.risk import (
    Drawdown,
    annualised_vol,
    confidence_from_obs,
    daily_returns,
    downside_deviation,
    max_drawdown,
    sharpe,
    sortino,
)

#: The fixed look-back windows, in years. `since_first_nav` is not here: its
#: start is a property of the series, not of the calendar.
WINDOW_YEARS = {"1y": 1, "3y": 3, "5y": 5}

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


def compute_return_window(navs: list[NavPoint], window_key: str) -> ReturnWindow | None:
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

    dates = [p.nav_date for p in navs]
    horizon, step = timedelta(days=horizon_days), timedelta(days=step_days)
    rets: list[Decimal] = []
    start = dates[0]
    while start + horizon <= dates[-1]:
        # The series is sorted, so both ends are a binary search rather than
        # the linear scan M1's nav_on_or_before does over a dict.
        i = bisect_left(dates, start)
        j = bisect_right(dates, start + horizon) - 1
        # STRICTLY less: i == j means both ends resolve to the SAME price, so
        # the ratio is forced to 1.0 and the window reports exactly 0.00% --
        # a fabricated figure. On a series sparse relative to the horizon every
        # window can land that way: points every 150 days rising 100 -> 2050
        # reported 0.00% across all 49 windows before this.
        if i < j:
            rets.append(
                annualise(navs[j].nav / navs[i].nav, horizon_days).quantize(RATE_Q)
            )
        start += step

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
