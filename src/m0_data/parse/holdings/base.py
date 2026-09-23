"""Portfolio disclosure workbooks -> staged rows. MODULE_0.md §6.

SEBI mandates the disclosure columns, so every AMC publishes the same facts with
different header wording, sheet names, units and section labels. That is
configuration, not five parsers: one table reader driven by a per-AMC
`HoldingsFormat`.

Columns are located by HEADER TEXT, never by position (V0-23).

§6.3 rule 1 strictly: nothing here converts a unit, cleans a name or resolves an
entity. The unit is read from the header and carried as a label.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal

import openpyxl

from src.m0_data.normalise.numbers import (
    CoercionError,
    month_number,
    to_decimal,
)
from src.m0_data.normalise.units import AmbiguousUnitError, unit_from_header
from src.m0_data.parse.base import (
    HoldingsParseResult,
    ParseFailed,
    ParseWarning,
    RawFile,
    StagedHolding,
)
from src.m0_data.resolve.isin import is_valid_isin

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
    r"as\s+on\s+("
    r"\d{1,2}[-/\s][A-Za-z]{3,9}[-/\s]\d{4}"      # HDFC: 31-Jul-2026
    r"|[A-Za-z]{3,9}\s+\d{1,2}\s*,\s*\d{4}"       # ICICI: Jul 31,2026
    r"|\d{4}-\d{2}-\d{2}"
    r")",
    re.I,
)

#: `31-Jul-2026`, `31 July 2026`. The month is captured as letters and looked
#: up in `MONTH_NUMBERS`, never handed to `%b`/`%B` — those render through
#: `LC_TIME`, so on a machine with a German locale this parser would refuse a
#: disclosure it reads here, and the archive would rebuild by geography
#: (invariant 10).
_DAY_FIRST = re.compile(
    r"^(?P<day>\d{1,2})[-\s]+(?P<month>[A-Za-z]+)[-\s]+(?P<year>\d{4})$"
)

#: ICICI's month-first form, `Jul 31,2026`. §7.5 would otherwise fall through
#: to the filename, and ICICI's members are named for the scheme with no date
#: in them at all — so the file would be refused for want of an as-of date it
#: states plainly in row 3.
_MONTH_FIRST = re.compile(
    r"^(?P<month>[A-Za-z]+)[-\s]+(?P<day>\d{1,2}),(?P<year>\d{4})$"
)

#: The all-numeric forms, which name no month and so carry no `LC_TIME`
#: dependence: `%Y`, `%m` and `%d` are digits in every locale.
#:
#: The slash form cannot actually fire — `_parse_date` rewrites `/` to `-`
#: before matching, and `AS_ON_RE` requires letters where it puts a month
#: anyway. It is kept because removing it is a behaviour question about which
#: spellings §7.5 accepts, not part of the locale fix.
_NUMERIC_FORMATS = ("%Y-%m-%d", "%d/%m/%Y")

#: A NAV-history line in the notes: `Direct Plan - Growth Option | 2267.177`.
#: Not a holding, but an independent witness that we mapped the file to the
#: right scheme — see `stated_navs`.
NAV_LABEL_RE = re.compile(r"(growth|idcw|dividend|payout|reinvest)", re.I)


@dataclass(frozen=True)
class HoldingsFormat:
    """Per-AMC configuration. The only thing that differs between AMCs.

    Each entry is a list of header substrings tried in order, so `Name of
    Instrument` and `Instrument Name` need no code between them.
    """

    amc_id: str
    parser_id: str
    version: str
    #: Header substrings -> canonical field. Order matters within each list.
    columns: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("isin", ("isin",)),
        # `instrument` is tried LAST because it is the most generic needle and
        # ICICI's sheet also carries `Yield of the instrument`. It resolved
        # correctly there only because the real name column happens to sit at a
        # lower index; matching the specific spellings first finds the column
        # by intent rather than by that accident.
        ("name", ("name of the instrument", "name of instrument",
                  "company/issuer", "security name", "particulars",
                  "instrument")),
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

#: A `% to NAV` column that totals within this of 1 is written as a fraction.
PCT_FRACTION_MAX = Decimal("2")
#: One that totals within this range is already a percentage.
PCT_PERCENT_MIN = Decimal(50)
PCT_PERCENT_MAX = Decimal(200)

#: How far down a sheet the scheme's own name might be. Four AMCs put it on
#: rows 1-3 and the column titles arrive by row 5; six is slack.
HEADER_SCAN_ROWS = 6

#: The shared reader's own version, distinct from any AMC's `HoldingsFormat`.
#: Every rule that has ever mattered lives here rather than in an AMC module, so
#: "which parser read this file" is answered by this number far more than by
#: `parser_id`. It is in `holding_disclosure`'s skip key, so fixing a rule
#: re-parses the disclosures the old rule got wrong (V1-36).
#:
#: Bump on any change that would read an already-loaded file differently.
#: 5: a listing-status heading nests under the instrument heading above it.
READER_VERSION = "5"

#: A heading that says how a security trades, not what it is. Kotak prints
#: `Debentures and Bonds`, then `Listed/Awaiting listing on Stock Exchange`
#: beneath it; kept as the whole section, the second heading's "listed" read as
#: equity and every Kotak bond, CP and CD was classed equity with it.
LISTING_STATUS = re.compile(r"^\s*(listed|unlisted|privately placed|awaiting)", re.I)

#: Between a heading and the listing status nested under it.
NEST = " :: "


def nest(section: str | None, label: str) -> str:
    """The section a row sits in once `label` is read.

    A listing status qualifies the instrument heading above it rather than
    replacing it, so the type survives for `instrument_class`; a sibling status
    replaces the previous one. Any other heading replaces the section outright.
    """
    if section and LISTING_STATUS.match(label):
        return f"{section.split(NEST)[0]}{NEST}{label}"
    return label

#: How close a candidate subtotal must sit to the sum of the rows beneath it
#: before it is read as their total rather than as a position of its own. One
#: basis point: wide enough for the float noise a spreadsheet cell carries
#: (`6180420.349999999` for a figure printed as `6180420.35`), far too tight for
#: a real holding to land on by coincidence.
SUBTOTAL_TOLERANCE = Decimal("0.0001")


def parse_holdings(
    f: RawFile, fmt: HoldingsFormat, sheet: str | None = None
) -> HoldingsParseResult:
    """Read every table-shaped sheet in the workbook, or just the one named. §6.1.

    `sheet` exists because Nippon publishes one workbook holding 108 schemes,
    one per sheet; reading all of them merges 108 portfolios into one.

    Raises rather than returning a partial result (§6.3 rule 3): a disclosure
    that half-parsed is a portfolio that is half-there.
    """
    try:
        # `data_only=True` reads Excel's cached value, not the formula text.
        # Without it a formula cell returns `'=342762.56+E193-105.07'`, the
        # market value becomes None, and `classify_row` calls the row a section
        # heading — it stops being a holding without being dropped loudly.
        # PPFAS found it: seven formula cells, Rs 303 Cr, landing 0.2044% under
        # the file's own total and INSIDE the 2% guard (V1-15).
        workbook = openpyxl.load_workbook(
            io.BytesIO(f.content), read_only=True, data_only=True
        )
    except Exception as exc:
        raise ParseFailed(f"{f.filename}: not a readable workbook ({exc})") from exc

    result = HoldingsParseResult()
    headers_seen: list[str] = []

    if sheet is not None and sheet not in workbook.sheetnames:
        raise ParseFailed(
            f"{f.filename}: no sheet {sheet!r}; has {workbook.sheetnames[:12]}"
            f"{'...' if len(workbook.sheetnames) > 12 else ''}"
        )

    for sheet_name in workbook.sheetnames:
        if sheet is not None and sheet_name != sheet:
            continue
        if sheet is None and any(
            skip in sheet_name.lower() for skip in fmt.skip_sheets
        ):
            continue
        rows = [list(r) for r in workbook[sheet_name].iter_rows(values_only=True)]
        _read_sheet(rows, sheet_name, fmt, result, headers_seen)

    if not result.securities:
        raise ParseFailed(
            f"{f.filename}: no security rows found in {workbook.sheetnames}"
        )

    result.pct_scale = detect_pct_scale(result)

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
    # Set once the sheet states what the portfolio adds up to. See _stage_row.
    after_total = False
    # Staged per sheet rather than straight onto the result, because whether a
    # row is a holding or the total of the rows beneath it cannot be known
    # until the rows beneath it have been read. See _demote_subtotals.
    staged: list[StagedHolding] = []

    for index, raw_row in enumerate(rows, start=1):
        cells = ["" if v is None else str(v).strip() for v in raw_row]
        text = " ".join(c for c in cells if c)
        if not text:
            continue

        if result.as_of_date is None:
            found = _as_on_date(text)
            if found:
                result.as_of_date = found
        if index <= HEADER_SCAN_ROWS:
            result.header_candidates.extend(c for c in cells if c)
        if result.scheme_raw_name is None and index <= 3 and cells:
            result.scheme_raw_name = next((c for c in cells if c), None)

        if columns is None:
            mapped = _header_map(cells, fmt)
            if mapped:
                columns = mapped
                headers_seen.extend(c for c in cells if c)
                unit = _unit_for(cells, columns, result, index)
            continue

        section, after_total = _stage_row(
            cells, columns, unit or "absolute", sheet_name, index, result,
            section, staged, after_total,
        )

    _demote_subtotals(staged)
    result.rows.extend(staged)


def _stage_row(
    cells: list[str],
    columns: dict[str, int],
    unit: str,
    sheet_name: str,
    index: int,
    result: HoldingsParseResult,
    section: str | None,
    staged: list[StagedHolding],
    after_total: bool,
) -> tuple[str | None, bool]:
    """Stage one row; return the section and whether the total has been passed."""
    name = _name_at(cells, columns)
    isin = _at(cells, columns, "isin")
    if not name:
        # A row with no name where the header says one should be still has a
        # label somewhere — Kotak's section totals live in the Industry column.
        # See `_stray_label`.
        name = _stray_label(cells, columns)

    # Coerce leniently, classify, THEN decide how much the failures matter. The
    # tail of a disclosure is prose in the table's columns — `Top Ten Holdings`
    # in the quantity cell, a bare `@` in the percentage cell. Strictness
    # belongs where the number is load-bearing: a security's market value.
    quantity, quantity_bad = _soft(_at(cells, columns, "quantity"))
    market_value, mv_bad = _soft(_at(cells, columns, "market_value"))
    pct, pct_bad = _soft(_at(cells, columns, "pct"))

    kind = classify_row(name, isin, quantity, market_value, pct)
    if kind == "unknown" and _is_summary_row(cells, columns):
        # Not unknown — a row of the SECOND table. See `_is_summary_row`.
        kind = "summary"
        _capture_stated_nav(cells, result)
    if after_total and kind in ("security", "unknown"):
        # §6.3 rule 4, positional. The file has already said what the portfolio
        # comes to, so a row beneath that line is not part of it.
        #
        # Nippon prints a stock-future table with the SAME column shape after
        # `GRAND TOTAL`: read as holdings it adds Rs 78.5 crore to Rs 5,075
        # crore — +0.155%, inside the 2% reconciliation tolerance, so the guard
        # that catches a doubled portfolio cannot see it (V1-15).
        #
        # AFTER the summary branch above, and the order is load-bearing: HDFC
        # prints its industry summary and NAV history BELOW its own Grand Total,
        # so rewriting `unknown` first swallows `stated_navs`.
        kind = "after_total"

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
        # And the percentage beside it, which is what says whether this AMC
        # writes 99.99 or 0.9999 for "the whole portfolio". See detect_pct_scale.
        result.stated_total_pct = pct
        after_total = True
    if kind == "blank":
        return section, after_total
    if kind == "section_header" and (name or isin).strip():
        section = nest(section, (name or isin).strip())
    if kind == "unknown":
        # §6.4: staged and surfaced, never discarded. An unrecognised row in a
        # disclosure is a holding we may be missing.
        result.warnings.append(
            ParseWarning("ROW_UNCLASSIFIED", f"{name[:60]!r}", index)
        )

    staged.append(
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
    return section, after_total


def reconciliation_error(result: HoldingsParseResult) -> Decimal | None:
    """How far the parsed securities sit from the file's own stated total, in %.

    `None` when the file states no total — an absent check is reported as
    absent, not silently passed.

    The general defence no list of section labels can be: HDFC nests one level
    deep, ICICI four, and a parser tuned to one over-counts the other by 2.9x.
    Neither is detectable from the rows — but both files state their total.
    """
    if result.stated_total is None or not result.stated_total:
        return None
    total = sum(
        (r.market_value_raw or Decimal(0) for r in result.securities), Decimal(0)
    )
    return (total - result.stated_total) / abs(result.stated_total) * 100


def detect_pct_scale(result: HoldingsParseResult) -> Decimal:
    """What the `% to NAV` column must be multiplied by to be a percentage.

    HDFC writes `9.21` for 9.21%; ICICI writes `0.0596550260489` and totals
    `0.99999999999`. Both headers say `% to Nav`. Reading ICICI's as percentages
    makes the weights sum to 1, failing §10's V1 and making `weight_residual`
    read 99; assuming the reverse inflates HDFC's by 100x.

    The total row states what the portfolio comes to in that column, so the
    file's own arithmetic says which convention it used (V1-08). Raises rather
    than guessing: a column we could not interpret usually means a misread
    sheet, not an unusual convention.
    """
    witness = result.stated_total_pct
    if witness is None:
        witness = sum(
            (r.pct_to_nav_raw or Decimal(0) for r in result.securities), Decimal(0)
        )
    magnitude = abs(witness)
    if not magnitude:
        # No percentage column at all, or one entirely empty. Nothing to
        # scale, and `pct_normalised` is recomputed from market value anyway.
        return Decimal(1)
    if magnitude <= PCT_FRACTION_MAX:
        return Decimal(100)
    if PCT_PERCENT_MIN <= magnitude <= PCT_PERCENT_MAX:
        return Decimal(1)
    raise ParseFailed(
        f"the % to NAV column totals {witness}, which is neither a fraction "
        f"(~1) nor a percentage (~100); the sheet was misread"
    )


def _demote_subtotals(staged: list[StagedHolding]) -> None:
    """Reclassify section rows that carry their own subtotal. §6.4, generalised.

    ICICI writes the section label and that section's total on the SAME row, so
    `classify_row` calls them securities — rightly, by its own rule, which is
    what stops HDFC's TREPS cash being dropped as a heading (V1-04). Two AMCs
    need opposite answers and vocabulary cannot settle it: a hand-written list
    of section labels still left ICICI 18.8% too large (V1-08).

    Arithmetic settles it. **A row whose value equals the sum of the rows
    beneath it is their total, not a peer.** Only rows with no ISIN and no
    quantity are candidates, so a genuine holding is never at risk.

    Nesting resolves INNERMOST-first and a demoted subtotal then counts once:
    accumulating raw values double-counts a child section's own subtotal row,
    which is V1-25's +93.8%. The run accumulates over the forest — a demoted row
    contributes its value and its span is skipped.
    """
    positions = [i for i, r in enumerate(staged) if r.row_kind == "security"]

    # offset of a demoted subtotal -> offset of the last row its run covers.
    # Both are offsets into `positions`, which is fixed for the whole function:
    # demoting a row must not remove it from the accumulation, because a
    # subtotal still stands for its subtree when its own parent is measured.
    spans: dict[int, int] = {}

    # Pass one: decide, innermost first.
    for offset in range(len(positions) - 1, -1, -1):
        row = staged[positions[offset]]
        if row.isin_raw or row.quantity_raw is not None:
            continue
        target = row.market_value_raw
        if target is None or not target:
            continue
        running = Decimal(0)
        position = offset + 1
        while position < len(positions):
            value = staged[positions[position]].market_value_raw
            if value is None:
                break
            running += value
            # A row already demoted stands for everything beneath it, so it is
            # counted once and its span stepped over. An ordinary row covers
            # only itself.
            covered = spans.get(position, position)
            if abs(running - target) <= abs(target) * SUBTOTAL_TOLERANCE:
                spans[offset] = covered
                break
            position = covered + 1

    # Pass two: apply, outermost first, so a nested label wins over its parent's.
    for offset in sorted(spans):
        index = positions[offset]
        row = staged[index]
        staged[index] = replace(row, row_kind="subtotal")
        _assign_section(staged, positions, offset, spans[offset], row)


def _assign_section(
    staged: list[StagedHolding],
    positions: list[int],
    offset: int,
    last: int,
    header: StagedHolding,
) -> None:
    """Give a demoted subtotal's rows the section it names.

    ICICI's section labels ARE its subtotal rows, and the section heading is the
    only thing on the sheet saying a row is a derivative (V1-07). Callers apply
    demotions OUTERMOST first so a nested label overwrites its parent's;
    `_demote_subtotals` decides in the opposite order, hence two passes.
    """
    label = header.instrument_raw_name.strip()
    if not label:
        return
    for position in range(offset + 1, last + 1):
        index = positions[position]
        staged[index] = replace(staged[index], section=nest(staged[index].section, label))


def classify_row(
    name: str,
    isin: str,
    quantity: Decimal | None,
    market_value: Decimal | None,
    pct: Decimal | None,
) -> str:
    """§6.4, with the heuristic that misclassifies a real file corrected.

    §6.4 ends with `if isin or qty or mv: return "security"`. HDFC's trailing
    industry summary is offset by one column, so the sector name lands in the
    ISIN cell:

        [122]  ISIN='Banks'   Name='28.94'   Quantity=None   MarketValue=None

    `isin` is truthy, so all 32 rows read as securities and every sector was
    counted twice. VALIDATE rather than test truthiness: a real ISIN, or a
    non-numeric name beside an actual market value.
    """
    if not name.strip() and not isin.strip():
        return "blank"
    if NOTE_ROW.match(name) or NOTE_ROW.match(isin):
        return "note"
    if TOTAL_ROW.match(name) or TOTAL_ROW.match(isin):
        return "total"
    if is_valid_isin(isin):
        return "security"
    # §6.4's `and mv is None` guard, load-bearing. HDFC writes a section label
    # twice: as a bare heading, and on the row carrying the numbers. Testing the
    # name alone classified both as headings and dropped `TREPS` — the fund's
    # whole Rs 343 crore cash position, 3.1%, with the rest renormalising to
    # 100 so nothing downstream could tell (V1-04).
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

    HDFC's trailing block is a second table offset from the first — a label in
    the ISIN column, a number where the name belongs:

        [122]  ISIN='Banks'                        Name='28.94'

    Calling these `unknown` counted nothing but produced 45 warnings on a clean
    file, and a warning list that is always long is one nobody reads.
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

    The disclosure states its own NAV per unit and AMFI states it too, so the
    two cross-check: the cheapest independent witness in the pipeline.
    """
    populated = [c for c in cells if c.strip()]
    if len(populated) < 2 or not NAV_LABEL_RE.search(populated[0]):
        return
    # The FIRST numeric after the label. The NAV-history block lists this month
    # then last month, so taking any numeric captures the prior month's NAV —
    # close enough to look right, wrong enough to break the cross-check.
    for cell in populated[1:]:
        value, failed = _soft(cell)
        if not failed and value is not None and value > 0:
            result.stated_navs.setdefault(populated[0].strip(), value)
            return


def _header_map(cells: list[str], fmt: HoldingsFormat) -> dict[str, int] | None:
    """Locate the header row and map its columns. Never positional — V0-23.

    Whitespace inside a header cell is collapsed before matching: a header
    spelled across two lines is the same header. PPFAS writes `% to Net\nAssets`,
    which matched nothing, so no percentage column was mapped and 100% of the
    weight was derived rather than reported.
    """
    lowered = [re.sub(r"\s+", " ", c).strip().lower() for c in cells]
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


def _name_span(columns: dict[str, int]) -> tuple[int, int]:
    """The columns the instrument name may occupy: its own, up to the next
    mapped field.

    Kotak indents by COLUMN, not by whitespace — the nesting level IS the
    column, so a single-column read came back empty for all 51 holdings. A span
    rather than a special case: every other AMC's span is one column wide and
    behaves exactly as before.
    """
    start = columns["name"]
    after = [i for field, i in columns.items() if field != "name" and i > start]
    return start, (min(after) if after else start + 1)


def _name_at(cells: list[str], columns: dict[str, int]) -> str:
    """First cell in the name span that could be a name.

    A number is skipped: Kotak's debt sheets carry an unlabelled numeric column
    left of the name, so "first non-empty" read YES BANK's AT1 bonds as a
    security named `0`. The span looks for the innermost LABEL.
    """
    start, stop = _name_span(columns)
    for index in range(start, min(stop, len(cells))):
        text = cells[index].strip()
        if text and not _looks_numeric(text):
            return cells[index]
    return ""


def _stray_label(cells: list[str], columns: dict[str, int]) -> str:
    """The label of a row that put nothing in the name span.

    Kotak closes each section with `Total` in the INDUSTRY column, and
    `TOTAL_ROW` is matched against name and ISIN only (§6.4) — so all three
    such rows parsed as securities: 794,038.80 against a stated 402,258.02.

    Narrow on purpose: only when the name span is empty, and ignoring columns
    already mapped to numbers so a quantity cannot pass as a label.
    """
    start, stop = _name_span(columns)
    skip = {columns.get(f) for f in ("isin", "quantity", "market_value", "pct")}
    for index, text in enumerate(cells):
        if start <= index < stop or index in skip:
            continue
        if text.strip():
            return text
    return ""


def _as_on_date(text: str) -> date | None:
    match = AS_ON_RE.search(text)
    return _parse_date(match.group(1)) if match else None


def _date_from_filename(filename: str) -> date | None:
    """§7.5 rule 2. `Monthly HDFC Flexi Cap Fund - 31 July 2026.xlsx`."""
    match = re.search(r"(\d{1,2}[-\s][A-Za-z]{3,9}[-\s]\d{4})", filename)
    return _parse_date(match.group(1)) if match else None


def _parse_date(text: str) -> date | None:
    """§7.5. None rather than raising — every caller has another candidate."""
    cleaned = re.sub(r"\s*,\s*", ",", text.replace("/", "-").strip())
    for pattern in (_DAY_FIRST, _MONTH_FIRST):
        match = pattern.match(cleaned)
        if match is None:
            continue
        month = month_number(match["month"])
        if month is None:
            continue
        try:
            return date(int(match["year"]), month, int(match["day"]))
        except ValueError:
            return None
    for fmt in _NUMERIC_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None
