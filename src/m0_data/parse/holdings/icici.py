"""ICICI Prudential Mutual Fund. MODULE_0.md §6.1, DECISIONS V1-08.

Verified against ICICI Prudential Multi-Asset Fund's disclosure for
31-Jul-2026:

    row 3  Portfolio as on Jul 31,2026
    row 4  Company/Issuer/Instrument Name | ISIN | Coupon | Industry/Rating |
           Quantity | Exposure/Market Value(Rs.Lakh) | % to Nav | Yield ...

Four things differ from HDFC's sheet, and each one corrupts silently rather
than loudly. None of them needed code here — every one is handled by a general
rule in `holdings/base.py`, which is the argument for one configurable parser
rather than five:

1. **Name comes before ISIN.** Columns are located by header text, never by
   position (V0-23), so the order is not this module's problem.
2. **The as-on date is month-first** — `Jul 31,2026`, not `31-Jul-2026`. ICICI's
   workbooks are named for the scheme with no date in them, so §7.5's filename
   fallback cannot save a sheet whose date was not read. `AS_ON_RE` reads both.
3. **`% to Nav` is a fraction**, summing to 1.0 rather than 100 — its own total
   row reads `0.9999999999896085`. `detect_pct_scale` reads the convention off
   that total row rather than assuming one.
4. **The subtotal sits ON the section row**, nested four levels deep, where
   HDFC puts a bare heading above the numbers. `_demote_subtotals` settles it
   arithmetically: a row whose value equals the sum of the rows beneath it is
   their total. Reading these rows as holdings gives 2.887x the true portfolio,
   and it still normalises to 100% — see V1-08.

Also on the sheet, and deliberately not special-cased here: interest-rate swaps
disclosed at NOTIONAL value under their own heading. Notional is not market
value and must never enter the portfolio total. The Multi-Asset file carries
none; when a scheme that holds them is added, §10's V2 and the reconciliation
against the file's own total are what will catch it, and the fix belongs in
classification rather than in this config.

**The ZIP is not handled yet.** ICICI publishes 146 per-scheme workbooks in one
25 MB archive, and §6.5's treatment — archive the ZIP as one `raw_file`, stage
each member under its member name — is a separate slice. This parser takes one
extracted member.
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
