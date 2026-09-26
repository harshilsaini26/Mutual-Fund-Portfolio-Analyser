"""Which of Groww's pages a build reads, and what it remembers. DECISIONS V1-79.

`jobs.fetch_groww.plan` decides the day's reading from the sitemap and the map
of what each page said; nothing here touches the network. What it pins: the cap
holds, a live fund's month-old page comes before a page never read, a fund its
fund house's own file covers is never read, and a page that named no fund we
list waits three months before it is tried again.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from jobs import fetch_groww as groww

TODAY = date(2026, 9, 26)
LIVE = {"INF000A00001", "INF000B00001", "INF000C00001"}


def _seen(isin: str, days_ago: int) -> groww.Seen:
    return groww.Seen(isin, "2026-08-31", TODAY - timedelta(days=days_ago))


def test_a_stale_live_page_comes_before_a_new_one_and_the_cap_holds() -> None:
    listed = ["a-direct-growth", "b-direct-growth", "new-direct-growth"]
    seen = {"a-direct-growth": _seen("INF000A00001", 31),
            "b-direct-growth": _seen("INF000B00001", 3)}
    assert groww.plan(listed, seen, LIVE, set(), TODAY, 10) == [
        ("a-direct-growth", "INF000A00001"),  # a month old: read again, promised
        ("new-direct-growth", None),          # never read: its page says who it is
    ]
    assert groww.plan(listed, seen, LIVE, set(), TODAY, 1) == [
        ("a-direct-growth", "INF000A00001"),
    ]


def test_a_fund_its_fund_house_covers_is_not_read() -> None:
    seen = {"c-direct-growth": _seen("INF000C00001", 60)}
    covered = {"INF000C00001"}
    assert groww.plan(["c-direct-growth"], seen, LIVE, covered, TODAY, 10) == []


def test_a_page_for_no_fund_we_list_waits_three_months() -> None:
    listed = ["gone-direct-growth"]
    assert groww.plan(listed, {"gone-direct-growth": _seen("", 89)},
                      LIVE, set(), TODAY, 10) == []
    assert groww.plan(listed, {"gone-direct-growth": _seen("", 90)},
                      LIVE, set(), TODAY, 10) == [("gone-direct-growth", None)]
    # A page no longer in the sitemap is not chased.
    assert groww.plan([], {"gone-direct-growth": _seen("", 400)},
                      LIVE, set(), TODAY, 10) == []


def test_the_map_survives_a_round_trip(tmp_path: Path) -> None:
    seen = {"b-direct-growth": groww.Seen("INF000B00001", "2026-08-31", TODAY,
                                          "NIFTY 500 Total Return Index"),
            "a-direct-growth": _seen("", 100)}
    path = tmp_path / "data" / "groww.csv"
    groww.write_map(path, seen)
    assert groww.read_map(path) == seen
    assert path.read_text(encoding="utf-8").splitlines()[1].startswith("a-direct")
    assert groww.declared_benchmarks(path) == {
        "INF000B00001": "NIFTY 500 Total Return Index"}
    assert groww.read_map(tmp_path / "none.csv") == {}
    # A map written before the benchmark column still reads.
    old = tmp_path / "old.csv"
    old.write_text("slug,isin,as_of,checked\nb,INF000B00001,2026-08-31,2026-09-26\n",
                   encoding="utf-8")
    assert groww.read_map(old)["b"].benchmark == ""


def test_the_sitemap_yields_direct_growth_pages_only() -> None:
    xml = (
        "<url><loc>https://groww.in/mutual-funds/hdfc-equity-fund-direct-growth</loc>"
        "</url><url><loc>https://groww.in/mutual-funds/hdfc-equity-fund-regular"
        "-growth</loc></url><url><loc>https://groww.in/mutual-funds/amc/hdfc-mutual"
        "-funds</loc></url>"
    )
    assert groww._SLUG.findall(xml) == ["hdfc-equity-fund-direct-growth"]
