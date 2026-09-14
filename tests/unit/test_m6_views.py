"""The view builders. MODULE_6.md §19.1, §19.2, §19.5.

The most important test here is `test_upstream_caveats_reach_the_envelope`.
§19.2 calls caveat propagation *the honesty test*, and §3.2 is blunt about the
stakes: *"if a caveat exists upstream and isn't in this list, that is a bug."*

Staleness, coverage and the unresolved share are all computed in M0 through M3.
M6 is where they either become visible or quietly evaporate — a chart that drops
them is indistinguishable from one whose data never had a problem, and the user
concludes they know what they own.

The second most important is `test_the_unresolved_node_is_never_folded_away`.
Synthetics are small, so ordinary tail aggregation folds exactly the missing mass
into a bucket labelled "smaller holdings". §20's runbook lists it as a known
failure mode and Appendix A pins them outside `top_n` to prevent it.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from src.common.types import IssuerId, SchemeId, UserId, ViewState
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
from src.m6_views.builder import Scope
from src.m6_views.builders import portfolio  # noqa: F401  — registers builders
from src.m6_views.colors import assign_colors
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.registry import (
    VIEW_DEFS,
    VIEW_REGISTRY,
    seed_view_definitions,
)

from tests.conftest import migrated, reopen, reopen_ledger

USER = UserId("USER-01")
AS_OF = date(2026, 9, 4)
JULY = date(2026, 7, 31)
JUNE = date(2026, 6, 30)
KEY = "test-key-not-a-real-secret"

S1, S2 = SchemeId("S1"), SchemeId("S2")
DATES = {S1: JULY, S2: JUNE}
SCOPE = Scope(user_id=USER, as_of=AS_OF, scope_type="portfolio")


def w(issuer: str, weight: str, klass: str = "equity") -> IssuerWeight:
    return IssuerWeight(IssuerId(issuer), Decimal(weight), klass)


# A portfolio designed to exercise the honesty paths: a scheme with no
# disclosure (so coverage < 100), an unresolved slice, and cash.
WEIGHTS = {
    S1: [
        w("ACME", "55"),
        w("BETA", "30"),
        w("__UNRESOLVED__", "10", "unknown"),
        w("__CASH__", "5", "cash"),
    ],
    S2: [w("BETA", "60"), w("GAMMA", "40")],
}
POSITIONS = [
    Position(S1, Decimal("100000")),
    Position(S2, Decimal("50000")),
    Position(SchemeId("S_DARK"), Decimal("25000")),
]


@pytest.fixture
def warehouse(tmp_path: Path) -> sqlite3.Connection:
    from src.common.decimals import connect

    db = str(tmp_path / "warehouse.db")
    migrated(db)
    conn = connect(db)
    conn.execute(
        "INSERT OR REPLACE INTO issuer (issuer_id, canonical_name, is_listed)"
        " VALUES ('ACME', 'Acme Industries Ltd.', 1)"
    )
    conn.commit()
    return conn


@pytest.fixture
def ledger(tmp_path: Path) -> sqlite3.Connection:
    db = connect_ledger(str(tmp_path / "personal.db"), key=KEY)
    apply_ledger_schema(db)
    return db


@pytest.fixture
def deps(ledger: sqlite3.Connection, warehouse: sqlite3.Connection) -> Deps:
    result = compute_lookthrough(POSITIONS, WEIGHTS, AS_OF)
    save_lookthrough(ledger, USER, AS_OF, result, DATES)
    save_concentration(
        ledger, USER, AS_OF, [concentration(result.exposures, s) for s in SCOPES]
    )
    save_overlap(
        ledger, USER, AS_OF,
        [
            pairwise_overlap(
                S1, S2, JULY, JUNE, WEIGHTS[S1], WEIGHTS[S2],
                value_a=Decimal("100000"), value_b=Decimal("50000"),
            )
        ],
    )
    save_duplication(
        ledger, USER, AS_OF,
        portfolio_duplication(result.contributions, result.summary.total_value_inr),
    )
    ledger.execute(
        "INSERT INTO position (user_id, folio, scheme_id, as_of, units, nav,"
        " nav_date, market_value, reconciled, confidence, rebuilt_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(USER), "F1", "S1", AS_OF.isoformat(), Decimal("100"),
            Decimal("1000"), JULY.isoformat(), Decimal("100000"), 1, "high",
            "2026-09-04T00:00:00Z",
        ),
    )
    ledger.commit()
    return Deps.over(ledger, warehouse)


def build(view_id: str, deps: Deps, **params: Any) -> ViewEnvelope:
    return VIEW_REGISTRY[view_id](deps).build(SCOPE, params)


# --- §19.1 envelope completeness ---------------------------------------------


@pytest.mark.parametrize("view_id", sorted(VIEW_REGISTRY))
def test_every_builder_returns_a_complete_envelope(
    view_id: str, deps: Deps
) -> None:
    """The provenance fields have no defaults on the contract precisely so a
    view cannot omit its staleness by accident. This asserts the same thing
    from outside."""
    env = build(view_id, deps)
    assert env.question
    assert env.as_of and env.data_as_of
    assert env.staleness_days is not None
    assert env.confidence in ("high", "medium", "low")
    assert env.state in tuple(ViewState)
    assert isinstance(env.caveats, list)
    if env.state != ViewState.OK:
        assert env.state_reason, f"{view_id} missing state_reason"


@pytest.mark.parametrize("view_id", sorted(VIEW_REGISTRY))
def test_every_builder_declares_the_sources_its_definition_does(
    view_id: str,
) -> None:
    """§5.1: declared rather than inferred, so a cache key cannot go stale when
    a builder starts reading a new source."""
    assert VIEW_DEFS[view_id].requires_fields


def test_the_registry_and_the_catalogue_match_exactly() -> None:
    """§5.3. Both directions — a definition with no builder is a broken screen,
    a builder with no definition is code nothing routes to."""
    assert set(VIEW_REGISTRY) == set(VIEW_DEFS)


@pytest.mark.parametrize("view_id", sorted(VIEW_DEFS))
def test_every_view_states_one_question(view_id: str) -> None:
    """§2.3's forcing-function: if the question cannot be stated in a sentence,
    the view should not exist."""
    question = VIEW_DEFS[view_id].question
    assert question.endswith("?")
    assert question.count("?") == 1


# --- §19.2 caveat propagation, the honesty test ------------------------------


def test_upstream_caveats_reach_the_envelope(deps: Deps) -> None:
    """§3.2: a caveat that exists upstream and is not in this list is a bug.

    The fixture holds `S_DARK` with no disclosure and a 10% unresolved slice, so
    M3 stored caveats about both. They must arrive verbatim.
    """
    stored = deps.lookthrough.summary(USER, AS_OF).caveats
    assert stored, "fixture is not exercising the path"
    env = build("lookthrough_sankey", deps)
    for caveat in stored:
        assert caveat in env.caveats


def test_an_unresolved_share_above_the_threshold_is_stated(deps: Deps) -> None:
    env = build("lookthrough_sankey", deps)
    assert any("could not be identified" in c for c in env.caveats)


def test_incomplete_coverage_is_stated(deps: Deps) -> None:
    """`S_DARK` is a sixth of the portfolio with no disclosure at all."""
    env = build("lookthrough_sankey", deps)
    assert any("holdings data" in c for c in env.caveats)


def test_caveats_are_never_truncated(deps: Deps) -> None:
    """§6.1 rule 5. If there are six, six appear."""
    env = build("lookthrough_sankey", deps)
    assert len(env.caveats) == len(set(env.caveats))
    assert len(env.caveats) >= 3


def test_coverage_and_unresolved_ride_on_every_ok_envelope(deps: Deps) -> None:
    """§2.2: provenance is a required field, not optional metadata."""
    for view_id in sorted(VIEW_REGISTRY):
        env = build(view_id, deps)
        if env.state == ViewState.OK:
            assert env.coverage_pct is not None, view_id
            assert env.unresolved_pct is not None, view_id


def test_data_as_of_never_claims_to_be_fresher_than_the_staleness(deps: Deps) -> None:
    """An envelope saying "as of today" beside "43 days old" contradicts
    itself, and the reader believes whichever half suits them."""
    env = build("lookthrough_sankey", deps)
    assert env.staleness_days > 0
    assert env.data_as_of < env.as_of


# --- §19.5 aggregation -------------------------------------------------------


def test_the_unresolved_node_is_never_folded_away(deps: Deps) -> None:
    """Appendix A pins synthetics outside `top_n`. With top_n=1 everything else
    folds; the missing mass must still be its own visible node."""
    env = build("lookthrough_sankey", deps, top_n=1)
    ids = [n["id"] for n in env.payload["nodes"]]
    assert "__UNRESOLVED__" in ids
    assert "__NO_DISCLOSURE__" in ids
    assert "__OTHERS__" in ids


def test_a_folded_node_is_marked_truncated_and_caveated(deps: Deps) -> None:
    env = build("lookthrough_sankey", deps, top_n=1)
    assert env.truncated
    assert any("grouped" in c for c in env.caveats)


def test_aggregation_reduces_what_is_drawn_never_what_is_counted(deps: Deps) -> None:
    """§19.5. Σ(shown) + others == Σ(everything), to the rupee."""
    env = build("lookthrough_sankey", deps, top_n=1)
    drawn = sum(
        (Decimal(str(link["value"])) for link in env.payload["links"]),
        Decimal(0),
    )
    every = sum(
        (e.exposure_inr for e in deps.lookthrough.exposures(USER, AS_OF)),
        Decimal(0),
    )
    assert drawn == every


def test_closure_survives_the_view_layer(deps: Deps) -> None:
    """§2.2's identity, asserted on the payload M6 would actually serve.

    The engine asserts closure on what it computes and the provider round trip
    asserts it on what is read back. Nothing asserted it on what is *drawn*, and
    a link dropped by the tail split would be invisible to both.
    """
    env = build("lookthrough_sankey", deps)
    drawn = sum(
        (Decimal(str(link["value"])) for link in env.payload["links"]),
        Decimal(0),
    )
    total = deps.lookthrough.summary(USER, AS_OF).total_value_inr
    assert abs(drawn - total) <= Decimal(1)


def test_synthetic_nodes_carry_a_redundant_channel(deps: Deps) -> None:
    """§10.3: never encode by colour alone. A hatch pattern survives greyscale
    and colour-blind vision; a grey fill does not."""
    nodes = {n["id"]: n for n in build("lookthrough_sankey", deps).payload["nodes"]}
    assert nodes["__UNRESOLVED__"]["kind"] == "synthetic"
    assert nodes["__UNRESOLVED__"]["pattern"] == "hatch"
    assert nodes["ACME"]["pattern"] is None


def test_issuer_nodes_are_labelled_with_names_not_ids(deps: Deps) -> None:
    nodes = {n["id"]: n for n in build("lookthrough_sankey", deps).payload["nodes"]}
    assert nodes["ACME"]["label"] == "Acme Industries Ltd."


# --- §3.3 empty states name the fix ------------------------------------------


@pytest.mark.parametrize("view_id", sorted(VIEW_REGISTRY))
def test_an_unpopulated_database_gives_an_explained_absence(
    view_id: str, ledger: sqlite3.Connection, warehouse: sqlite3.Connection
) -> None:
    """A blank chart teaches the user the tool is broken; an explained absence
    teaches them how it works. "No data" is not an acceptable reason."""
    env = build(view_id, Deps.over(ledger, warehouse))
    assert env.state == ViewState.EMPTY
    assert env.state_reason
    assert env.state_reason != "No data"
    assert len(env.state_reason) > 40


# --- other views -------------------------------------------------------------


def test_the_overlap_view_marks_an_unaligned_pair(deps: Deps) -> None:
    """§9.3: never silently compare mismatched dates. The fixture's two funds
    disclosed a month apart."""
    env = build("overlap_heatmap", deps)
    assert env.payload["cells"][0]["aligned"] is False
    assert any("different disclosure dates" in c for c in env.caveats)


def test_the_duplication_view_carries_its_own_definition(deps: Deps) -> None:
    """Six months later nobody remembers whether this counted the issuer's
    whole exposure or only the part beyond its largest provider, and the two
    differ by roughly a factor of two."""
    env = build("duplication_summary", deps)
    assert "largest fund" in env.payload["definition"]


def test_the_summary_shows_uncomputed_tiles_as_null_not_zero(deps: Deps) -> None:
    """A zero XIRR is a claim. A null is an admission, and only one of them is
    true right now."""
    env = build("portfolio_summary", deps)
    tiles = {t["key"]: t["value"] for t in env.payload["tiles"]}
    assert tiles["portfolio_xirr"] is None
    assert tiles["blended_ter"] is None
    assert tiles["total_value_inr"] == Decimal("175000")


def test_the_fund_list_carries_reconciliation_per_row(deps: Deps) -> None:
    """`MODULE_1.md` §12.1: data quality is a property of the fact, not a
    page-level banner that applies to everything and therefore to nothing."""
    rows = build("fund_list", deps).payload["rows"]
    assert rows
    assert all("reconciled" in r and "confidence" in r for r in rows)


def test_the_concentration_view_returns_a_curve_and_its_summary(deps: Deps) -> None:
    env = build("concentration_curve", deps, scope="equity")
    assert env.payload["curve"][0]["issuer_share"] == Decimal(0)
    assert env.payload["curve"][-1]["exposure_share"] == Decimal(1)
    assert env.payload["hhi"] is not None


class TestWhatIsWrittenSurvivesTheConnection:
    """Every assertion here reads through a SECOND connection.

    Measured before these existed: deleting the `conn.commit()` from any of
    twelve functions in `src/` left `tests/unit` green, because a test that
    reads back on the connection that wrote sees uncommitted rows either way.
    The production failure is correct numbers reported and nothing stored.
    DECISIONS V1-58, `tests/conftest.py`.
    """

    def test_seed_view_definitions_commits(
        self, warehouse: sqlite3.Connection
    ) -> None:
        seed_view_definitions(warehouse)
        reopened = reopen(warehouse)
        seeded = reopened.execute("SELECT count(*) FROM view_definition").fetchone()[0]
        reopened.close()
        assert seeded > 0

    def test_assign_colors_commits(self, ledger: sqlite3.Connection) -> None:
        """`color_assignment` is a ZONE B table (`migrations/zone_b/004`), so
        this writes the ledger where `seed_view_definitions` writes Zone A."""
        assign_colors(ledger, "issuer", ["ACME", "BETA"])
        reopened = reopen_ledger(ledger, KEY)
        assigned = reopened.execute("SELECT count(*) FROM color_assignment").fetchone()[0]
        reopened.close()
        assert assigned > 0
