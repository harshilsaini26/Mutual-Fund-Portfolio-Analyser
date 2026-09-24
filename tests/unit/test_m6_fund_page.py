"""The fund page, search and the period tabs. DECISIONS V1-70.

Over the same fixture as `test_m6_render.py`: a fund with four years of prices
beside a total-return benchmark, and a loaded portfolio. What is asserted here
is what a reader relies on and would not notice breaking: that a tooltip says
what the export says, that a period tab swaps one panel with its own footer,
and that a hostile name from a downloaded file stays text.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from src.m6_views.format import format_inr
from src.m6_views.registry import FUND_PAGE

from tests.unit.test_m6_render import QS, client  # noqa: F401  (fixture)


def test_search_finds_a_fund_by_any_word_of_its_name(client: TestClient) -> None:  # noqa: F811
    hits = client.get("/api/search?q=fund one").json()
    assert [h["scheme_id"] for h in hits] == ["S1"]
    assert hits[0]["url"] == "/fund/S1"
    assert client.get("/api/search?q=no such fund").json() == []
    assert client.get("/api/search?q=").json() == []


def test_search_works_without_javascript(client: TestClient) -> None:  # noqa: F811
    html = client.get("/search?q=one").text
    assert 'href="/fund/S1"' in html and "Fund One" in html
    assert 'action="/search"' in html  # the masthead box, on every page


def test_the_fund_page_shows_every_picture_and_loads_echarts_not_d3(
    client: TestClient,  # noqa: F811
) -> None:
    html = client.get(f"/fund/S1{QS}").text
    for view_id in FUND_PAGE:
        assert f'data-view-id="{view_id}"' in html, view_id
    assert '<script src="/static/vendor/echarts.v6.1.0.min.js"></script>' in html
    assert '<script src="/static/charts.js"></script>' in html
    assert "/static/vendor/d3" not in html
    assert "<title>Fund One</title>" in html
    assert client.get("/static/vendor/echarts.v6.1.0.min.js").status_code == 200


def test_a_tooltip_says_what_the_export_says(client: TestClient) -> None:  # noqa: F811
    """§16.4: the label a reader hovers is formatted in Python from the same
    figure the CSV writes, so the two cannot disagree."""
    env = client.get(f"/api/views/fund_growth{QS}&scope_id=S1").json()
    fund = env["payload"]["charts"][0]["series"][0]["points"]
    last_row = env["payload"]["rows"][-1]
    assert fund[-1][0] == last_row["date"]
    assert fund[-1][2] == format_inr(
        Decimal(last_row["fund_value_inr"]), precision=0, compact=False
    )
    assert env["payload"]["headline"].startswith("₹10,000 put into Fund One")


def test_a_period_tab_swaps_one_panel_with_its_own_footer(
    client: TestClient,  # noqa: F811
) -> None:
    page = client.get(f"/fund/S1{QS}").text
    tab = re.search(r'data-fragment="([^"]+window=1y)"', page)
    assert tab, "no period tab on the growth chart"
    fragment = client.get(tab.group(1).replace("&amp;", "&")).text
    assert fragment.count('class="view view--') == 1
    assert 'data-view-id="fund_growth"' in fragment
    assert "Export CSV" in fragment and "About this data" in fragment
    assert "<html" not in fragment
    assert client.get("/fragment/no_such_view").status_code == 404


def test_an_unknown_fund_explains_itself_rather_than_failing(
    client: TestClient,  # noqa: F811
) -> None:
    response = client.get(f"/fund/INF000X01000{QS}")
    assert response.status_code == 200
    assert "Nothing to show yet" in response.text


def _one_fund(tmp_path: Path, holdings: list[tuple[str, str, str, str]]) -> TestClient:
    """An app over one fund, S9, disclosed on 31 Jul 2026 with `holdings`
    as (issuer_id, issuer name, class, share of fund)."""
    from src.common.decimals import connect
    from src.m1_ledger.db import apply_ledger_schema, connect_ledger
    from src.m6_views.api.app import create_app

    from tests.conftest import migrated

    db = str(tmp_path / "w.db")
    migrated(db)
    warehouse = connect(db, check_same_thread=False)
    warehouse.execute(
        "INSERT INTO scheme (scheme_id, scheme_name, plan, option, status)"
        " VALUES ('S9', 'Fund nine', 'direct', 'growth', 'active')"
    )
    warehouse.execute(
        "INSERT INTO raw_file (file_id, source_id, fetched_at, storage_path, byte_size)"
        " VALUES ('f9', 'S5', '2026-08-01', '/x', 0)"
    )
    warehouse.execute(
        "INSERT INTO holding_disclosure (scheme_id, as_of_date, revision,"
        " source_file_id, row_count, unresolved_mv_pct, total_mv,"
        " validation_status, ingested_at, is_current)"
        " VALUES ('S9', '2026-07-31', 1, 'f9', ?, 0, 100, 'ok', '2026-08-01', 1)",
        (len(holdings),),
    )
    for row, (issuer, name, klass, weight) in enumerate(holdings, start=1):
        warehouse.execute(
            "INSERT OR IGNORE INTO issuer (issuer_id, canonical_name, is_listed)"
            " VALUES (?,?,1)",
            (issuer, name),
        )
        warehouse.execute(
            "INSERT INTO holding (scheme_id, as_of_date, revision, row_number,"
            " issuer_id, instrument_raw_name, market_value, pct_normalised,"
            " instrument_class, resolution_method, source_file_id, ingested_at,"
            " is_current) VALUES ('S9', '2026-07-31', 1, ?, ?, 'x', 1, ?, ?,"
            " 'isin', 'f9', '2026-08-01', 1)",
            (row, issuer, Decimal(weight), klass),
        )
    warehouse.commit()
    ledger = connect_ledger(
        str(tmp_path / "l.db"), key="test-key", check_same_thread=False
    )
    apply_ledger_schema(ledger)
    return TestClient(create_app(ledger, warehouse), base_url="http://127.0.0.1:8765")


def test_a_hostile_holding_name_stays_text_on_the_fund_page(tmp_path: Path) -> None:
    """Holding names come from fund houses' files. One written to close the
    chart's JSON block must arrive escaped in the JSON and in the table."""
    app = _one_fund(tmp_path, [("EVIL", HOSTILE, "equity", "100")])
    html = app.get("/fund/S9?as_of=2026-09-04").text
    assert 'data-view-id="fund_portfolio"' in html
    assert HOSTILE not in html
    assert "<script>alert(1)</script>" not in html
    assert "\\u003c/script\\u003e" in html  # escaped inside the JSON block
    assert "&lt;/script&gt;" in html  # escaped in the table


def test_a_fund_owing_more_than_it_is_owed_says_so(tmp_path: Path) -> None:
    """V1-71: V8 no longer warns on negative net current assets, so the page
    says it where a reader looks -- the holdings panel's own sentence."""
    app = _one_fund(tmp_path, [
        ("ACME", "Acme", "equity", "101.5"),
        ("__RECV__", "Net Receivables / Payables", "cash", "-1.5"),
    ])
    env = app.get("/api/views/fund_portfolio?as_of=2026-09-04&scope_id=S9").json()
    assert "net current assets were -1.50%" in env["payload"]["headline"]
    assert "owed more than it was owed" in env["payload"]["headline"]
    assert not any("negative weight" in c for c in env["quality"]["caveats"])


HOSTILE = "Acme</script><script>alert(1)</script>"
