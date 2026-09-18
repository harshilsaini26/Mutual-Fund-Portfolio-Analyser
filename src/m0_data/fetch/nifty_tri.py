"""NSE Indices Total Return Index history. MODULE_0.md §2.1 source S12, §5.

The endpoint the site's own historical-data page calls:

    POST https://www.niftyindices.com/BackPage/getTotalReturnIndexString
    {"cinfo": "{'name':'NIFTY 50','startDate':'01-Jan-2024',
                'endDate':'31-Dec-2024','indexName':'NIFTY 50'}"}

That inner value really is a string containing single-quoted pseudo-JSON, and
it really is the shape the server wants -- it is what `IISLComponet.js` builds
before posting. Sending proper nested JSON returns an empty result rather than
an error, which is the failure mode worth knowing about: nothing raises, the
job records zero levels for the year and reports success.

**One year per request, because the server says so.** The page's own script
refuses a wider range with "Please select date range not more than 1 Year"
before it posts, and the endpoint honours the same limit. So a fifteen-year
backfill is fifteen requests per index, at §2.3's one-per-two-seconds.

**Chunks land on calendar years, not on a rolling window from today.** §3.1
archives by content hash, so a request whose boundaries move with the run date
archives a new file every run and the warehouse stops rebuilding byte-identical
(invariant 10). The same reason `amfi_history.year_chunks` does it.

**A browser User-Agent, with the contact in `From:`.** niftyindices drops a
request carrying no User-Agent and answers a browser-shaped one with 200;
DECISIONS V1-05 settled this host by name, and `config/sources.yaml` carries
`user_agent: browser` for it. robots.txt was read before this module was
written: `Allow: /`, with four unrelated pages disallowed and nothing under
`/BackPage`. No challenge is being answered -- there is none to answer.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

TRI_URL = "https://www.niftyindices.com/BackPage/getTotalReturnIndexString"

#: The page posts from its own historical-data screen and the server checks it.
REFERER = "https://www.niftyindices.com/reports/historical-data"

#: NSE's query format is `01-Jan-2024`. Built from this table rather than
#: `strftime("%d-%b-%Y")` because `%b` renders through `LC_TIME`: on a machine
#: with a German locale the request would carry `01-Mrz-2024` and come back
#: empty. Invariant 10 wants the same archived bytes from any machine, and a
#: request that varies by locale cannot deliver that.
MONTH_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def query_date(when: date) -> str:
    """`date(2024, 1, 1)` -> `'01-Jan-2024'`, locale-independently."""
    return f"{when.day:02d}-{MONTH_ABBR[when.month - 1]}-{when.year}"


@dataclass(frozen=True)
class TriChunk:
    """One request: one index over one calendar year (or part of one)."""

    index_name: str
    start: date
    end: date

    @property
    def body(self) -> dict[str, str]:
        """The `cinfo` envelope, exactly as the site's own script builds it."""
        name = self.index_name.upper().strip()
        cinfo = (
            "{'name':'" + name + "'"
            ",'startDate':'" + query_date(self.start) + "'"
            ",'endDate':'" + query_date(self.end) + "'"
            ",'indexName':'" + name + "'}"
        )
        return {"cinfo": cinfo}

    @property
    def json_body(self) -> str:
        return json.dumps(self.body)

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Referer": REFERER,
            "X-Requested-With": "XMLHttpRequest",
        }

    @property
    def label(self) -> str:
        return f"{self.index_name} {self.start:%Y-%m-%d}..{self.end:%Y-%m-%d}"


def year_chunks(index_name: str, start: date, end: date) -> Iterator[TriChunk]:
    """Split a range on calendar-year boundaries, oldest first.

    Never wider than one calendar year, which is also never wider than the
    server's limit. Their script refuses a range whose day DIFFERENCE exceeds
    365; 1 January to 31 December is a difference of 364 in a common year and
    365 in a leap year, so both pass. Verified against the live endpoint rather
    than reasoned about: 2024 returned 249 trading days and 2023 returned 246,
    which is the full NSE calendar for each.
    """
    if end < start:
        raise ValueError(f"end {end} precedes start {start}")
    cursor = start
    while cursor <= end:
        year_end = min(date(cursor.year, 12, 31), end)
        yield TriChunk(index_name=index_name, start=cursor, end=year_end)
        cursor = date(cursor.year + 1, 1, 1)
