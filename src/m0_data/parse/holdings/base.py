"""Portfolio disclosure workbooks -> staged rows. MODULE_0.md §6.

SEBI mandates the disclosure columns, so every AMC publishes the same facts —
instrument name, ISIN, industry or rating, quantity, market value, % to NAV —
with different header wording, sheet names, units and section labels. That is
configuration, not five different parsers, so one table reader is driven by a
per-AMC `HoldingsFormat`.

**Columns are located by header text, never by position.** V0-23 is why: AMFI
publishes the same eight columns in two different orders across two endpoints,
and a positional parser read one of them silently wrong. The same risk applies
across five AMCs with far less excuse.

§6.3 rule 1 is observed strictly — nothing here converts a unit, cleans a name
or resolves an entity. The unit is *read* from the header and carried as a
label; `normalise/units.py` applies it.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import openpyxl

from src.m0_data.normalise.numbers import CoercionError, to_decimal
from src.m0_data.normalise.units import AmbiguousUnitError, unit_from_header
from src.m0_data.parse.base import (
    HoldingsParseResult,
    ParseFailed,
    ParseWarning,
    RawFile,
    StagedHolding,
)
from src.m0_data.resolve.isin import is_valid_isin

#: §6.4, extended. The section labels that open a block of securities.
#: §6.4, extended. The labels that open a block of securities. HDFC writes
#: each of these twice — once as a heading in the ISIN column, once on the
#: row that carries the numbers — so recognising the heading is what stops
#: the block being counted alongside its own contents.
SECTION_HEADERS = re.compile(
    r"^\s*\(?[a-z]?\)?\s*(equity|debt|money\s*market|derivatives?|others?|listed|unlisted|awaiting|government|treasury|mutual\s*fund|reits?|invits?|treps|repo|options?|futures?|net\s+current\s+assets?|cash|margin|units?\s+issued)\b",
    re.I,
)

#: Block titles that open a non-holdings section. HDFC repeats a section label
#: in the ISIN column immediately above the row carrying its numbers, so these
#: are labels for data that lives elsewhere — counting them would double the
#: block they introduce.
BLOCK_TITLE = re.compile(
    r"(classification\s+by|history\s*[-:]|riskometer|holdings?\s*$|:\s*$)", re.I
)
TOTAL_ROW = re.compile(r"^\s*(sub\s*)?total|grand\s+total|net\s+asset", re.I)
NOTE_ROW = re.compile(r"^\s*(notes?\s*:|\d+\)|\*|#|disclaimer)", re.I)

#: `Portfolio as on 31-Jul-2026`. §7.5 rule 1 — an explicit date cell beats the
#: filename, because a file can be re-uploaded under a new name.
AS_ON_RE = re.compile(
    r"as\s+on\s+(\d{1,2}[-/\s][A-Za-z]{3,9}[-/\s]\d{4}|\d{4}-\d{2}-\d{2})", re.I
)
_DATE_FORMATS = ("%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y")

#: A NAV-history line in the notes: `Direct Plan - Growth Option | 2267.177`.
#: Not a holding, but an independent witness that we mapped the file to the
#: right scheme — see `stated_navs`.
NAV_LABEL_RE = re.compile(r"(growth|idcw|dividend|payout|reinvest)", re.I)


@dataclass(frozen=True)
class HoldingsFormat:
    """Per-AMC configuration. The only thing that differs between AMCs.

    Each entry is a list of header substrings tried in order, so an AMC that
    writes `Name of Instrument` and another that writes `Instrument Name` need
    no code between them.
    """

    amc_id: str
    parser_id: str
    version: str
    #: Header substrings -> canonical field. Order matters within each list.
    columns: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("isin", ("isin",)),
        ("name", ("name of the instrument", "name of instrument", "instrument",
                  "security name", "particulars")),
        ("sector", ("industry", "sector", "rating")),
        ("quantity", ("quantity", "qty", "units", "no. of shares")),
        ("market_value", ("market/ fair value", "market value", "fair value",
                          "amount", "value")),
        ("pct", ("% to nav", "% of nav", "% to net asset", "percentage to nav")),
        ("coupon", ("coupon", "yield")),
    )
    #: Sheets to skip entirely. Derivative annexures are prose, not tables.
    skip_sheets: tuple[str, ...] = ("derivative",)


#: The fields a sheet must expose before it is a holdings table at all.
REQUIRED = ("name", "market_value")

#: How far the parsed securities may sit from the file's own stated total.
#: Generous, because a disclosure's total row is itself rounded and some AMCs
#: exclude a line or two from it — but nowhere near the 2x a counted subtotal
#: produces, which is what this exists to catch.
TOTAL_TOLERANCE_PCT = Decimal("2")

#: The row that states what the portfolio adds up to.
GRAND_TOTAL = re.compile(r"^\s*(grand\s+total|total\s+net\s+asset)", re.I)


def parse_holdings(f: RawFile, fmt: HoldingsFormat) -> HoldingsParseResult:
    """Read every table-shaped sheet in the workbook. §6.1.

    Raises rather than returning a partial result (§6.3 rule 3): a disclosure
    that half-parsed is a portfolio that is half-there, and a look-through
    built on it is wrong in a way that still sums to 100.
    """
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(f.content), read_only=True)
    except Exception as exc:
        raise ParseFailed(f"{f.filename}: not a readable workbook ({exc})") from exc

    result = HoldingsParseResult()
    headers_seen: list[str] = []

    for sheet_name in workbook.sheetnames:
        if any(skip in sheet_name.lower() for skip in fmt.skip_sheets):
            continue
        rows = [list(r) for r in workbook[sheet_name].iter_rows(values_only=True)]
        _read_sheet(rows, sheet_name, fmt, result, headers_seen)

    if not result.securities:
        raise ParseFailed(
            f"{f.filename}: no security rows found in {workbook.sheetnames}"
        )

    error = reconciliation_error(result)
    if error is not None and abs(error) > TOTAL_TOLERANCE_PCT:
        # §6.3 rule 3. Loading a portfolio that disagrees with the file's own
        # total by this much means we counted subtotals as holdings, or missed
        # a block entirely — and the weights would still normalise to 100, so
        # nothing downstream could tell. Refuse rather than publish it.
        raise ParseFailed(
            f"{f.filename}: parsed securities are {error:+.1f}% from the file's "
            f"own stated total ({result.stated_total}); the sheet was misread"
        )

    result.headers_seen = tuple(headers_seen)
    if result.as_of_date is None:
        result.as_of_date = _date_from_filename(f.filename)
    if result.as_of_date is None:
        # §7.5: "Fail. Do not default to last month-end." A wrong as-of date
        # files a portfolio against the wrong month, and every drift figure
        # computed from it is nonsense that looks plausible.
        raise ParseFailed(f"{f.filename}: no as-of date in the sheet or the filename")
    return result


def _read_sheet(
    rows: list[list[object]],
    sheet_name: str,
    fmt: HoldingsFormat,
    result: HoldingsParseResult,
    headers_seen: list[str],
) -> None:
    columns: dict[str, int] | None = None
    unit: str | None = None
    section: str | None = None

    for index, raw_row in enumerate(rows, start=1):
        cells = ["" if v is None else str(v).strip() for v in raw_row]
        text = " ".join(c for c in cells if c)
        if not text:
            continue

        if result.as_of_date is None:
            found = _as_on_date(text)
            if found:
                result.as_of_date = found
        if result.scheme_raw_name is None and index <= 3 and cells:
            result.scheme_raw_name = next((c for c in cells if c), None)

        if columns is None:
            mapped = _header_map(cells, fmt)
            if mapped:
                columns = mapped
                headers_seen.extend(c for c in cells if c)
                unit = _unit_for(cells, columns, result, index)
            continue

        section = _stage_row(
            cells, columns, unit or "absolute", sheet_name, index, result, section
        )


def _stage_row(
    cells: list[str],
    columns: dict[str, int],
    unit: str,
    sheet_name: str,
    index: int,
    result: HoldingsParseResult,
    section: str | None,
) -> str | None:
    """Stage one row and return the section in force after it."""
    name = _at(cells, columns, "name")
    isin = _at(cells, columns, "isin")

    # Coerce leniently first, classify, THEN decide how much the failures
    # matter. The tail of a disclosure is prose in the same columns the table
    # used — `Top Ten Holdings` lands in the quantity cell, a bare `@` in the
    # percentage cell — so a strict read of every cell would raise on rows that
    # are not holdings at all. Strictness belongs where the number is
    # load-bearing, which is a security's market value.
    quantity, quantity_bad = _soft(_at(cells, columns, "quantity"))
    market_value, mv_bad = _soft(_at(cells, columns, "market_value"))
    pct, pct_bad = _soft(_at(cells, columns, "pct"))

    kind = classify_row(name, isin, quantity, market_value, pct)
    if kind == "unknown" and _is_summary_row(cells, columns):
        # Not unknown — a row of the SECOND table. See `_is_summary_row`.
        kind = "summary"
        _capture_stated_nav(cells, result)

    if kind == "security":
        if mv_bad:
            # §6.3 rule 3. A security whose market value will not parse cannot
            # be staged as zero — that understates the portfolio silently and
            # the weights still normalise to 100.
            raise ParseFailed(
                f"row {index}: security {name[:40]!r} has an unreadable "
                f"market value: {_at(cells, columns, 'market_value')!r}"
            )
        for bad, field_name in ((quantity_bad, "quantity"), (pct_bad, "pct_to_nav")):
            if bad:
                result.warnings.append(
                    ParseWarning(
                        "CELL_UNPARSEABLE", f"{field_name} on {name[:40]!r}", index
                    )
                )
    if kind == "total" and market_value is not None and GRAND_TOTAL.match(
        name or isin
    ):
        # The file's own answer. Last one wins: a workbook with several sheets
        # states a total per sheet, and the portfolio-level one comes last.
        result.stated_total = market_value
    if kind == "blank":
        return section
    if kind == "section_header":
        section = (name or isin).strip() or section
    if kind == "unknown":
        # §6.4: staged and surfaced, never discarded. An unrecognised row in a
        # disclosure is a holding we may be missing.
        result.warnings.append(
            ParseWarning("ROW_UNCLASSIFIED", f"{name[:60]!r}", index)
        )

    result.rows.append(
        StagedHolding(
            row_number=index,
            row_kind=kind,
            instrument_raw_name=name,
            isin_raw=isin or None,
            quantity_raw=quantity,
            market_value_raw=market_value,
            market_value_unit=unit,
            pct_to_nav_raw=pct,
            reported_sector=_at(cells, columns, "sector") or None,
            coupon_or_rating=_at(cells, columns, "coupon") or None,
            sheet_name=sheet_name,
            section=section,
        )
    )
    return section


def reconciliation_error(result: HoldingsParseResult) -> Decimal | None:
    """How far the parsed securities sit from the file's own stated total, in %.

    `None` when the file states no total — some do not, and an absent check is
    reported as absent rather than silently passing.

    This is the general defence that no list of section labels can be. HDFC
    nests its sections one level deep and puts the numbers on a separate row;
    ICICI nests four levels and puts a subtotal on the section row itself. A
    parser tuned to one over-counts the other by 2.9x. Neither is detectable
    from the rows alone — but both files say what they add up to.
    """
    if result.stated_total is None or not result.stated_total:
        return None
    total = sum(
        (r.market_value_raw or Decimal(0) for r in result.securities), Decimal(0)
    )
    return (total - result.stated_total) / abs(result.stated_total) * 100


def classify_row(
    name: str,
    isin: str,
    quantity: Decimal | None,
    market_value: Decimal | None,
    pct: Decimal | None,
) -> str:
    """§6.4, with the heuristic that misclassifies a real file corrected.

    §6.4 ends with `if isin or qty or mv: return "security"`. On HDFC's sheet
    that is wrong in a way that doubles the portfolio. After the holdings comes
    a **portfolio-classification-by-industry summary**, and its rows are offset
    from the securities table: the sector name lands in the ISIN column and the
    percentage lands in the name column.

        [122]  ISIN='Banks'   Name='28.94'   Quantity=None   MarketValue=None

    `isin` is truthy, so §6.4 calls all 32 of those rows securities and every
    sector is counted twice — once as its constituents and once as itself.

    The fix is to **validate** rather than test for truthiness. A row is a
    security when it carries a real ISIN, or a name that is not a number
    alongside an actual market value. `Banks` fails the check digit and `28.94`
    is not an instrument name, so the summary block classifies as `unknown` and
    is surfaced rather than counted.
    """
    if not name.strip() and not isin.strip():
        return "blank"
    if NOTE_ROW.match(name) or NOTE_ROW.match(isin):
        return "note"
    if TOTAL_ROW.match(name) or TOTAL_ROW.match(isin):
        return "total"
    if is_valid_isin(isin):
        return "security"
    # §6.4's `and mv is None` guard, and it is load-bearing. HDFC writes the
    # section label twice: once as a bare heading, and once on the row that
    # carries the numbers. `TREPS - Tri-party Repo` is a heading on one row and
    # a Rs 343 crore position on the next. Testing the name alone would classify
    # both as headings and drop the fund's entire cash position — 3.1% of the
    # portfolio, silently, with the remaining weights renormalising to 100 so
    # nothing downstream could tell.
    #
    # A row carrying a market value is a position, whatever its name looks like.
    if market_value is None:
        if SECTION_HEADERS.match(name) or SECTION_HEADERS.match(isin):
            return "section_header"
        if BLOCK_TITLE.search(name) or BLOCK_TITLE.search(isin):
            return "section_header"
    if name and not _looks_numeric(name) and market_value is not None:
        return "security"
    if not name.strip() or _looks_numeric(name):
        return "unknown"
    if quantity is None and market_value is None and pct is None:
        return "section_header"
    return "unknown"


def _is_summary_row(cells: list[str], columns: dict[str, int]) -> bool:
    """A row of the trailing summary table, not an unrecognised holding.

    Everything after the securities in HDFC's sheet is a **second table, offset
    from the first**: a label in the ISIN column and a number where the
    instrument name belongs. That covers the portfolio-classification-by-
    industry block, the NAV history and the hedged-exposure lines.

        [122]  ISIN='Banks'                        Name='28.94'
        [161]  ISIN='Direct Plan - Growth Option'  Name='2267.177'

    Calling these `unknown` was technically safe — nothing counted them — but it
    produced 45 warnings on a clean file, and a warning list that is always long
    is a warning list nobody reads. Naming them `summary` says what they are and
    leaves `unknown` meaning what it should: a row we genuinely did not expect.

    The shape is precise: no market value in the table's own column, a text
    label first, and a number after it.
    """
    if columns.get("market_value") is not None:
        mv_index = columns["market_value"]
        if mv_index < len(cells) and cells[mv_index].strip():
            return False
    populated = [(i, c) for i, c in enumerate(cells) if c.strip()]
    if len(populated) < 2:
        return False
    first_text = not _looks_numeric(populated[0][1])
    has_number = any(_looks_numeric(c) is not False for _, c in populated[1:])
    return first_text and has_number


def _soft(text: str) -> tuple[Decimal | None, bool]:
    """Coerce, reporting failure rather than raising. Returns (value, failed)."""
    try:
        return to_decimal(text), False
    except CoercionError:
        return None, True


def _looks_numeric(text: str) -> bool:
    return bool(re.fullmatch(r"[\d,]+\.?\d*", text.strip()))


def _capture_stated_nav(cells: list[str], result: HoldingsParseResult) -> None:
    """Pull `Direct Plan - Growth Option | 2267.177` out of the notes.

    The disclosure states its own NAV per unit. We have that scheme's NAV from
    AMFI, so the two cross-check — and a file mapped to the wrong scheme, or a
    units error, breaks the comparison. It is the cheapest independent witness
    in the whole pipeline and it is sitting in the notes block.
    """
    populated = [c for c in cells if c.strip()]
    if len(populated) < 2 or not NAV_LABEL_RE.search(populated[0]):
        return
    # The FIRST numeric after the label, because the NAV-history block lists
    # the as-of month first and the prior month second:
    #     NAVs per unit (Rs.) | July 31, 2026 | June 30, 2026
    #     Direct Plan - Growth Option | 2267.177 | 2201.717
    # Taking any numeric would silently capture last month's NAV, which is
    # close enough to look right and wrong enough to break the cross-check.
    for cell in populated[1:]:
        value, failed = _soft(cell)
        if not failed and value is not None and value > 0:
            result.stated_navs.setdefault(populated[0].strip(), value)
            return


def _header_map(cells: list[str], fmt: HoldingsFormat) -> dict[str, int] | None:
    """Locate the header row and map its columns. Never positional — V0-23."""
    lowered = [c.lower() for c in cells]
    found: dict[str, int] = {}
    for field_name, needles in fmt.columns:
        for needle in needles:
            match = next(
                (i for i, text in enumerate(lowered) if needle in text), None
            )
            if match is not None and field_name not in found:
                found[field_name] = match
                break
    return found if all(k in found for k in REQUIRED) else None


def _unit_for(
    cells: list[str],
    columns: dict[str, int],
    result: HoldingsParseResult,
    index: int,
) -> str:
    """§7.2. The 100x path — read it, or raise."""
    mv_index = columns["market_value"]
    header = cells[mv_index] if mv_index < len(cells) else ""
    try:
        return unit_from_header(header)
    except AmbiguousUnitError as exc:
        raise ParseFailed(f"row {index}: {exc}") from exc


def _at(cells: list[str], columns: dict[str, int], field_name: str) -> str:
    index = columns.get(field_name)
    return cells[index] if index is not None and index < len(cells) else ""


def _as_on_date(text: str) -> date | None:
    match = AS_ON_RE.search(text)
    return _parse_date(match.group(1)) if match else None


def _date_from_filename(filename: str) -> date | None:
    """§7.5 rule 2. `Monthly HDFC Flexi Cap Fund - 31 July 2026.xlsx`."""
    match = re.search(r"(\d{1,2}[-\s][A-Za-z]{3,9}[-\s]\d{4})", filename)
    return _parse_date(match.group(1)) if match else None


def _parse_date(text: str) -> date | None:
    cleaned = text.replace("/", "-").strip()
    for fmt in _DATE_FORMATS:
        for candidate in (cleaned, cleaned.replace("-", " ")):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None
