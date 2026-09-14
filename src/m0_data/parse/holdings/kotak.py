"""Kotak Mahindra Mutual Fund. MODULE_0.md §6.1, DECISIONS V1-33.

Verified against `Consolidated SEBI Portfolio July 2026` — 119 sheets, one per
scheme, sheet `KPF` being Kotak Pioneer Fund as on 31-Jul-2026. Downloaded by
hand: Kotak's site is behind Radware bot management and the portfolio dropdown
serves a CAPTCHA, which this project does not solve (V1-32). Its backend answers
a plain GET, which is what `fetch/amc_direct.py` uses (V1-44).

Two things differ from the three formats already parsing, and **neither needed
code here** — both are general rules in `holdings/base.py`:

1. **The sheet indents by COLUMN, not by whitespace.** The nesting level IS the
   column index: sections in columns 0 and 1, all 51 holdings in column 2.
   `_name_span` reads the innermost label between a name column and the next
   mapped field, which leaves the other three AMCs byte-identical because each
   of their spans is one column wide.
2. **Section totals are labelled in the Industry column**, not in the name or
   ISIN column that §6.4 matches `TOTAL_ROW` against. Read as holdings, those
   three rows bring the parse to 794,038.80 against a stated 402,258.02.
   `_stray_label` looks outside the name span only when the span is empty.

Note what is NOT here: this sheet writes subtotals BELOW the rows they cover
where ICICI writes them ON the section row, so `_demote_subtotals` never fires.
Two opposite layouts, settled by different rules.

**Sheet selection is required** — a parse with no `sheet` merges 119 portfolios
into one.
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
