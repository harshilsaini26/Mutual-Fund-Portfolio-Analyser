"""Number, percentage and date formatting. MODULE_6.md §9.

Indian grouping is **last three digits, then pairs**: 1234567.89 becomes
12,34,567.89, not 1,234,567.89.

**`None` renders as an em dash** — never `0`, never blank, never `N/A` (§9.3).
The distinction is load-bearing: a NULL means nobody computed it, a zero means
somebody did and the answer was nothing. Rendering a NULL XIRR as 0.0% would be
a lie with a number on it.

Formatting is the one thing M6 does to a figure (§2.1 forbids deriving one), and
it lives here rather than in a builder so there is exactly one place to check.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

#: §9.3. The only rendering of a null, everywhere.
DASH = "—"

CRORE = Decimal("1e7")
LAKH = Decimal("1e5")
THOUSAND = Decimal("1e3")


def group_indian(n: Decimal, precision: int = 2) -> str:
    """1234567.89 -> 12,34,567.89. Last three digits, then pairs.

    **§9.1's code and §19.4's test table contradict each other**, on the same
    input. For 12345678901 the code below produces `12,34,56,78,901.00` and the
    table expects `1,23,45,67,89,01.00`. The code is right: Indian grouping is
    last-three-then-pairs, so a grouped number always ends in a three-digit
    block. The table's expectation ends in two and groups from the left, which
    is not a convention anywhere — it reads as 1,23,45,67,89,01 where the value
    is twelve hundred thirty-four crore, fifty-six lakh, seventy-eight thousand,
    nine hundred and one. DECISIONS V1-22; the table's other three rows agree
    with the code and are kept.
    """
    sign = "-" if n < 0 else ""
    whole, _, frac = f"{abs(n):.{precision}f}".partition(".")
    if len(whole) <= 3:
        head = whole
    else:
        last3, rest = whole[-3:], whole[:-3]
        parts: list[str] = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        head = ",".join(parts) + "," + last3
    return f"{sign}{head}" + (f".{frac}" if precision else "")


def format_inr(
    v: Decimal | None,
    style: str = "indian",
    precision: int = 2,
    compact: bool = True,
) -> str:
    """§9.1. Rupees, in crore/lakh/thousand when compact.

    **Axis labels and KPI tiles use `compact=True`; tables and exports use
    `compact=False`.** Never mix within one view — two tiles reading "₹1.50 Cr"
    and "₹1,50,00,000.00" beside each other are the same number and look like
    two.
    """
    if v is None:
        return DASH
    if style != "indian":
        return f"₹{v:,.{precision}f}"
    if not compact:
        return f"₹{group_indian(v, precision)}"

    a, sign = abs(v), ("-" if v < 0 else "")
    if a >= CRORE:
        return f"{sign}₹{a / CRORE:.{precision}f} Cr"
    if a >= LAKH:
        return f"{sign}₹{a / LAKH:.{precision}f} L"
    if a >= THOUSAND:
        return f"{sign}₹{a / THOUSAND:.{precision}f} K"
    return f"{sign}₹{a:.{precision}f}"


def format_pct(
    v: Decimal | None, precision: int = 1, signed: bool = False
) -> str:
    """§9.2. Takes a percentage already — 13.32 renders as "13.3%".

    Note the asymmetry with `format_return`, which takes a *fraction*. It is
    §9.2's, kept rather than harmonised: every percentage in this codebase is
    stored out of 100 (`pct_normalised`, `coverage_pct`, `overlap_pct`) and
    every return M2 will produce is a fraction. Changing either would put a
    silent factor of 100 somewhere.
    """
    if v is None:
        return DASH
    return f"{v:+.{precision}f}%" if signed else f"{v:.{precision}f}%"


def format_return(v: Decimal | None, annualised: bool) -> str:
    """§9.2. Takes a FRACTION: 0.12 renders as "+12.0% p.a.".

    **`" p.a." is mandatory on annualised figures and forbidden on cumulative
    ones.`** `MODULE_2.md` §7.3 forbids annualising a sub-year window, so a
    cumulative three-month figure labelled `p.a.` is not a cosmetic error — it
    quadruples the number in the reader's head.
    """
    if v is None:
        return DASH
    return f"{v * 100:+.1f}%" + (" p.a." if annualised else "")


def format_date(d: date | None) -> str:
    """§9.4. `31 Jul 2026`. Never MM/DD or DD/MM — both are ambiguous, and this
    project's data spans Indian (DD/MM) and American (MM/DD) sources."""
    if d is None:
        return DASH
    return d.strftime("%d %b %Y")


def format_staleness(days: int | None) -> str:
    """§9.4: a relative form SUPPLEMENTS an absolute date, never replaces it.

    Used inside caveats that already carry the date, which is why this returns
    only the relative half.
    """
    if days is None:
        return DASH
    if days == 0:
        return "today"
    if days == 1:
        return "1 day old"
    return f"{days} days old"


__all__ = [
    "DASH",
    "format_date",
    "format_inr",
    "format_pct",
    "format_return",
    "format_staleness",
    "group_indian",
]
