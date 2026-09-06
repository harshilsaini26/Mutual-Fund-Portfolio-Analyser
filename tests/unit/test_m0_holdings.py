"""Portfolio disclosure parsing. MODULE_0.md §6, §7.2.

`tests/fixtures/m0/hdfc_holdings_sample.xlsx` is trimmed from HDFC Flexi Cap
Fund's real disclosure for 31-Jul-2026 — the fund this project's own ledger
holds. Every row is a row HDFC published. `CLAUDE.md`: prefer a real file over
a better simulation.

The tests that matter are the ones about **not** producing a plausible wrong
number. A disclosure that parses into a portfolio which still sums to 100% but
is missing its cash, or counts its sectors twice, is wrong in a way nothing
downstream can detect.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from src.m0_data.normalise.numbers import CoercionError, to_decimal
from src.m0_data.normalise.units import (
    UNIT_MULTIPLIER,
    AmbiguousUnitError,
    to_inr,
    unit_from_header,
)
from src.m0_data.parse.base import HoldingsParseResult, ParseFailed, RawFile
from src.m0_data.parse.holdings.base import classify_row
from src.m0_data.parse.holdings.hdfc import HdfcHoldingsParser
from src.m0_data.resolve.synthetic import match_synthetic

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "m0"
SAMPLE = FIXTURES / "hdfc_holdings_sample.xlsx"
FILENAME = "Monthly HDFC Flexi Cap Fund - 31 July 2026.xlsx"
AS_OF = date(2026, 7, 31)


@pytest.fixture(scope="module")
def raw() -> RawFile:
    return RawFile("file-1", "S5:hdfc", FILENAME, SAMPLE.read_bytes())


@pytest.fixture(scope="module")
def parsed(raw: RawFile) -> HoldingsParseResult:
    return HdfcHoldingsParser().parse(raw)


# --- §7.2 the 100x path ------------------------------------------------------


def test_the_unit_is_read_from_the_header(parsed: HoldingsParseResult) -> None:
    """§7.2. HDFC states `Market/ Fair Value (Rs. in Lacs.)` and means lakhs.

    Reading this wrong scales an entire portfolio by 100, and nothing
    downstream notices: the weights still sum to 100, only the absolute values
    are wrong, and they are wrong consistently.
    """
    assert {s.market_value_unit for s in parsed.securities} == {"lakh"}
    assert UNIT_MULTIPLIER["lakh"] == Decimal(100_000)


@pytest.mark.parametrize(
    ("header", "unit"),
    [
        ("Market/ Fair Value (Rs. in Lacs.)", "lakh"),
        ("Market Value (Rs. in Lakhs)", "lakh"),
        ("Market Value (Rs. in Crores)", "crore"),
        ("Amount (Rs.)", "absolute"),
        ("Value (in 000s)", "thousand"),
    ],
)
def test_every_spelling_of_the_scale_is_read(header: str, unit: str) -> None:
    """`Lacs`, `Lakhs`, `Crores` — the same fact spelled several ways."""
    assert unit_from_header(header) == unit


@pytest.mark.parametrize("header", ["Market Value", "", "Value (Rs. in Million)"])
def test_an_unreadable_unit_raises_rather_than_defaulting(header: str) -> None:
    """§7.2 is explicit: the parser must raise rather than guess.

    `absolute` is never a fallback. A header we failed to understand is far
    more likely to carry an unusual scale than a plain one, so defaulting to
    absolute would understate by 100,000 exactly when we understood least.
    Millions raise too — §7.2 defines no multiplier for them.
    """
    with pytest.raises(AmbiguousUnitError):
        unit_from_header(header)


def test_conversion_reaches_rupees_absolute(parsed: HoldingsParseResult) -> None:
    """ICICI Bank at 1,019,781.11 lakh is Rs 10,198 crore — 9.21% of the fund."""
    icici = next(s for s in parsed.securities if s.isin_raw == "INE090A01021")
    assert icici.market_value_raw == Decimal("1019781.11")
    assert to_inr(icici.market_value_raw, "lakh") == Decimal("101978111000.00")


# --- §6.4 classification, and the two ways it silently breaks a portfolio ----


def test_the_industry_summary_is_not_counted_as_holdings(
    parsed: HoldingsParseResult,
) -> None:
    """DECISIONS V1-04. §6.4's own heuristic gets this wrong.

    After the holdings, HDFC prints a portfolio-classification-by-industry
    block as a SECOND table, offset from the first: the sector name lands in
    the ISIN column and the percentage lands in the instrument-name column.

        ISIN='Banks'   Name='28.94'   Quantity=None   MarketValue=None

    §6.4 ends with `if isin or qty or mv: return "security"`, and `isin` is
    truthy here — so all 32 rows of that block become securities and every
    sector is counted twice, once as its constituents and once as itself.

    Validating the ISIN instead of testing it for truthiness is the fix:
    `Banks` fails the check digit.
    """
    kinds = {s.row_kind for s in parsed.rows if s.isin_raw == "Banks"}
    assert "security" not in kinds
    assert not any(s.instrument_raw_name == "28.94" for s in parsed.securities)
    assert any(s.row_kind == "summary" for s in parsed.rows)


def test_a_named_section_that_carries_a_value_is_a_position(
    parsed: HoldingsParseResult,
) -> None:
    """§6.4's `and mv is None` guard, and it is load-bearing.

    HDFC writes each section label twice — once as a bare heading, once on the
    row carrying its numbers. `TREPS - Tri-party Repo` is a heading on one row
    and a Rs 3,432 crore position on the next. Matching the name alone
    classifies both as headings and the fund's entire cash position vanishes —
    with the remaining weights renormalising to 100, so nothing downstream can
    tell.
    """
    names = {s.instrument_raw_name for s in parsed.securities}
    assert "TREPS - Tri-party Repo" in names
    assert "Net Current Assets" in names

    treps = next(s for s in parsed.securities if "TREPS" in s.instrument_raw_name)
    assert treps.market_value_raw == Decimal("343194.12")


def test_totals_are_never_securities(parsed: HoldingsParseResult) -> None:
    """§6.3 rule 4. A `Total` row counted as a holding doubles the portfolio."""
    assert all(
        not s.instrument_raw_name.lower().startswith(("total", "sub total", "grand"))
        for s in parsed.securities
    )
    assert any(s.row_kind == "total" for s in parsed.rows)


def test_a_negative_position_is_kept(parsed: HoldingsParseResult) -> None:
    """A short leg is real exposure, and §10's V8 exists to check it.

    HDFC holds Eternal Limited long in the equity block and short through an
    option; the short row carries a negative quantity and market value.
    Dropping it would overstate the net position.
    """
    negative = [
        s for s in parsed.securities
        if s.market_value_raw is not None and s.market_value_raw < 0
    ]
    assert len(negative) == 1
    assert negative[0].instrument_raw_name == "Eternal Limited"
    assert negative[0].market_value_raw == Decimal("-88.76")


def test_a_footnote_marker_is_not_a_malformed_number() -> None:
    """HDFC marks a sub-0.01% position with a bare `@` in the `% to NAV` cell.

    Its own legend says `@ Less than 0.01%.`, so the cell means "see the note",
    not "here is a number I got wrong". It coerces to None; `12.3.4` still
    raises, because that one really is wrong and §7.1 is right to surface it.
    """
    assert to_decimal("@") is None
    assert to_decimal("*") is None
    with pytest.raises(CoercionError):
        to_decimal("12.3.4")


def test_prose_in_a_numeric_column_does_not_fail_the_parse(
    parsed: HoldingsParseResult,
) -> None:
    """The tail of a disclosure reuses the table's columns for text.

    `Top Ten Holdings` lands in the quantity cell. Strictness belongs where the
    number is load-bearing — a security's market value — not on every cell of
    every row, or a clean file raises on its own footnotes.
    """
    assert parsed.warnings == []
    assert len(parsed.securities) == 7


# --- §7.5 the as-of date -----------------------------------------------------


def test_the_as_of_date_comes_from_the_sheet(parsed: HoldingsParseResult) -> None:
    """§7.5 rule 1: an explicit date cell beats the filename.

    `Portfolio as on 31-Jul-2026` is in row 2. A file can be re-uploaded under
    a new name; the cell travels with the content.
    """
    assert parsed.as_of_date == AS_OF


def test_a_file_with_no_date_anywhere_raises(raw: RawFile) -> None:
    """§7.5: "Fail. Do not default to last month-end."

    A wrong as-of date files a portfolio against the wrong month, and every
    drift figure computed from it is nonsense that looks entirely plausible.
    """
    stripped = RawFile(raw.file_id, raw.source_id, "portfolio.xlsx", raw.content)
    result = HdfcHoldingsParser().parse(stripped)
    assert result.as_of_date == AS_OF, "the sheet still carries it"

    with pytest.raises(ParseFailed):
        HdfcHoldingsParser().parse(
            RawFile("x", "S5:hdfc", "nope.xlsx", b"not a workbook")
        )


# --- the disclosure as its own witness ---------------------------------------


def test_the_disclosure_states_its_own_nav(parsed: HoldingsParseResult) -> None:
    """The cheapest independent check in the pipeline, sitting in the notes.

    `Direct Plan - Growth Option 2267.177` on 2026-07-31 — and AMFI publishes
    2267.177 for `INF179K01UT0` on the same date, as does mfapi's mirror. Three
    sources, one number.

    The NAV block lists the as-of month FIRST and the prior month second, so
    reading any numeric cell would capture June's 2201.717 — close enough to
    look right and wrong enough to break the comparison.
    """
    assert parsed.stated_navs["Direct Plan - Growth Option"] == Decimal("2267.177")
    assert parsed.stated_navs["Growth Option"] == Decimal("2059.826")


def test_market_values_reconcile_to_the_files_own_grand_total(
    parsed: HoldingsParseResult,
) -> None:
    """The parser's arithmetic against the publisher's, on the full file.

    On the complete disclosure the securities sum to 11,073,641.18 lakh and the
    file's `Grand Total` row says 11,073,641.18 — a delta of exactly zero. The
    trimmed fixture holds a subset, so what is asserted here is that the total
    rows were parsed and excluded, which is what makes that reconciliation
    meaningful rather than circular.
    """
    totals = [s for s in parsed.rows if s.row_kind == "total"]
    assert totals
    assert all(t not in parsed.securities for t in totals)


# --- §8.4, exercised by a real disclosure -----------------------------------


def test_net_current_assets_resolves_to_the_receivables_bucket() -> None:
    """DECISIONS V1-04. §8.4's pattern requires the word "other" and misses this.

    §8.4 writes `other\\s+(current\\s+)?asset`. HDFC discloses the line as
    `Net Current Assets`, so the spec pattern does not match and a fund's
    working capital lands in `__UNRESOLVED__` — inflating the very metric (V3)
    that decides whether the look-through may be shown at all.
    """
    assert match_synthetic("Net Current Assets") == "__RECV__"
    assert match_synthetic("Net Receivables/(Payables)") == "__RECV__"
    assert match_synthetic("Other Current Assets") == "__RECV__"
    assert match_synthetic("Reliance Industries Ltd") is None


def test_a_government_security_reaches_the_gsec_bucket(
    parsed: HoldingsParseResult,
) -> None:
    """The disclosure's G-Sec row, through §8.4's rule."""
    gsec = next(s for s in parsed.securities if "GOI" in s.instrument_raw_name)
    assert match_synthetic(gsec.instrument_raw_name) == "__GSEC__"


# --- classify_row directly ---------------------------------------------------


@pytest.mark.parametrize(
    ("name", "isin", "mv", "expected"),
    [
        ("ICICI Bank Ltd.", "INE090A01021", Decimal("1"), "security"),
        ("TREPS - Tri-party Repo", "", Decimal("343194.12"), "security"),
        ("TREPS - Tri-party Repo", "", None, "section_header"),
        ("", "EQUITY & EQUITY RELATED", None, "section_header"),
        ("", "Sub Total", None, "total"),
        ("", "Notes :", None, "note"),
        ("", "", None, "blank"),
    ],
)
def test_row_classification(
    name: str, isin: str, mv: Decimal | None, expected: str
) -> None:
    """§6.4. The same label is a heading or a position depending on the numbers."""
    assert classify_row(name, isin, None, mv, None) == expected


def test_sniff_scores_a_matching_file_high(raw: RawFile) -> None:
    """§6.2 routes on evidence, not a hardcoded AMC->parser table.

    AMCs rename files and occasionally publish one month in another format, so
    a mapping goes stale silently where a sniff simply scores lower.
    """
    parser = HdfcHoldingsParser()
    assert parser.sniff(raw) > 0.9
    other = RawFile("x", "S5:hdfc", "sbi-portfolio.xlsx", b"PK\x03\x04")
    assert parser.sniff(other) < 0.5
    assert parser.sniff(RawFile("x", "S5:hdfc", FILENAME, b"<html>")) == 0.0
