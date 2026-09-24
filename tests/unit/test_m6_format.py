"""Formatting. MODULE_6.md §9 and §19.4.

**§19.4's test table contradicts §9.1's code, and this file follows the code.**
For `12345678901` the code produces `12,34,56,78,901.00` and the table expects
`1,23,45,67,89,01.00`. Indian grouping is last-three-then-pairs, so a grouped
number always ends in a three-digit block; the table's expectation ends in two
and groups from the left, which is not a convention anywhere. The table's other
three rows agree with the code and are kept verbatim. DECISIONS V1-22.

Formatting is the one thing M6 is allowed to do to a number, so it is also the
only place a presentation bug can change what the reader believes: `" p.a."` on
a cumulative return quadruples it in their head, and a `0` where a `—` belongs
turns "not computed" into "computed, and it was nothing".
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from src.m6_views.format import (
    DASH,
    format_date,
    format_inr,
    format_pct,
    format_return,
    format_staleness,
    group_indian,
)


def D(v: str) -> Decimal:
    return Decimal(v)


# --- §9.1 Indian grouping ----------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1234567.89", "12,34,567.89"),
        ("100", "100.00"),
        ("-1234567", "-12,34,567.00"),
        # §19.4's fourth row, corrected to what §9.1's own code produces:
        # 1234 crore, 56 lakh, 78 thousand, 901.
        ("12345678901", "12,34,56,78,901.00"),
        ("999", "999.00"),
        ("1000", "1,000.00"),
        ("100000", "1,00,000.00"),
        ("10000000", "1,00,00,000.00"),
    ],
)
def test_indian_grouping(value: str, expected: str) -> None:
    assert group_indian(D(value)) == expected


def test_every_grouped_number_ends_in_a_three_digit_block() -> None:
    """The property §19.4's table violates, asserted directly rather than
    row by row — it is what makes the grouping Indian rather than arbitrary."""
    for n in ("1234", "123456", "12345678", "1234567890", "123456789012"):
        assert len(group_indian(D(n), precision=0).split(",")[-1]) == 3


def test_grouping_survives_a_zero_precision_request() -> None:
    assert group_indian(D("1234567"), precision=0) == "12,34,567"


# --- §9.1 compact rupees -----------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("15000000", "₹1.50 Cr"),
        ("1017929885000", "₹1,01,792.99 Cr"),
        ("250000", "₹2.50 L"),
        ("5000", "₹5.00 K"),
        ("-250000", "-₹2.50 L"),
        ("999", "₹999.00"),
    ],
)
def test_compact_inr(value: str, expected: str) -> None:
    assert format_inr(D(value)) == expected


def test_a_table_gets_full_grouping_not_a_compact_form() -> None:
    """§9.1's rule: axis labels and KPI tiles compact, tables and exports not.
    Two tiles reading "₹1.50 Cr" and "₹1,50,00,000.00" are the same number and
    look like two."""
    assert format_inr(D("15000000"), compact=False) == "₹1,50,00,000.00"


# --- §9.3 nulls --------------------------------------------------------------


def test_none_renders_as_an_em_dash_everywhere() -> None:
    """Never 0, never blank, never N/A. The distinction is load-bearing: a NULL
    XIRR means the returns engine has not run, and a 0.0% would say the
    portfolio returned nothing."""
    assert format_inr(None) == DASH
    assert format_pct(None) == DASH
    assert format_return(None, annualised=True) == DASH
    assert format_date(None) == DASH
    assert format_staleness(None) == DASH
    assert DASH == "—"


def test_zero_is_not_a_dash() -> None:
    """The other half of the rule. A computed zero must look computed."""
    assert format_inr(Decimal(0)) == "₹0.00"
    assert format_pct(Decimal(0)) == "0.0%"


# --- §9.2 percentages and returns -------------------------------------------


def test_annualised_label_only_when_annualised() -> None:
    """`MODULE_2.md` §7.3 forbids annualising a sub-year window, so a
    cumulative figure carrying `p.a.` is not a cosmetic error."""
    assert " p.a." in format_return(D("0.12"), annualised=True)
    assert " p.a." not in format_return(D("0.12"), annualised=False)


def test_a_return_is_a_fraction_and_a_pct_is_already_a_percentage() -> None:
    """The asymmetry is §9.2's and it is deliberate — every percentage stored in
    this project is out of 100, every return is a fraction. Asserted so a later
    harmonisation cannot quietly introduce a factor of 100."""
    assert format_return(D("0.12"), annualised=False) == "+12.0%"
    assert format_pct(D("12")) == "12.0%"


def test_a_signed_percentage_shows_its_sign_even_when_positive() -> None:
    assert format_pct(D("2.5"), signed=True) == "+2.5%"
    assert format_pct(D("2.5")) == "2.5%"


# --- §9.4 dates --------------------------------------------------------------


def test_dates_are_unambiguous() -> None:
    """Never MM/DD or DD/MM — this project's sources use both."""
    assert format_date(date(2026, 7, 31)) == "31 Jul 2026"


def test_staleness_reads_as_english() -> None:
    assert format_staleness(0) == "today"
    assert format_staleness(1) == "1 day old"
    assert format_staleness(43) == "43 days old"
