"""Kotak Mahindra Mutual Fund. MODULE_0.md §6.1, DECISIONS V1-33.

Verified against `Consolidated SEBI Portfolio July 2026`, the real published
workbook — 119 sheets, one per scheme, sheet `KPF` being Kotak Pioneer Fund as
on 31-Jul-2026 (sha256 `e519d3c8ce2672c3`). The fixture keeps that sheet
unchanged and drops the other 118; see V1-33 for why that is not the edit
V1-28 warned about. That file had to be downloaded by hand: Kotak's site is behind
Radware bot management and selecting the portfolio dropdown serves a CAPTCHA,
which this project does not solve (V1-03, V1-32).

    row 1  Portfolio of Kotak Pioneer Fund as on 31-Jul-2026
    row 2  Name of Instrument | ISIN Code | Industry | Yield | Quantity |
           Market Value (Rs.in Lacs) | % to Net Assets

Two things differ from the three formats already parsing, and **neither needed
code here** — both are general rules in `holdings/base.py`, which is the same
answer V1-10 reached for ICICI:

1. **The sheet indents by COLUMN, not by whitespace.** `Name of Instrument` is
   the header of column 0, but `Equity & Equity related` sits in column 0,
   `Listed/Awaiting listing` in column 1, and all 51 holdings in column 2. The
   nesting level is the column index. `_name_span` reads the name as the range
   from its own column to the next mapped field and takes the innermost label
   present, which leaves HDFC, ICICI and Nippon byte-identical because each of
   their spans is one column wide.

2. **Section totals are labelled in the Industry column.** Each block closes
   with a row reading `Total`, and the portfolio with `Grand Total`, in column
   4 — not in the name column and not in the ISIN column, which are the two
   §6.4 matches `TOTAL_ROW` against. Read as holdings those three rows bring
   the parse to 794,038.80 against a stated 402,258.02. `_stray_label` looks
   for a row's label outside the name span only when the span is empty.

Note what is NOT here: this sheet writes its subtotals BELOW the rows they
cover, where ICICI writes them ON the section row above. `_demote_subtotals`
never fires on it, because the `Total` rows are classified as totals before
they can be mistaken for positions. Two opposite layouts, settled by different
rules, and neither AMC's file needed the other's.

**Sheet selection is required.** Like Nippon's 108-sheet workbook, a parse with
no `sheet` merges 119 portfolios into one. `KPF` is Kotak Pioneer; the codes are
opaque and are read off the sheet's own row 1, which names the scheme in prose.
"""

from __future__ import annotations

from src.m0_data.parse.base import HoldingsParseResult, RawFile
from src.m0_data.parse.holdings.base import HoldingsFormat, parse_holdings

FORMAT = HoldingsFormat(
    amc_id="kotak",
    parser_id="holdings.kotak",
    version="2",
)


class KotakHoldingsParser:
    """Satisfies `HoldingsParser`."""

    parser_id = FORMAT.parser_id
    version = FORMAT.version
    amc_id = FORMAT.amc_id

    def sniff(self, f: RawFile) -> float:
        """Cheap checks only (§6.1): filename and magic bytes.

        Kotak's published filenames say what the document is and not who
        published it — `ConsolidatedSEBIPortfolioJuly2026.xlsx`,
        `FortnightlyPortfolioJuly312026.xlsx`. So `kotak` is scored when
        present but the document words have to carry the match on their own,
        which is exactly the case §6.1 said would one day earn a content check.
        It has not yet: no other AMC here publishes under those names.
        """
        if f.content[:2] != b"PK":
            return 0.0
        name = f.filename.lower()
        score = 0.0
        if "kotak" in name:
            score += 0.6
        flat = name.replace(" ", "")
        # BOTH of Kotak's own document names. The docstring above named the
        # fortnightly file from the start and the scoring did not reach it:
        # `FortnightlyPortfolioAugust312026.xlsx` scored 0.40 against a 0.50
        # floor, so `route` raised `NoParserMatched` on a genuine Kotak
        # workbook. Nothing noticed while the file arrived by hand, because
        # the hand-downloaded one was always the consolidated SEBI file;
        # automating the fetch (V1-44) is what fetched the other one.
        if "consolidatedsebiportfolio" in flat or "fortnightlyportfolio" in flat:
            score += 0.6
        elif "portfolio" in name:
            score += 0.3
        if name.endswith((".xlsx", ".xls")):
            score += 0.1
        return min(score, 1.0)

    def parse(
        self, f: RawFile, sheet: str | None = None
    ) -> HoldingsParseResult:
        return parse_holdings(f, FORMAT, sheet)
