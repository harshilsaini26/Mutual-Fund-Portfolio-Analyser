"""What the warehouse owes. DECISIONS V1-45.

Every test here is offline and every one builds its own warehouse, because the
thing under test is arithmetic on dates and a report that only tells the truth
about one person's data is not tested.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import pytest
from jobs.status import (
    Standing,
    _disclosure_page,
    _index,
    _published_label,
    expected_as_of,
    next_step,
    standings,
)

#: Stated, not read from the clock. After the 10th, so August 2026 is due.
TODAY = date(2026, 9, 13)

SCHEMA = """
CREATE TABLE scheme (scheme_id TEXT PRIMARY KEY, amc_id TEXT, scheme_name TEXT);
CREATE TABLE raw_file (file_id TEXT PRIMARY KEY, parser_id TEXT);
CREATE TABLE holding_disclosure (
  scheme_id TEXT, as_of_date DATE, revision INTEGER, source_file_id TEXT,
  source_tier TEXT NOT NULL DEFAULT 'amc_direct', is_current INTEGER DEFAULT 1
);
"""


def _warehouse(
    rows: list[tuple[str, str, str, str, str]], tmp_path: Path
) -> sqlite3.Connection:
    """`rows` are (scheme_id, amc_id, as_of, parser_id, tier)."""
    conn = sqlite3.connect(tmp_path / "w.db", detect_types=sqlite3.PARSE_DECLTYPES)
    conn.executescript(SCHEMA)
    for i, (scheme_id, amc_id, as_of, parser_id, tier) in enumerate(rows):
        conn.execute(
            "INSERT OR IGNORE INTO scheme VALUES (?,?,?)", (scheme_id, amc_id, scheme_id)
        )
        conn.execute("INSERT OR IGNORE INTO raw_file VALUES (?,?)", (f"f{i}", parser_id))
        conn.execute(
            "INSERT INTO holding_disclosure VALUES (?,?,1,?,?,1)",
            (scheme_id, date.fromisoformat(as_of), f"f{i}", tier),
        )
    conn.commit()
    return conn


class TestExpectedAsOf:
    """SEBI allows ten days after the month end, so a report that demanded
    August's portfolio on the 1st of September would cry wolf for a third of
    every month."""

    @pytest.mark.parametrize(
        ("today", "due"),
        [
            ("2026-09-01", "2026-07-31"),  # August is not late yet
            ("2026-09-10", "2026-07-31"),  # still inside the grace period
            ("2026-09-11", "2026-08-31"),  # now August is due
            ("2026-09-30", "2026-08-31"),
            ("2026-01-05", "2025-11-30"),  # across a year boundary, still grace
            ("2026-01-20", "2025-12-31"),
            ("2026-03-12", "2026-02-28"),  # February is not assumed to be 30 days
        ],
    )
    def test_the_due_disclosure(self, today: str, due: str) -> None:
        assert expected_as_of(date.fromisoformat(today)) == date.fromisoformat(due)


class TestItCountsSchemesNotHouses:
    def test_one_current_scheme_does_not_make_a_house_current(
        self, tmp_path: Path
    ) -> None:
        """THE defect this command shipped with, and the reason for the shape.

        Kotak's August fortnightly carried 21 of its 96 schemes. Grouped with
        `MAX(as_of_date)` the house's newest disclosure was August, so the
        report said `current` while 75 schemes sat on July — and the bigger
        the fund house, the more confident the wrong answer got.
        """
        conn = _warehouse(
            [("A", "kotak_mahindra", "2026-08-31", "holdings.kotak", "amc_direct")]
            + [
                (f"S{i}", "kotak_mahindra", "2026-07-31", "holdings.kotak", "amc_direct")
                for i in range(5)
            ],
            tmp_path,
        )
        (row,) = standings(conn, TODAY)

        assert row.schemes == 6
        assert row.current == 1
        assert row.have == date(2026, 7, 31), "`have` must be the oldest, not the newest"
        assert "5/6 behind" in row.verdict(TODAY)

    def test_a_fully_current_house_says_so(self, tmp_path: Path) -> None:
        conn = _warehouse(
            [
                (f"S{i}", "kotak_mahindra", "2026-08-31", "holdings.kotak", "amc_direct")
                for i in range(3)
            ],
            tmp_path,
        )
        (row,) = standings(conn, TODAY)
        assert row.verdict(TODAY) == "current"

    def test_the_tiers_are_counted_apart(self, tmp_path: Path) -> None:
        """A scheme covered by the aggregator and a scheme covered by the AMC
        are different situations with different remedies, so they are
        different rows rather than one blended one."""
        conn = _warehouse(
            [
                ("A", "axis", "2026-08-31", "holdings.groww", "aggregator"),
                ("B", "axis", "2026-07-31", "holdings.hdfc", "amc_direct"),
            ],
            tmp_path,
        )
        rows = standings(conn, TODAY)
        assert {r.tier for r in rows} == {"aggregator", "amc_direct"}

    def test_the_worst_house_is_listed_first(self, tmp_path: Path) -> None:
        """A report is read from the top. What is most behind belongs there."""
        conn = _warehouse(
            [("A", "current_house", "2026-08-31", "holdings.kotak", "amc_direct")]
            + [
                (f"S{i}", "behind_house", "2026-07-31", "holdings.kotak", "amc_direct")
                for i in range(4)
            ],
            tmp_path,
        )
        assert [r.amc_id for r in standings(conn, TODAY)] == [
            "behind_house",
            "current_house",
        ]


class TestItFindsTheAdapterThroughProvenance:
    """No AMC-to-adapter table. `ingest_inbox` refused to keep one and the
    warehouse already records which parser read each file."""

    def test_a_parser_with_a_discovery_adapter_is_found(self, tmp_path: Path) -> None:
        conn = _warehouse(
            [("A", "kotak_mahindra", "2026-07-31", "holdings.kotak", "amc_direct")],
            tmp_path,
        )
        (row,) = standings(conn, TODAY)
        assert row.adapter == "kotak", (
            "scheme.amc_id is `kotak_mahindra` and the adapter key is `kotak`;"
            " the link is raw_file.parser_id, not a string match on the name"
        )

    def test_a_parser_without_one_gets_none(self, tmp_path: Path) -> None:
        conn = _warehouse(
            [("A", "nippon_india", "2026-07-31", "holdings.nippon", "amc_direct")],
            tmp_path,
        )
        (row,) = standings(conn, TODAY)
        assert row.adapter is None

    def test_an_unknown_parser_id_does_not_raise(self, tmp_path: Path) -> None:
        """A disclosure loaded by a parser since renamed still has to appear in
        the report. Losing the row would hide a stale fund."""
        conn = _warehouse(
            [("A", "some_amc", "2026-07-31", "holdings.gone", "amc_direct")], tmp_path
        )
        (row,) = standings(conn, TODAY)
        assert row.adapter is None
        assert row.schemes == 1


class TestNextStep:
    """Saying a thing is stale and leaving the reader to work out what to do
    about it is the easy half."""

    def _row(self, **kw: object) -> Standing:
        base = dict(
            amc_id="kotak_mahindra",
            schemes=2,
            have=date(2026, 7, 31),
            current=0,
            tier="amc_direct",
            adapter="kotak",
        )
        base.update(kw)
        return Standing(**base)  # type: ignore[arg-type]

    def test_a_house_with_an_adapter_gets_the_command(self) -> None:
        step = next_step(self._row(), TODAY)
        assert "jobs.fetch_amc --amc kotak --period 2026-08" in step
        assert "jobs.ingest_inbox" in step

    def test_a_house_without_one_gets_its_page(self) -> None:
        step = next_step(self._row(amc_id="nippon_india", adapter=None), TODAY)
        assert "data/inbox/" in step
        assert "http" in step, "the page URL from AMFI's own directory (V1-32)"

    def test_a_disclosure_the_amc_has_not_published_is_not_the_readers_fault(
        self,
    ) -> None:
        """`--check` said the newest published is July. Telling someone to
        fetch August would send them after a file that does not exist."""
        step = next_step(self._row(available=date(2026, 7, 31)), TODAY)
        assert "not published yet" in step
        assert "fetch_amc" not in step

    def test_a_current_house_gets_no_step(self) -> None:
        assert next_step(self._row(current=2), TODAY) == ""


class TestThePublishedLabel:
    """V1-47. The first version collapsed four states into one nested
    conditional and printed `no adapter` for a house that HAS one whose listing
    came back empty — the opposite of the truth about the single thing
    `--check` exists to establish."""

    def _row(
        self, adapter: str | None, available: date | None, error: str | None
    ) -> Standing:
        return Standing(
            "x", 1, date(2026, 7, 31), 0, "amc_direct", adapter, available, error
        )

    def test_a_house_with_no_adapter_says_so(self) -> None:
        assert _published_label(self._row(None, None, None), True) == "no adapter"

    def test_a_house_whose_listing_was_empty_is_not_called_adapterless(self) -> None:
        assert _published_label(self._row("kotak", None, None), True) == "none listed"

    def test_a_house_that_could_not_be_reached_says_so(self) -> None:
        assert _published_label(self._row("kotak", None, "boom"), True) == "unreachable"

    def test_an_answer_is_the_date(self) -> None:
        label = _published_label(self._row("kotak", date(2026, 8, 31), None), True)
        assert label == "2026-08-31"

    def test_nothing_is_claimed_when_nobody_was_asked(self) -> None:
        assert _published_label(self._row("kotak", None, None), False) == "-"


class TestTheDisclosurePageLookup:
    """A bare substring scan over an unordered list hands a short `amc_id` to
    whichever entry happens to contain those letters first, and a reader who
    follows that link downloads another fund house's portfolio."""

    def test_each_held_house_finds_its_own_page(self) -> None:
        for amc_id, host in (
            ("kotak_mahindra", "kotakmf.com"),
            ("nippon_india", "nipponindiaim.com"),
            ("hdfc", "hdfcfund.com"),
            ("ppfas", "ppfas.com"),
            ("icici_prudential", "icicipruamc.com"),
        ):
            page = _disclosure_page(amc_id)
            assert page and host in page, f"{amc_id} -> {page}"

    def test_an_unknown_house_gets_nothing_rather_than_a_near_miss(self) -> None:
        assert _disclosure_page("not_a_real_fund_house") is None

    def test_the_index_is_read_once(self) -> None:
        """`next_step` asks per stale row and the file cannot change mid-run."""
        _index.cache_clear()
        _disclosure_page("hdfc")
        _disclosure_page("ppfas")
        assert _index.cache_info().misses == 1


class TestCheckNeverMakesTheReportWorse:
    """V1-48. `--check` adds what the AMC has published; it must not subtract
    what the warehouse already knew."""

    def test_a_listing_failure_does_not_erase_the_local_count(self) -> None:
        """`verdict()` returned `unknown` whenever `error` was set, throwing
        away scheme counts computed from a local query with no network
        involved — so one timed-out listing made `--check` strictly less
        informative than no `--check`."""
        offline = Standing(
            "kotak_mahindra", 96, date(2026, 7, 31), 21, "amc_direct", "kotak"
        )
        failed = Standing(
            "kotak_mahindra",
            96,
            date(2026, 7, 31),
            21,
            "amc_direct",
            "kotak",
            None,
            "ConnectError: timed out",
        )

        assert failed.verdict(TODAY) == offline.verdict(TODAY) == "75/96 behind, 1 month"
        assert _published_label(failed, True) == "unreachable", (
            "the failure belongs in the published column, not the status one"
        )


class TestAHouseNeverGetsAnotherHousesPage:
    """V1-48. Two matching rules have been wrong here in turn: a bare substring
    scan, then `wanted in name or name in wanted`, which fixed one direction and
    broke the other."""

    def test_bank_of_baroda_does_not_resolve_to_bank_of_india(self) -> None:
        """The concrete failure. `india` was treated as noise, so `Bank of
        India Mutual Fund` normalised to the fragment `bank of` — a genuine
        word-boundary prefix of `bank of baroda`. No tightening of the matcher
        could fix that; the noise list was the defect."""
        _index.cache_clear()
        boi = _disclosure_page("bank_of_india")
        assert boi and "boimf.in" in boi

        baroda = _disclosure_page("bank_of_baroda")
        assert baroda is None or "boimf.in" not in baroda, (
            f"bank_of_baroda resolved to {baroda}"
        )

    def test_india_still_identifies_the_houses_that_need_it(self) -> None:
        """Removing it from the noise list must not break the house it was
        added for."""
        _index.cache_clear()
        page = _disclosure_page("nippon_india")
        # The HOST, not a substring of the whole URL. `"x.com" in url` is also
        # true of `evil-x.com/...` and of a path or query that merely mentions
        # it, so it is a weaker claim than this test means to make -- it means
        # the lookup resolved to Nippon's own page. CodeQL flags the substring
        # shape for exactly that reason (alert #4); here it was a false
        # positive, since an assertion decides nothing, but the precise form
        # is the better test anyway.
        assert page and urlparse(page).netloc == "mf.nipponindiaim.com"

    def test_no_index_entry_is_a_word_prefix_of_another(self) -> None:
        """The property that makes prefix matching safe at all. If this ever
        fails, two houses can capture each other and the fallback has to go."""
        _index.cache_clear()
        names = [n for n, _ in _index()]
        clashes = [
            (a, b) for a in names for b in names if a != b and b.startswith(a + " ")
        ]
        assert not clashes, f"prefix collisions between fund houses: {clashes}"

    @pytest.mark.parametrize(
        ("amc_id", "host"),
        [
            ("kotak_mahindra", "kotakmf.com"),
            ("hdfc", "hdfcfund.com"),
            ("ppfas", "ppfas.com"),
            ("icici_prudential", "icicipruamc.com"),
            ("quant", "quantmutual.com"),
            ("quantum", "quantumamc.com"),
        ],
    )
    def test_each_house_still_finds_its_own(self, amc_id: str, host: str) -> None:
        _index.cache_clear()
        page = _disclosure_page(amc_id)
        assert page and host in page, f"{amc_id} -> {page}"
