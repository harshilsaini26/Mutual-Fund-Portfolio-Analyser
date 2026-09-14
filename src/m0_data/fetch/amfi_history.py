"""AMFI's historical NAV export. MODULE_0.md §5, DECISIONS OPEN-07.

OPEN-07 sets the depth: full history for schemes the user holds,
earliest-transaction-onward otherwise, and 31-Jan-2018 regardless because equity
grandfathering needs that day's NAV. A one-time overnight job; the daily leading
edge is `jobs/fetch_nav.py`.

Filtering by AMC is a 23x reduction (~43 KB against ~1.0 MB for one day) and a
multi-year range costs one round trip rather than a thousand. Ranges are still
chunked by year, for reasons unrelated to size: each chunk is its own
`raw_file`, so a failure part-way keeps what succeeded and a re-run skips the
chunks whose bytes are unchanged.

**An unknown AMC code returns HTTP 200 with an HTML error page**, not a 404 and
not an empty file — so anything treating "no rows parsed" as "published
nothing" records zero NAVs and reports success. `parse_navall` raises on it and
this module lets that raise.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"

#: The date format AMFI's query parameters take, which is also the format its
#: files print. Not ISO — passing ISO returns the HTML error page.
QUERY_DATE = "%d-%b-%Y"

#: 31-Jan-2018. Equity units acquired before this date use
#: `max(actual, min(FMV_31Jan2018, sale_price))` as their cost basis, so
#: without this day's NAV the gain cannot be computed and the consumption is
#: marked `confidence=low`. OPEN-07 makes it mandatory for that reason.
GRANDFATHER_DATE = date(2018, 1, 31)


@dataclass(frozen=True)
class HistoryChunk:
    """One request: an AMC (or all of them) over one date range."""

    start: date
    end: date
    amfi_amc_code: str | None = None

    @property
    def params(self) -> dict[str, str]:
        params = {
            "frmdt": self.start.strftime(QUERY_DATE),
            "todt": self.end.strftime(QUERY_DATE),
        }
        if self.amfi_amc_code:
            params["mf"] = self.amfi_amc_code
        return params

    @property
    def label(self) -> str:
        who = self.amfi_amc_code or "all"
        return f"mf={who} {self.start:%Y-%m-%d}..{self.end:%Y-%m-%d}"


def year_chunks(
    start: date, end: date, amfi_amc_code: str | None = None
) -> Iterator[HistoryChunk]:
    """Split a range on calendar-year boundaries, oldest first.

    Calendar years rather than fixed windows so a re-run produces byte-identical
    requests: a rolling 365-day window would shift with the run date and every
    chunk would archive as a new file.
    """
    if end < start:
        raise ValueError(f"end {end} precedes start {start}")
    cursor = start
    while cursor <= end:
        year_end = min(date(cursor.year, 12, 31), end)
        yield HistoryChunk(cursor, year_end, amfi_amc_code)
        cursor = date(cursor.year + 1, 1, 1)


def plan_backfill(
    amfi_amc_codes: list[str],
    start: date,
    today: date,
) -> list[HistoryChunk]:
    """The chunks OPEN-07 asks for: year by year, per AMC, oldest first.

    `start` is clamped to no later than 31-Jan-2018, which is how the
    grandfathering NAV is guaranteed to fall inside the fetched range rather
    than being requested as a special case. A lot bought before 2018 needs that
    day's NAV even when the imported ledger begins after it.

    Held schemes are fetched from the earliest date given; OPEN-07's
    "earliest-transaction-onward for all others" is the same call with a later
    start and no AMC filter, and is left to whoever needs an unheld scheme —
    nothing in V0 does.
    """
    begin = min(start, GRANDFATHER_DATE)
    chunks: list[HistoryChunk] = []
    for code in sorted(amfi_amc_codes):
        chunks.extend(year_chunks(begin, today, code))
    return chunks
