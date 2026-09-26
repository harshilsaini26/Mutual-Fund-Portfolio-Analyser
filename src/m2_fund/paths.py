"""The series behind the fund page's pictures. MODULE_2.md §8.3 and §9.

`windows.py` reduces a price history to a few numbers per window. A chart needs
the path those numbers summarise: what Rs 10,000 became along the way, how far
below its last high the fund stood on each day, and what every rolling three
years returned. They are computed here, not in the view layer (MODULE_6.md
§2.1): M6 downsamples and labels them, and draws nothing it did not receive.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from src.common.contracts.market import NavPoint
from src.common.decimals import RATE_Q, annualise
from src.common.types import IndexId, SchemeId
from src.m0_data.providers.market_data import MarketDataProvider

#: What "Rs 10,000 invested" means on the growth chart.
GROWTH_BASE = Decimal(10000)
#: A week: a window whose first price falls on the day after a holiday still
#: spans its period.
SPAN_SLACK_DAYS = 7
RUPEE_Q = Decimal("0.01")

#: §9's floor: below twelve windows a distribution describes the sample.
MIN_ROLLING_WINDOWS = 12


def price_history(
    md: MarketDataProvider, scheme_id: SchemeId, as_of: date
) -> tuple[list[NavPoint], dict[date, Decimal], IndexId | None]:
    """Adjusted NAVs to `as_of`, and the benchmark's TRI level on each NAV date.

    One range read for the index rather than a query per NAV date. Only levels
    on a day the fund priced are kept: a comparison pairs same-day prices.
    """
    navs = md.nav_series(scheme_id, date.min, as_of, adjusted=True)
    index_id = md.benchmark_for(scheme_id)
    if not navs or index_id is None:
        return navs, {}, index_id
    published = {
        pt.level_date: pt.level
        for pt in md.index_series(index_id, navs[0].nav_date, navs[-1].nav_date)
    }
    paired = {p.nav_date: published[p.nav_date] for p in navs if p.nav_date in published}
    return navs, paired, index_id


@dataclass(frozen=True)
class GrowthPath:
    """Rs 10,000 in the fund and in its benchmark, from the same day.

    `start` is the first day both priced, so the two lines begin at the same
    point. A benchmark value is None on a day the index published no level.
    """

    start: date
    points: list[tuple[date, Decimal, Decimal | None]]


def growth_path(
    navs: list[NavPoint],
    levels: Mapping[date, Decimal],
    start: date,
    base: Decimal = GROWTH_BASE,
) -> GrowthPath | None:
    window = [p for p in navs if p.nav_date >= start]
    if len(window) < 2:
        return None
    # Anchor on the first day the index also priced, so both start at `base`;
    # but only if that is the window's start. An index (or an index fund
    # standing in for one, V1-81) that begins later would cut the fund's own
    # line short, so the fund is drawn alone instead.
    anchor = next((p for p in window if p.nav_date in levels), None)
    if anchor is not None and (
        anchor.nav_date - window[0].nav_date
    ).days > SPAN_SLACK_DAYS:
        anchor = None
    if anchor is not None:
        window = [p for p in window if p.nav_date >= anchor.nav_date]
    first = window[0]
    points = [
        (
            p.nav_date,
            (base * p.nav / first.nav).quantize(RUPEE_Q),
            (base * levels[p.nav_date] / levels[first.nav_date]).quantize(RUPEE_Q)
            if anchor is not None and p.nav_date in levels
            else None,
        )
        for p in window
    ]
    return GrowthPath(start=first.nav_date, points=points) if len(points) > 1 else None


def drawdown_path(navs: list[NavPoint]) -> list[tuple[date, Decimal]]:
    """How far below its highest price so far the fund stood, each day.

    Zero at a new high and negative below one, as a fraction: -0.25 is a fund
    worth a quarter less than at its peak. `risk.max_drawdown` is the deepest
    point of this line.
    """
    peak = Decimal(0)
    out: list[tuple[date, Decimal]] = []
    for p in navs:
        peak = max(peak, p.nav)
        out.append((p.nav_date, (p.nav / peak - 1).quantize(RATE_Q)))
    return out


def rolling_windows(
    dates: list[date], horizon_days: int, step_days: int
) -> Iterator[tuple[int, int]]:
    """Index pairs `(i, j)` of every `horizon_days` window, stepped by `step_days`.

    Both ends are a binary search on the sorted dates. STRICTLY `i < j`: equal
    means both ends resolved to the same price, and the window would report a
    fabricated 0.00% (see `windows.rolling_returns`).
    """
    horizon, step = timedelta(days=horizon_days), timedelta(days=step_days)
    start = dates[0]
    while start + horizon <= dates[-1]:
        i = bisect_left(dates, start)
        j = bisect_right(dates, start + horizon) - 1
        if i < j:
            yield i, j
        start += step


@dataclass(frozen=True)
class RollingPath:
    """Every rolling window's annual return, fund and benchmark, by end date.

    `pct_ahead` counts only windows where the benchmark priced at both ends,
    and is None when there are none: "ahead in 0% of periods" would be a claim.
    """

    horizon_days: int
    points: list[tuple[date, Decimal, Decimal | None]]
    worst: Decimal
    median: Decimal
    best: Decimal
    pct_ahead: Decimal | None


def rolling_path(
    navs: list[NavPoint],
    levels: Mapping[date, Decimal],
    horizon_days: int,
    step_days: int = 7,
) -> RollingPath | None:
    if len(navs) < 2:
        return None
    dates = [p.nav_date for p in navs]
    points: list[tuple[date, Decimal, Decimal | None]] = []
    for i, j in rolling_windows(dates, horizon_days, step_days):
        a, b = navs[i].nav_date, navs[j].nav_date
        bench = (
            annualise(levels[b] / levels[a], horizon_days).quantize(RATE_Q)
            if a in levels and b in levels
            else None
        )
        fund = annualise(navs[j].nav / navs[i].nav, horizon_days).quantize(RATE_Q)
        points.append((b, fund, bench))
    if len(points) < MIN_ROLLING_WINDOWS:
        return None
    returns = [f for _, f, _ in points]
    paired = [(f, b) for _, f, b in points if b is not None]
    ahead = Decimal(sum(1 for f, b in paired if f > b))
    return RollingPath(
        horizon_days=horizon_days,
        points=points,
        worst=min(returns),
        median=median(returns),
        best=max(returns),
        pct_ahead=(ahead * 100 / len(paired)).quantize(RATE_Q) if paired else None,
    )


def day_change(navs: list[NavPoint]) -> Decimal | None:
    """The last published price against the one before it, as a fraction:
    the "+0.43% on the day" beside a fund's NAV (DECISIONS V1-80, after Fundoo).

    Published prices only: an interpolated NAV is ours, not the fund's, and a
    change to or from one would be a change nobody published. None with fewer
    than two published prices.
    """
    published = [n for n in navs if not n.is_interpolated]
    if len(published) < 2 or published[-2].nav <= 0:
        return None
    return (published[-1].nav / published[-2].nav - 1).quantize(RATE_Q)


__all__ = [
    "GROWTH_BASE",
    "GrowthPath",
    "RollingPath",
    "day_change",
    "drawdown_path",
    "growth_path",
    "price_history",
    "rolling_path",
    "rolling_windows",
]
