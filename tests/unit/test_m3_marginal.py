"""Marginal contribution, and M3's golden portfolio. MODULE_3.md §11, §18.8.

§18.8 asks for a permanent regression baseline with hand-verified figures. It
describes ~200 issuers; this is deliberately small instead, because a fixture
whose numbers were captured from the implementation verifies only that the
implementation did not change. Every number below can be checked with a
calculator, which is the same argument `scripts/verify_v0_ledger.py` makes by
recomputing the ledger longhand.

The portfolio, all four funds held at Rs 100,000 (total Rs 400,000):

    GROWTH   40% ALPHA, 35% BETA, 25% GAMMA
    VALUE    50% GAMMA, 50% DELTA
    TWIN     40% ALPHA, 35% BETA, 25% GAMMA      (identical to GROWTH)
    FOF      100% units of VALUE                  (expands to 50/50 GAMMA/DELTA)
    ORPHAN   60% EPSILON, 40% unresolved         (held only in the wider case)

Hand-computed exposures for the four-fund portfolio:

    ALPHA    GROWTH 40,000 + TWIN 40,000              =  80,000   20%
    BETA     GROWTH 35,000 + TWIN 35,000              =  70,000  17.5%
    GAMMA    GROWTH 25,000 + VALUE 50,000
             + TWIN 25,000 + FOF 50,000               = 150,000  37.5%
    DELTA    VALUE 50,000 + FOF 50,000                = 100,000   25%
                                                        -------
                                                        400,000  100%
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.decimals import connect
from src.common.types import IssuerId, SchemeId
from src.m3_lookthrough.concentration import concentration
from src.m3_lookthrough.engine import IssuerWeight, Position, compute_lookthrough
from src.m3_lookthrough.marginal import marginal_contribution
from src.m3_lookthrough.weights import rebuild_weights

from tests.conftest import migrated
from tests.unit.test_m3_fof import AS_OF, fund

LOT = Decimal("100000")


Portfolio = tuple[list[Position], dict[SchemeId, list[IssuerWeight]]]


@pytest.fixture
def portfolio(tmp_path: Path) -> Iterator[Portfolio]:
    db = tmp_path / "golden.db"
    migrated(db)
    conn: sqlite3.Connection = connect(str(db))
    for issuer in ("ALPHA", "BETA", "GAMMA", "DELTA", "EPSILON"):
        conn.execute(
            "INSERT INTO issuer (issuer_id, canonical_name, is_synthetic)"
            " VALUES (?,?,0)",
            (issuer, issuer),
        )
    fund(conn, "GROWTH", [("ALPHA", "40", None), ("BETA", "35", None),
                          ("GAMMA", "25", None)])
    fund(conn, "VALUE", [("GAMMA", "50", None), ("DELTA", "50", None)])
    fund(conn, "TWIN", [("ALPHA", "40", None), ("BETA", "35", None),
                        ("GAMMA", "25", None)])
    fund(conn, "FOF", [("__MFUNIT__", "100", "VALUE")])
    fund(conn, "ORPHAN", [("EPSILON", "60", None), ("__UNRESOLVED__", "40", None)])

    weights, _ = rebuild_weights(conn)
    positions = [
        Position(SchemeId(s), LOT) for s in ("GROWTH", "VALUE", "TWIN", "FOF")
    ]
    yield positions, weights
    conn.close()


# --- the golden baseline -----------------------------------------------------


def test_the_golden_portfolio_matches_the_hand_computation(
    portfolio: Portfolio,
) -> None:
    """Every figure in the module docstring, checked against the engine."""
    positions, weights = portfolio
    result = compute_lookthrough(positions, weights, AS_OF)
    by_issuer = {str(e.issuer_id): e.exposure_inr for e in result.exposures}

    assert by_issuer == {
        "ALPHA": Decimal(80_000),
        "BETA": Decimal(70_000),
        "GAMMA": Decimal(150_000),
        "DELTA": Decimal(100_000),
    }
    assert result.summary.total_value_inr == Decimal(400_000)
    assert sum(by_issuer.values()) == Decimal(400_000)


def test_the_fof_is_expanded_not_bucketed(
    portfolio: Portfolio,
) -> None:
    """FOF is 100% units of VALUE, so it must contribute GAMMA and DELTA.
    Before §6 it contributed nothing a person could name."""
    positions, weights = portfolio
    result = compute_lookthrough(positions, weights, AS_OF)

    assert "__MFUNIT__" not in {str(e.issuer_id) for e in result.exposures}
    fof = [c for c in result.contributions if str(c.scheme_id) == "FOF"]
    assert {str(c.issuer_id) for c in fof} == {"GAMMA", "DELTA"}
    assert sum(c.exposure_inr for c in fof) == LOT


def test_effective_n_over_the_equity_pool(
    portfolio: Portfolio,
) -> None:
    """HHI = 0.20^2 + 0.175^2 + 0.375^2 + 0.25^2
           = 0.04 + 0.030625 + 0.140625 + 0.0625
           = 0.27375
    so effective-N = 1 / 0.27375 = 3.6529... — four issuers that behave like
    3.65 because GAMMA carries more than its share.

    The first draft of this docstring said 0.2678125 and the engine disagreed.
    The engine was right. That is what a hand-checkable fixture is for: it
    catches the arithmetic in the comment, which nothing else does.
    """
    positions, weights = portfolio
    exposures = compute_lookthrough(positions, weights, AS_OF).exposures
    c = concentration(exposures, "equity")

    assert c.issuer_count == 4
    assert c.hhi == Decimal("0.273750")
    assert c.effective_n is not None
    assert abs(c.effective_n - Decimal("3.652968")) < Decimal("0.000002")


# --- marginal contribution ---------------------------------------------------


def test_a_duplicate_fund_adds_no_issuers(
    portfolio: Portfolio,
) -> None:
    """§11.1's question. TWIN holds exactly what GROWTH holds, so dropping it
    loses no issuer at all — the number that changes a decision."""
    positions, weights = portfolio
    m = marginal_contribution(positions, weights, AS_OF, SchemeId("TWIN"))

    assert m.new_issuers == 0
    assert m.new_exposure_inr == Decimal(0)
    assert m.position_inr == LOT


def test_a_fund_bringing_something_new_says_so(
    portfolio: Portfolio,
) -> None:
    """VALUE is the only route to DELTA except through FOF, which holds VALUE.
    Dropping VALUE alone still leaves DELTA, via FOF — so the honest answer is
    zero new issuers, and that is the case overlap alone would get wrong."""
    positions, weights = portfolio
    m = marginal_contribution(positions, weights, AS_OF, SchemeId("VALUE"))

    assert m.new_issuers == 0

    # Drop BOTH routes and DELTA really does disappear.
    without_fof = [p for p in positions if str(p.scheme_id) != "FOF"]
    m2 = marginal_contribution(without_fof, weights, AS_OF, SchemeId("VALUE"))
    assert m2.new_issuers == 1
    assert m2.new_exposure_inr == Decimal(50_000)


def test_a_concentrating_fund_raises_hhi(
    portfolio: Portfolio,
) -> None:
    """A positive delta means the fund concentrates the portfolio; §11 records
    that a negative one means diversifying."""
    positions, weights = portfolio
    m = marginal_contribution(positions, weights, AS_OF, SchemeId("TWIN"))

    assert m.hhi_with is not None and m.hhi_without is not None
    assert m.hhi_delta == m.hhi_with - m.hhi_without


def test_the_two_fields_no_data_supports_stay_none(
    portfolio: Portfolio,
) -> None:
    """style_shift_pp needs M2's style snapshot and fee_cost_inr a TER, which
    `WarehouseMarketDataProvider.ter` raises for by design (V0-18)."""
    positions, weights = portfolio
    m = marginal_contribution(positions, weights, AS_OF, SchemeId("GROWTH"))

    assert m.style_shift_pp is None
    assert m.fee_cost_inr is None


def test_a_scheme_not_held_reports_a_zero_position(
    portfolio: Portfolio,
) -> None:
    """ORPHAN is disclosed but not held. Asking what it adds is a fair
    question — it just adds it from a position of nothing."""
    positions, weights = portfolio
    m = marginal_contribution(positions, weights, AS_OF, SchemeId("ORPHAN"))

    assert m.position_inr == Decimal(0)
    assert m.new_issuers == 0


def test_dropping_the_only_fund_leaves_no_concentration_to_compare(
    portfolio: Portfolio,
) -> None:
    """A false zero here would say the fund changes nothing when it is
    everything, so the delta is None rather than 0."""
    _positions, weights = portfolio
    solo = [Position(SchemeId("GROWTH"), LOT)]
    m = marginal_contribution(solo, weights, AS_OF, SchemeId("GROWTH"))

    assert m.hhi_without is None
    assert m.hhi_delta is None
    assert m.effective_n_delta is None
    assert m.new_issuers == 3
    assert IssuerId("ALPHA")


def test_a_fund_with_no_disclosure_adds_an_unknown_not_nothing(
    portfolio: Portfolio,
) -> None:
    """DARK is held but has no disclosure, so the engine books it to
    __NO_DISCLOSURE__ and the difference finds no new issuer. "Adds 0
    companies" would be a confident wrong answer; what it adds is unknown."""
    positions, weights = portfolio
    held = [*positions, Position(SchemeId("DARK"), LOT)]
    m = marginal_contribution(held, weights, AS_OF, SchemeId("DARK"))

    assert m.position_inr == LOT
    assert m.new_issuers is None
    assert m.new_exposure_inr is None
    assert m.hhi_delta is None


def test_a_fund_held_in_two_folios_counts_both(portfolio: Portfolio) -> None:
    """One scheme, two folios, two positions. The first alone understated
    what the fund is worth to the portfolio by half."""
    positions, weights = portfolio
    held = [*positions, Position(SchemeId("GROWTH"), LOT)]
    m = marginal_contribution(held, weights, AS_OF, SchemeId("GROWTH"))

    assert m.position_inr == 2 * LOT
