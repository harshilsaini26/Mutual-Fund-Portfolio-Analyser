"""Nippon India Mutual Fund. MODULE_0.md §6.1, DECISIONS V1-15.

Verified against `NIMF-MONTHLY-PORTFOLIO-31-July-26.xls`, 1.3 MB.

The third AMC, and the one that answered whether V1-10's rules generalise or
were tuned to a sample of two. Most did. Three things it needed:

1. **108 schemes in one workbook, one per sheet** — the third packaging model in
   three AMCs. Reading every sheet merges 108 portfolios, measured at +222,869.8%
   of the last sheet's total. `parse_holdings(sheet=...)` selects one.
2. **A second table after the GRAND TOTAL, with the same column shape.** See
   `_stage_row`'s `after_total` branch — the one that mattered.
3. **A leading internal-code column** no other AMC has. Costs nothing: columns
   are located by header text (V0-23) and its header is blank.

Already handled by rules written for the first two, which is the useful result:
the fractional `% to NAV` (read off the GRAND TOTAL row), the month-first
as-on date, bare section headings that `_demote_subtotals` correctly leaves
alone, and `TOTAL_ROW` catching `Subtotal` unchanged.

**The extension lies and so does the Content-Type.** Served as `.xls` with
`application/vnd.ms-excel`, it is a ZIP — a real .xlsx. openpyxl refuses it by
filename, so `parse_holdings` hands it a nameless `BytesIO` and `sniff` checks
the magic bytes.
"""

from __future__ import annotations

from src.m0_data.parse.base import HoldingsParseResult, RawFile
from src.m0_data.parse.holdings.base import HoldingsFormat, parse_holdings

FORMAT = HoldingsFormat(
    amc_id="nippon",
    parser_id="holdings.nippon",
    version="1",
)


class NipponHoldingsParser:
    """Satisfies `HoldingsParser`."""

    parser_id = FORMAT.parser_id
    version = FORMAT.version
    amc_id = FORMAT.amc_id

    def sniff(self, f: RawFile) -> float:
        """Cheap checks only (§6.1): filename and magic bytes.

        `NIMF` is how Nippon names the file; `nippon` covers a rename. The
        magic-byte check earns its keep here more than anywhere else — this
        file claims to be a legacy `.xls` in both its extension and its
        Content-Type, and is neither.
        """
        if f.content[:2] != b"PK":
            return 0.0
        name = f.filename.lower()
        score = 0.0
        if "nimf" in name or "nippon" in name:
            score += 0.6
        if "portfolio" in name or "monthly" in name:
            score += 0.3
        if name.endswith((".xlsx", ".xls")):
            score += 0.1
        return min(score, 1.0)

    def parse(
        self, f: RawFile, sheet: str | None = None
    ) -> HoldingsParseResult:
        return parse_holdings(f, FORMAT, sheet)
