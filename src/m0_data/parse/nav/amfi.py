"""AMFI NAVAll.txt -> staged scheme and NAV rows. MODULE_0.md §2.2, §6.

**The file is sectioned, not flat** (§2.2). Blank lines, SEBI-category headers
and AMC names interleave with data rows and carry no delimiter, so a CSV reader
produces garbage. A line state machine carries category and AMC context down
onto each row.

The live file does not match §2.2's description (V0-20): eight columns, not six,
with **plan and option stated rather than inferred**. That is the most valuable
line here — V0-05, a Direct NAV series filed against a Regular scheme record
10.13% apart, was possible because plan had to be read out of a scheme name.

Pure: takes `list[str]` and returns rows. The fetch layer owns the network and
the loader owns the database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from src.m0_data.normalise.numbers import CoercionError, to_date, to_decimal
from src.m0_data.resolve.isin import is_valid_isin

PARSER_ID = "nav.amfi"
PARSER_VERSION = "1"

#: The header row. It repeats mid-file in the history exports, and it is the
#: only thing that says which column is which — see `column_map`.
HEADER_RE = re.compile(r"^Scheme\s+Code\s*;", re.I)

#: Header text -> canonical field, tried IN ORDER. AMFI publishes the same eight
#: columns in two different orders (NAVAll.txt against the history endpoint), so
#: reading either positionally puts the scheme NAME into the ISIN field on the
#: other — which fails validation and silently unresolves every row (V0-23).
#:
#: Order matters: `ISIN Div Payout/ ISIN Growth` contains "growth", so the ISIN
#: rules must be tried before the name and option rules.
_HEADER_RULES: tuple[tuple[str, str], ...] = (
    ("scheme code", "code"),
    ("reinvestment", "isin_reinvest"),
    ("isin", "isin_primary"),
    ("scheme name", "name"),
    ("nav name", "name"),
    ("net asset value", "nav"),
    ("plan", "plan"),
    ("option", "option"),
    ("date", "nav_date"),
)

REQUIRED_COLUMNS = frozenset({"code", "name", "nav", "nav_date"})

#: A SEBI category line: `Open Ended Schemes(Equity Scheme - Flexi Cap Fund)`.
#: Everything else without a delimiter is an AMC name.
CATEGORY_RE = re.compile(
    r"^(Open|Close|Interval)\s+Ended\s+Schemes?\s*\(?(.*?)\)?\s*$", re.I
)

#: Minimum delimiters for a line to be a data row. The live file has 8 columns;
#: accepting 7 tolerates a trailing field being dropped, which some mirrors do.
MIN_FIELDS = 7

#: `-` in the reinvestment ISIN column means the scheme has no reinvestment
#: variant, not that the ISIN is unknown. Anything else in an ISIN column is
#: validated rather than trusted — see `_isin_or_none`.
NO_ISIN = frozenset({"-", "", "N.A.", "NA"})


class AmfiParseError(ValueError):
    """The file is not shaped like NAVAll.txt at all."""


@dataclass(frozen=True)
class StagedScheme:
    """One scheme as the file describes it. L1 — verbatim, plus normalisation."""

    scheme_id: str
    amfi_code: str
    isin: str | None
    scheme_name: str
    plan: str  # direct|regular|unknown
    option: str  # growth|idcw_payout|idcw_reinvest|unknown
    option_raw: str
    amc_name: str
    sebi_category: str | None
    row_number: int


@dataclass(frozen=True)
class StagedNav:
    scheme_id: str
    nav_date: date
    nav: Decimal


@dataclass
class AmfiParseResult:
    """§6.1. Warnings are data, not logging — they end up in `job_run.warnings`."""

    schemes: list[StagedScheme] = field(default_factory=list)
    navs: list[StagedNav] = field(default_factory=list)
    warnings: list[tuple[int, str]] = field(default_factory=list)
    #: Lines inside the body that nothing consumed. Same guarantee as V0-15's
    #: CAS parser: a row the state machine did not understand is a scheme
    #: missing from the master, and silence about it is the failure mode.
    unparsed: list[tuple[int, str]] = field(default_factory=list)


def normalise_plan(raw: str) -> str:
    """`Direct Plan` / `Regular Plan` -> `direct` / `regular`.

    Returns `unknown` rather than guessing. Some very old schemes predate the
    Direct/Regular split and print an empty plan column; calling those Regular
    would be inventing the distinction the column exists to record.
    """
    text = raw.strip().lower()
    if "direct" in text:
        return "direct"
    if "regular" in text:
        return "regular"
    return "unknown"


def normalise_option(raw: str) -> str:
    """Collapse AMFI's option wording to the three the schema stores.

    §4.4 allows `growth | idcw_payout | idcw_reinvest`; the file is wider
    (`IDCW-Re-investment`, `MONTHLY DCW Payout`, `Growth Option`). The payout
    frequency is real information with nowhere to go, so `option_raw` keeps the
    original and the frequency is dropped rather than smuggled into `option`.

    Reinvestment is checked BEFORE payout: `IDCW-Re-investment` contains
    neither payout nor growth, but a frequency-qualified one could contain both.
    """
    text = raw.strip().lower()
    if "reinvest" in text or "re-invest" in text:
        return "idcw_reinvest"
    if "growth" in text:
        return "growth"
    if "payout" in text or "idcw" in text or "dcw" in text or "dividend" in text:
        return "idcw_payout"
    return "unknown"


def scheme_id_for(isin: str | None, amfi_code: str, option: str) -> str:
    """§4.4: the ISIN where available, else `AMFI:{code}:{option}`.

    The option qualifier on the fallback is load-bearing — one AMFI code can
    describe two schemes (a payout and a reinvestment variant), so the code
    alone is not a key.
    """
    return isin if isin else f"AMFI:{amfi_code}:{option}"


def column_map(header_line: str) -> dict[str, int]:
    """Map canonical field -> column index, from the header the file printed.

    The two AMFI layouts carry the same eight columns in a different order, so
    a positional parser reads the scheme name where the other puts an ISIN.
    Nothing crashes: the name fails ISIN validation, the row resolves to
    nothing, and an export lands in quarantine for a reason no message explains
    (V0-23).
    """
    mapping: dict[str, int] = {}
    for index, raw in enumerate(header_line.split(";")):
        text = raw.strip().lower()
        for needle, field_name in _HEADER_RULES:
            if needle in text and field_name not in mapping:
                mapping[field_name] = index
                break
    missing = REQUIRED_COLUMNS - set(mapping)
    if missing:
        raise AmfiParseError(
            f"header is missing {sorted(missing)}: {header_line[:120]!r}"
        )
    return mapping


def _field(fields: list[str], columns: dict[str, int], name: str) -> str:
    """One column by name, empty when the header did not carry it.

    `plan` and `option` are genuinely absent from some exports, so a missing
    column is a blank rather than an error; `REQUIRED_COLUMNS` already refused
    the header if anything load-bearing was missing.
    """
    index = columns.get(name)
    if index is None or index >= len(fields):
        return ""
    return fields[index]


def _isin_or_none(raw: str, lineno: int, result: AmfiParseResult) -> str | None:
    """Validate an ISIN column. §8.3: reject malformed, warn, fall through.

    The live file needs this — its ISIN columns carry the literal `Redeemed` on
    nine rows and `HDFCNIVODG` on one, and accepting them keys nine unrelated
    IL&FS schemes to a single row.

    An absent ISIN is normal and silent; a present but INVALID one warns,
    because the source printed something it should not have.
    """
    token = raw.strip()
    if token in NO_ISIN:
        return None
    if not is_valid_isin(token):
        result.warnings.append((lineno, f"not a valid ISIN, ignored: {token!r}"))
        return None
    return token.upper()


def parse_navall(lines: list[str]) -> AmfiParseResult:
    """Run the state machine over the file's lines.

    Emits ONE OR TWO schemes per data row (§2.2's two-ISIN trap). Column 2 is
    the ISIN for the payout-or-growth variant; column 3 is the reinvestment
    ISIN, present only when the scheme offers one. When it is present the row
    describes two schemes with two NAV series, and collapsing them is the §4.4
    error.
    """
    result = AmfiParseResult()
    amc_name: str | None = None
    category: str | None = None
    columns: dict[str, int] | None = None
    saw_data = False

    for lineno, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue

        if HEADER_RE.match(line):
            # Re-read on every occurrence. The history export repeats it, and
            # taking the first one on faith would miss a layout change mid-file.
            columns = column_map(line)
            continue

        if ";" not in line:
            match = CATEGORY_RE.match(line)
            if match:
                category = match.group(2).strip() or None
            else:
                # Not a category, not a data row: the AMC name that owns every
                # row until the next one.
                amc_name = line
            continue

        fields = [f.strip() for f in line.split(";")]
        if len(fields) < MIN_FIELDS:
            result.unparsed.append((lineno, line))
            continue
        if columns is None:
            raise AmfiParseError(
                f"line {lineno}: data row before any header; column order is unknown"
            )
        if amc_name is None:
            raise AmfiParseError(f"line {lineno}: data row before any AMC context")

        saw_data = True
        code = _field(fields, columns, "code")
        isin_primary = _isin_or_none(
            _field(fields, columns, "isin_primary"), lineno, result
        )
        isin_reinvest = _isin_or_none(
            _field(fields, columns, "isin_reinvest"), lineno, result
        )
        name = _field(fields, columns, "name")
        plan_raw = _field(fields, columns, "plan")
        option_raw = _field(fields, columns, "option")
        nav_raw = _field(fields, columns, "nav")
        date_raw = _field(fields, columns, "nav_date")

        if not code or not name:
            result.unparsed.append((lineno, line))
            continue

        plan = normalise_plan(plan_raw)
        try:
            nav = to_decimal(nav_raw)
            nav_date = to_date(date_raw)
        except CoercionError as exc:
            result.warnings.append((lineno, str(exc)))
            nav = nav_date = None

        # The row's own variant, then the reinvestment variant if it has one.
        variants = [(isin_primary, normalise_option(option_raw), option_raw)]
        if isin_reinvest:
            variants.append(
                (isin_reinvest, "idcw_reinvest", f"{option_raw} (reinvestment)")
            )

        for isin, option, opt_raw in variants:
            scheme_id = scheme_id_for(isin, code, option)
            result.schemes.append(
                StagedScheme(
                    scheme_id=scheme_id,
                    amfi_code=code,
                    isin=isin,
                    scheme_name=name,
                    plan=plan,
                    option=option,
                    option_raw=opt_raw.strip(),
                    amc_name=amc_name,
                    sebi_category=category,
                    row_number=lineno,
                )
            )
            # A NAV row only for the variant this line actually priced. The
            # reinvestment variant has its own NAV series and this file does
            # not carry it, so inventing one from the payout NAV would be
            # fabricating the very series §4.4 says must stay separate.
            if nav is not None and nav_date is not None and isin == isin_primary:
                result.navs.append(StagedNav(scheme_id, nav_date, nav))

    if not saw_data:
        raise AmfiParseError("no data rows found; this is not an AMFI NAV file")
    return result
