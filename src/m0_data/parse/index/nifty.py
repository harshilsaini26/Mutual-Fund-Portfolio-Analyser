"""NSE Indices Total Return Index history. MODULE_0.md §2.1 source S12, §6.3.

`https://www.niftyindices.com/BackPage/getTotalReturnIndexString` answers a
POST with one row per trading day, newest first:

    {"d": "[{\\"RequestNumber\\": \\"TRI639253533442672968\\",
             \\"Index Name\\": \\"Nifty 50\\",
             \\"Date\\": \\"28 Mar 2024\\",
             \\"TotalReturnsIndex\\": \\"32867.23\\",
             \\"NTR_Value\\": \\"29763.42\\"}, ...]"}

The payload arrives three ways depending on how the endpoint feels: a bare
list, a dict whose `d` is a list, or a dict whose `d` is a JSON *string* that
has to be decoded a second time. All three are handled here rather than in the
fetcher, because which one turns up is a property of the file, and §6.3 rule 1
puts properties of the file in the parser.

**Gross and net are both staged, and only gross is stored.** `TotalReturnsIndex`
reinvests dividends in full; `NTR_Value` reinvests them after withholding tax.
`PLAN.md` §9.4 wants the gross series, and `migrations/014_index.sql` says why
there is no column for the other one. A parser still reports what the file
said -- rule 1 -- so the net value rides along in the staged row and the loader
is what drops it. That way the day someone wants NTR, the parser already has
it and only the schema has to move.

**Dates are read from an explicit month table, not `%b`.** `strptime`'s
abbreviated-month token is locale-sensitive, and invariant 10 requires the same
archived bytes to rebuild the same warehouse on any machine. A German locale
turning "Mar" into a `ValueError` would be a rebuild that fails by geography.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from src.m0_data.parse.base import ParseFailed

PARSER_ID = "index.nifty_tri"
PARSER_VERSION = "1"

#: NSE renders dates as "28 Mar 2024". See the module docstring on `%b`.
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

#: The keys every row must carry. A row missing one is a shape change in the
#: endpoint, not a gap in the data, so it raises rather than being skipped.
REQUIRED = ("Index Name", "Date", "TotalReturnsIndex")


@dataclass(frozen=True)
class StagedIndexLevel:
    """One published index level, verbatim. §6.3 rules 1 and 2."""

    index_name: str
    level_date: date
    level: Decimal
    #: Net of withholding tax. Staged, not stored -- see the module docstring.
    net_level: Decimal | None
    file_id: str
    row_number: int


def _rows(content: bytes) -> list[dict[str, Any]]:
    """The row list, out of whichever envelope this response used."""
    try:
        payload: Any = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ParseFailed(f"{PARSER_ID}: response is not JSON: {exc}") from exc

    if isinstance(payload, dict):
        payload = payload.get("d", payload)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ParseFailed(f"{PARSER_ID}: `d` is not JSON: {exc}") from exc

    if not isinstance(payload, list):
        raise ParseFailed(
            f"{PARSER_ID}: expected a list of rows, got {type(payload).__name__}"
        )
    return payload


def parse_date(text: str) -> date:
    """"28 Mar 2024" -> date(2024, 3, 28)."""
    parts = text.strip().split()
    if len(parts) != 3:
        raise ParseFailed(f"{PARSER_ID}: cannot read date {text!r}")
    day, mon, year = parts
    month = MONTHS.get(mon.lower()[:3])
    if month is None:
        raise ParseFailed(f"{PARSER_ID}: unknown month in {text!r}")
    try:
        return date(int(year), month, int(day))
    except ValueError as exc:
        raise ParseFailed(f"{PARSER_ID}: impossible date {text!r}: {exc}") from exc


def _decimal(raw: Any, field: str, row_number: int) -> Decimal:
    try:
        return Decimal(str(raw).strip().replace(",", ""))
    except (InvalidOperation, AttributeError) as exc:
        raise ParseFailed(
            f"{PARSER_ID}: row {row_number} has unreadable {field} {raw!r}"
        ) from exc


def parse_tri(content: bytes, *, file_id: str = "") -> list[StagedIndexLevel]:
    """Every level in the response, oldest first.

    Sorted on the way out because the endpoint answers newest-first and every
    consumer of a level series wants it the other way. That is ordering, not
    normalisation -- no value is altered.

    Raises rather than returning a partial list (§6.3 rule 3). An index that
    published 250 days and parsed 249 is the silent gap invariant 4 exists to
    forbid, and a level series with a hole computes a return across it without
    complaining.
    """
    staged: list[StagedIndexLevel] = []
    for i, row in enumerate(_rows(content), start=1):
        if not isinstance(row, dict):
            raise ParseFailed(
                f"{PARSER_ID}: row {i} is {type(row).__name__}, not an object"
            )
        missing = [k for k in REQUIRED if k not in row]
        if missing:
            raise ParseFailed(f"{PARSER_ID}: row {i} is missing {missing}")

        net_raw = row.get("NTR_Value")
        staged.append(
            StagedIndexLevel(
                index_name=str(row["Index Name"]).strip(),
                level_date=parse_date(str(row["Date"])),
                level=_decimal(row["TotalReturnsIndex"], "TotalReturnsIndex", i),
                net_level=(
                    _decimal(net_raw, "NTR_Value", i)
                    if net_raw not in (None, "")
                    else None
                ),
                file_id=file_id,
                row_number=i,
            )
        )

    staged.sort(key=lambda s: s.level_date)
    return staged
