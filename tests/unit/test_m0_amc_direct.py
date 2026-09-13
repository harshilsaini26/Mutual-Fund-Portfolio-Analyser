"""AMC listing discovery. MODULE_0.md §5, DECISIONS V1-44.

Both fixtures are real responses captured on 2026-09-13:

    kotak_listing_2026-09-13.json   GET  java17vlbapi.kotakmf.com/.../417?option=51
    icici_listing_2026-09-13.json   POST apps.digital.icicipruamc.com/nms/v1/downloads

ICICI's is whole, 20 files. Kotak's is trimmed from 307 entries to 100 —
every 2026 entry, one sample per older month, and the strays whose titles state
no date — because 307 near-identical rows prove nothing that 100 do not. Every
retained entry is verbatim; nothing was edited to make a test pass.

**Every test here runs offline.** `parse_listing` is a pure function on bytes,
which is `fetch/base.py`'s opening rule: everything that touches the network
lives in the fetch layer so the layer below it can be tested without one.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from src.m0_data.fetch.amc_direct import (
    DISCOVERY,
    DiscoveredFile,
    ListingError,
    for_period,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "m0"
KOTAK = FIXTURES / "kotak_listing_2026-09-13.json"
ICICI = FIXTURES / "icici_listing_2026-09-13.json"


def _kotak() -> list[DiscoveredFile]:
    return DISCOVERY["kotak"].parse_listing(KOTAK.read_bytes())


def _icici() -> list[DiscoveredFile]:
    return DISCOVERY["icici"].parse_listing(ICICI.read_bytes())


class TestKotak:
    def test_it_finds_both_kinds_and_they_are_kept_apart(self) -> None:
        """SEBI mandates a monthly and a fortnightly and they differ.

        Kotak titles its MONTHLY disclosure `Consolidated SEBI Portfolio`,
        which is not a word either document's name would suggest, so the kind
        is read from the title rather than assumed from the endpoint.
        """
        files = _kotak()
        kinds = {f.kind for f in files}
        assert kinds == {"monthly", "fortnightly"}

        monthly = [f for f in files if f.kind == "monthly"]
        assert all("consolidated" in f.title.lower() for f in monthly)

    def test_the_august_fortnightly_resolves_to_a_fetchable_url(self) -> None:
        """The file this tier was built to reach: Kotak's own workbook, whose
        website dropdown answers with a CAPTCHA (V1-32).

        The content path carries commas and spaces and must be quoted, but it
        is a PATH, so the separators have to survive quoting.
        """
        august = [
            f
            for f in _kotak()
            if f.as_of == date(2026, 8, 31) and f.kind == "fortnightly"
        ]
        assert len(august) == 1
        found = august[0]

        assert found.filename == "FortnightlyPortfolioAugust312026.xlsx"
        assert found.url.startswith("https://vatseelabs-s3.kotakmf.com/FAD/Portfolios/")
        assert found.url.endswith(".xlsx")
        assert " " not in found.url and "," not in found.url

    def test_the_older_title_format_is_read_too(self) -> None:
        """`Consolidated SEBI Portfolio - November 2020` states no day.

        The first draft matched only `as on <Month> <D>, <YYYY>` and skipped
        every pre-2022 entry in silence while the module docstring advertised
        an archive back to 2020. A monthly disclosure with only a month named
        is unambiguous — SEBI's is as of the month end, one per month.
        """
        monthly = [f for f in _kotak() if f.kind == "monthly"]
        old = [f for f in monthly if f.as_of.year < 2022]

        assert old, "no pre-2022 monthly survived the parse"
        assert all(f.as_of.day >= 28 for f in old), "an old monthly is not month-end"

    def test_a_fortnightly_without_a_day_is_refused_not_guessed(self) -> None:
        """The asymmetry, and the reason it is not an oversight.

        `Fortnightly Portfolio July 2021` could be the 15th or the 31st and
        the listing does not say. Filing it under either would put a portfolio
        on a date the publisher never claimed — the exact failure this module
        is careful about — so it is dropped.
        """
        entries = json.loads(KOTAK.read_text(encoding="utf-8"))["subHeaderList"]
        ambiguous = [
            e["subHeaderTitle"]
            for e in entries
            if "fortnightly" in (e.get("subHeaderTitle") or "").lower()
            and "as on" not in (e.get("subHeaderTitle") or "").lower()
        ]
        assert ambiguous, "the fixture no longer carries the case being tested"

        kept = {f.title for f in _kotak()}
        assert not (set(ambiguous) & kept)

    def test_the_monthly_archive_goes_back_years(self) -> None:
        """The tier's advantage over the aggregator, asserted rather than said.

        V1-43's coverage tier serves one month and keeps no archive, so a
        month not fetched is lost. This listing is historical, which is what
        makes a backfill possible at all.
        """
        monthly = [f for f in _kotak() if f.kind == "monthly"]
        span = max(f.as_of for f in monthly).year - min(f.as_of for f in monthly).year
        assert span >= 5, f"monthly archive spans only {span} years"


class TestIcici:
    def test_it_finds_the_monthly_zips(self) -> None:
        files = _icici()
        assert files
        assert all(f.kind == "monthly" for f in files)
        assert all(f.filename.endswith(".zip") for f in files)

    def test_a_month_only_title_becomes_the_month_end(self) -> None:
        """`Monthly Portfolio Disclosure August 2026` names no day, and the
        as-of date of a monthly portfolio is the last day of its month."""
        august = [f for f in _icici() if f.as_of == date(2026, 8, 31)]
        assert len(august) == 1
        assert "August 2026" in august[0].title

    def test_february_is_not_assumed_to_have_thirty_days(self) -> None:
        """`_month_end` is arithmetic on the first of the next month, so this
        is really a guard against someone replacing it with `day=30`."""
        febs = [f for f in _icici() if f.as_of.month == 2]
        assert febs, "the fixture carries no February"
        assert all(f.as_of.day in (28, 29) for f in febs)

    def test_the_url_is_absolute_and_its_spaces_are_quoted(self) -> None:
        """ICICI's `url` is site-relative and contains spaces
        (`/downloads/Files/Monthly Portfolio Disclosures/...`)."""
        found = _icici()[0]
        assert found.url.startswith("https://www.icicipruamc.com/")
        assert " " not in found.url
        assert "%20" in found.url


class TestForPeriod:
    def test_no_period_gives_only_the_newest_as_of(self) -> None:
        """A monthly job wants what is out now, not a mixed bag of everything.

        Newest as-of, not newest entry: Kotak's listing is roughly
        newest-first and ICICI's is exactly so, and "roughly" is not something
        to schedule a job against.
        """
        latest = for_period(_kotak(), None)
        assert latest
        assert len({f.as_of for f in latest}) == 1
        assert latest[0].as_of == max(f.as_of for f in _kotak())

    def test_a_period_selects_that_month_only(self) -> None:
        july = for_period(_kotak(), "2026-07")
        assert july
        assert all((f.as_of.year, f.as_of.month) == (2026, 7) for f in july)

    def test_a_kind_narrows_further(self) -> None:
        """July 2026 has a monthly AND a month-end fortnightly. A caller that
        means one of them has to be able to say so."""
        july = for_period(_kotak(), "2026-07")
        assert {f.kind for f in july} == {"monthly", "fortnightly"}

        only = for_period(_kotak(), "2026-07", kind="monthly")
        assert only and all(f.kind == "monthly" for f in only)

    def test_a_month_with_nothing_published_is_empty_not_an_error(self) -> None:
        """Distinguished from a broken endpoint, which raises. The caller
        reports them differently and cannot if the layer conflates them."""
        assert for_period(_kotak(), "1999-01") == []

    def test_a_malformed_period_raises(self) -> None:
        with pytest.raises(ValueError, match="YYYY-MM"):
            for_period(_kotak(), "August 2026")


class TestItRaisesRatherThanReturningNothing:
    """ "The AMC published nothing" and "the endpoint moved" are different
    facts, and a caller handed an empty list cannot tell them apart."""

    @pytest.mark.parametrize("amc_id", sorted(DISCOVERY))
    def test_a_non_json_response_raises(self, amc_id: str) -> None:
        with pytest.raises(ListingError, match="not JSON"):
            DISCOVERY[amc_id].parse_listing(b"<html>bot check</html>")

    def test_kotak_with_the_wrong_shape_raises(self) -> None:
        with pytest.raises(ListingError, match="subHeaderList"):
            DISCOVERY["kotak"].parse_listing(b'{"status":"Success"}')

    def test_icici_with_the_wrong_shape_raises(self) -> None:
        with pytest.raises(ListingError, match=r"success\.data\.files"):
            DISCOVERY["icici"].parse_listing(b'{"success":{"data":{}}}')

    def test_a_listing_of_non_portfolios_raises(self) -> None:
        """An endpoint that starts answering with factsheets has moved, and
        silently discovering nothing would read as a quiet month."""
        payload = json.dumps(
            {
                "subHeaderList": [
                    {
                        "subHeaderTitle": "Factsheet August 2026",
                        "content": "a/b.pdf",
                        "fileName": "b.pdf",
                    },
                ]
            }
        ).encode()
        with pytest.raises(ListingError, match="no portfolios"):
            DISCOVERY["kotak"].parse_listing(payload)


def test_the_registry_and_the_parsers_agree_on_who_exists() -> None:
    """A discovery adapter with no parser fetches files nothing can read.

    Not the reverse: a parser without a fetcher is the normal state for the
    houses still loaded by hand.
    """
    from src.m0_data.parse.holdings.registry import REGISTRY

    parsers = {p.amc_id for p in REGISTRY}
    orphans = sorted(set(DISCOVERY) - parsers)
    assert not orphans, f"discovery adapters with no parser: {orphans}"


def test_a_period_yields_one_as_of_not_both_halves_of_the_month() -> None:
    """V1-47. Kotak publishes a fortnightly on the 15th and again on the month
    end, and the first version returned both — 4.2 MB fetched for the 2.1 MB
    wanted, and then `ingest_inbox` loaded the 15th as an ordinary disclosure
    that `latest_disclosure` and the staleness report would treat as a month's
    portfolio."""
    got = for_period(_kotak(), "2026-08", kind="fortnightly")
    assert [f.as_of for f in got] == [date(2026, 8, 31)]


def test_two_kinds_on_one_date_still_come_back_together() -> None:
    """A monthly and a fortnightly dated the same day are different documents;
    choosing between them is what `--kind` is for."""
    july = for_period(_kotak(), "2026-07")
    assert {f.as_of for f in july} == {date(2026, 7, 31)}
    assert {f.kind for f in july} == {"monthly", "fortnightly"}
