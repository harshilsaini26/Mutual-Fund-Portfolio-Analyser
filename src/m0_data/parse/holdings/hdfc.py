"""HDFC Mutual Fund. MODULE_0.md §6.1.

Verified against `Monthly HDFC Flexi Cap Fund - 31 July 2026.xlsx`:

    row 2  Portfolio as on 31-Jul-2026
    row 5  ISIN | Coupon (%) | Name Of the Instrument | Industry+ /Rating |
           Quantity | Market/ Fair Value (Rs. in Lacs.) | % to NAV

Market value is in **lakhs**, stated in the header — §7.2's 100x path, read
rather than assumed. Derivatives are on a second sheet in prose form and are
skipped; they are not a table and pretending otherwise would stage nonsense.
"""

from __future__ import annotations

from src.m0_data.parse.base import HoldingsParseResult, RawFile
from src.m0_data.parse.holdings.base import HoldingsFormat, parse_holdings

FORMAT = HoldingsFormat(
    amc_id="hdfc",
    parser_id="holdings.hdfc",
    version="1",
)


class HdfcHoldingsParser:
    """Satisfies `HoldingsParser`."""

    parser_id = FORMAT.parser_id
    version = FORMAT.version
    amc_id = FORMAT.amc_id

    def sniff(self, f: RawFile) -> float:
        """Cheap checks only (§6.1): filename and magic bytes.

        Deliberately not a hardcoded AMC->parser mapping (§6.2) — AMCs rename
        files and occasionally publish one month in another format, so routing
        on evidence beats routing on a table.
        """
        if f.content[:2] != b"PK":
            return 0.0
        name = f.filename.lower()
        score = 0.0
        if "hdfc" in name:
            score += 0.6
        if "monthly" in name or "portfolio" in name:
            score += 0.3
        if name.endswith((".xlsx", ".xls")):
            score += 0.1
        return min(score, 1.0)

    def parse(self, f: RawFile) -> HoldingsParseResult:
        return parse_holdings(f, FORMAT)
