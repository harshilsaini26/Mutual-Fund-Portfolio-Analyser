"""The public fund explorer: `jobs/publish_site.py`. DECISIONS V1-72.

A public website built from the same warehouse as the private app, so every
test here is about what must NOT reach it -- index levels, the portfolio --
what must reach it only marked (an aggregator's holdings), and about the links
still working from the subdirectory GitHub Pages serves a project site under.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import jobs.publish_site as publish
import pytest
from src.common.decimals import connect
from src.m0_data.categories import category_of
from src.m6_views.builders.fund.common import INDEX_WITHHELD

from tests.conftest import migrated

TODAY = date(2026, 9, 24)
BASE = "/Repo"
DIRECT, REGULAR, AGGREGATED, SHORT = (
    "INF000T01011", "INF000T01029", "INF000T01037", "INF000T01045",
)


def _prices(c: sqlite3.Connection, scheme: str, days: int) -> None:
    c.executemany(
        "INSERT INTO nav_daily (scheme_id, nav_date, nav, nav_adj) VALUES (?,?,?,?)",
        [
            (scheme, TODAY - timedelta(days=days - t), Decimal(100 + t), Decimal(100 + t))
            for t in range(days)
        ],
    )


def _disclose(c: sqlite3.Connection, scheme: str, tier: str) -> None:
    c.execute(
        "INSERT INTO holding_disclosure (scheme_id, as_of_date, revision,"
        " source_file_id, row_count, unresolved_mv_pct, total_mv,"
        " validation_status, ingested_at, is_current, source_tier)"
        " VALUES (?, '2026-08-31', 1, 'f1', 1, 0, 100, 'ok', '2026-09-01', 1, ?)",
        (scheme, tier),
    )
    c.execute(
        "INSERT INTO holding (scheme_id, as_of_date, revision, row_number,"
        " issuer_id, instrument_raw_name, market_value, pct_normalised,"
        " instrument_class, resolution_method, source_file_id, ingested_at,"
        " is_current) VALUES (?, '2026-08-31', 1, 1, 'ACME', 'Acme', 1, 100,"
        " 'equity', 'isin', 'f1', '2026-09-01', 1)",
        (scheme,),
    )


@pytest.fixture
def warehouse(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "w.db"
    migrated(db)
    c: sqlite3.Connection = connect(str(db))
    c.execute(
        "INSERT INTO benchmark_index (index_id, index_name, is_total_return)"
        " VALUES ('NSE:TEST_TRI', 'Test 50', 1)"
    )
    for sid, plan, family in (
        (DIRECT, "direct", "fund one"),
        (REGULAR, "regular", "fund one"),  # one fund, two share classes
        (AGGREGATED, "direct", "fund two"),
        (SHORT, "direct", "fund three"),
    ):
        c.execute(
            "INSERT INTO scheme (scheme_id, scheme_name, fund_name, plan, option,"
            " amc_id, scheme_family, sebi_category, benchmark_id, status, last_seen)"
            " VALUES (?,?,?,?,'growth','amc1',?,'Equity Scheme - Flexi Cap Fund',"
            " 'NSE:TEST_TRI','active',?)",
            (sid, family.title(), family.title(), plan, family, TODAY),
        )
    for sid in (DIRECT, REGULAR, AGGREGATED):
        _prices(c, sid, 300)
    _prices(c, SHORT, 20)  # too little history for a page
    c.executemany(
        "INSERT INTO index_level (index_id, level_date, level) VALUES (?,?,?)",
        [("NSE:TEST_TRI", TODAY - timedelta(days=t), Decimal(5000 + t))
         for t in range(300)],
    )
    c.execute(
        "INSERT INTO raw_file (file_id, source_id, fetched_at, storage_path, byte_size)"
        " VALUES ('f1', 'S5', '2026-09-01', '/x', 0)"
    )
    c.execute("INSERT INTO issuer (issuer_id, canonical_name) VALUES ('ACME', 'Acme')")
    _disclose(c, DIRECT, "amc_direct")
    _disclose(c, AGGREGATED, "aggregator")
    c.commit()
    return c


@pytest.fixture
def site(
    warehouse: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """The site, built with the personal ledger made impossible to open."""
    import src.m1_ledger.db as ledger_db

    opened: list[str] = []
    real = ledger_db.connect_ledger

    def spy(path: str, **kw: object) -> sqlite3.Connection:
        opened.append(path)
        return real(path, **kw)  # type: ignore[arg-type]

    def forbidden() -> Path:
        raise AssertionError("the public build asked for the personal ledger")

    monkeypatch.setattr(publish, "connect_ledger", spy)
    monkeypatch.setattr(ledger_db, "ledger_path", forbidden)
    out = tmp_path / "site"
    summary = publish.build_site(warehouse, out, BASE, TODAY)
    assert summary["funds"] == 2
    assert set(opened) == {":memory:"}
    return out


def _page(site: Path, scheme: str) -> str:
    return (site / "fund" / scheme / "index.html").read_text(encoding="utf-8")


def test_one_page_per_fund_with_a_year_of_prices(site: Path) -> None:
    pages = {p.parent.name for p in (site / "fund").glob("*/index.html")}
    # The Direct plan stands for its fund; the Regular one and a fund with
    # twenty days of prices get no page.
    assert pages == {DIRECT, AGGREGATED}
    listed = json.loads((site / "search.json").read_text(encoding="utf-8"))
    assert {h["url"] for h in listed} == {
        f"{BASE}/fund/{DIRECT}/", f"{BASE}/fund/{AGGREGATED}/"
    }


def test_every_link_works_from_the_project_subdirectory(site: Path) -> None:
    for html in site.rglob("*.html"):
        text = html.read_text(encoding="utf-8")
        for link in re.findall(r'(?:href|src|action)="([^"]*)"', text):
            assert link.startswith((BASE, "https://")), f"{html.name}: {link}"
        assert f'data-index="{BASE}/search.json"' in text  # the search box's list
        assert "/api/" not in text and "/view/" not in text and "/fragment/" not in text


def test_a_panels_figures_are_linked_not_inlined(site: Path) -> None:
    """Phase 2, lean pages: §10.4's table equivalent is the CSV beside the page,
    so a public page links it; inline, the tables were ~3/4 of every page."""
    page = (site / "fund" / DIRECT / "index.html").read_text(encoding="utf-8")
    assert '<details class="chart-table">' not in page
    assert f'href="{BASE}/fund/{DIRECT}/fund_growth.csv">The same figures' in page
    assert (site / "fund" / DIRECT / "fund_growth.csv").exists()


def test_no_index_level_reaches_the_public_copy(site: Path) -> None:
    page = _page(site, DIRECT)
    block = re.search(
        r'<script type="application/json" class="echart-data">(.*?)</script>',
        page, re.S,
    )
    assert block is not None
    charts = json.loads(block.group(1))["charts"]
    assert all(s["role"] == "fund" for s in charts[0]["series"])
    assert INDEX_WITHHELD in page
    growth = (site / "fund" / DIRECT / "fund_growth.csv").read_text(
        encoding="utf-8-sig"
    )
    # The benchmark column is there and empty.
    assert all(line.endswith(",") for line in growth.splitlines()[-3:])


def test_an_aggregators_holdings_are_published_and_say_whose_they_are(
    site: Path,
) -> None:
    """V1-79 (reversing V1-72's withholding): drawn, with the source named."""
    page = _page(site, AGGREGATED)
    assert "Largest holdings" in page
    assert "comes from Groww&#39;s page for the fund, an aggregator" in page
    # Marked where it is always read, not only in the notes, which start closed;
    # and never badged "current, complete, and resolved".
    assert "holdings in the portfolio shown on Groww&#39;s page for" in page
    owns = page[page.index("What does this fund own?"):]
    assert 'badge--medium' in owns[:600]
    assert "Groww&#39;s page" not in _page(site, DIRECT)


def test_the_policy_travels_in_the_page(site: Path) -> None:
    """Pages cannot send headers, so the CSP is a <meta> tag, and the page
    names what it is and what it leaves out."""
    page = _page(site, DIRECT)
    assert '<meta http-equiv="Content-Security-Policy"' in page
    assert "script-src 'self'" in page
    assert "A public copy of the fund pages" in page
    assert "Your portfolio" not in page  # no portfolio navigation
    assert f'href="{BASE}/fund/{DIRECT}/fund_growth.csv"' in page


def test_a_directory_it_did_not_write_is_not_deleted(
    warehouse: sqlite3.Connection, tmp_path: Path
) -> None:
    precious = tmp_path / "notes"
    precious.mkdir()
    (precious / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(RuntimeError, match="did not write"):
        publish.build_site(warehouse, precious, BASE, TODAY)
    assert (precious / "keep.txt").exists()


def test_a_site_too_large_for_pages_stops_the_build() -> None:
    publish.check_budget(100, budget=100)
    with pytest.raises(publish.SiteTooLarge):
        publish.check_budget(101, budget=100)


def test_every_fund_is_listed_in_one_table_on_its_own_page(site: Path) -> None:
    """DECISIONS V1-74, moved to /funds/ in V1-80: a table of every published
    fund, sortable and narrowed by family or category in the browser, and the
    grouped lists beneath for no script."""
    index = (site / "funds" / "index.html").read_text(encoding="utf-8")
    assert "<table data-sortable data-filterable>" in index
    flexi = '<tr data-family="equity" data-category="equity/flexi_cap">'
    assert index.count(flexi) == 2
    assert '<option value="equity/flexi_cap">Flexi cap</option>' in index
    assert f'href="{BASE}/fund/{DIRECT}/"' in index
    assert 'class="explorer__group"' in index
    for name in publish.STATIC_FILES:
        assert (site / "static" / name).exists(), name
    # Three hundred days of prices span no fixed window: every return is a
    # dash, never a figure for a period the history does not cover. The
    # fixture has no fund size, expense ratio (V1-78) or peers (V1-77) either:
    # three dashes more.
    row = re.search(r'<tr data-family="equity"[^>]*>(.*?)</tr>', index, re.S)
    assert row is not None and "data-value=\"0." not in row.group(1)
    assert row.group(1).count("—") == 6


def test_the_front_page_is_a_way_in_not_a_list(site: Path) -> None:
    """V1-80, after MF Zone: a hero with search, the category cards and what a
    fund page shows -- and small, now that the full list lives at /funds/."""
    index = (site / "index.html").read_text(encoding="utf-8")
    assert 'data-island="blur-text">See what every fund owns' in index
    assert 'data-index="/Repo/search.json"' in index
    assert f'href="{BASE}/funds/"' in index and 'id="about"' in index
    assert "<table data-sortable" not in index
    # Islands enhance text the server already wrote: with scripts off it reads.
    assert re.search(r'data-island="count-up">2<', index)
    assert len(index.encode("utf-8")) < 200_000


def _leader_row(sid: str, key: str, three: str | None) -> dict[str, object]:
    cell = {"value": three or "", "label": three or "—"}
    return {"scheme_id": sid, "name": sid, "category_key": key,
            "category_short": key.split("/")[1], "returns": [cell, cell, cell]}


def test_category_cards_hold_the_highest_three_year_returns_in_order() -> None:
    rows = [_leader_row(f"F{i}", "equity/flexi_cap", v)
            for i, v in enumerate(["0.12", "0.3", "0.05", None, "0.21", "0.09", "0.18"])]
    rows.append(_leader_row("M0", "equity/mid_cap", None))
    cards = publish.category_leaders(rows)
    assert [c["key"] for c in cards] == ["equity/flexi_cap"]  # no 3-year figure: no card
    flexi = cards[0]
    # Decimal order, not text order ("0.3" > "0.21"), five of them, of seven.
    assert [f["scheme_id"] for f in flexi["funds"]] == ["F1", "F4", "F6", "F0", "F5"]
    assert flexi["count"] == 7


@pytest.mark.parametrize(("category", "family"), [
    ("Equity Scheme - Flexi Cap Fund", "equity"),
    ("Equity Schemes - Thematic Fund", "equity"),
    ("Growth", "equity"),
    ("ELSS", "equity"),
    ("Debt Scheme - Gilt Fund", "debt"),
    ("Income/Debt Oriented Schemes - Liquid Fund", "debt"),
    ("Income", "debt"),
    ("Hybrid Schemes - Arbitrage Fund", "hybrid"),
    ("Solution Oriented Schemes ** - Retirement Fund", "solution"),
    ("Other Scheme - Index Funds", "other"),
    ("Exchange Traded Funds (ETFs) - Equity ETF", "other"),
    ("Overseas Fund of Funds - Fund of Funds investing overseas", "other"),
])
def test_every_generation_of_category_name_finds_its_family(
    category: str, family: str
) -> None:
    """AMFI's list mixes naming generations; the tiles must count them all, or a
    legacy "Income" fund is filed under index funds and ETFs."""
    assert category_of(category).family == family
