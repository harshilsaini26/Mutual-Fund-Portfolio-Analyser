"""Unit re-denominations in a NAV series. MODULE_0.md §2.2, §9.1.

A 10-for-1 re-denomination multiplies units and divides NAV; the holding is
worth exactly what it was a moment earlier. Left in the series it reads as a
900% gain in one day, which is how ICICI Prudential Overnight Fund -- a fund
that cannot move 1% in a day -- appeared to return 14.8x over seven years and
poisoned every volatility, drawdown and Sharpe computed from it.

55 schemes in this warehouse carry one, clustered on three dates, all at x10,
x1/10 or x1/100.

§2.2 already states the rule for securities: "Do not use bhavcopy close for
return computation without applying `security_adjustment` -- a bonus issue
otherwise reads as a 50% crash." Fund units split for the same reasons.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from src.m0_data.derive.nav_adj import _split_ratio, rescale_splits

START = date(2024, 1, 1)


def series(values: list[str]) -> list[tuple[date, Decimal]]:
    return [(START + timedelta(days=i), Decimal(v)) for i, v in enumerate(values)]


def values(got: list[tuple[date, Decimal]]) -> list[Decimal]:
    return [v for _, v in got]


def test_a_ten_for_one_split_leaves_the_series_continuous() -> None:
    """The real ICICI Overnight shape: x10.0014, the extra being that day's
    own return. Everything before is restated onto the later scale."""
    got = values(rescale_splits(series(["100", "116.4731", "1164.8919"])))

    assert got[0] == Decimal("1000.000000")
    assert got[1] == Decimal("1164.731000")
    assert got[2] == Decimal("1164.891900")
    # and the day-over-day move is now a day's return, not 900%
    assert (got[2] / got[1] - 1) < Decimal("0.001")


def test_a_hundred_for_one_split_is_handled_too() -> None:
    """HDFC Gold ETF, 4120.06 -> 41.52 on 2021-02-22."""
    got = values(rescale_splits(series(["4120.0604", "41.5242", "41.7432"])))

    assert got[0] == Decimal("41.200604")   # 4120.0604 / 100
    assert (got[1] / got[0] - 1) < Decimal("0.01")


def test_a_real_market_move_is_left_alone() -> None:
    """A fund can rise 50% over a series. It cannot be re-denominated by 1.5."""
    assert values(rescale_splits(series(["100", "150", "140"]))) == [
        Decimal("100"), Decimal("150"), Decimal("140"),
    ]


def test_a_dying_funds_last_row_is_not_a_split() -> None:
    """`INF174KA1DB4` drops 10.0727 -> 0.0001, which IS a clean power of ten
    (10^-5) and is NOT a re-denomination. Restricting to the ratios AMCs
    actually use keeps this visible as the defect it is rather than silently
    rescaling a fund's whole history by 100,000."""
    assert _split_ratio(Decimal("10.0727"), Decimal("0.0001")) is None
    assert values(rescale_splits(series(["10.0727", "0.0001"]))) == [
        Decimal("10.0727"), Decimal("0.0001"),
    ]


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ("116.4731", "1164.8919", Decimal(10)),    # ICICI Overnight, with a day's return
        ("100.0005", "1000.0050", Decimal(10)),    # exact
        ("4120.06", "41.5242", Decimal("0.01")),   # HDFC Gold ETF
        ("211.8310", "21.1506", Decimal("0.1")),
        # Equity ETFs move on the split day too, so the ratio lands further
        # off. These are the real extremes in this warehouse, 2.7% and 3.0%
        # from a clean tenth; a 1% tolerance missed 13 splits like them.
        ("149.9692", "14.5898", Decimal("0.1")),   # HDFC Midcap 150 ETF
        ("822.0532", "84.6849", Decimal("0.1")),   # Kotak PSU Bank ETF
        ("100", "150", None),                      # a real move
        ("100", "49", None),                       # a real crash
        ("10.0727", "0.0001", None),               # a defect
    ],
)
def test_the_ratio_classifier(before: str, after: str, expected: Decimal | None) -> None:
    assert _split_ratio(Decimal(before), Decimal(after)) == expected


def test_the_tolerance_does_not_reach_a_real_market_move() -> None:
    """10% around a tenth admits 0.09-0.11 and nothing else. The worst day any
    fund has is nowhere near that, so widening for ETFs costs no safety."""
    for ratio in ("0.30", "0.5", "0.8", "2", "3"):
        assert _split_ratio(Decimal(100), Decimal(100) * Decimal(ratio)) is None


def test_two_splits_in_one_series_compound() -> None:
    """Each applies to everything before it, so the earliest row carries both."""
    got = values(rescale_splits(series(["1", "10", "100"])))
    assert got == [Decimal("100.000000"), Decimal("100.000000"), Decimal("100.000000")]


def test_a_series_too_short_to_have_a_split_is_returned_as_is() -> None:
    assert values(rescale_splits(series(["100"]))) == [Decimal("100")]
    assert rescale_splits([]) == []
