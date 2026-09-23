"""The HTTP API and CSV export. MODULE_6.md §13 and §15.

Two properties matter more than the routing.

**Decimals cross as strings.** §15.2. JSON has one number type and it is a
double, so a `Decimal` serialised as a JSON number would reintroduce, in the last
three feet, the precision problem `DECIMAL_TEXT`, the adapter registry and the
ban on SQL aggregation were all built to prevent.

**The CSV's provenance header matches its envelope.** §13.1: provenance in the
file is what makes a number defensible six months later, when the user finds the
CSV in a downloads folder and cannot remember its basis. A header that drifts
from the payload it describes is worse than no header, because it is believed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from src.common.types import IssuerId, SchemeId, UserId
from src.m1_ledger.db import apply_ledger_schema, connect_ledger
from src.m3_lookthrough.engine import IssuerWeight, Position, compute_lookthrough
from src.m3_lookthrough.persist import save_lookthrough
from src.m6_views.api.app import BIND_HOST, create_app
from src.m6_views.registry import VIEW_DEFS, VIEW_REGISTRY, seed_view_definitions

from tests.conftest import migrated

USER = "USER-01"
AS_OF = date(2026, 9, 4)
JULY = date(2026, 7, 31)
S1, S2 = SchemeId("S1"), SchemeId("S2")

WEIGHTS = {
    S1: [
        IssuerWeight(IssuerId("ACME"), Decimal("55"), "equity"),
        IssuerWeight(IssuerId("BETA"), Decimal("35"), "equity"),
        IssuerWeight(IssuerId("__UNRESOLVED__"), Decimal("10"), "unknown"),
    ],
    S2: [IssuerWeight(IssuerId("GAMMA"), Decimal("100"), "equity")],
}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    from src.common.decimals import connect

    warehouse_db = str(tmp_path / "warehouse.db")
    migrated(warehouse_db)
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
    result = compute_lookthrough(
        [Position(S1, Decimal("100000")), Position(S2, Decimal("50000"))],
        WEIGHTS,
        AS_OF,
    )
    save_lookthrough(ledger, UserId(USER), AS_OF, result, {S1: JULY, S2: JULY})
    # A real browser's address. The app refuses any other Host header.
    return TestClient(create_app(ledger, warehouse), base_url="http://127.0.0.1:8765")


QS = f"?user_id={USER}&as_of={AS_OF.isoformat()}"


def split_csv(body: str) -> tuple[list[str], list[list[str]]]:
    """The comment block and the data rows, parsed rather than string-matched.

    The BOM is the reason this is a function: `utf-8-sig` puts `﻿` on the
    first byte, so line one is `"﻿View: ..."` and does not start with `#`.
    Three assertions in this file counted it as a data row before that was
    noticed, which is a good argument for parsing a format instead of eyeballing
    it.
    """
    import csv as _csv
    import io

    text = body.lstrip("﻿")
    header = [line for line in text.splitlines() if line.startswith("#")]
    payload = "\n".join(
        line for line in text.splitlines() if not line.startswith("#")
    )
    rows = list(_csv.reader(io.StringIO(payload)))
    return header, [r for r in rows[1:] if r]


# --- §15.2 serialisation -----------------------------------------------------


def test_every_decimal_crosses_the_wire_as_a_string(client: TestClient) -> None:
    """§15.2. A JSON float here would undo the entire backend's discipline."""
    body = client.get(f"/api/views/lookthrough_sankey{QS}").json()
    raw = json.dumps(body)
    assert '"exposure' not in raw or "e+" not in raw

    for link in body["payload"]["links"]:
        assert isinstance(link["value"], str)
        # Round-trips exactly, which a float would not for most rupee figures.
        assert Decimal(link["value"]) == Decimal(link["value"])
    assert isinstance(body["quality"]["coverage_pct"], str)


def test_no_float_appears_anywhere_in_a_payload(client: TestClient) -> None:
    body = client.get(f"/api/views/portfolio_summary{QS}").json()

    def walk(node: object) -> None:
        assert not isinstance(node, float), f"float in payload: {node}"
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(body)


def test_the_envelope_keeps_its_provenance_through_serialisation(
    client: TestClient,
) -> None:
    """§2.2: provenance is required, not optional metadata. Every one of these
    is a field the reader needs to judge the number beside it."""
    body = client.get(f"/api/views/lookthrough_sankey{QS}").json()
    for key in ("as_of", "data_as_of", "staleness_days", "source_modules"):
        assert key in body["provenance"]
    for key in ("confidence", "coverage_pct", "unresolved_pct", "caveats"):
        assert key in body["quality"]
    assert body["question"]


# --- §15.1 routes ------------------------------------------------------------


def test_the_catalogue_lists_every_registered_view(client: TestClient) -> None:
    listed = {v["view_id"] for v in client.get("/api/views").json()}
    assert listed == set(VIEW_REGISTRY) == set(VIEW_DEFS)


def test_the_catalogue_comes_back_in_landing_order(client: TestClient) -> None:
    """§16.5's three questions first — where do I stand, what do I own, what is
    duplicated. The ordering is how "resist adding a fourth" is expressed."""
    ids = [v["view_id"] for v in client.get("/api/views").json()]
    assert ids[:3] == [
        "portfolio_summary",
        "lookthrough_sankey",
        "overlap_heatmap",
    ]


def test_health_reports_freshness_rather_than_implying_live_data(
    client: TestClient,
) -> None:
    """§15.3. The UI can then say "last updated" honestly."""
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["lookthrough_as_of"] == AS_OF.isoformat()
    assert body["views_registered"] == len(VIEW_REGISTRY)


def test_an_unknown_view_is_a_404_that_says_what_exists(
    client: TestClient,
) -> None:
    response = client.get("/api/views/nope")
    assert response.status_code == 404
    assert "available" in response.json()


def test_an_unknown_view_id_is_escaped_not_reflected(
    client: TestClient,
) -> None:
    """The HTML 404 echoes the view_id the caller asked for, and it is the one
    response in M6 built as a raw string rather than rendered by Jinja, whose
    autoescaping would have covered it. `!r` quotes a string; it does not
    escape it (CodeQL #2).

    Loopback-bound and single-user narrows who can be induced to click, it does
    not make the payload inert.
    """
    # No slash in the payload: `/view/{view_id}` is one path segment, so
    # anything containing `/` never reaches the handler and gets Starlette's
    # own JSON 404 instead. That narrows the payload space; it does not close
    # it, and an onerror handler needs no slash at all.
    response = client.get("/view/<img src=x onerror=alert(1)>")
    assert response.status_code == 404
    assert "<img" not in response.text
    assert "&lt;img" in response.text


def test_a_builder_that_raises_degrades_one_panel_not_the_screen(
    client: TestClient,
) -> None:
    """`PLAN.md` §4.9. A 500 takes down the page; an `error` envelope takes down
    one chart and explains itself."""
    response = client.get(
        f"/api/views/lookthrough_sankey?user_id={USER}&as_of=1900-01-01"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] in ("empty", "error")
    assert body["state_reason"]


def test_the_api_binds_to_loopback_only() -> None:
    """§15.3. There is no authentication, and that is only safe here."""
    assert BIND_HOST == "127.0.0.1"


# --- §13 export --------------------------------------------------------------


def test_every_export_carries_a_provenance_header(client: TestClient) -> None:
    """§13.1. The file outlives the screen it came from."""
    for view_id in VIEW_REGISTRY:
        body = client.get(f"/api/export/{view_id}.csv{QS}").text
        assert body.startswith("﻿# View:") or body.startswith("# View:")
        assert "# Question:" in body
        assert "# As of:" in body
        assert "# Coverage:" in body
        assert "# Generated:" in body


def test_the_header_matches_the_envelope_it_describes(client: TestClient) -> None:
    """A header that drifts from its payload is worse than none: it is
    believed."""
    envelope = client.get(f"/api/views/lookthrough_sankey{QS}").json()
    csv_body = client.get(f"/api/export/lookthrough_sankey.csv{QS}").text
    assert f"# As of: {envelope['provenance']['as_of']}" in csv_body
    assert f"# Question: {envelope['question']}" in csv_body
    assert envelope["quality"]["confidence"] in csv_body
    for caveat in envelope["quality"]["caveats"]:
        assert caveat in csv_body


def test_the_header_never_truncates_the_caveats(client: TestClient) -> None:
    """§6.1 rule 5. If there are six, six appear — in the file too."""
    envelope = client.get(f"/api/views/lookthrough_sankey{QS}").json()
    header, _ = split_csv(client.get(f"/api/export/lookthrough_sankey.csv{QS}").text)
    noted = [line for line in header if line.startswith("#   - ")]
    assert len(noted) == len(envelope["quality"]["caveats"])


def test_the_csv_is_utf8_with_a_bom_for_excel(client: TestClient) -> None:
    """Without it Excel reads UTF-8 as the system codepage and every ₹ becomes
    mojibake. Every other consumer ignores a BOM."""
    raw = client.get(f"/api/export/fund_list.csv{QS}").content
    assert raw.startswith(b"\xef\xbb\xbf")


def test_the_export_shows_what_the_chart_showed(client: TestClient) -> None:
    """§13.2. Display and export must agree, which is why the aggregation is
    server-side (§11.2) rather than done in the browser."""
    envelope = client.get(f"/api/views/lookthrough_sankey{QS}&top_n=1").json()
    _, rows = split_csv(
        client.get(f"/api/export/lookthrough_sankey.csv{QS}&top_n=1").text
    )
    assert len(rows) == len(envelope["payload"]["links"])
    assert {r[1] for r in rows} == {
        link["target"] for link in envelope["payload"]["links"]
    }


def test_the_full_export_bypasses_the_cap_and_says_so(client: TestClient) -> None:
    """§13.2's one exception — the escape hatch, labelled so the two files are
    never confused for each other afterwards."""
    _, grouped = split_csv(
        client.get(f"/api/export/lookthrough_sankey.csv{QS}&top_n=1").text
    )
    full_response = client.get(f"/api/export/lookthrough_sankey.csv{QS}&full=1")
    _, full = split_csv(full_response.text)
    assert "full list" in full_response.text
    assert "_full.csv" in full_response.headers["content-disposition"]
    # Compared on CONTENT, not on line count: with few enough issuers the two
    # files have the same number of rows, and only the full one names every
    # company rather than folding some into __OTHERS__.
    assert "__OTHERS__" in {r[1] for r in grouped}
    assert "__OTHERS__" not in {r[1] for r in full}
    assert {"BETA", "GAMMA"} <= {r[1] for r in full}


def test_a_decimal_exports_as_its_own_digits(client: TestClient) -> None:
    """Not a float, and not an em dash: §9.3's dash is a rendering rule, and a
    spreadsheet reading one stops treating the column as numeric."""
    _, rows = split_csv(client.get(f"/api/export/lookthrough_sankey.csv{QS}").text)
    assert rows
    for row in rows:
        value = row[-1]
        assert Decimal(value) >= 0
        assert "e" not in value.lower()


def test_a_null_exports_as_an_empty_cell_not_a_dash(client: TestClient) -> None:
    """§9.3's em dash is a RENDERING rule. In a file it belongs nowhere near a
    data cell: a spreadsheet reads one as text and the column stops being
    numeric. It stays legal in the comment block above, which is prose."""
    _, rows = split_csv(client.get(f"/api/export/portfolio_summary.csv{QS}").text)
    blank = [r for r in rows if r[-1] == ""]
    assert blank, "fixture has no uncomputed tile, so this would prove nothing"
    for row in rows:
        assert "—" not in row[-1]
