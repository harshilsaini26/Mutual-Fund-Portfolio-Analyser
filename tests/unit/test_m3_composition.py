"""What one fund holds, summed for the fund page: `m3_lookthrough.composition`.

The look-through of a portfolio of one fund. Every figure is a share of the
fund, from the disclosure's own normalised weights, so the asset-mix slices sum
to exactly 100 and nothing is dropped on the way to a picture.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.common.types import ClassificationBasis, IssuerId
from src.m3_lookthrough.composition import CompositionRow, composition

LIST_DATE = date(2026, 6, 30)


def _row(
    issuer: str, weight: str, klass: str, sector: str | None = None
) -> CompositionRow:
    return CompositionRow(IssuerId(issuer), Decimal(weight), klass, sector)


ROWS = [
    _row("BANK", "30", "equity", "Banks"),
    _row("BANK", "5", "debt", None),  # the same company's bond
    _row("SOFT", "25", "equity", "IT - Software"),
    _row("TINY", "10", "equity", None),
    _row("GOVT", "20", "debt"),
    _row("__CASH__", "12", "cash"),
    _row("__RECV__", "-2", "cash"),
]
BUCKETS = {IssuerId("BANK"): "large", IssuerId("SOFT"): "mid"}


def test_a_company_held_twice_is_one_holding_classed_by_its_larger_part() -> None:
    fund = composition(ROWS, BUCKETS, ClassificationBasis.CURRENT, LIST_DATE)
    first = fund.holdings[0]
    assert (first.issuer_id, first.weight, first.instrument_class) == (
        IssuerId("BANK"), Decimal("35"), "equity"
    )
    assert [h.weight for h in fund.holdings] == sorted(
        (h.weight for h in fund.holdings), reverse=True
    )


def test_the_asset_mix_sums_to_the_whole_fund_negatives_included() -> None:
    """Net payables are a real negative slice; dropping them would make the
    other slices sum past 100."""
    fund = composition(ROWS, BUCKETS, ClassificationBasis.CURRENT, LIST_DATE)
    assert dict(fund.by_class) == {
        "equity": Decimal("65"), "debt": Decimal("25"), "cash": Decimal("10")
    }
    assert sum(v for _, v in fund.by_class) == Decimal(100)
    assert [k for k, _ in fund.by_class] == ["equity", "debt", "cash"]


def test_sectors_are_shares_of_the_equity_and_a_missing_one_is_said() -> None:
    fund = composition(ROWS, BUCKETS, ClassificationBasis.CURRENT, LIST_DATE)
    sectors = dict(fund.by_sector)
    assert sectors["Banks"] == Decimal("46.153846")  # 30 of 65
    assert sectors["Not stated"] == Decimal("15.384615")  # TINY's 10 of 65
    assert "cash" not in sectors and len(sectors) == 3


def test_the_size_profile_is_equity_only_and_keeps_the_unranked() -> None:
    fund = composition(ROWS, BUCKETS, ClassificationBasis.CURRENT, LIST_DATE)
    size = {t.dimension_value: t.exposure_pct for t in fund.size}
    assert size == {
        "large": Decimal("46.153846"),
        "mid": Decimal("38.461538"),
        "small": Decimal("0"),
        "unranked": Decimal("15.384615"),
    }
    assert fund.size[0].mcap_basis == LIST_DATE


def test_a_fund_with_no_equity_has_no_sectors_and_no_size() -> None:
    fund = composition(
        [_row("GOVT", "90", "debt"), _row("__TREPS__", "10", "cash")],
        {}, ClassificationBasis.CURRENT, LIST_DATE,
    )
    assert fund.by_sector == [] and fund.size == []
