"""Return and risk arithmetic over a NAV series. MODULE_2.md §8.

Pure functions: every one takes a series and returns a number. No SQL, no
connection, no I/O — M2 reads through `MarketDataProvider` (invariant 3), and
everything here operates on what that hands back.

**The series must be adjusted NAV.** Raw NAV of an IDCW plan drops on every
payout, so a return computed on it reads a distribution as a loss
(`MODULE_0.md` §9.1). `nav_series` defaults to `adjusted=True`; nothing here
re-checks it, because the column is chosen one layer up and checking twice
invites the two checks to disagree.

Callers hand over a validated series: `compute_return_window` rejects a
non-positive NAV once, before any of this runs.

Every figure is a `Decimal` (invariant 1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise

from src.common.contracts.market import NavPoint
from src.common.decimals import RATE_Q

#: Trading days in a year. The series is trading days, not calendar days, so
#: daily volatility scales by sqrt(252) and NOT sqrt(365) — using 365 on a
#: trading-day series overstates volatility by about 18%.
TRADING_DAYS = Decimal(252)


def daily_returns(navs: list[NavPoint]) -> list[Decimal]:
    """Period-over-period returns. One shorter than the series it is given."""
    return [cur.nav / prev.nav - 1 for prev, cur in pairwise(navs)]


def annualised_vol(returns: list[Decimal]) -> Decimal:
    """Annualised standard deviation of the daily return series.

    Sample standard deviation (n-1), because the series is a sample of the
    fund's behaviour rather than its whole population.
    """
    n = len(returns)
    if n < 2:
        return Decimal(0)
    mean = sum(returns, Decimal(0)) / n
    variance = sum(((r - mean) ** 2 for r in returns), Decimal(0)) / (n - 1)
    return (variance.sqrt() * TRADING_DAYS.sqrt()).quantize(RATE_Q)


@dataclass(frozen=True)
class Drawdown:
    """MODULE_2.md §8.3. `depth` is negative; zero means the fund never fell."""

    depth: Decimal
    peak: date
    trough: date
    #: None means the peak has not been regained within the window.
    recovery: date | None
    duration_days: int
    recovery_days: int | None


def max_drawdown(navs: list[NavPoint]) -> Drawdown:
    """The worst peak-to-trough fall in the window, and whether it recovered.

    Recovery is measured against the peak that PRECEDED the trough, not the
    highest point in the whole window — a fund that falls, recovers, then rises
    further has recovered, and comparing against the later high would say it
    never did.
    """
    if not navs:
        raise ValueError("max_drawdown needs at least one NAV point")

    peak_v, peak_d = navs[0].nav, navs[0].nav_date
    depth, worst_peak, trough_d = Decimal(0), peak_d, navs[0].nav_date
    worst_peak_v = peak_v

    for p in navs:
        if p.nav > peak_v:
            peak_v, peak_d = p.nav, p.nav_date
        fall = p.nav / peak_v - 1
        if fall < depth:
            depth, worst_peak, trough_d = fall, peak_d, p.nav_date
            worst_peak_v = peak_v

    # Only a real fall can recover. Without this guard a series that never
    # fell reports the peak it set on day two as a "recovery", because the
    # trough still points at the first observation.
    recovery = (
        next(
            (p.nav_date for p in navs if p.nav_date > trough_d and p.nav >= worst_peak_v),
            None,
        )
        if depth < 0
        else None
    )
    return Drawdown(
        depth=depth.quantize(RATE_Q),
        peak=worst_peak,
        trough=trough_d,
        recovery=recovery,
        duration_days=(trough_d - worst_peak).days,
        recovery_days=(recovery - trough_d).days if recovery else None,
    )


def confidence_from_obs(obs_days: int) -> str:
    """MODULE_2.md §8.2, verbatim.

    Three years is `high`, one year `medium`, anything shorter `low`. The point
    is not the thresholds but that the tier travels with every number: an
    eleven-month figure rendered with the same weight as a seven-year one is
    the attribution failure this project exists to refuse.
    """
    if obs_days >= 1095:
        return "high"
    if obs_days >= 365:
        return "medium"
    return "low"
