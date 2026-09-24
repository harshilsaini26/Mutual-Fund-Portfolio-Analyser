"""AMFI's scheme master (sources.yaml S2): each ISIN's fund and launch date.

`DownloadSchemeData_Po.aspx?mf=0`, one row per share class. Two columns matter:
"Scheme Name" names the FUND, identically for every plan of it, and "Launch
Date" is when it launched. The last column runs the payout and reinvestment
ISINs together with no separator (`INF174K01KI5INF174K01KJ3`), so ISINs are
read out of it by shape rather than split on a delimiter.
"""

from __future__ import annotations

import csv
import re
from datetime import date

from src.m0_data.normalise.numbers import CoercionError, to_date
from src.m0_data.parse.base import ParseFailed

#: Every column up to and including the ISINs, which are read by position.
HEADER = ["AMC", "Code", "Scheme Name", "Scheme Type", "Scheme Category",
          "Scheme NAV Name", "Scheme Minimum Amount", "Launch Date", "Closure Date",
          "ISIN Div Payout/ ISIN GrowthISIN Div Reinvestment"]
ISIN = re.compile(r"IN[A-Z0-9]{10}")


def parse_scheme_master(text: str) -> dict[str, tuple[str, date | None]]:
    """ISIN -> (fund name, launch date). A row with no ISIN has nothing to key.

    An ISIN AMFI lists under two different funds is left out rather than filed
    under whichever row came last: five do today, all old fixed-horizon series.
    """
    rows = csv.reader(text.splitlines())
    header = [cell.strip() for cell in next(rows, [])]
    if header[: len(HEADER)] != HEADER:
        raise ParseFailed(f"scheme master header changed: {header[:10]}")
    out: dict[str, tuple[str, date | None]] = {}
    clashing: set[str] = set()
    for row in rows:
        if len(row) < len(HEADER):
            continue
        entry = (row[2].strip(), _launched(row[7]))
        for isin in ISIN.findall(row[9]):
            if isin in out and out[isin][0] != entry[0]:
                clashing.add(isin)
            out[isin] = entry
    return {isin: v for isin, v in out.items() if isin not in clashing}


def _launched(cell: str) -> date | None:
    """One unreadable date is that fund's unknown launch, not the file's failure."""
    try:
        return to_date(cell.strip() or None)
    except CoercionError:
        return None
