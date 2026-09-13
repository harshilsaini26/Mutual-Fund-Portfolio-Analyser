"""AMFI's scheme-wise average AUM. MODULE_0.md §2.1's S3, DECISIONS V1-49.

The source §10's V2 has been waiting for. AMFI's `aum-data/average-aum` page is
a Next.js app, and — the third time this argument has paid (V1-44 for Kotak and
ICICI) — its file list arrives as JSON from an endpoint that is neither
authenticated nor challenged. robots.txt disallows only `/admin/`, `/login/`
and `/search/`.

Three calls, because the page's own dropdowns are populated in that order:

    /api/average-aum-schemewise?strType=Categorywise&MF_ID=0
        -> the financial years, newest first, `id` 1 upward

    /api/average-aum-schemewise?fyId=<id>&strType=Categorywise&MF_ID=0
        -> the quarters published within that year

    /api/average-aum-schemewise?strType=Categorywise&fyId=<id>&periodId=<id>&MF_ID=0
        -> ~8,500 schemes with their AAUM

## It is per PLAN, and the scheme is the sum of its plans

`HDFC Flexi Cap Fund - Growth Option - Direct Plan` reports Rs 34,740 Cr where
the fund's portfolio is Rs 113,606 Cr. The difference is not an error: AMFI
publishes one row per share class, and V1-37 established that a disclosure
describes the SCHEME — every plan of which holds one pool of assets. So a
scheme's AUM is the sum over its family, and comparing a disclosure against any
single plan's figure would fail by a factor of three.

`AMFI_Code` is what makes that safe to do: it joins `scheme.amfi_code`
directly, 8,448 of 8,545 rows (98.9%), with no name matching anywhere.

## It is an AVERAGE over a QUARTER

The field is `AverageAumForTheMonth` and the period is `April - June 2026`.
Against the two funds whose portfolios are loaded, that average sits **-10.4%**
(HDFC Flexi Cap) and **-4.6%** (PPFAS Flexi Cap) from their August portfolios —
two months of market movement and flows, which is real and is not a defect in
either number.

§10 gives V2 a ±3% tolerance, which assumes a same-date balance. Storing this
as though it were one would quarantine both of those funds. `basis` travels
with the figure so the check can pick a tolerance that matches what it is
comparing; see `validate/checks.py`.

## Units

Lakhs, as AMFI publishes everywhere. Asserted here rather than read — no field
states it — and the assertion has a witness: a scheme's family sum lands within
a few percent of its own disclosed portfolio total, which it could not do if
the scale were wrong by 100x. `AAUM_UNIT` is carried as a label and
`normalise/units.py` applies it, the same discipline §6.3 rule 1 puts on every
parser.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

BASE = "https://www.amfiindia.com/api/average-aum-schemewise"

#: `MF_ID=0` is every fund house at once. The page sends a specific id when a
#: house is chosen from its dropdown; nothing here needs that.
ALL_HOUSES = 0

#: AMFI publishes AAUM in lakhs. See the module docstring for the witness.
AAUM_UNIT = "lakh"

#: What `scheme_aum.basis` records for anything loaded from here.
BASIS = "quarterly_average"

#: The figure to take. AMFI reports two columns; this is the one that counts a
#: fund's own assets — the other (`FundOfFundsDomestic`) is the slice invested
#: in other domestic schemes, and adding it would double-count the industry.
AAUM_FIELD = "ExcludingFundOfFundsDomesticButIncludingFundOfFundsOverseas"

MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        start=1,
    )
}


class AumPayloadError(ValueError):
    """The response did not have the shape this reader was written against.

    Raised rather than returning nothing, because "AMFI published no schemes"
    and "the endpoint moved" are different facts and a caller handed an empty
    list cannot tell them apart.
    """


@dataclass(frozen=True)
class SchemeAaum:
    """One share class's average AUM, exactly as published."""

    amfi_code: str
    scheme_name: str
    amc_name: str
    #: In `AAUM_UNIT`. Not converted here — §6.3 rule 1.
    aaum_raw: Decimal


def years_url() -> str:
    return f"{BASE}?strType=Categorywise&MF_ID={ALL_HOUSES}"


def periods_url(fy_id: int) -> str:
    return f"{BASE}?fyId={fy_id}&strType=Categorywise&MF_ID={ALL_HOUSES}"


def data_url(fy_id: int, period_id: int) -> str:
    return (
        f"{BASE}?strType=Categorywise&fyId={fy_id}"
        f"&periodId={period_id}&MF_ID={ALL_HOUSES}"
    )


def parse_years(payload: bytes) -> list[tuple[int, str]]:
    """`(id, label)` per financial year, in the order AMFI lists them."""
    blob = _json(payload)
    rows = blob.get("data") if isinstance(blob, dict) else None
    if not isinstance(rows, list) or not rows:
        raise AumPayloadError("no financial years in the response")
    return [
        (int(r["id"]), str(r["financial_year"]))
        for r in rows
        if isinstance(r, dict) and "id" in r and "financial_year" in r
    ]


def parse_periods(payload: bytes) -> list[tuple[int, str]]:
    """`(id, label)` per quarter published within a financial year."""
    blob = _json(payload)
    data = blob.get("data") if isinstance(blob, dict) else None
    rows = data.get("periods") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise AumPayloadError("no periods in the response")
    return [
        (int(r["id"]), str(r["period"]))
        for r in rows
        if isinstance(r, dict) and "id" in r and "period" in r
    ]


def parse_aaum(payload: bytes) -> list[SchemeAaum]:
    """Every share class the payload names, verbatim.

    A row whose AAUM will not parse is SKIPPED rather than zeroed: zero is a
    real AUM and a scheme that reported one would be indistinguishable from a
    scheme whose figure we could not read. A skipped row simply has no AUM on
    record, which V2 already knows how to say.
    """
    blob = _json(payload)
    houses = blob.get("data") if isinstance(blob, dict) else None
    if not isinstance(houses, list) or not houses:
        raise AumPayloadError("no fund houses in the response")

    out: list[SchemeAaum] = []
    for house in houses:
        if not isinstance(house, dict):
            continue
        amc_name = str(house.get("Mfname") or "")
        for scheme in house.get("schemes") or []:
            if not isinstance(scheme, dict):
                continue
            code = scheme.get("AMFI_Code")
            figures = scheme.get("AverageAumForTheMonth")
            if code is None or not isinstance(figures, dict):
                continue
            raw = _decimal(figures.get(AAUM_FIELD))
            if raw is None:
                continue
            out.append(
                SchemeAaum(
                    amfi_code=str(code).strip(),
                    scheme_name=str(scheme.get("SchemeNAVName") or "").strip(),
                    amc_name=amc_name,
                    aaum_raw=raw,
                )
            )
    if not out:
        raise AumPayloadError("the response named fund houses but no schemes")
    return out


def quarter_end(label: str) -> date:
    """`April - June 2026` -> 2026-06-30, the last day the average covers.

    The END of the window, not its midpoint. `scheme_aum.as_of_date` is what
    `aum_for` compares against a disclosure date, and a quarterly average dated
    at its midpoint would look fresher than the data it summarises.
    """
    # Named by code point: RUF001 rejects the literals, and AMFI has
    # spelled this separator all three ways over the years.
    text = label.replace(chr(0x2013), "-").replace(chr(0x2014), "-")
    parts = [p.strip() for p in text.split("-")]
    if len(parts) != 2:
        raise AumPayloadError(f"period {label!r} is not `Month - Month YYYY`")

    # BOTH halves are checked, though only the second is used. The first is
    # what says this label is still the shape we think it is: `Smarch - June
    # 2026` would otherwise yield a confident 2026-06-30, and a period label
    # whose first month stopped being a month is a format change, not a date.
    if MONTHS.get(parts[0].split()[0].lower() if parts[0].split() else "") is None:
        raise AumPayloadError(f"period {label!r} does not start with a month")

    tail = parts[1].split()
    if len(tail) != 2 or not tail[1].isdigit():
        raise AumPayloadError(f"period {label!r} does not end in `Month YYYY`")
    month = MONTHS.get(tail[0].lower())
    if month is None:
        raise AumPayloadError(f"period {label!r} names no month this reader knows")

    year = int(tail[1])
    first_of_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return date.fromordinal(first_of_next.toordinal() - 1)


def _json(payload: bytes) -> Any:
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AumPayloadError(f"response is not JSON: {exc}") from exc


def _decimal(value: Any) -> Decimal | None:
    """Through `str`, so the published text is what binds (invariant 1)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
