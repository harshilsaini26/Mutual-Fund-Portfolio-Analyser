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

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.common.contracts.market import NavPoint
from src.m2_fund.risk import (
    METRIC_Q,
    Drawdown,
    annualise,
    annualised_vol,
    confidence_from_obs,
    daily_returns,
    max_drawdown,
)

#: The fixed look-back windows, in years. `since_first_nav` is not here: its
#: start is a property of the series, not of the calendar.
WINDOW_YEARS = {"1y": 1, "3y": 3, "5y": 5}


class NonPositiveNav(ValueError):
    """A NAV at or below zero. Invariant 5: raise rather than clamp.

    No real scheme prices at zero, so this means the series is corrupt. The
    arithmetic downstream would otherwise divide by it or take its log, and
    produce a number that looks like a return.
    """


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
    if len(navs) < 2 or len({p.nav for p in navs}) < 2:
        return None

    for p in navs:
        if p.nav <= 0:
            raise NonPositiveNav(f"{p.scheme_id} priced {p.nav} on {p.nav_date}")

    first, last = navs[0], navs[-1]
    obs_days = (last.nav_date - first.nav_date).days
    if obs_days <= 0:
        return None

    growth = last.nav / first.nav
    filled = sum(1 for p in navs if p.is_interpolated)

    return ReturnWindow(
        window_key=window_key,
        return_cum=(growth - 1).quantize(METRIC_Q),
        return_ann=annualise(growth, obs_days),
        volatility_ann=annualised_vol(daily_returns(navs)),
        drawdown=max_drawdown(navs),
        obs_days=obs_days,
        obs_count=len(navs),
        interpolated_pct=(Decimal(filled) * 100 / Decimal(len(navs))).quantize(METRIC_Q),
        confidence=confidence_from_obs(obs_days),
    )
