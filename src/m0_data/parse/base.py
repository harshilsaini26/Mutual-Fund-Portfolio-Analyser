"""Parser plugin protocol. MODULE_0.md §6.1.

§6.3's rules are non-negotiable and they are all about restraint:

1. **Parsers never normalise.** No unit conversion, no name cleaning, no number
   coercion, no entity resolution. A parser says what the file said. Everything
   after that is a separate, re-runnable step — which is what makes a bug in
   normalisation fixable by re-running L1 -> L2 rather than by re-fetching.
2. **Every staged row carries `file_id` and `row_number`.** This is what makes
   `PLAN.md` §4.2 real: a number in the UI traces to a row in an archived file.
3. **A parser that cannot parse RAISES.** Never a partial result, silently.
4. **Classify every row.** Misclassifying a "Total" row as a security
   double-counts the entire portfolio.
5. **Bump `version` on any behaviour change**, so a fixed parser can replay
   every file the old one touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol


class ParseFailed(ValueError):
    """§6.3 rule 3. Carries enough context to reproduce."""


@dataclass(frozen=True)
class RawFile:
    """An archived file, as the parser sees it."""

    file_id: str
    source_id: str
    filename: str
    content: bytes


@dataclass(frozen=True)
class ParseWarning:
    code: str
    message: str
    row_number: int | None = None


@dataclass(frozen=True)
class StagedHolding:
    """One disclosed row, VERBATIM. §6.3 rule 1.

    Numbers are `Decimal` because reading a cell already produces one and
    keeping it as text would only invite a `float()` later — but no *unit* has
    been applied. `market_value_raw` is in whatever `market_value_unit` says,
    and converting it is `normalise/units.py`'s job.

    `row_kind` is §6.4's classification, and it is the difference between a
    portfolio and a portfolio counted twice.
    """

    row_number: int
    #: security|subtotal|after_total|section_header|total|note|blank|unknown
    row_kind: str
    instrument_raw_name: str
    isin_raw: str | None
    quantity_raw: Decimal | None
    market_value_raw: Decimal | None
    market_value_unit: str  # absolute|thousand|lakh|crore — verbatim intent
    pct_to_nav_raw: Decimal | None
    reported_sector: str | None
    coupon_or_rating: str | None
    sheet_name: str
    #: The section heading this row sits under — `EQUITY & EQUITY RELATED`,
    #: `MONEY MARKET INSTRUMENTS`, `OPTIONS`. Carried down from the last
    #: heading seen, the same way the AMFI NAV parser carries AMC and category
    #: context onto rows that do not repeat it.
    #:
    #: It is the only thing on the sheet that says a row is a derivative. The
    #: instrument name does not: HDFC's short leg is written `Eternal Limited`,
    #: identically to the long position twelve rows above it.
    section: str | None = None


@dataclass
class HoldingsParseResult:
    """§6.1. Warnings are data — they end up in `job_run` and the disclosure header."""

    rows: list[StagedHolding] = field(default_factory=list)
    as_of_date: date | None = None
    scheme_raw_name: str | None = None
    #: The NAV per unit the disclosure states for itself, by option label. An
    #: independent witness to whether we mapped the file to the right scheme.
    stated_navs: dict[str, Decimal] = field(default_factory=dict)
    #: The portfolio total the file states for itself — `Grand Total` on HDFC's
    #: sheet, `Total Net Assets` on ICICI's, in the file's own units.
    #:
    #: This is the only general defence against counting a subtotal as a
    #: holding. Every AMC nests its sections differently and names them
    #: differently, so no list of section labels generalises; but every AMC
    #: prints what the portfolio adds up to, and a parse that disagrees with
    #: that number has misread the file. See `reconciliation_error`.
    stated_total: Decimal | None = None
    #: The percentage the file's own total row carries — 99.99999999999996 on
    #: HDFC's sheet, 0.9999999999896085 on ICICI's. The witness `pct_scale` is
    #: read from, and it is the publisher's own arithmetic again (see V1-08).
    stated_total_pct: Decimal | None = None
    #: What the `% to NAV` column must be MULTIPLIED BY to be a percentage:
    #: 1 where the file writes 9.21 for 9.21%, 100 where it writes 0.0921.
    #:
    #: The percentage analogue of `StagedHolding.market_value_unit`, and the
    #: same discipline (§6.3 rule 1): the parser *observes* the scale and
    #: carries it as a label, and normalisation applies it. HDFC reports
    #: percentages and ICICI reports fractions, so a parser that assumed either
    #: would make one AMC's reported weights sum to 1 and fail §10's V1.
    pct_scale: Decimal = Decimal(1)
    warnings: list[ParseWarning] = field(default_factory=list)
    headers_seen: tuple[str, ...] = ()

    @property
    def securities(self) -> list[StagedHolding]:
        return [r for r in self.rows if r.row_kind == "security"]


class HoldingsParser(Protocol):
    """§6.1. One per AMC, registered and selected by `sniff`."""

    parser_id: str  # 'holdings.hdfc'
    version: str  # bump on ANY behaviour change (§6.3 rule 5)
    amc_id: str

    def sniff(self, f: RawFile) -> float:
        """Confidence 0.0-1.0 that this parser handles this file.

        Cheap checks only — filename, magic bytes, the first sheet's header
        row. §6.2 routes on this rather than a hardcoded AMC->parser mapping
        because AMCs rename files, merge entities, and occasionally publish one
        month in a different format.
        """
        ...

    def parse(
        self, f: RawFile, sheet: str | None = None
    ) -> HoldingsParseResult:
        """Parse the whole workbook, or the one sheet named.

        `sheet` is additive and optional because only one AMC needs it so far:
        Nippon publishes 108 schemes as 108 sheets of a single workbook, where
        HDFC ships a file per scheme and ICICI a ZIP of them. Reading every
        sheet there merges 108 portfolios into one result.
        """
        ...
