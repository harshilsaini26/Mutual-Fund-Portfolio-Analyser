"""mfapi.in scheme NAV history. MODULE_0.md §2.2, DECISIONS V1-19.

One scheme's entire NAV history as JSON, keyed on the AMFI scheme code:

    {"meta": {"scheme_code": 118955, "scheme_name": "HDFC Flexi Cap...",
              "fund_house": "HDFC Mutual Fund", ...},
     "data": [{"date": "04-09-2026", "nav": "2271.32400"}, ...],
     "status": "SUCCESS"}

**Why this exists when AMFI already publishes history.** AMFI's export is keyed
on the AMC code, so fetching three funds means fetching three whole fund houses
— 3,118,359 NAV rows to serve the 5,924 that belong to schemes anyone holds.
mfapi is per scheme, which AMFI's endpoint does not offer.

**A mirror, treated as one.** AMFI remains the source of record (V1-02).
Measured against our AMFI history for HDFC Flexi Cap: 2,117 overlapping dates,
one disagreement — `2111.846` against `2111.779`. One in two thousand, and
exactly the size that moves an XIRR without moving anything visible, so rows
from here only ever FILL GAPS (`load_navs_where_absent`).

**The identity checks are the load-bearing part.** The payload says which scheme
it is about twice, and both are checked. The ISIN check is the stronger one
because it is the same key `scheme_id` is. Accepting a mismatch would file one
fund's NAV history under another's ISIN: V0-05's error class, ~10%/year between
a Direct and a Regular plan, invisible downstream.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from src.m0_data.parse.nav.amfi import StagedNav

PARSER_ID = "nav.mfapi"
PARSER_VERSION = "1"

#: mfapi renders dates as DD-MM-YYYY.
_DATE_FORMAT = "%d-%m-%Y"


class MfapiParseError(ValueError):
    """Malformed payload, wrong scheme, or a NAV that will not coerce."""


@dataclass
class MfapiParseResult:
    """One scheme's history, plus what the payload said about itself."""

    scheme_id: str
    amfi_code: str
    scheme_name: str
    fund_house: str
    navs: list[StagedNav] = field(default_factory=list)
    warnings: list[tuple[int, str]] = field(default_factory=list)

    @property
    def first(self) -> date | None:
        return min((n.nav_date for n in self.navs), default=None)

    @property
    def last(self) -> date | None:
        return max((n.nav_date for n in self.navs), default=None)


def parse_mfapi(content: bytes, scheme_id: str, amfi_code: str) -> MfapiParseResult:
    """Parse one scheme's history. Raises rather than returning a partial result.

    `scheme_id` is the ISIN the rows will be stored under and `amfi_code` is what
    was requested; both are passed in because the payload carries only the code,
    and the mapping between them belongs to the warehouse (`scheme.amfi_code`).
    """
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise MfapiParseError(f"{amfi_code}: not JSON ({exc})") from exc

    if not isinstance(payload, dict):
        raise MfapiParseError(f"{amfi_code}: payload is not an object")

    status = str(payload.get("status", "")).upper()
    if status and status != "SUCCESS":
        raise MfapiParseError(f"{amfi_code}: status {status!r}")

    meta = payload.get("meta")
    if not isinstance(meta, dict):
        raise MfapiParseError(f"{amfi_code}: no meta block")

    # The check that matters. A payload for a different scheme, stored under
    # this ISIN, is V0-05 with no symptom: plausible NAVs, wrong fund.
    returned = str(meta.get("scheme_code", "")).strip()
    if returned != str(amfi_code).strip():
        raise MfapiParseError(
            f"asked for scheme_code {amfi_code}, payload says {returned!r} "
            f"({meta.get('scheme_name')!r}); refusing to file it under {scheme_id}"
        )

    # The payload also states its own ISINs, which is a SECOND and stronger
    # identity check than the code: it is the same key our `scheme_id` is, so a
    # mismatch is caught without trusting AMFI's code mapping at all. Only
    # enforced when present — older entries omit it.
    claimed = {
        str(meta.get(k)).strip()
        for k in ("isin_growth", "isin_div_reinvestment")
        if meta.get(k)
    }
    if claimed and scheme_id not in claimed:
        raise MfapiParseError(
            f"scheme_code {amfi_code} resolves to ISIN(s) {sorted(claimed)}, "
            f"not {scheme_id}; refusing to file one fund's history under another"
        )

    rows = payload.get("data")
    if not isinstance(rows, list):
        raise MfapiParseError(f"{amfi_code}: no data array")

    result = MfapiParseResult(
        scheme_id=scheme_id,
        amfi_code=str(amfi_code),
        scheme_name=str(meta.get("scheme_name") or ""),
        fund_house=str(meta.get("fund_house") or ""),
    )

    seen: set[date] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            result.warnings.append((index, f"row is {type(row).__name__}, not object"))
            continue
        raw_date, raw_nav = str(row.get("date", "")), str(row.get("nav", "")).strip()
        try:
            on = datetime.strptime(raw_date, _DATE_FORMAT).date()
        except ValueError:
            result.warnings.append((index, f"unparseable date {raw_date!r}"))
            continue
        try:
            # Through `str`, never `float` — the value arrives as a string and
            # must stay one all the way into the Decimal.
            nav = Decimal(raw_nav)
        except (InvalidOperation, ValueError):
            result.warnings.append((index, f"unparseable nav {raw_nav!r} on {on}"))
            continue
        if not nav.is_finite() or nav <= 0:
            # A zero NAV is not a price. mfapi carries a few for dates before a
            # scheme was priced; storing them would make a return series that
            # starts with a division by zero.
            #
            # `is_finite` is checked FIRST because `nav <= 0` cannot catch a
            # NaN: every comparison against NaN is False, so `Decimal("NaN")`
            # passed this guard and was stored as a price. The Decimal is built
            # from the JSON string directly here rather than through
            # `to_decimal`, so it does not inherit that module's guard.
            result.warnings.append((index, f"non-positive nav {nav} on {on}"))
            continue
        if on in seen:
            result.warnings.append((index, f"duplicate date {on}"))
            continue
        seen.add(on)
        result.navs.append(StagedNav(scheme_id=scheme_id, nav_date=on, nav=nav))

    if not result.navs:
        raise MfapiParseError(
            f"{amfi_code}: no usable NAV rows in {len(rows)} entries"
        )
    result.navs.sort(key=lambda n: n.nav_date)
    return result
