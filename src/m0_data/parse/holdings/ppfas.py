"""PPFAS Mutual Fund. MODULE_0.md §6.1, DECISIONS V1-36.

The fifth AMC, and **the fifth `HoldingsFormat` identical to the other four**.
`amc_id` and `parser_id` differ; `columns` and `skip_sheets` have not changed
since HDFC. Every AMC added has contributed a general rule to `holdings/base.py`
and not one has needed a line of per-AMC parsing — what these modules carry is a
`sniff`, which is a filename question, and a label.

That is an argument for collapsing them into one configurable parser routed by
whether the file reconciles against its own stated total rather than by what it
is called. Recorded rather than acted on: it is the shape of the per-sheet work,
not a thing to do while adding a fund.

PPFAS publishes one file per scheme and a consolidated workbook at a stable URL.
Its site returns 403 to an honest agent, like HDFC's CDN (V1-05), and accepts
the browser string S5 already sends. There is no bot challenge.
"""

from __future__ import annotations

from src.m0_data.parse.base import HoldingsParseResult, RawFile
from src.m0_data.parse.holdings.base import HoldingsFormat, parse_holdings

FORMAT = HoldingsFormat(
    amc_id="ppfas",
    parser_id="holdings.ppfas",
    version="1",
)


class PpfasHoldingsParser:
    """Satisfies `HoldingsParser`."""

    parser_id = FORMAT.parser_id
    version = FORMAT.version
    amc_id = FORMAT.amc_id

    def sniff(self, f: RawFile) -> float:
        """Cheap checks only (§6.1): filename and magic bytes.

        PPFAS names its files after itself and after the scheme, which is the
        easy case — `PPFCF_PPFAS_Monthly_Portfolio_Report_July_31_2026.xlsx`
        says both who published it and which fund it is.
        """
        if f.content[:2] != b"PK":
            return 0.0
        name = f.filename.lower()
        score = 0.0
        if "ppfas" in name:
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
