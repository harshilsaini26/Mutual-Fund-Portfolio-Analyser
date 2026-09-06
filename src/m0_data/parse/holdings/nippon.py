"""Nippon India Mutual Fund. MODULE_0.md §6.1, DECISIONS V1-15.

Verified against `NIMF-MONTHLY-PORTFOLIO-31-July-26.xls`, 1.3 MB, fetched from
`mf.nipponindiaim.com`:

    row 1  RLMF001 | Nippon India Growth Mid Cap Fund | ... | Index
    row 2  Monthly Portfolio Statement as on July 31,2026
    row 4  <code> | ISIN | Name of the Instrument | Industry / Rating |
           Quantity | Market/Fair Value\\n( Rs. in Lacs) | % to NAV | YIELD

The third AMC, and the one that answered whether V1-10's rules generalise or
were tuned to a sample of two. Most of them did. Three things it needed that
the first two did not:

1. **108 schemes in one workbook, one per sheet.** HDFC ships a file per
   scheme, ICICI a ZIP of files per scheme; this is the third packaging model
   in three AMCs. Reading every sheet merges 108 portfolios — measured at
   **+222,869.8%** of the last sheet's total, which the reconciliation guard
   refuses outright. `parse_holdings(sheet=...)` selects one, and the manifest
   entry names it.
2. **A second table after the GRAND TOTAL, with the same column shape.** See
   `_stage_row`'s `after_total` branch — this is the one that mattered.
3. **A leading internal-code column** (`RLMF001`, `FEBA02`) that no other AMC
   has. Costs nothing: columns are located by header text, never by position
   (V0-23), and the code column's header is blank so nothing maps to it.

Already handled by rules written for the first two, which is the useful result:

- `% to NAV` is a **fraction** (`0.029` for 2.9%), like ICICI and unlike HDFC.
  `detect_pct_scale` reads it off the GRAND TOTAL row's own `1`.
- The as-on date is **month-first** (`July 31,2026`), like ICICI. `AS_ON_RE`
  learned that in V1-10.
- Sections are bare headings above their numbers, like HDFC, so
  `_demote_subtotals` finds nothing to demote and correctly leaves the TREPS,
  cash-margin and net-current-asset rows as the positions they are.
- `Subtotal` and `Total` rows are caught by `TOTAL_ROW` unchanged.

**The extension lies and so does the Content-Type.** The file is served as
`.xls` with `application/vnd.ms-excel` and is a **ZIP** — a real .xlsx. openpyxl
refuses it by filename, which is why `parse_holdings` hands it a `BytesIO` with
no name; `sniff` checks the magic bytes for the same reason.
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
