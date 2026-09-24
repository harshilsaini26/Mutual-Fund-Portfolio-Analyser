"""Portfolio disclosure parsing. MODULE_0.md §6, §7.2.

`tests/fixtures/m0/hdfc_holdings_sample.xlsx` is trimmed from HDFC Flexi Cap
Fund's real disclosure for 31-Jul-2026 — the fund this project's own ledger
holds. Every row is a row HDFC published, with one deliberate edit: the
`Grand Total` figure is set to the sum of the rows the fixture retains, so the
file is internally consistent. On the full disclosure that reconciliation is
+0.000000% against HDFC's own printed total.

`icici_holdings_sample.xlsx` is trimmed the same way from ICICI Multi-Asset's
real file, and carries the same deliberate edit for the same reason: its
`Total Net Assets` is the sum of the rows retained. It keeps every trap the
full file has — name before ISIN, a month-first as-on date, `% to Nav` written
as a fraction, and each subtotal sitting on the section row itself.

`icici_multi_asset_2026-07-31.xlsx` is that same disclosure **untrimmed and
unedited** — 450 rows, byte-identical to the archived download, sha256
`5dc05168…`. It is here because the trimmed fixture did not catch the defect
the real file did: trimming flattened a three-level tree to two, and two levels
were all V1-10's arithmetic could resolve. Read with that rule, the file parsed
94% too large. It is the one fixture in this module that must never be edited
for convenience — its value is that nobody chose what is in it.

`nippon_holdings_sample.xlsx` is trimmed from Nippon India Growth Mid Cap's
sheet of a 108-sheet workbook, with the same deliberate edit again. Its traps
are a leading internal-code column, a units header split across two lines, and
— the one that matters — a stock-future table printed BELOW the `GRAND TOTAL`
with the portfolio's own column shape. On the real file those trailing rows are
+0.155% of the total, which is inside the reconciliation guard's tolerance.

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
from src.m0_data.normalise.instrument_class import instrument_class
from src.m0_data.normalise.numbers import CoercionError, to_decimal
from src.m0_data.normalise.units import (
    UNIT_MULTIPLIER,
    AmbiguousUnitError,
    to_inr,
    unit_from_header,
)
from src.m0_data.parse.base import (
    HoldingsParseResult,
    ParseFailed,
    RawFile,
    StagedHolding,
)
from src.m0_data.parse.holdings.base import (
    TOTAL_TOLERANCE_PCT,
    _as_on_date,
    classify_row,
    nest,
    reconciliation_error,
)
from src.m0_data.parse.holdings.hdfc import HdfcHoldingsParser
from src.m0_data.parse.holdings.icici import IciciHoldingsParser
from src.m0_data.parse.holdings.kotak import KotakHoldingsParser
from src.m0_data.parse.holdings.nippon import NipponHoldingsParser
from src.m0_data.parse.holdings.ppfas import PpfasHoldingsParser
from src.m0_data.parse.holdings.registry import route
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


# --- the file's own total as the general guard -------------------------------


def test_the_parse_reconciles_to_the_files_own_stated_total(
    parsed: HoldingsParseResult,
) -> None:
    """DECISIONS V1-08. The only defence that generalises across AMCs.

    HDFC nests its sections one level deep and puts the numbers on a separate
    row. ICICI nests four levels and puts each subtotal on the section row
    itself, then adds covered calls, stock futures and interest-rate swaps at
    *notional* value. A parser tuned to one over-counts the other by 2.9x.

    No list of section labels distinguishes them — but both files state what
    the portfolio adds up to, and a parse that disagrees with that number has
    misread the sheet. On the full HDFC disclosure the error is +0.000000%.
    """
    assert parsed.stated_total is not None
    error = reconciliation_error(parsed)
    assert error is not None
    assert abs(error) <= TOTAL_TOLERANCE_PCT


def _workbook(rows: list[list[object]]) -> bytes:
    """A minimal disclosure, for the negative cases that need a broken file.

    Built here rather than committed as a binary: these sheets exist to be
    wrong in one specific way, and a hand-made xlsx nobody can read in a diff
    is a poor way to say which way that is.
    """
    import io

    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "SHEET1"
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


_HEADER: list[object] = [
    "Name Of the Instrument", "ISIN", "Quantity",
    "Market/ Fair Value (Rs. in Lacs.)", "% to NAV",
]


def test_a_sheet_that_disagrees_with_its_own_total_is_refused() -> None:
    """§6.3 rule 3, on the failure mode that matters most. DECISIONS V1-08.

    The section row here claims 900 while the two holdings under it come to
    1000, so it is not their subtotal and `_demote_subtotals` correctly leaves
    it alone — at which point it is counted as a third position and the sheet
    reads 1900 against its own stated 1000.

    That is the shape of every misread disclosure: the portfolio is nearly
    twice too large and **still normalises to 100%**, so no weight, no
    validation check and no look-through closure downstream can tell. Only the
    file's own arithmetic can, and it raises.
    """
    content = _workbook([
        ["Test Fund"],
        ["Portfolio as on 31-Jul-2026"],
        _HEADER,
        ["EQUITY BLOCK", None, None, 900.00, 90.00],
        ["Alpha Ltd", "INE040A01034", 10, 500.00, 50.00],
        ["Beta Ltd", "INE090A01021", 10, 500.00, 50.00],
        ["Grand Total", None, None, 1000.00, 100.00],
    ])
    with pytest.raises(ParseFailed, match="stated total"):
        HdfcHoldingsParser().parse(RawFile("x", "S5:test", "test.xlsx", content))


def test_a_percentage_column_that_is_neither_scale_raises() -> None:
    """§7.2's discipline, applied to the percentage column.

    A column totalling neither ~1 nor ~100 is not an unusual convention, it is
    a sheet we misread — and guessing a scale for it would produce weights that
    look perfectly ordinary.
    """
    content = _workbook([
        ["Test Fund"],
        ["Portfolio as on 31-Jul-2026"],
        _HEADER,
        ["Alpha Ltd", "INE040A01034", 10, 1000.00, 5000.00],
        ["Grand Total", None, None, 1000.00, 5000.00],
    ])
    with pytest.raises(ParseFailed, match="neither a fraction"):
        HdfcHoldingsParser().parse(RawFile("x", "S5:test", "test.xlsx", content))


# --- ICICI: the second format ------------------------------------------------


@pytest.fixture(scope="module")
def icici() -> HoldingsParseResult:
    return IciciHoldingsParser().parse(
        RawFile("file-2", "S5:icici", "ICICI Prudential Multi-Asset Fund.xlsx",
                (FIXTURES / "icici_holdings_sample.xlsx").read_bytes())
    )


def test_icici_subtotals_are_not_counted_as_holdings(
    icici: HoldingsParseResult,
) -> None:
    """DECISIONS V1-08. ICICI puts the subtotal ON the section row.

    `Equity & Equity Related Instruments` and `Listed / Awaiting Listing On
    Stock Exchanges` each carry a market value, so `classify_row` calls them
    securities — and it is right to by its own rule, which is what keeps HDFC's
    TREPS cash from being dropped as a heading.

    Arithmetic separates them: a row whose value equals the sum of the rows
    beneath it is their total. Both are demoted, and the portfolio then equals
    what the file says it equals.
    """
    kinds = {r.instrument_raw_name: r.row_kind for r in icici.rows}
    assert kinds["Listed / Awaiting Listing On Stock Exchanges"] == "subtotal"
    assert kinds["Equity & Equity Related Instruments (Note -1)"] == "subtotal"

    error = reconciliation_error(icici)
    assert error is not None
    assert abs(error) < Decimal("0.000001")


def test_counting_icicis_subtotals_would_inflate_the_portfolio(
    icici: HoldingsParseResult,
) -> None:
    """The trap is present in the fixture, not merely described in a comment.

    Without the demotion these two rows are staged as positions and the
    portfolio reads 1.7x its true size — while still normalising to 100%.
    """
    counted = sum(
        (r.market_value_raw or Decimal(0) for r in icici.rows
         if r.row_kind in ("security", "subtotal")),
        Decimal(0),
    )
    assert icici.stated_total is not None
    assert counted / icici.stated_total > Decimal("1.7")


REAL_ICICI = FIXTURES / "icici_multi_asset_2026-07-31.xlsx"


def _demoted_near(result: HoldingsParseResult, value: Decimal) -> bool:
    """Was a row worth about `value` demoted to a subtotal?

    Compared with a paisa of tolerance rather than exactly, because the sheet
    stores what it prints as `762548.54` as `762548.5400000003` — a spreadsheet
    float, not a decimal. Pinning those digits would assert the noise instead of
    the figure, and would break the day openpyxl rounds differently. A paisa is
    far tighter than any real position could collide within.
    """
    return any(
        r.market_value_raw is not None
        and abs(r.market_value_raw - value) < Decimal("0.01")
        for r in result.rows
        if r.row_kind == "subtotal"
    )


@pytest.fixture
def icici_real() -> HoldingsParseResult:
    """The whole published disclosure, not a trimmed one. See the module
    docstring for why this file exists alongside the sample."""
    return IciciHoldingsParser().parse(
        RawFile("file-real", "S5:icici",
                "ICICI Prudential Multi-Asset Fund.xlsx",
                REAL_ICICI.read_bytes()),
        "MULTI",
    )


PPFAS = FIXTURES / "ppfas_flexi_cap_2026-07-31.xlsx"


@pytest.fixture(scope="module")
def ppfas() -> HoldingsParseResult:
    """Parag Parikh Flexi Cap, fetched from amc.ppfas.com and archived
    unedited. One scheme per file, so no sheet is needed."""
    return PpfasHoldingsParser().parse(
        RawFile("file-ppfas", "S5:ppfas",
                "PPFCF_PPFAS_Monthly_Portfolio_Report_July_31_2026.xlsx",
                PPFAS.read_bytes()),
        None,
    )


def test_a_header_spelled_across_two_lines_is_still_that_header(
    ppfas: HoldingsParseResult,
) -> None:
    """PPFAS writes `% to Net
 Assets`, with a newline inside the cell.

    The needle `% to net asset` matches every other AMC and could not match
    across it, so no percentage column was mapped and every weight in a
    148,000 Cr portfolio was derived rather than reported. V1 caught it —
    `pct_sum_raw` of 0 against a 95-105 band — and quarantined the load, which
    is the gate working. The file was fine; the reader was wrong.

    Whitespace in a header cell is now collapsed before matching. Nippon's
    `Market/Fair Value
( Rs. in Lacs)` has the same shape and matched only by
    luck, on a shorter alternative further down the needle list.
    """
    assert ppfas.pct_scale is not None
    reported = sum(
        (r.pct_to_nav_raw or Decimal(0)) for r in ppfas.securities
    ) * ppfas.pct_scale
    assert Decimal(95) <= reported <= Decimal(105), f"weights sum to {reported}"


def test_a_formula_cell_is_read_as_its_value_not_its_text(
    ppfas: HoldingsParseResult,
) -> None:
    """openpyxl returns formula TEXT unless asked for cached values, and a
    market value that will not parse does not raise — `classify_row` sees a
    name with no numbers and calls the row a section heading.

    So the row stops being a holding, silently. PPFAS's `Net Receivables /
    (Payables)` is `=342762.56+E193-105.07` on the sheet, worth Rs 303 Cr, and
    losing it put the parse 0.2044% under the file's own total — **inside the
    2% guard**, which is exactly how Nippon's stock futures hid at +0.155%
    (V1-15). A wrong number that reconciles is the failure mode this project
    exists to refuse.

    Seven formula cells on this sheet; zero across HDFC, ICICI and Kotak. Four
    AMCs parsed before one file was written by someone who used a formula.
    """
    receivables = [
        r for r in ppfas.securities
        if "receivab" in r.instrument_raw_name.lower()
    ]
    assert len(receivables) == 1, "the formula row is not a holding"
    assert receivables[0].market_value_raw == Decimal("30343.649999999972")


def test_ppfas_reconciles_and_carries_its_foreign_equity(
    ppfas: HoldingsParseResult,
) -> None:
    """The fund's signature: a large US-listed allocation alongside Indian
    equity, which is why it is a useful fifth format rather than a fifth file.
    """
    error = reconciliation_error(ppfas)
    assert error is not None
    # Exact, like the other real files. It was -0.2044% until the reader began
    # asking openpyxl for cached VALUES rather than formula text: `Net
    # Receivables / (Payables)` is a formula on this sheet, and reading it as
    # `'=342762.56+E193-105.07'` turned a Rs 303 Cr row into a section
    # heading. Inside the 2% guard the whole time.
    assert abs(error) < Decimal("0.000001"), f"reconciles at {error}"
    assert ppfas.as_of_date == date(2026, 7, 31)
    foreign = [r for r in ppfas.securities if (r.isin_raw or "").startswith("US")]
    assert len(foreign) >= 4
    names = " ".join(r.instrument_raw_name for r in foreign).lower()
    assert "alphabet" in names and "microsoft" in names


KOTAK = FIXTURES / "kotak_pioneer_2026-07-31.xlsx"


@pytest.fixture(scope="module")
def kotak() -> HoldingsParseResult:
    """Kotak Pioneer's sheet, lifted unchanged out of the real 119-sheet
    consolidated workbook (source sha256 `e519d3c8ce2672c3`).

    The other 118 schemes' sheets were dropped and nothing else was: 3.77 MB of
    workbook for one 158 KB sheet is not a fixture, it is an archive, and the
    full file lives under its hash in `data/raw/` where archives belong.

    This is NOT the edit V1-28 warned about. That was about trimming ROWS,
    which flattened ICICI's three-level nesting into two and hid the defect the
    real file exposed. Dropping a sibling scheme's sheet does not touch this
    one: the column indentation, the `Total` in the Industry column, every
    figure and the fine-grained AMFI taxonomy are all exactly as published.
    Verified by parsing both and comparing.
    """
    return KotakHoldingsParser().parse(
        RawFile("file-kotak", "S5:kotak",
                "ConsolidatedSEBIPortfolioJuly2026.xlsx", KOTAK.read_bytes()),
        "KPF",
    )


def test_kotak_reconciles_to_its_own_stated_total(
    kotak: HoldingsParseResult,
) -> None:
    """323,957.09 + 67,823.69 + 10,966.88 - 489.64 = 402,258.02, which is the
    Grand Total the sheet prints."""
    error = reconciliation_error(kotak)
    assert error is not None
    assert abs(error) < Decimal("0.000001"), f"reconciles at {error}"
    assert kotak.stated_total == Decimal("402258.02")
    assert kotak.as_of_date == date(2026, 7, 31)


def test_kotak_indents_by_column_and_the_name_is_still_read(
    kotak: HoldingsParseResult,
) -> None:
    """The sheet's nesting level IS the column index.

    `Name of Instrument` heads column 0, but `Equity & Equity related` sits in
    column 0, `Listed/Awaiting listing` in column 1 and all 51 holdings in
    column 2. Read as a single column the name is empty for every holding, and
    a parser that accepted that would stage 51 nameless securities.
    """
    eternal = next(
        r for r in kotak.securities if r.isin_raw == "INE758T01015"
    )
    assert eternal.instrument_raw_name.strip().upper() == "ETERNAL LIMITED"
    assert eternal.market_value_raw == Decimal("22451.84")
    assert all(r.instrument_raw_name.strip() for r in kotak.securities)


def test_a_written_off_bond_is_a_holding_worth_nothing(
    kotak: HoldingsParseResult,
) -> None:
    """`0.00 $` is zero with a footnote, and the row is a real position.

    Kotak's debt sheets carry YES BANK's AT1 bonds — 428 units, written down to
    nothing, the `$` pointing at the note that explains it. Refusing to parse
    the value cost eight sheets of the 119-sheet workbook, because §6.3 rule 3
    turns an unreadable market value into a raise and the raise takes the whole
    sheet.

    A held instrument worth zero is a fact about the portfolio. Dropping it and
    raising on it are both wrong, and dropping is worse because it is quiet.
    """
    from src.m0_data.normalise.numbers import to_decimal

    assert to_decimal("0.00 $") == Decimal("0.00")
    assert to_decimal("0.00 #") == Decimal("0.00")
    assert to_decimal("12.5*") == Decimal("12.5")


def test_stripping_a_marker_cannot_rescue_something_that_is_not_a_number(
) -> None:
    """The safety argument for the rule above, and the whole of it.

    The marker is removed only when what remains parses. `#N/A` loses its `#`
    and is still not a number, so it still raises — as do `#DIV/0!` and `B.C.`,
    which are what AMFI is documented to ship in a NAV column. A marker on a
    number is the only case whose answer changes.
    """
    from src.m0_data.normalise.numbers import CoercionError, to_decimal

    for token in ("#N/A", "#DIV/0!", "B.C.", "B. C.", "$", "@"):
        result: object
        try:
            result = to_decimal(token)
        except CoercionError:
            result = "raised"
        assert result in {"raised", None}, f"{token!r} became {result!r}"


def test_an_instrument_is_never_called_a_number(
    kotak: HoldingsParseResult,
) -> None:
    """The name span takes the innermost LABEL, and a bare number is not one.

    Kotak's debt sheets put an unlabelled numeric column to the left of the
    name, so "first non-empty in the span" read YES BANK's AT1 bonds as a
    security named `0` — which then failed on its market value and took the
    sheet with it. Neither half of that was visible from the error.
    """
    for row in kotak.securities:
        assert not row.instrument_raw_name.strip().replace(".", "").isdigit()


def test_kotak_labels_its_totals_in_the_industry_column(
    kotak: HoldingsParseResult,
) -> None:
    """Each block closes with `Total`, and the portfolio with `Grand Total`,
    in the Industry column — not in the name column and not in the ISIN
    column, which are the two §6.4 matches `TOTAL_ROW` is applied to.

    Counted as holdings the three of them bring the parse to 794,038.80
    against a stated 402,258.02: +97.4%. The reconciliation guard would have
    refused the load without ever saying why.
    """
    # Totals that carry a VALUE — the ones that would be counted as holdings.
    # A note below the grand total reading `Total value of illiquid equity
    # shares...` also classifies as a total and carries none, so counting rows
    # here would be counting the prose.
    totals = [
        r for r in kotak.rows
        if r.row_kind == "total" and r.market_value_raw is not None
    ]
    assert len(totals) == 3
    values = {r.market_value_raw for r in totals}
    assert Decimal("323957.09") in values      # equity block
    assert Decimal("67823.69") in values       # overseas fund units
    assert Decimal("402258.02") in values      # grand total


def test_kotak_agrees_with_an_independent_witness(
    kotak: HoldingsParseResult,
) -> None:
    """A third party's arithmetic on the same portfolio, which no other
    fixture in this module has.

    Taken from a public fund-research page for Kotak Pioneer as of Jul 2026:
    51 holdings, top 5 = 18.85%, top 10 = 31.72%, largest Eternal at 5.58%.
    `company` here means an Indian corporate ISIN — the witness counts neither
    the two overseas fund units, nor Triparty Repo, nor net current assets.

    Its `14 sectors` is deliberately NOT asserted: Kotak publishes the
    fine-grained AMFI taxonomy (25 industries here) and the witness rolls them
    into coarse buckets, putting Retailing and Transport Services both under
    `Services`. Two taxonomies, not a disagreement.
    """
    total = kotak.stated_total
    assert total is not None
    companies = sorted(
        (r for r in kotak.securities if (r.isin_raw or "").startswith("INE")),
        key=lambda r: -(r.market_value_raw or Decimal(0)),
    )
    def pct(rows: list[StagedHolding]) -> Decimal:
        return sum(
            (r.market_value_raw or Decimal(0) for r in rows), Decimal(0)
        ) / total * 100

    assert len(companies) == 51
    assert round(pct(companies[:5]), 2) == Decimal("18.85")
    assert round(pct(companies[:10]), 2) == Decimal("31.72")
    assert companies[0].instrument_raw_name.strip().upper() == "ETERNAL LIMITED"
    assert round(pct(companies[:1]), 2) == Decimal("5.58")


def test_kpf_alone_is_55_rows(kotak: HoldingsParseResult) -> None:
    """The count that says `--sheet` picked one scheme and not the workbook.

    **What this fixture cannot prove**, and the reason is worth stating rather
    than leaving to a reader: the published workbook holds 119 sheets, and
    parsed whole it does not fail — it returns one portfolio containing every
    Kotak scheme, a plausible answer to a question nobody asked. This extract
    has one sheet, so it cannot demonstrate that. The 119 figure is recorded in
    V1-33 and was observed on the real file; the guard that matters is that
    `--sheet` exists and is documented as required for this AMC.
    """
    assert len(kotak.securities) == 55


def test_the_real_icici_file_reconciles_to_its_own_stated_total(
    icici_real: HoldingsParseResult,
) -> None:
    """The gate this slice exists to pass. Before the demotion rule learned to
    resolve nesting deeper than two levels this was +93.8% — four section rows
    carrying Rs 8,138,769.97 onto a stated Rs 8,678,503.80 (V1-25).

    Asserted far tighter than `TOTAL_TOLERANCE_PCT`. The 2% guard is what stops
    a bad parse loading; this is a golden file whose every subtotal is exact, so
    anything above float noise means the tree was misread.
    """
    error = reconciliation_error(icici_real)
    assert error is not None
    assert abs(error) < Decimal("0.000001"), f"reconciles at {error}"


def test_every_multi_child_section_in_the_real_file_is_demoted(
    icici_real: HoldingsParseResult,
) -> None:
    """The four V1-25 named, by value. Each one has more than one child, which
    is precisely what the old rule could not see: it summed the raw rows
    beneath, so a child that was itself a section got counted alongside the
    constituents it already stood for.

    Checked by value rather than by name because two of them are called
    `Listed / Awaiting Listing On Stock Exchanges` — one under equity, one
    under debt. Vocabulary does not identify a section on this sheet, which is
    V1-10's whole argument.
    """
    for value in (
        Decimal("6180420.35"),          # Equity & Equity Related Instruments
        Decimal("762548.54"),           # Debt Instruments
        Decimal("725279.24"),           # Listed / Awaiting Listing, under debt
        Decimal("470521.84"),           # Money Market Instruments
    ):
        assert _demoted_near(icici_real, value), f"{value} was kept as a holding"


def test_the_real_file_nests_three_levels_deep(
    icici_real: HoldingsParseResult,
) -> None:
    """The property that broke the old rule, asserted directly so that a
    future fixture swap cannot quietly remove it.

    `Debt Instruments` -> `Listed / Awaiting Listing` -> `Government
    Securities` is three levels, and the middle one is a subtotal whose own
    children are subtotals. A two-level file would still pass every other test
    in this module.
    """
    assert _demoted_near(icici_real, Decimal("762548.54"))    # parent
    assert _demoted_near(icici_real, Decimal("725279.24"))    # child, itself a parent
    assert _demoted_near(icici_real, Decimal("338978.74"))    # grandchild
    assert _demoted_near(icici_real, Decimal("386300.50"))    # grandchild
    # And the middle level really is the sum of the two beneath it.
    assert Decimal("338978.74") + Decimal("386300.50") == Decimal("725279.24")


def test_a_demoted_section_labels_its_rows_with_the_innermost_name(
    icici_real: HoldingsParseResult,
) -> None:
    """Deciding and labelling run in opposite orders, and this is why.

    Demotion has to resolve innermost-first to get the arithmetic right, but
    the section a row belongs to is the NEAREST heading above it, not the
    outermost. Applying demotions in the order they were decided would label
    every equity row `Equity & Equity Related Instruments`, losing the
    distinction V1-07 depends on to tell a derivative from a holding.
    """
    hdfc_bank = next(
        r for r in icici_real.securities if r.isin_raw == "INE040A01034"
    )
    # The listing status is innermost, and it qualifies the instrument heading
    # above it rather than replacing it (READER_VERSION 5).
    assert hdfc_bank.section == (
        "Equity & Equity Related Instruments (Note -1) :: "
        "Listed / Awaiting Listing On Stock Exchanges"
    )


def test_the_real_file_keeps_the_holdings_it_should(
    icici_real: HoldingsParseResult,
) -> None:
    """Demotion must not take real positions with it. A rule that demotes too
    eagerly also reconciles — against a smaller portfolio — so the count and a
    named holding are pinned here.
    """
    assert len(icici_real.securities) == 290
    assert icici_real.as_of_date == date(2026, 7, 31)
    hdfc_bank = next(
        r for r in icici_real.securities if r.isin_raw == "INE040A01034"
    )
    assert hdfc_bank.market_value_raw == Decimal("517716.37")
    assert hdfc_bank.quantity_raw == Decimal("69199542")


def test_the_real_file_drops_the_notional_swaps_printed_after_the_total(
    icici_real: HoldingsParseResult,
) -> None:
    """§6.3 rule 4. The sheet prints fifteen interest-rate swaps AT NOTIONAL
    VALUE below `Total Net Assets`. Notional is not market value, and read as
    holdings they would add Rs 105,000 lakh to the portfolio — inside the 2%
    guard on a Rs 8.7 lakh-crore book, so the reconciliation gate could not
    catch them. Position settles it, as it did for Nippon's stock futures.
    """
    names = [r.instrument_raw_name.lower() for r in icici_real.securities]
    assert not any("interest rate swap" in n for n in names)


def test_icici_reports_fractions_and_the_scale_is_read_from_the_total(
    icici: HoldingsParseResult, parsed: HoldingsParseResult,
) -> None:
    """`% to Nav` means 0.0596 at ICICI and 9.21 at HDFC. The header says neither.

    The total row does: HDFC's reads 99.99999999999996, ICICI's
    0.9999999999896085. Getting this wrong makes ICICI's disclosed weights sum
    to 1, failing §10's V1 and reporting a `weight_residual` of 99.
    """
    assert icici.pct_scale == Decimal(100)
    assert parsed.pct_scale == Decimal(1)

    scaled = sum(
        (s.pct_to_nav_raw or Decimal(0)) * icici.pct_scale for s in icici.securities
    )
    assert Decimal(95) <= scaled <= Decimal(105)


def test_icici_states_its_as_on_date_month_first(icici: HoldingsParseResult) -> None:
    """§7.5 rule 1. `Portfolio as on Jul 31,2026`, not `31-Jul-2026`.

    ICICI's workbooks are named for the scheme and carry no date at all, so
    rule 2's filename fallback cannot rescue a sheet whose date went unread —
    the file would be refused for want of a date it states plainly in row 3.
    """
    assert icici.as_of_date == AS_OF


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Portfolio as on 31-Mar-2026", date(2026, 3, 31)),
        ("Portfolio as on 31 March 2026", date(2026, 3, 31)),
        ("Portfolio as on 1-Oct-2026", date(2026, 10, 1)),
        ("Portfolio as on 31 DEC 2026", date(2026, 12, 31)),
        ("Portfolio as on Mar 31,2026", date(2026, 3, 31)),
        ("Portfolio as on March 31, 2026", date(2026, 3, 31)),
        ("Portfolio as on 2026-03-31", date(2026, 3, 31)),
    ],
)
def test_the_as_on_date_does_not_go_through_the_locale(
    text: str, expected: date
) -> None:
    """§7.5 rule 1, every spelling the AMCs use, month by name and numeric.

    `%b` and `%B` read through `LC_TIME`, so on a German locale every
    month-name row here would go unread and the file would fall back to a
    filename that ICICI does not date — a disclosure refused by geography.
    Invariant 10 wants the same archive to rebuild the same warehouse anywhere.

    March is the case that matters: German renders it `Mrz`, so a table-driven
    reader and a locale-driven one agree on `Jan` and `Dec` and part here.
    Asserted directly rather than by forcing a locale, which needs one that is
    installed — `de_DE.UTF-8` is not present on Windows CI.
    """
    assert _as_on_date(text) == expected


def test_a_locale_rendered_month_is_not_read_as_a_date() -> None:
    """The table is the whole vocabulary: `Mrz` is not a month AMCs publish,
    and reading it would mean the locale had leaked back in."""
    assert _as_on_date("Portfolio as on 31-Mrz-2026") is None


def test_icici_columns_are_found_by_header_not_position(
    icici: HoldingsParseResult,
) -> None:
    """V0-23. ICICI writes name before ISIN; HDFC writes ISIN before name.

    A positional parser reads one of the two silently wrong, which is exactly
    what AMFI's two NAV endpoints did.
    """
    hdfc_bank = next(s for s in icici.securities if s.isin_raw == "INE040A01034")
    assert hdfc_bank.instrument_raw_name == "HDFC Bank Ltd."
    assert hdfc_bank.market_value_raw == Decimal("517716.37")
    assert hdfc_bank.market_value_unit == "lakh"
    assert hdfc_bank.reported_sector == "Banks"


def test_icici_rows_inherit_the_section_their_subtotal_names(
    icici: HoldingsParseResult,
) -> None:
    """V1-07. The section heading is the only thing that says what a row is.

    ICICI's headings ARE its subtotal rows, so demoting them without carrying
    the label down would leave every holding with no section — and
    `instrument_class` would fall back to calling all of them equity.
    """
    equities = [s for s in icici.securities if s.isin_raw]
    assert equities
    assert {s.section for s in equities} == {
        "Equity & Equity Related Instruments (Note -1) :: "
        "Listed / Awaiting Listing On Stock Exchanges"
    }
    # The cash rows sit under no heading in this sheet, and are left to resolve
    # by their synthetic issuer rather than handed a borrowed one.
    treps = next(s for s in icici.securities if s.instrument_raw_name == "TREPS")
    assert treps.section is None


def test_the_router_tells_the_two_formats_apart(raw: RawFile) -> None:
    """§6.2. Routing on evidence, not on a hardcoded AMC->parser table."""
    icici_raw = RawFile("x", "S5:icici", "ICICI Prudential Multi-Asset Fund.xlsx",
                        (FIXTURES / "icici_holdings_sample.xlsx").read_bytes())
    assert route(raw).parser_id == "holdings.hdfc"
    assert route(icici_raw).parser_id == "holdings.icici"


# --- Nippon: the third format ------------------------------------------------


@pytest.fixture(scope="module")
def nippon() -> HoldingsParseResult:
    return NipponHoldingsParser().parse(
        RawFile("file-3", "S5:nippon", "NIMF-MONTHLY-PORTFOLIO-31-July-26.xls",
                (FIXTURES / "nippon_holdings_sample.xlsx").read_bytes())
    )


def test_nothing_after_the_grand_total_is_a_holding(
    nippon: HoldingsParseResult,
) -> None:
    """DECISIONS V1-15, and the one the reconciliation guard could not catch.

    Nippon prints a stock-future table below `GRAND TOTAL` **with the same
    column shape as the portfolio**, and further down a derivatives annexure
    whose `Margin maintained` column sits under `Market/Fair Value`. Read as
    holdings they are plausible in every respect except that the file has
    already said the portfolio is finished.

    On the real disclosure that is Rs 78.5 crore on a Rs 5,075 crore portfolio
    — **+0.155%**, comfortably inside the ±2% tolerance, so V1-08's guard sees
    nothing wrong. Position, not size, is what disqualifies these rows.
    """
    future = next(
        r for r in nippon.rows if "Astral" in r.instrument_raw_name
    )
    assert future.row_kind == "after_total"
    assert future.market_value_raw == Decimal("6540.75")
    assert future not in nippon.securities

    error = reconciliation_error(nippon)
    assert error is not None
    assert abs(error) < Decimal("0.000001")


def test_counting_the_post_total_table_stays_inside_the_tolerance(
    nippon: HoldingsParseResult,
) -> None:
    """Why the fix had to be positional rather than a tighter tolerance.

    Adding the trailing rows back moves the total by well under 2%, so no
    threshold that still tolerates ordinary rounding would have caught them.
    Tightening the guard instead would have produced false refusals on files
    that are merely rounded.
    """
    assert nippon.stated_total is not None
    strays = sum(
        (r.market_value_raw or Decimal(0)
         for r in nippon.rows if r.row_kind == "after_total"),
        Decimal(0),
    )
    assert strays > 0
    inflated = (strays / nippon.stated_total) * 100
    assert inflated < TOTAL_TOLERANCE_PCT, (
        f"the stray rows move the total by {inflated}%, which the ±"
        f"{TOTAL_TOLERANCE_PCT}% guard would have caught after all"
    )


def test_hdfcs_summary_and_nav_history_survive_the_post_total_rule(
    parsed: HoldingsParseResult,
) -> None:
    """The ordering inside `_stage_row`, asserted rather than trusted.

    HDFC prints its industry summary and its NAV history *below* its own Grand
    Total. Applying the post-total rule before the summary branch swallows both
    — and with them `stated_navs`, which is the disclosure's own NAV and the
    cheapest independent witness in the pipeline. This is the regression that
    fix caused once already.
    """
    assert parsed.stated_navs.get("Direct Plan - Growth Option") == Decimal("2267.177")
    assert any(r.row_kind == "summary" for r in parsed.rows)


def test_one_workbook_of_many_schemes_needs_the_sheet_named() -> None:
    """V1-15. Nippon ships 108 schemes as 108 sheets of a single workbook.

    Reading every sheet merges every portfolio into one result that reconciles
    against whichever total came last. On the real file that reads
    **+222,869.8%** and is refused — loudly, which is the point — but the
    refusal is not the feature. Naming the sheet is.
    """
    def sheet(name: str, mv: float) -> list[list[object]]:
        return [
            [name, "Scheme " + name, "", "", "", "", "", ""],
            ["", "Monthly Portfolio Statement as on July 31,2026"],
            [],
            ["", "ISIN", "Name of the Instrument", "Industry / Rating",
             "Quantity", "Market/Fair Value\n( Rs. in Lacs)", "% to NAV", ""],
            ["X1", "INE040A01034", "HDFC Bank Limited", "Banks", 10, mv, 1.0, ""],
            ["", "", "GRAND TOTAL", "", "", mv, 1, ""],
        ]

    import io as _io

    import openpyxl as _openpyxl

    book = _openpyxl.Workbook()
    book.remove(book.active)
    for name, mv in (("AA", 100.0), ("BB", 250.0)):
        ws = book.create_sheet(name)
        for row in sheet(name, mv):
            ws.append(row)
    buffer = _io.BytesIO()
    book.save(buffer)
    raw = RawFile("x", "S5:nippon", "NIMF-MONTHLY-PORTFOLIO-31-July-26.xls",
                  buffer.getvalue())

    with pytest.raises(ParseFailed, match="stated total"):
        NipponHoldingsParser().parse(raw)

    for name, stated in (("AA", "100"), ("BB", "250")):
        one = NipponHoldingsParser().parse(raw, sheet=name)
        assert len(one.securities) == 1
        assert one.stated_total == Decimal(stated)
        # Not `or Decimal(1)`: an exact reconciliation IS zero, and zero is
        # falsy. `None` (no total stated) and 0.0 (a perfect match) are
        # opposite outcomes and must not collapse into one branch.
        error = reconciliation_error(one)
        assert error is not None and abs(error) < Decimal("0.000001")

    with pytest.raises(ParseFailed, match="no sheet 'ZZ'"):
        NipponHoldingsParser().parse(raw, sheet="ZZ")


def test_nippon_reports_fractions_like_icici_not_percentages_like_hdfc(
    nippon: HoldingsParseResult,
) -> None:
    """The rule written for ICICI in V1-10, doing its job unchanged on a third AMC.

    Nippon writes `0.029` for 2.9% and its GRAND TOTAL row reads `1`. Nothing
    in the header distinguishes that from HDFC's `9.21`.
    """
    assert nippon.pct_scale == Decimal(100)
    scaled = sum(
        (s.pct_to_nav_raw or Decimal(0)) * nippon.pct_scale
        for s in nippon.securities
    )
    assert Decimal(95) <= scaled <= Decimal(105)


def test_nippon_states_a_month_first_date_and_a_unit_split_across_lines(
    nippon: HoldingsParseResult,
) -> None:
    """Two more rules that generalised without change.

    The as-on line is `July 31,2026` — month-first, learned for ICICI in V1-10.
    The units header is `Market/Fair Value\\n( Rs. in Lacs)`, with an embedded
    newline and an unclosed parenthesis; §7.2's reader handles it, and getting
    it wrong is the 100x path.
    """
    assert nippon.as_of_date == AS_OF
    assert {s.market_value_unit for s in nippon.securities} == {"lakh"}


def test_nippons_extra_code_column_does_not_shift_the_mapping(
    nippon: HoldingsParseResult,
) -> None:
    """V0-23 again. Nippon carries a leading internal-code column no one else has.

    Its header cell is blank, so nothing maps to it — but a positional parser
    would read every column one to the left and silently mistake the industry
    for the quantity.
    """
    federal = next(
        s for s in nippon.securities if s.isin_raw == "INE171A01029"
    )
    assert federal.instrument_raw_name == "The Federal Bank Limited"
    assert federal.reported_sector == "Banks"
    assert federal.quantity_raw == Decimal("41000000")
    assert federal.market_value_raw == Decimal("147128.50")


def test_nippons_cash_rows_are_positions_not_headings(
    nippon: HoldingsParseResult,
) -> None:
    """V1-04's guard, still load-bearing on the third format.

    `Triparty Repo`, `Cash Margin - Derivatives` and `Net Current Assets` carry
    a market value and no ISIN and no quantity — the exact shape
    `_demote_subtotals` considers. None is a subtotal of the rows beneath it,
    so all three survive as the positions they are.
    """
    names = {s.instrument_raw_name for s in nippon.securities}
    assert {"Triparty Repo", "Cash Margin - Derivatives", "Net Current Assets"} <= names
    assert not [r for r in nippon.rows if r.row_kind == "subtotal"]


def test_a_file_that_states_no_total_reports_the_check_as_absent() -> None:
    """An absent check is reported as absent, never as a pass."""
    empty = HoldingsParseResult()
    assert reconciliation_error(empty) is None


# --- instrument class: debt is not equity ------------------------------------

#: Indian ISIN security codes (characters 8-9) that are unambiguously debt:
#: debentures, bonds, commercial paper, certificates of deposit. The test's
#: oracle only -- the reader classes by the sheet's own headings.
DEBT_CODES = {"07", "08", "14", "16", "D6"}


def _debt_rows(result: HoldingsParseResult) -> list[StagedHolding]:
    return [
        s for s in result.securities
        if (isin := s.isin_raw or "").startswith("INE") and isin[7:9] in DEBT_CODES
    ]


@pytest.mark.parametrize("fixture", ["ppfas", "icici_real"])
def test_every_bond_cp_and_cd_in_a_real_file_classes_as_debt(
    fixture: str, request: pytest.FixtureRequest
) -> None:
    """18.0% of equity-classed weight was bonds, CPs and CDs: `Certificate of
    Deposit`, `Commercial Paper` and `Non Convertible Debentures` matched no
    heading, and an unmatched heading falls back to equity."""
    rows = _debt_rows(request.getfixturevalue(fixture))
    assert rows, "the fixture no longer exercises the path"
    wrong = {
        (s.isin_raw, s.section) for s in rows
        if instrument_class(s.section, "") != "debt"
    }
    assert not wrong


def test_a_listing_status_qualifies_the_heading_above_it() -> None:
    """Kotak's sequence, verbatim. Kept alone, the status's "listed" read as
    equity for every bond, CP and CD Kotak holds."""
    cp = nest(
        "Money Market Instruments", "Commercial Paper (CP)/Certificate of Deposits (CD)"
    )
    unlisted = nest(cp, "Privately placed / Unlisted")
    listed = nest(unlisted, "Listed/Awaiting listing on Stock Exchange")

    assert listed == (
        "Commercial Paper (CP)/Certificate of Deposits (CD) :: "
        "Listed/Awaiting listing on Stock Exchange"
    ), "a sibling status replaces the previous one, not stacks on it"
    assert instrument_class(unlisted, "") == instrument_class(listed, "") == "debt"
    assert instrument_class(
        nest("Equity & Equity related", "Listed/Awaiting listing on Stock Exchange"), ""
    ) == "equity"


def test_kotak_overseas_fund_units_are_units_not_equity(
    kotak: HoldingsParseResult,
) -> None:
    """The same nesting, in the real Kotak Pioneer sheet: an overseas fund held
    as units sits under `Listed/Awaiting listing`, and read alone that made it
    equity rather than a fund to look through."""
    unit = next(s for s in kotak.securities if s.isin_raw == "IE00B53SZB19")
    assert instrument_class(unit.section, "") == "mfunit"


@pytest.mark.parametrize(
    ("section", "issuer", "expected"),
    [
        # Trailing cash under the last heading seen: its own identity wins.
        ("Units of an Alternative Investment Fund (AIF)", "__TREPS__", "cash"),
        ("Treasury Bills", "__RECV__", "cash"),
        # A derivative heading still wins: `Repo Future` resolves to __TREPS__.
        ("Stock Futures", "__TREPS__", "derivative"),
        # And a real issuer is still classed by its heading.
        ("Units of an Alternative Investment Fund (AIF)", "DISC:INE0ABC", "other"),
        ("Infrastructure Investment Trusts :: Listed/Awaiting listing", "X", "other"),
    ],
)
def test_cash_is_cash_under_a_borrowed_heading(
    section: str, issuer: str, expected: str
) -> None:
    """Adding debt headings made a stale one decisive: ICICI's TREPS, printed
    after its AIF block, read as `other`, and Kotak's after `Treasury Bills` as
    debt. Before, the stale heading matched nothing and the issuer answered."""
    assert instrument_class(section, issuer) == expected


def test_a_covered_call_is_a_derivative_under_the_equity_heading(
    icici_real: HoldingsParseResult,
) -> None:
    """ICICI prints its written calls inside the equity block, each named
    `Larsen & Toubro Ltd. (Covered call) $$` at a negative value. The name
    already resolved to `__DERIV__`, but the heading classed them equity, so
    V8 warned on the fund and every equity-scoped figure counted a short
    option as stock."""
    from src.m0_data.normalise.instrument_class import class_from_section
    from src.m0_data.resolve.synthetic import match_synthetic

    calls = [
        s for s in icici_real.securities
        if "covered call" in s.instrument_raw_name.lower()
    ]
    assert len(calls) == 43
    assert {class_from_section(s.section) for s in calls} == {"equity"}
    assert {
        instrument_class(s.section, str(match_synthetic(
            s.instrument_raw_name, class_from_section(s.section), s.isin_raw
        )))
        for s in calls
    } == {"derivative"}
