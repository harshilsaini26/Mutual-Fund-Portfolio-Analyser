"""AMFI NAVAll.txt -> staged scheme and NAV rows. MODULE_0.md §2.2, §6.

**The file is sectioned, not flat** (§2.2). Blank lines, SEBI-category headers
and AMC names interleave with data rows, and none of them carry a delimiter, so
a CSV reader produces garbage. A line state machine carries the category and AMC
context down onto each row — the same shape as M1's CAS parser, for the same
reason.

The live file does not match §2.2's description, and the differences change what
this parser can do. DECISIONS V0-20:

  §2.2:   Scheme Code;ISIN Growth;ISIN Reinvestment;Scheme Name;NAV;Date
  actual: Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;
          Scheme Name;Plan;Option;Net Asset Value;Date

Eight columns, and **plan and option are stated rather than inferred**. That is
the most valuable line in this module: V0-05 — a Direct NAV series filed against
a Regular scheme record, 10.13% apart — was possible because plan had to be read
out of a scheme name. It no longer does.

This module is pure. It takes `list[str]` and returns rows; the fetch layer
above it owns the network and the loader below it owns the database.
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

#: The header row, which repeats in some exports. Recognised so it is skipped
#: rather than counted as an unparsed line.
HEADER_RE = re.compile(r"^Scheme\s+Code\s*;", re.I)

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

    The DDL (§4.4) allows `growth | idcw_payout | idcw_reinvest`. The file is
    wider than that: `IDCW-Re-investment`, `MONTHLY DCW Payout`,
    `QUARTERLY IDCW Payout`, `Growth Option`. The payout frequency is real
    information with nowhere to go, so `option_raw` keeps the original string
    and the frequency is dropped rather than smuggled into `option`.

    Reinvestment is checked before payout: `IDCW-Re-investment` contains
    neither the word payout nor growth, but a frequency-qualified reinvestment
    option could contain both.
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


def _isin_or_none(raw: str, lineno: int, result: AmfiParseResult) -> str | None:
    """Validate an ISIN column. §8.3: reject malformed, warn, fall through.

    The live file needs this. Its ISIN columns contain the literal string
    `Redeemed` on nine rows and `HDFCNIVODG` on one — neither is an ISIN, and
    accepting them keys nine unrelated IL&FS schemes to a single row called
    `Redeemed`, which then collapse into one another on load.

    An absent ISIN is normal and silent; a *present but invalid* one is a
    warning, because it means the source printed something it should not have.
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

    Emits **one or two schemes per data row** (§2.2's two-ISIN trap). Column 2
    is the ISIN for the payout-or-growth variant this row describes; column 3
    is the reinvestment ISIN, present only when the scheme also offers
    reinvestment. When it is present the row describes two distinct schemes
    with two distinct NAV series, and collapsing them is the §4.4 error.
    """
    result = AmfiParseResult()
    amc_name: str | None = None
    category: str | None = None
    saw_data = False

    for lineno, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue

        if HEADER_RE.match(line):
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
        if amc_name is None:
            raise AmfiParseError(f"line {lineno}: data row before any AMC context")

        saw_data = True
        code = fields[0]
        isin_primary = _isin_or_none(fields[1], lineno, result)
        isin_reinvest = _isin_or_none(fields[2], lineno, result)
        name, plan_raw, option_raw = fields[3], fields[4], fields[5]
        nav_raw, date_raw = fields[6], fields[7] if len(fields) > 7 else ""

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
