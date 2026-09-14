"""AMFI's half-yearly market-capitalisation list. MODULE_0.md §2.2 S4, §4.3.

The authoritative point-in-time definition of the equity universe: rank 1-100
Large, 101-250 Mid, 251+ Small. §2.2 requires **every style computation to apply
the list in force at the holding date**, not today's — otherwise a stock that
changed bucket creates phantom drift or masks real drift.

Three traps in the file, all found by reading a real one (V1-02):

1. **Both the rank and the market cap are FORMULAS**, and the figure rank
   operates on is the mean of the BSE, NSE and MSEI columns. Reading the sheet
   yields formula text, or a cached value only if Excel saved one, so both are
   recomputed here. Reading the first header containing "market cap" would
   silently rank on BSE alone.
2. **AMFI states the answer in column K.** Not a reason to skip the arithmetic —
   a reason to do it and compare: a disagreement means we misread the file, and
   it is reported rather than resolved in either direction.
3. **The exchange figures are raw floats**, converted to `Decimal` at this
   boundary like every other number entering the system (§8.2 rule 1).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import openpyxl

from src.m0_data.normalise.numbers import to_decimal
from src.m0_data.resolve.isin import is_valid_isin

PARSER_ID = "mcap.amfi"
PARSER_VERSION = "1"

#: §2.2 S4. The statutory boundaries, and they are ranks rather than absolute
#: sizes — which is why the list has to be re-read each half-year rather than a
#: threshold stored once.
LARGE_CAP_MAX_RANK = 100
MID_CAP_MAX_RANK = 250

#: The period end is in the filename, not in the sheet:
#: `AverageMarketCapitalization30Jun2026.xlsx`.
PERIOD_RE = re.compile(r"(\d{2})([A-Za-z]{3})(\d{4})")

#: Header cells we need, matched case-insensitively on a substring. The sheet's
#: header row is not row 1 — a title spans the top — so it is located rather
#: than assumed.
_WANTED = (
    ("company name", "name"),
    ("isin", "isin"),
    ("nse symbol", "nse"),
    ("bse symbol", "bse"),
    ("bse 6 month", "mcap_bse"),
    ("nse 6 month", "mcap_nse"),
    ("msei 6 month", "mcap_msei"),
    ("categorization", "stated_bucket"),
)

#: AMFI's own wording in column K, folded to our three values.
_STATED = {"large cap": "large", "mid cap": "mid", "small cap": "small"}


class McapParseError(ValueError):
    """The workbook is not shaped like an AMFI market-cap list."""


@dataclass(frozen=True)
class McapRow:
    """One listed company as of `basis_date`.

    `market_cap`, `rank` and `bucket` are all optional together. 320 of the
    5,427 companies in the June-2026 list publish no average market cap —
    newly listed or suspended — and they are **kept, not dropped**
    (`CLAUDE.md` invariant 4). They are real issuers with real ISINs, and a
    holding in one of them must resolve to that issuer rather than to
    `__UNRESOLVED__`; what they lack is a rank, so they simply get no
    `amfi_mcap` classification.
    """

    isin: str
    company_name: str
    market_cap: Decimal | None
    rank: int | None
    bucket: str | None  # large|mid|small — computed from our own ranking
    #: AMFI's own answer, from column K. Kept alongside ours so the two can be
    #: compared rather than one trusted; a disagreement is a parse warning.
    stated_bucket: str | None
    nse_symbol: str | None
    bse_symbol: str | None


@dataclass
class McapParseResult:
    basis_date: date
    rows: list[McapRow] = field(default_factory=list)
    warnings: list[tuple[int, str]] = field(default_factory=list)
    #: Companies kept as issuers but excluded from the ranking, because they
    #: publish no average market cap. Not warnings — this is a normal state for
    #: a recent listing, and reporting 320 of them as problems would drown the
    #: ones that are.
    unranked: list[tuple[int, str, str]] = field(default_factory=list)


def basis_date_from_name(filename: str) -> date:
    """The list's effective date, from the filename. §7.5's rule 2.

    §7.5 forbids defaulting to "last month-end" when a date cannot be found,
    and this is why: `mcap_basis` is what stops a 2026 classification being
    applied to a 2021 holding, so a wrong one is silent look-ahead bias.
    """
    match = PERIOD_RE.search(filename)
    if not match:
        raise McapParseError(f"no period end in filename: {filename!r}")
    day, month, year = match.groups()
    from datetime import datetime

    return datetime.strptime(f"{day}-{month}-{year}", "%d-%b-%Y").date()


def bucket_for(rank: int) -> str:
    """§2.2 S4's statutory boundaries."""
    if rank <= LARGE_CAP_MAX_RANK:
        return "large"
    if rank <= MID_CAP_MAX_RANK:
        return "mid"
    return "small"


def parse_mcap_xlsx(content: bytes, filename: str) -> McapParseResult:
    """Read the workbook into ranked rows.

    `data_only` is deliberately NOT set. It would return the cached value of
    the rank formula when Excel saved one and `None` when it did not, so the
    parser's behaviour would depend on how the file was last opened. Rank is
    computed from the market cap instead.
    """
    result = McapParseResult(basis_date=basis_date_from_name(filename))
    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
    sheet = workbook[workbook.sheetnames[0]]

    columns: dict[str, int] = {}
    staged: list[
        tuple[str, str, Decimal | None, str | None, str | None, str | None]
    ] = []

    for lineno, row in enumerate(sheet.iter_rows(values_only=True), start=1):
        cells = ["" if v is None else str(v).strip() for v in row]
        if not columns:
            found = _header_map(cells)
            if found:
                columns = found
            continue

        isin = cells[columns["isin"]] if columns["isin"] < len(cells) else ""
        name = cells[columns["name"]] if columns["name"] < len(cells) else ""
        if not isin and not name:
            continue
        if not is_valid_isin(isin):
            # §8.3: reject rather than create a garbage instrument. A row
            # without a usable ISIN cannot be joined to a holding anyway.
            result.warnings.append((lineno, f"not a valid ISIN, row skipped: {isin!r}"))
            continue

        # Column J is `=AVERAGE(E,G,I)` and unreadable, so the average is
        # recomputed over whichever exchanges actually quoted the company.
        # Averaging over the present ones rather than dividing by three is what
        # AMFI's own formula does: AVERAGE ignores blanks.
        quotes = [
            v for v in (
                to_decimal(_optional(cells, columns.get(k)))
                for k in ("mcap_bse", "mcap_nse", "mcap_msei")
            )
            if v is not None and v > 0
        ]
        # `sum(..., Decimal(0))`, not bare `sum`: an untyped start is `int`,
        # and the result then widens to a float on an empty list.
        mcap = sum(quotes, Decimal(0)) / len(quotes) if quotes else None
        if mcap is None:
            # Kept as an issuer, excluded from the ranking. See McapRow.
            result.unranked.append((lineno, isin.upper(), name))

        staged.append((
            isin.upper(), name,
            mcap,
            _optional(cells, columns.get("nse")),
            _optional(cells, columns.get("bse")),
            _STATED.get(_optional(cells, columns.get("stated_bucket")).strip().lower()),
        ))

    if not staged:
        raise McapParseError("no ranked rows found; not an AMFI market-cap list")

    # Rank by market cap, descending. Ties break on ISIN so a rebuild is
    # byte-identical (CLAUDE.md invariant 10).
    ranked = sorted(
        (r for r in staged if r[2] is not None),
        key=lambda r: (-(r[2] or Decimal(0)), r[0]),
    )
    rank_by_isin = {row[0]: position for position, row in enumerate(ranked, start=1)}

    for isin, _name, _mcap, _nse, _bse, stated in staged:
        computed = bucket_for(rank_by_isin[isin]) if isin in rank_by_isin else None
        if stated and computed and stated != computed:
            # We misread the file, or AMFI's own column disagrees with its own
            # ranking. Either way it is reported, never silently reconciled.
            result.warnings.append((
                0,
                f"{isin}: AMFI says {stated}, "
                f"rank {rank_by_isin[isin]} says {computed}",
            ))

    result.rows = [
        McapRow(
            isin=isin,
            company_name=name,
            market_cap=mcap,
            rank=rank_by_isin.get(isin),
            bucket=bucket_for(rank_by_isin[isin]) if isin in rank_by_isin else None,
            stated_bucket=stated,
            nse_symbol=nse or None,
            bse_symbol=bse or None,
        )
        for isin, name, mcap, nse, bse, stated in sorted(staged, key=lambda r: r[0])
    ]
    return result


def _header_map(cells: list[str]) -> dict[str, int] | None:
    """Locate the header row and map the columns we need onto it."""
    lowered = [c.lower() for c in cells]
    found: dict[str, int] = {}
    for needle, key in _WANTED:
        for index, text in enumerate(lowered):
            if needle in text and key not in found:
                found[key] = index
                break
    required = {"isin", "name", "mcap_bse"}
    return found if required <= set(found) else None


def _optional(cells: list[str], index: int | None) -> str:
    return cells[index] if index is not None and index < len(cells) else ""
