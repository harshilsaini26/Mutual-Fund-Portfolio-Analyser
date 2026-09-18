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


def downside_deviation(returns: list[Decimal], mar: Decimal = Decimal(0)) -> Decimal:
    """Annualised deviation of returns BELOW `mar`, the minimum acceptable return.

    Divided by the full observation count, not by the number of down days.
    Dividing by the down-day count would make a fund that rarely falls look
    MORE volatile than one that always does, which inverts the statistic.

    Deleted once, for having no consumer: Sortino needs a risk-free rate and
    none was on record. `config/risk_free.yaml` gives it one, so it is back
    with something reading it.
    """
    n = len(returns)
    if n < 2:
        return Decimal(0)
    shortfall = sum((min(r - mar, Decimal(0)) ** 2 for r in returns), Decimal(0)) / n
    return (shortfall.sqrt() * TRADING_DAYS.sqrt()).quantize(RATE_Q)


def sharpe(
    return_ann: Decimal, volatility_ann: Decimal, rf_pct: Decimal
) -> Decimal | None:
    """Excess return per unit of total volatility. MODULE_2.md §8.1.

    `rf_pct` is a percent a year as written in the config; the returns are
    rates. None when there is no volatility to divide by -- a fund that never
    moved has no risk-adjusted return, and dividing by zero would assert an
    infinitely good one.
    """
    if volatility_ann <= 0:
        return None
    return ((return_ann - rf_pct / 100) / volatility_ann).quantize(RATE_Q)


def sortino(
    return_ann: Decimal, downside_ann: Decimal, rf_pct: Decimal
) -> Decimal | None:
    """Sharpe, but punishing only the falls. None when nothing fell."""
    if downside_ann <= 0:
        return None
    return ((return_ann - rf_pct / 100) / downside_ann).quantize(RATE_Q)


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


def paired_returns(
    navs: list[NavPoint], levels: list[tuple[date, Decimal]]
) -> tuple[list[Decimal], list[Decimal]]:
    """Fund and benchmark daily returns over the dates BOTH priced.

    Every benchmark-relative statistic is a comparison of two series, and two
    series compared on different days are not being compared at all. A fund
    that did not price on a day the index fell would otherwise contribute the
    index's fall against its own previous day's move -- which reads as tracking
    error the fund did not have.

    Returns are computed WITHIN the intersection, consecutively: if the fund
    misses a Tuesday, Monday-to-Wednesday is one return on both sides rather
    than a gap on one side and two steps on the other.
    """
    common = aligned(navs, levels)
    fund = [cur[1] / prev[1] - 1 for prev, cur in pairwise(common)]
    bench = [cur[2] / prev[2] - 1 for prev, cur in pairwise(common)]
    return fund, bench


def aligned(
    navs: list[NavPoint], levels: list[tuple[date, Decimal]]
) -> list[tuple[date, Decimal, Decimal]]:
    """`(date, nav, level)` for every date both series priced, in order.

    Separate from `paired_returns` because the benchmark's own return over the
    window has to be measured across the SAME dates as the fund's. Taking it
    from the index's first and last level instead would credit the fund with a
    benchmark move on days it did not trade.
    """
    by_date = {level_date: level for level_date, level in levels}
    return [
        (p.nav_date, p.nav, by_date[p.nav_date]) for p in navs if p.nav_date in by_date
    ]


def beta(fund: list[Decimal], bench: list[Decimal]) -> Decimal | None:
    """Sensitivity to the benchmark: cov(f, b) / var(b). MODULE_2.md §8.1.

    None when the benchmark never moved, because dividing by zero variance
    asserts an infinite sensitivity rather than an unknown one.
    """
    n = len(bench)
    if n < 2 or len(fund) != n:
        return None
    mean_f = sum(fund, Decimal(0)) / n
    mean_b = sum(bench, Decimal(0)) / n
    var_b = sum(((b - mean_b) ** 2 for b in bench), Decimal(0)) / (n - 1)
    if var_b <= 0:
        return None
    cov = sum(((f - mean_f) * (b - mean_b) for f, b in zip(fund, bench, strict=True)),
              Decimal(0)) / (n - 1)
    return (cov / var_b).quantize(RATE_Q)


def tracking_error(fund: list[Decimal], bench: list[Decimal]) -> Decimal | None:
    """Annualised standard deviation of the ACTIVE return, f - b. §8.1.

    The number an index fund is actually judged on: how far it drifts from the
    thing it promised to copy. Sample standard deviation and sqrt(252), for the
    same reasons `annualised_vol` uses them.
    """
    n = len(fund)
    if n < 2 or len(bench) != n:
        return None
    active = [f - b for f, b in zip(fund, bench, strict=True)]
    mean = sum(active, Decimal(0)) / n
    variance = sum(((a - mean) ** 2 for a in active), Decimal(0)) / (n - 1)
    return (variance.sqrt() * TRADING_DAYS.sqrt()).quantize(RATE_Q)


def alpha_annual(
    return_ann: Decimal, bench_ann: Decimal, beta_value: Decimal, rf_pct: Decimal
) -> Decimal:
    """Jensen's alpha: the return left over once the market exposure is paid for.

    `fund - (rf + beta * (bench - rf))`. Measured against a TOTAL RETURN index,
    which is why §9.4 refuses a price series here: a PRI benchmark understates
    `bench_ann` by its dividend yield and hands that difference straight to
    alpha, as skill the manager did not have.
    """
    rf = rf_pct / 100
    return (return_ann - (rf + beta_value * (bench_ann - rf))).quantize(RATE_Q)


def information_ratio(
    return_ann: Decimal, bench_ann: Decimal, tracking: Decimal
) -> Decimal | None:
    """Active return per unit of active risk. None when there is no drift."""
    if tracking <= 0:
        return None
    return ((return_ann - bench_ann) / tracking).quantize(RATE_Q)


def capture(fund: list[Decimal], bench: list[Decimal], *, rising: bool) -> Decimal | None:
    """Share of the benchmark's move the fund captured, up or down. §8.1.

    Compounded over the days the benchmark rose (or fell), not averaged: a
    ratio of arithmetic means describes a portfolio nobody holds. Above 1 on
    the up side and below 1 on the down side is the shape every fund claims.
    """
    picked = [
        (f, b) for f, b in zip(fund, bench, strict=True) if (b > 0) is rising and b != 0
    ]
    if not picked:
        return None
    grow_f, grow_b = Decimal(1), Decimal(1)
    for f, b in picked:
        grow_f *= 1 + f
        grow_b *= 1 + b
    if grow_b == 1:
        return None
    return ((grow_f - 1) / (grow_b - 1)).quantize(RATE_Q)
