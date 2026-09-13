"""Asking an AMC's own backend what it has published. DECISIONS V1-44.

V1-03 recorded that discovery could not be automated because every AMC's
disclosure page is JavaScript-rendered, and drew from that the conclusion that
finding a link needs a browser. **The conclusion was backwards.** A page that
renders its file list in JavaScript is a page whose file list arrives as JSON,
and the endpoint it arrives from is usually neither authenticated nor guarded:

    Kotak   GET  java17vlbapi.kotakmf.com/kotakapi/forms/user/v1/getsubheaderList/417
    ICICI   POST apps.digital.icicipruamc.com/nms/v1/downloads/files

Kotak's is the pointed case. V1-32 recorded three dead ends and stopped at a
Radware CAPTCHA, correctly -- this project does not solve those. But the CAPTCHA
guards the *portfolio dropdown on the website*, and the backend the dropdown
calls answers a plain GET with 307 entries, 198 of them portfolios reaching
back to April 2013. Nothing here
defeats a bot check; it declines to visit the page that has one.

That archive is the other reason this tier matters. The coverage tier (V1-43)
serves one month and forgets, so a month not fetched is lost; these listings are
historical -- Kotak's monthly archive runs to April 2013 -- so a fund loaded
here can be backfilled.

**This module discovers; it does not download.** `parse_listing` is a pure
function on bytes, which is what keeps every test in this module offline
(`fetch/base.py`'s opening rule), and `jobs/fetch_amc.py` does the fetching
through the same polite, robots-respecting, archive-before-manifest path
everything else uses.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol
from urllib.parse import quote

#: `as on August 31, 2026`, and `Monthly Portfolio Disclosure August 2026`.
#: Two publishers, two spellings, one shape: a month name and a year.
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


class ListingError(ValueError):
    """The listing did not have the shape this adapter was written against.

    Raised rather than returning an empty list, because "the AMC published
    nothing this month" and "the endpoint changed under us" are different
    facts and a caller that cannot tell them apart will report the wrong one.
    """


@dataclass(frozen=True)
class DiscoveredFile:
    """One disclosure an AMC says it has published."""

    amc_id: str
    as_of: date
    url: str
    filename: str
    #: `monthly` or `fortnightly`. SEBI mandates both and they are different
    #: documents; a caller asking for a month's portfolio wants the one whose
    #: as-of date is the month end, whichever heading it sits under.
    kind: str
    #: The publisher's own title, verbatim, so a surprise is diagnosable
    #: without re-fetching.
    title: str


class AmcDiscovery(Protocol):
    amc_id: str

    def request(self) -> dict[str, Any]:
        """The HTTP call to make, as kwargs a caller can hand to httpx."""
        ...

    def parse_listing(self, payload: bytes) -> list[DiscoveredFile]:
        """Every disclosure the listing names. Pure: no network, no clock."""
        ...


def _month_end(year: int, month: int) -> date:
    """The last day of the month, which is the as-of date a monthly discloses.

    ICICI titles its file by month alone (`Monthly Portfolio Disclosure August
    2026`) where Kotak spells the day out, so this is the one place a date is
    computed rather than read. `calendar.monthrange` would do, and the first
    day of the next month less one day is the same answer without the import.
    """
    first_of_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return date.fromordinal(first_of_next.toordinal() - 1)


def _json(payload: bytes, who: str) -> Any:
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ListingError(f"{who}: listing is not JSON: {exc}") from exc


class KotakDiscovery:
    """Kotak Mahindra Mutual Fund.

    The listing is one flat array under `subHeaderList`, each entry carrying a
    human title (`Fortnightly Portfolio as on August 31, 2026`) and a `content`
    path relative to a public CloudFront origin. 307 entries, of which 198 are
    portfolios: 70 monthly reaching back to 2013-04-30, 128 fortnightly.

    **Kotak's monthly disclosure is titled `Consolidated SEBI Portfolio`**, and
    its fortnightly one lands on the month end every other issue. Both are
    month-end portfolios of every scheme; the kind is recorded and the caller
    picks, because they are not the same document and guessing which the user
    meant is not this layer's job.
    """

    amc_id = "kotak"

    API = "https://java17vlbapi.kotakmf.com/kotakapi/forms/user/v1/getsubheaderList/417"
    #: The public origin the SPA itself loads these files from.
    FILES = "https://vatseelabs-s3.kotakmf.com/"
    #: `Portfolios` header, `Consolidated & Fortnightly Portfolio` option.
    OPTION = 51

    #: `... as on August 31, 2026`. The modern spelling, and the only one that
    #: states a day.
    AS_ON = re.compile(r"as on\s+([A-Za-z]+)\s+(\d{1,2}),?\s*(\d{4})", re.I)

    #: `Consolidated SEBI Portfolio - November 2020`, `... for April 2021`.
    #: The pre-2022 spelling, which names a month and no day. 58 of the 100
    #: entries in the test fixture are written this way, and the first draft --
    #: which had only `AS_ON` -- skipped every one of them silently while the
    #: module docstring claimed the archive reached back to September 2020.
    #:
    #: The en dash is named by code point rather than written: RUF001 rejects
    #: the literal, and it is genuinely there in Kotak's own titles.
    MONTH_ONLY = re.compile(
        r"(?:for|[-" + chr(0x2013) + r"])?\s*([A-Za-z]+)\s+(\d{4})\s*$", re.I
    )

    def request(self) -> dict[str, Any]:
        return {
            "method": "GET",
            "url": f"{self.API}?option={self.OPTION}",
            "headers": {"Accept": "application/json"},
        }

    def parse_listing(self, payload: bytes) -> list[DiscoveredFile]:
        blob = _json(payload, "kotak")
        if not isinstance(blob, dict) or "subHeaderList" not in blob:
            raise ListingError("kotak: no `subHeaderList` in the response")
        entries = blob["subHeaderList"]
        if not isinstance(entries, list):
            raise ListingError("kotak: `subHeaderList` is not a list")

        out: list[DiscoveredFile] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("subHeaderTitle") or "")
            content = str(entry.get("content") or "")
            filename = str(entry.get("fileName") or "")
            if not content or not filename:
                continue
            # A portfolio, not a factsheet or an addendum sharing the hub.
            if "portfolio" not in title.lower():
                continue
            kind = "monthly" if "consolidated" in title.lower() else "fortnightly"
            as_of = self._as_of(title, kind)
            if as_of is None:
                continue

            out.append(
                DiscoveredFile(
                    amc_id=self.amc_id,
                    as_of=as_of,
                    # The path carries commas and spaces, and it is a path
                    # rather than a query, so `/` must survive quoting.
                    url=self.FILES + quote(content, safe="/"),
                    filename=filename,
                    kind=kind,
                    title=title,
                )
            )
        if not out:
            raise ListingError("kotak: listing named no portfolios at all")
        return out

    def _as_of(self, title: str, kind: str) -> date | None:
        """The date the title states, or None when it does not state one.

        A day beats a month. Where the title gives only a month, a **monthly**
        disclosure is unambiguous -- SEBI's monthly portfolio is as of the
        month end and there is one per month -- but a **fortnightly** one is
        not: `Fortnightly Portfolio July 2021` could be the 15th or the 31st,
        and there is nothing in the listing to say which. That one is skipped.

        The asymmetry is the point. Filing a portfolio under a date the
        publisher never claimed is the failure mode this whole module is
        careful about, and half a dozen old fortnightlies are not worth it.
        """
        match = self.AS_ON.search(title)
        if match is not None:
            month = MONTHS.get(match.group(1).lower())
            if month is None:
                return None
            try:
                return date(int(match.group(3)), month, int(match.group(2)))
            except ValueError:
                return None

        if kind != "monthly":
            return None
        older = self.MONTH_ONLY.search(title.strip())
        if older is None:
            return None
        month = MONTHS.get(older.group(1).lower())
        if month is None:
            return None
        return _month_end(int(older.group(2)), month)


class IciciDiscovery:
    """ICICI Prudential Mutual Fund.

    A POST whose body names a category id. The response nests the file list at
    `success.data.files`, each with a title (`Monthly Portfolio Disclosure
    August 2026`) and a site-relative `url` -- which contains spaces, so it is
    quoted before use.

    Every file is one 25 MB ZIP of ~146 per-scheme workbooks. MODULE_0.md
    §6.5's member-level staging is still not built (V1-03's note), so this
    discovers the ZIP and a human still extracts the member. That is a smaller
    manual step than finding the link, and an honest one to leave.
    """

    amc_id = "icici"

    BASE = "https://www.icicipruamc.com"
    API = "https://apps.digital.icicipruamc.com/nms/v1/downloads/files"
    CATEGORY_ID = "26a073d7-08d2-4a95-95fa-f83a4ee51e40"

    TITLE = re.compile(r"([A-Za-z]+)\s+(\d{4})\s*$")

    def request(self) -> dict[str, Any]:
        return {
            "method": "POST",
            "url": self.API,
            "headers": {
                "Content-Type": "application/json",
                "Origin": self.BASE,
                "Referer": f"{self.BASE}/",
                "env": "api",
            },
            "json": {
                "categoryId": self.CATEGORY_ID,
                "schemeCategory": "",
                "userType": "Investor",
                "fileType": "All",
                "page": "1",
                "size": "60",
                "filter": [],
                "categoryName": "OTHERS",
            },
        }

    def parse_listing(self, payload: bytes) -> list[DiscoveredFile]:
        blob = _json(payload, "icici")
        try:
            files = blob["success"]["data"]["files"]
        except (TypeError, KeyError) as exc:
            raise ListingError(f"icici: no success.data.files ({exc})") from exc
        if not isinstance(files, list):
            raise ListingError("icici: `files` is not a list")

        out: list[DiscoveredFile] = []
        for entry in files:
            if not isinstance(entry, dict):
                continue
            title = str((entry.get("title") or {}).get("text") or "")
            url = str(entry.get("url") or "")
            if not url or "portfolio" not in title.lower():
                continue
            match = self.TITLE.search(title.strip())
            if match is None:
                continue
            month = MONTHS.get(match.group(1).lower())
            if month is None:
                continue
            as_of = _month_end(int(match.group(2)), month)

            out.append(
                DiscoveredFile(
                    amc_id=self.amc_id,
                    as_of=as_of,
                    url=self.BASE + quote(url, safe="/"),
                    filename=url.rsplit("/", 1)[-1],
                    kind="monthly",
                    title=title,
                )
            )
        if not out:
            raise ListingError("icici: listing named no portfolios at all")
        return out


#: Adding an AMC is one entry, the same as the parser registry.
DISCOVERY: dict[str, AmcDiscovery] = {
    "kotak": KotakDiscovery(),
    "icici": IciciDiscovery(),
}


def for_period(
    files: list[DiscoveredFile], period: str | None, kind: str | None = None
) -> list[DiscoveredFile]:
    """The disclosures for `YYYY-MM`, newest first. No period means the latest.

    Sorted in Python rather than trusting the publisher's order: Kotak's
    listing is roughly newest-first and ICICI's is exactly so, and "roughly" is
    not a property to build a monthly job on.
    """
    chosen = [f for f in files if kind is None or f.kind == kind]
    if period:
        try:
            year, month = (int(part) for part in period.split("-", 1))
        except ValueError as exc:
            raise ValueError(f"period {period!r} is not YYYY-MM") from exc
        chosen = [f for f in chosen if (f.as_of.year, f.as_of.month) == (year, month)]
    chosen.sort(key=lambda f: (f.as_of, f.filename), reverse=True)
    if period or not chosen:
        return chosen
    newest = chosen[0].as_of
    return [f for f in chosen if f.as_of == newest]
