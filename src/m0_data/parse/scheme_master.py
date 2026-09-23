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

from src.m0_data.normalise.numbers import to_date
from src.m0_data.parse.base import ParseFailed

HEADER = ["AMC", "Code", "Scheme Name", "Scheme Type", "Scheme Category",
          "Scheme NAV Name", "Scheme Minimum Amount", "Launch Date"]
ISIN = re.compile(r"IN[A-Z0-9]{10}")


def parse_scheme_master(text: str) -> dict[str, tuple[str, date | None]]:
    """ISIN -> (fund name, launch date). A row with no ISIN has nothing to key."""
    rows = csv.reader(text.splitlines())
    header = [cell.strip() for cell in next(rows, [])]
    if header[: len(HEADER)] != HEADER:
        raise ParseFailed(f"scheme master header changed: {header[:8]}")
    return {
        isin: (row[2].strip(), to_date(row[7].strip() or None))
        for row in rows
        if len(row) >= 10
        for isin in ISIN.findall(row[9])
    }
