"""Return windows. MODULE_2.md §8.1.

§8.1 computes eighteen fields; eight of them need a benchmark or a risk-free
rate, and this warehouse has neither — `benchmark_id` is populated on 0 of
19,598 schemes and no risk-free series exists. So this builds §8.1's own
`bm_available=False, rf_available=False` path rather than a reduced version of
it, and the two flags are carried explicitly so ingesting benchmarks later adds
fields instead of changing what the existing ones mean.

Alpha, beta, tracking error, information ratio, Sharpe, Sortino and capture
ratios are therefore absent rather than `None`. A field that is always `None`
claims to be optional when it is in fact unavailable, and the difference
matters to whoever reads the output next.

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
    annualise,
    annualised_vol,
    confidence_from_obs,
    daily_returns,
    downside_deviation,
    max_drawdown,
)

#: The fixed look-back windows, in years. `since_first_nav` is not here: its
#: start is a property of the series, not of the calendar.
WINDOW_YEARS = {"1y": 1, "3y": 3, "5y": 5}


@dataclass(frozen=True)
class ReturnWindow:
    """One fund, one window, no benchmark.

    `interpolated_pct` is carried rather than dropped because a filled NAV is
    not a fetched one (`NavPoint.is_interpolated`), and interpolation is a
    straight line — a straight line has no variance, so a window built largely
    from filled points understates its own volatility. Reporting the figure
    without the proportion would hide that.
    """

    window_key: str
    start_date: date
    end_date: date
    return_cum: Decimal
    return_ann: Decimal
    volatility_ann: Decimal
    downside_dev_ann: Decimal
    max_drawdown: Decimal
    dd_peak_date: date
    dd_trough_date: date
    dd_recovery_date: date | None
    dd_duration_days: int
    dd_recovery_days: int | None
    obs_days: int
    obs_count: int
    interpolated_pct: Decimal
    confidence: str
    bm_available: bool = False
    rf_available: bool = False


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

    `None` rather than a zero-filled row: with fewer than two NAV points there
    is no return to report, and emitting zeros would be indistinguishable from
    a fund that went nowhere. The caller knows which window it asked for, so
    nothing is lost by saying nothing.
    """
    if len(navs) < 2:
        return None

    # A series that never moves has no return and no risk to report, and
    # zeros would be indistinguishable from a fund that genuinely went
    # nowhere. This is the shape a daily-IDCW plan takes when its
    # declarations were never loaded: the whole return was distributed
    # rather than accrued, so `nav_adj` -- which equals raw NAV with no
    # events on record -- is a flat line. Refusing here rather than only in
    # the caller means no consumer of this function can be handed 0.00%.
    if len({p.nav for p in navs}) < 2:
        return None

    first, last = navs[0], navs[-1]
    obs_days = (last.nav_date - first.nav_date).days
    if obs_days <= 0:
        return None

    growth = last.nav / first.nav
    rets = daily_returns(navs)
    dd = max_drawdown(navs)
    filled = sum(1 for p in navs if p.is_interpolated)

    return ReturnWindow(
        window_key=window_key,
        start_date=first.nav_date,
        end_date=last.nav_date,
        return_cum=(growth - 1).quantize(METRIC_Q),
        return_ann=annualise(growth, obs_days),
        volatility_ann=annualised_vol(rets),
        downside_dev_ann=downside_deviation(rets),
        max_drawdown=dd.depth,
        dd_peak_date=dd.peak,
        dd_trough_date=dd.trough,
        dd_recovery_date=dd.recovery,
        dd_duration_days=dd.duration_days,
        dd_recovery_days=dd.recovery_days,
        obs_days=obs_days,
        obs_count=len(navs),
        interpolated_pct=(
            Decimal(filled) * 100 / Decimal(len(navs))
        ).quantize(METRIC_Q),
        confidence=confidence_from_obs(obs_days),
    )
