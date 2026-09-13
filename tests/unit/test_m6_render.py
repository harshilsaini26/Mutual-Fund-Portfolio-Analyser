"""The rendered page. MODULE_6.md §16 and §19.7.

**This file is what closes the V1 acceptance gate.** The criterion is *"every
chart renders its as-of date, staleness, and coverage"*, and every other test in
this project asserts on a Python object or a JSON body. Here the assertions are
on HTML that a browser would show.

The load-bearing test is `test_no_chart_renders_outside_a_view_container`. §16.3
calls the wrapper the mechanism that makes the honesty commitments structural
rather than per-chart discipline, and enforces it with an ESLint rule over React
components. There are no components here, so the rule is asserted on the output:
every element carrying `data-chart` must be a descendant of a `section.view`.
Testing the rendered DOM rather than the template source means it survives a
refactor that moves the templates around, which a source scan would not.

§19.7's other three cases are here too — the caveat strip rendering every
caveat, a suppressed state showing its reason rather than a blank, and low
confidence carrying a class.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from src.common.types import IssuerId, SchemeId, UserId
from src.m0_data.schema.apply import apply_migrations
from src.m1_ledger.db import apply_ledger_schema, connect_ledger
from src.m3_lookthrough.concentration import concentration
from src.m3_lookthrough.duplication import portfolio_duplication
from src.m3_lookthrough.engine import IssuerWeight, Position, compute_lookthrough
from src.m3_lookthrough.overlap import pairwise_overlap
from src.m3_lookthrough.persist import save_lookthrough
from src.m3_lookthrough.persist_metrics import (
    SCOPES,
    save_concentration,
    save_duplication,
    save_overlap,
)
from src.m6_views.api.app import create_app
from src.m6_views.api.pages import LANDING
from src.m6_views.registry import VIEW_DEFS, VIEW_REGISTRY, seed_view_definitions
from src.m6_views.render import CHART_TEMPLATES

USER = "USER-01"
AS_OF = date(2026, 9, 4)
JULY = date(2026, 7, 31)
JUNE = date(2026, 6, 30)
S1, S2 = SchemeId("S1"), SchemeId("S2")

WEIGHTS = {
    S1: [
        IssuerWeight(IssuerId("ACME"), Decimal("55"), "equity"),
        IssuerWeight(IssuerId("BETA"), Decimal("30"), "equity"),
        IssuerWeight(IssuerId("__UNRESOLVED__"), Decimal("10"), "unknown"),
        IssuerWeight(IssuerId("__CASH__"), Decimal("5"), "cash"),
    ],
    S2: [
        IssuerWeight(IssuerId("BETA"), Decimal("60"), "equity"),
        IssuerWeight(IssuerId("GAMMA"), Decimal("40"), "equity"),
    ],
}
POSITIONS = [
    Position(S1, Decimal("100000")),
    Position(S2, Decimal("50000")),
    # No disclosure, so coverage is short of 100 and the caveats are real.
    Position(SchemeId("S_DARK"), Decimal("25000")),
]


class ChartNesting(HTMLParser):
    """Tracks whether each `[data-chart]` opened inside a `section.view`.

    A tiny parser rather than a regex: nesting is the entire question, and a
    regex cannot answer it.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.view_depth: list[int] = []
        self.charts: list[tuple[str, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in ("br", "hr", "img", "meta", "link", "input"):
            if "data-chart" in attributes:
                self.charts.append(
                    (attributes["data-chart"] or tag, bool(self.view_depth))
                )
            return
        self.depth += 1
        classes = (attributes.get("class") or "").split()
        if tag == "section" and "view" in classes:
            self.view_depth.append(self.depth)
        if "data-chart" in attributes:
            self.charts.append(
                (attributes["data-chart"] or tag, bool(self.view_depth))
            )

    def handle_endtag(self, tag: str) -> None:
        if tag in ("br", "hr", "img", "meta", "link", "input"):
            return
        if self.view_depth and self.view_depth[-1] == self.depth:
            self.view_depth.pop()
        self.depth -= 1


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    from src.common.decimals import connect

    warehouse_db = str(tmp_path / "warehouse.db")
    apply_migrations(warehouse_db)
    warehouse: sqlite3.Connection = connect(
        warehouse_db, check_same_thread=False
    )
    warehouse.execute(
        "INSERT OR REPLACE INTO issuer (issuer_id, canonical_name, is_listed)"
        " VALUES ('ACME', 'Acme Industries Ltd.', 1)"
    )
    warehouse.commit()
    seed_view_definitions(warehouse)

    ledger = connect_ledger(
        str(tmp_path / "personal.db"), key="test-key", check_same_thread=False
    )
    apply_ledger_schema(ledger)
    result = compute_lookthrough(POSITIONS, WEIGHTS, AS_OF)
    save_lookthrough(ledger, UserId(USER), AS_OF, result, {S1: JULY, S2: JUNE})
    save_concentration(
        ledger, UserId(USER), AS_OF,
        [concentration(result.exposures, s) for s in SCOPES],
    )
    save_overlap(
        ledger, UserId(USER), AS_OF,
        [
            pairwise_overlap(
                S1, S2, JULY, JUNE, WEIGHTS[S1], WEIGHTS[S2],
                value_a=Decimal("100000"), value_b=Decimal("50000"),
            )
        ],
    )
    save_duplication(
        ledger, UserId(USER), AS_OF,
        portfolio_duplication(result.contributions, result.summary.total_value_inr),
    )
    ledger.execute(
        "INSERT INTO position (user_id, folio, scheme_id, as_of, units, nav,"
        " nav_date, market_value, reconciled, confidence, rebuilt_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            USER, "F1", "S1", AS_OF.isoformat(), Decimal("100"), Decimal("1000"),
            JULY.isoformat(), Decimal("100000"), 0, "medium",
            "2026-09-04T00:00:00Z",
        ),
    )
    ledger.commit()
    return TestClient(create_app(ledger, warehouse))


QS = f"?user_id={USER}&as_of={AS_OF.isoformat()}"


def page(client: TestClient, view_id: str) -> str:
    response = client.get(f"/view/{view_id}{QS}")
    assert response.status_code == 200, view_id
    # Named rather than returned straight through. Starlette 1.6 widened
    # TestClient's response type to `Any` while it carries both httpx and
    # httpx2, so `response.text` is `str` on an older stack and `Any` on a
    # newer one — and `--strict`'s warn-return-any fails only on the newer.
    # The annotation makes this function's contract ours instead of the test
    # client's, so the check means the same thing whichever version resolves.
    text: str = response.text
    return text


# --- §16.3 the structural rule ----------------------------------------------


@pytest.mark.parametrize("view_id", sorted(VIEW_REGISTRY))
def test_no_chart_renders_outside_a_view_container(
    client: TestClient, view_id: str
) -> None:
    """§16.3. A chart outside the wrapper is a chart with no caveats and no
    provenance, and nothing else would notice."""
    parser = ChartNesting()
    parser.feed(page(client, view_id))
    assert parser.charts, f"{view_id} rendered no chart to check"
    for name, inside in parser.charts:
        assert inside, f"{view_id}: chart {name!r} is outside section.view"


def test_the_landing_page_wraps_every_panel(client: TestClient) -> None:
    parser = ChartNesting()
    parser.feed(client.get(f"/{QS}").text)
    assert len(parser.charts) >= 2
    assert all(inside for _, inside in parser.charts)


# --- the V1 gate criterion --------------------------------------------------


@pytest.mark.parametrize("view_id", sorted(VIEW_REGISTRY))
def test_every_view_renders_its_as_of_staleness_and_coverage(
    client: TestClient, view_id: str
) -> None:
    """**The V1 acceptance gate.** Asserted on HTML a browser would show, not on
    a payload — which is the difference between this slice and V1.8."""
    html = page(client, view_id)
    footer = re.search(
        r'<footer class="view__footer">(.*?)</footer>', html, re.S
    )
    assert footer, f"{view_id} has no provenance footer"
    body = footer.group(1)
    assert "As of" in body
    assert "Holdings as of" in body
    assert "Coverage" in body
    assert "Unresolved" in body
    # A date, rendered per §9.4 — never MM/DD or DD/MM.
    assert re.search(r"\d{2} [A-Z][a-z]{2} \d{4}", body), view_id


@pytest.mark.parametrize("view_id", sorted(VIEW_REGISTRY))
def test_every_view_states_its_question_on_the_page(
    client: TestClient, view_id: str
) -> None:
    """§2.3. The question is the view's contract with the reader."""
    assert VIEW_DEFS[view_id].question in page(client, view_id)


@pytest.mark.parametrize("view_id", sorted(VIEW_REGISTRY))
def test_every_view_offers_its_csv(client: TestClient, view_id: str) -> None:
    """§2.5: an export on every view — a trust feature and an escape hatch,
    signalling the data is not trapped in this UI."""
    assert f"/api/export/{view_id}.csv" in page(client, view_id)


def test_a_staleness_figure_is_rendered_in_words(client: TestClient) -> None:
    html = page(client, "lookthrough_sankey")
    assert re.search(r"\(\d+ days old\)|\(today\)|\(1 day old\)", html)


# --- §19.7 caveats and states ------------------------------------------------


def test_the_caveat_strip_renders_every_caveat(client: TestClient) -> None:
    """§6.1 rule 5: never truncate. If there are six, six appear."""
    envelope = client.get(f"/api/views/lookthrough_sankey{QS}").json()
    caveats = envelope["quality"]["caveats"]
    assert len(caveats) >= 2, "fixture is not exercising the path"
    html = page(client, "lookthrough_sankey")
    assert html.count('role="note"') == len(caveats)
    for caveat in caveats:
        assert caveat in html


def test_the_caveat_count_is_visible_even_when_collapsed(
    client: TestClient,
) -> None:
    """§6.1 rule 5's second half: M6 may collapse the list, but the count is
    always visible. The landing page collapses everything below the first."""
    html = client.get(f"/{QS}").text
    assert re.search(r"\d+ notes? about this data", html)


def test_an_empty_state_shows_its_reason_not_a_blank(client: TestClient) -> None:
    """§3.3: a blank chart teaches the user the tool is broken; an explained
    absence teaches them how the tool works."""
    html = client.get(f"/view/fund_list?user_id={USER}&as_of=1999-01-01").text
    assert "placeholder--empty" in html
    reason = re.search(r'class="placeholder__reason">(.*?)</p>', html, re.S)
    assert reason and len(reason.group(1).split()) >= 8
    assert "data-chart" not in html


def test_confidence_reaches_the_markup_as_a_class(client: TestClient) -> None:
    """§7.2. Low confidence is rendered muted and dashed — present, never
    hidden, and never encoded by colour alone."""
    html = page(client, "lookthrough_sankey")
    assert re.search(r'class="view view--(high|medium|low)"', html)
    assert re.search(r'class="badge badge--(high|medium|low)"', html)


# --- §9 formatting reaches the page -----------------------------------------


def test_figures_are_formatted_server_side(client: TestClient) -> None:
    """§16.4: the frontend receives chart-ready payloads. A raw `Decimal` repr
    on the page would mean a template did the formatting, or nobody did."""
    html = page(client, "portfolio_summary")
    assert "Decimal(" not in html
    assert "₹" in html


def test_an_uncomputed_figure_is_an_em_dash_not_a_zero(
    client: TestClient,
) -> None:
    """§9.3, and the reason it matters: M1's returns engine has not run, so XIRR
    is unknown. A 0.0% would be a claim that the portfolio returned nothing."""
    html = page(client, "portfolio_summary")
    assert "kpi__tile--absent" in html
    assert "—" in html


def test_indian_grouping_reaches_the_table(client: TestClient) -> None:
    """§9.1. 1,00,000 rather than 100,000 — getting this wrong makes the product
    feel foreign."""
    # `fund_list`, because it is the view whose figures are large enough to
    # show the difference: 100000 groups as 1,00,000 in Indian and 100,000
    # everywhere else, and below a lakh the two conventions agree.
    html = page(client, "fund_list")
    assert re.search(r"₹\d{1,2},\d{2},\d{3}", html)


# --- §10 accessibility -------------------------------------------------------


@pytest.mark.parametrize("view_id", ["lookthrough_sankey", "overlap_heatmap"])
def test_every_chart_has_a_table_equivalent(
    client: TestClient, view_id: str
) -> None:
    """§10.4: every chart has an accessible table equivalent reachable from the
    same view. An SVG is invisible to a screen reader; the same numbers in a
    table are not."""
    html = page(client, view_id)
    assert 'class="chart-table"' in html
    assert "<table>" in html


def test_the_unresolved_node_is_labelled_and_patterned(
    client: TestClient,
) -> None:
    """§10.3 and Appendix A. Missing mass stays visible, and is distinguishable
    without colour."""
    html = page(client, "lookthrough_sankey")
    assert "Unresolved Holdings" in html or "__UNRESOLVED__" in html


def test_an_unreconciled_row_carries_a_symbol_not_only_a_tint(
    client: TestClient,
) -> None:
    """§10.3. The fixture's one position failed reconciliation."""
    html = page(client, "fund_list")
    assert "row--unreconciled" in html
    assert 'class="flag"' in html


def test_the_svg_carries_a_text_alternative(client: TestClient) -> None:
    html = page(client, "overlap_heatmap")
    assert 'role="img"' in html
    assert "aria-label=" in html


# --- wiring -----------------------------------------------------------------


@pytest.mark.parametrize("view_id", sorted(VIEW_DEFS))
def test_every_registered_view_has_a_chart_template(view_id: str) -> None:
    """The startup check pairs definitions with builders; nothing pairs a chart
    type with a template, so a view could register and then fail to render."""
    assert VIEW_DEFS[view_id].chart_type in CHART_TEMPLATES


def test_the_landing_surface_is_three_questions(client: TestClient) -> None:
    """§16.5, and `PLAN.md` §5.10 on metric walls. "Resist adding a fourth."."""
    assert len(LANDING) == 3
    html = client.get(f"/{QS}").text
    assert html.count('class="view view--') == 3


def test_the_nav_reaches_every_view(client: TestClient) -> None:
    html = client.get(f"/{QS}").text
    for view_id in VIEW_REGISTRY:
        assert f"/view/{view_id}" in html


def test_d3_is_vendored_not_fetched_from_a_cdn(client: TestClient) -> None:
    """`PLAN.md` §6. A page that phones a CDN stops working offline and tells a
    third party when the user looks at their portfolio."""
    html = page(client, "lookthrough_sankey")
    # An UNESCAPED script tag. The first version of this test matched the
    # substring and passed while Jinja was escaping the whole block into
    # `&lt;script&gt;`, so d3 never loaded and the diagram never drew — a test
    # that passes on the text of a tag it cannot execute is not testing loading.
    assert '<script src="/static/vendor/d3.v7.min.js"></script>' in html
    assert '<script src="/static/sankey.js"></script>' in html
    assert "cdn." not in html
    assert client.get("/static/vendor/d3-sankey.v0.12.3.min.js").status_code == 200


def test_d3_only_loads_where_a_sankey_is_drawn(client: TestClient) -> None:
    """280 KB on a page with a table on it is 280 KB of nothing."""
    assert "/static/vendor/d3" not in page(client, "fund_list")


def test_an_unknown_view_is_a_404_that_names_the_known_ones(
    client: TestClient,
) -> None:
    response = client.get(f"/view/nope{QS}")
    assert response.status_code == 404
    assert "fund_list" in response.text


# --- V1.9 review fixes -------------------------------------------------------


def test_an_empty_panel_does_not_claim_its_data_is_current(
    client: TestClient,
) -> None:
    """A non-ok envelope carries `staleness_days = 0` and `data_as_of = as_of`,
    because §3.1 types both non-optional so a view cannot omit its staleness.
    Rendering them anyway printed "31 Jul 2026 (today)" beneath a panel with no
    data at all — a freshness claim for something that does not exist."""
    html = client.get(f"/view/fund_list?user_id={USER}&as_of=1999-01-01").text
    assert "placeholder--empty" in html
    footer = re.search(r'<footer class="view__footer">(.*?)</footer>', html, re.S)
    assert footer
    # The footer still renders — that is the gate — but says nothing it cannot.
    assert "Holdings as of" in footer.group(1)
    assert "(today)" not in footer.group(1)
    assert "days old" not in footer.group(1)


def test_a_panel_that_fails_to_render_does_not_take_the_page_with_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PLAN.md` §4.9: degrade one panel, never the screen.

    `build_view` caught everything the builder raised and then the route called
    `chart_context` unguarded, so a malformed stored payload 500'd the landing
    page along with the two panels either side of it.
    """
    import src.m6_views.api.pages as pages

    healthy = pages.chart_context
    seen = {"n": 0}

    def fails_once(env: object) -> dict[str, object]:
        seen["n"] += 1
        if seen["n"] == 1:
            raise KeyError("scheme_a")
        return healthy(env)  # type: ignore[arg-type]

    monkeypatch.setattr(pages, "chart_context", fails_once)
    response = client.get(f"/{QS}")

    assert response.status_code == 200
    assert "Could not be built" in response.text
    # The other two panels are untouched: three sections still render.
    assert response.text.count('class="view view--') == 3


def test_the_export_link_is_url_encoded() -> None:
    """`export_url` concatenated raw values, so a user_id or scope_id carrying
    an `&`, `=` or a space truncated the URL — the CSV route then fell back to
    its own `Query` defaults and exported a different scope than the panel
    above the link had displayed."""
    from src.m6_views.builder import Scope
    from src.m6_views.compose import export_url

    scope = Scope(
        user_id=UserId("USER ONE&admin=1"),
        as_of=AS_OF,
        scope_type="portfolio",
        scope_id="FOLIO 42&x",
    )
    url = export_url("fund_list", scope, {"top_n": 40})

    # Nothing after the first field can be read as a new parameter.
    assert url.count("?") == 1
    assert url.split("?")[1].count("&") == 3
    assert "admin=1" not in url.split("?")[1].replace("%26admin%3D1", "")
    assert "USER+ONE%26admin%3D1" in url
    assert "FOLIO+42%26x" in url


def test_a_populated_view_still_offers_a_working_export_link(
    client: TestClient,
) -> None:
    assert f"/api/export/fund_list.csv?user_id={USER}" in page(client, "fund_list")
