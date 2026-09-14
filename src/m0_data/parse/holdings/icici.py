"""ICICI Prudential Mutual Fund. MODULE_0.md §6.1, DECISIONS V1-08.

Verified against ICICI Prudential Multi-Asset Fund's real, untrimmed disclosure
for 31-Jul-2026, byte-identical to the archived download. Until V1-28 this
docstring claimed that verification without having it: the parser had seen only
a 12-row hand-built fixture, and shown the real file it parsed 94% too large
(V1-25).

Four things differ from HDFC's sheet and each corrupts silently. **None needed
code here** — every one is a general rule in `holdings/base.py`, which is the
argument for one configurable parser rather than five:

1. **Name comes before ISIN** — columns are located by header text (V0-23).
2. **The as-on date is month-first.** ICICI's workbooks carry no date in the
   filename, so §7.5's fallback cannot save a sheet whose date was not read.
3. **`% to Nav` is a fraction**, its total row reading `0.9999999999896085`.
   `detect_pct_scale` reads the convention off that row.
4. **The subtotal sits ON the section row**, nesting three levels deep.
   `_demote_subtotals` settles it arithmetically, innermost-first (V1-28). Read
   as holdings these give 1.9378x the true portfolio and still normalise to
   100%, so nothing downstream could catch it.

Also on the sheet: fifteen interest-rate swaps at NOTIONAL value, Rs 105,000
lakh. They are excluded only because they print BELOW `Total Net Assets` and
§6.3 rule 4 drops everything after it — position doing the work, not
classification. Printed above the total, nothing here would catch them: +1.2%
on this book, inside the 2% reconciliation guard.

**The ZIP is not handled yet** (§6.5); this parser takes one extracted member.
"""

from __future__ import annotations

from src.m0_data.parse.base import HoldingsParseResult, RawFile
from src.m0_data.parse.holdings.base import HoldingsFormat, parse_holdings

FORMAT = HoldingsFormat(
    amc_id="icici",
    parser_id="holdings.icici",
    version="1",
)


class IciciHoldingsParser:
    """Satisfies `HoldingsParser`."""

    parser_id = FORMAT.parser_id
    version = FORMAT.version
    amc_id = FORMAT.amc_id

    def sniff(self, f: RawFile) -> float:
        """Cheap checks only (§6.1): filename and magic bytes.

        Kept symmetric with HDFC's rather than peeking at the header row. The
        two formats are distinguishable by header text, but a sniff that opens
        the workbook costs a parse on every candidate parser in the registry,
        and the filename has been sufficient so far. If an AMC ever ships a
        file whose name does not say who published it, that is when this earns
        a content check.
        """
        if f.content[:2] != b"PK":
            return 0.0
        name = f.filename.lower()
        score = 0.0
        if "icici" in name:
            score += 0.6
        if "prudential" in name or "portfolio" in name or "monthly" in name:
            score += 0.3
        if name.endswith((".xlsx", ".xls")):
            score += 0.1
        return min(score, 1.0)

    def parse(
        self, f: RawFile, sheet: str | None = None
    ) -> HoldingsParseResult:
        return parse_holdings(f, FORMAT, sheet)
