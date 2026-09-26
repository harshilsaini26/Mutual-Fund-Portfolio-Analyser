"""The series behind the fund page's pictures: `src/m2_fund/paths.py`.

Hand-built prices with known answers. The windows these paths summarise are
tested in `test_m2_windows.py`; what is tested here is that each path agrees
with its summary and starts where a reader would assume it starts.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from src.common.contracts.market import NavPoint
from src.common.types import SchemeId
from src.m2_fund.paths import day_change, drawdown_path, growth_path, rolling_path
from src.m2_fund.risk import max_drawdown

D0 = date(2020, 1, 1)


def _navs(values: list[str], step: int = 1) -> list[NavPoint]:
    return [
        NavPoint(SchemeId("S"), D0 + timedelta(days=i * step), Decimal(v), False)
        for i, v in enumerate(values)
    ]


def test_ten_thousand_rupees_follow_the_price() -> None:
    navs = _navs(["50", "75", "100"])
    path = growth_path(navs, {}, D0)
    assert path is not None
    assert [v for _, v, _ in path.points] == [
        Decimal("10000.00"), Decimal("15000.00"), Decimal("20000.00")
    ]
    assert all(b is None for _, _, b in path.points)


def test_both_lines_start_together_on_the_first_day_the_index_priced() -> None:
    """Rebasing each line on its own first day would draw two lines that start
    at Rs 10,000 on different days, and every gap between them after that would
    be partly the gap in their start dates."""
    navs = _navs(["100", "110", "121", "133.1"])
    levels = {
        D0 + timedelta(days=1): Decimal("200"),
        D0 + timedelta(days=3): Decimal("250"),
    }
    path = growth_path(navs, levels, D0)
    assert path is not None
    assert path.start == D0 + timedelta(days=1)
    first, gap, last = path.points[0], path.points[1], path.points[-1]
    assert first[1] == first[2] == Decimal("10000.00")
    assert gap[2] is None  # no level that day: a gap, never a carried value
    assert last[1] == Decimal("12100.00") and last[2] == Decimal("12500.00")


def test_the_deepest_point_of_the_drawdown_line_is_the_max_drawdown() -> None:
    navs = _navs(["100", "120", "90", "60", "130", "117"])
    path = drawdown_path(navs)
    assert path[0][1] == Decimal(0) and path[1][1] == Decimal(0)
    assert path[3][1] == Decimal("-0.5")  # 60 against the 120 peak
    assert path[-1][1] == Decimal("-0.1")  # 117 against the new 130 peak
    assert min(v for _, v in path) == max_drawdown(navs).depth


def test_rolling_counts_periods_ahead_only_where_the_index_priced() -> None:
    # Fund doubles every 30 days' worth of steps; the index grows more slowly.
    navs = _navs([str(100 + i) for i in range(40)], step=10)
    levels = {p.nav_date: Decimal(100 + i // 2) for i, p in enumerate(navs)}
    # One date with no level: windows ending there are not counted either way.
    del levels[navs[20].nav_date]
    path = rolling_path(navs, levels, horizon_days=60, step_days=10)
    assert path is not None
    assert len(path.points) >= 12
    assert any(b is None for _, _, b in path.points)
    assert path.pct_ahead == Decimal(100)
    assert path.worst <= path.median <= path.best


def test_too_few_windows_is_no_answer_rather_than_a_thin_one() -> None:
    short = _navs(["100", "101", "102"], step=30)
    assert rolling_path(short, {}, horizon_days=60) is None


def test_no_benchmark_means_no_share_ahead_not_zero() -> None:
    path = rolling_path(_navs([str(100 + i) for i in range(40)], step=10), {}, 60, 10)
    assert path is not None and path.pct_ahead is None


def test_the_day_change_is_between_the_last_two_published_prices() -> None:
    """V1-80: "+0.43% on the day" beside the NAV. 78.34 to 78.68 is +0.434%.
    An interpolated price is skipped: nobody published it."""
    navs = _navs(["78.34", "78.68"])
    assert day_change(navs) == Decimal("0.004340")
    filled = NavPoint(SchemeId("S"), navs[-1].nav_date + timedelta(days=1),
                      Decimal("99"), True)
    assert day_change([*navs, filled]) == Decimal("0.004340")
    assert day_change(navs[:1]) is None
