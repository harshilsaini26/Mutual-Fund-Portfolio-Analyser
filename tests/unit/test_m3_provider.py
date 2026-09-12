"""`SqliteLookThroughProvider` — the boundary M6 reads M3 through. §15.1.

Written before the implementation, per `CLAUDE.md`'s working agreement.

The load-bearing test in this file is `test_the_provider_does_not_change_a_number`.
An adapter sits between the only thing that computes and the only thing that
displays, which makes it the ideal place for a defect to hide: every test on
either side keeps passing while the screen shows something else. So the round
trip is asserted issuer-for-issuer and rupee-for-rupee, not on totals — a total
can survive two errors that cancel.

The rest of the file is about what the provider must refuse to do: invent a
number it was never given, report an unbuilt module as an empty result, or hand
back rows in whatever order SQLite felt like.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.types import IssuerId, SchemeId, UserId, WeightBasis
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
from src.m3_lookthrough.providers.lookthrough import LookThroughProvider
from src.m3_lookthrough.providers.sqlite import SqliteLookThroughProvider

USER = UserId("USER-01")
AS_OF = date(2026, 9, 4)
JULY = date(2026, 7, 31)
JUNE = date(2026, 6, 30)
KEY = "test-key-not-a-real-secret"

S1, S2, S3 = SchemeId("S1"), SchemeId("S2"), SchemeId("S3")
DATES = {S1: JULY, S2: JUNE, S3: JULY}


def w(issuer: str, weight: str, klass: str = "equity") -> IssuerWeight:
    return IssuerWeight(IssuerId(issuer), Decimal(weight), klass)


WEIGHTS = {
    S1: [w("ACME", "60"), w("BETA", "35"), w("__CASH__", "5", "cash")],
    S2: [w("BETA", "70"), w("GAMMA", "30")],
    S3: [w("ACME", "50"), w("DELTA", "50", "debt")],
}
POSITIONS = [
    Position(S1, Decimal("100000")),
    Position(S2, Decimal("50000")),
    Position(S3, Decimal("25000")),
]


@pytest.fixture
def warehouse(tmp_path: Path) -> sqlite3.Connection:
    """Zone A. `issuer` is seeded by migration 003, synthetics included."""
    from src.common.decimals import connect

    db = str(tmp_path / "warehouse.db")
    apply_migrations(db)
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
def result():  # type: ignore[no-untyped-def]
    return compute_lookthrough(POSITIONS, WEIGHTS, AS_OF)


@pytest.fixture
def provider(  # type: ignore[no-untyped-def]
    ledger: sqlite3.Connection, warehouse: sqlite3.Connection, result
) -> SqliteLookThroughProvider:
    """A fully populated store: §4.2's tables and all three of §4.3's."""
    save_lookthrough(ledger, USER, AS_OF, result, DATES)
    save_concentration(
        ledger, USER, AS_OF, [concentration(result.exposures, s) for s in SCOPES]
    )
    values = {p.scheme_id: p.value_inr for p in POSITIONS}
    save_overlap(
        ledger, USER, AS_OF,
        [
            pairwise_overlap(
                a, b, DATES[a], DATES[b], WEIGHTS[a], WEIGHTS[b],
                value_a=values[a], value_b=values[b],
            )
            for a, b in ((S1, S2), (S1, S3), (S2, S3))
        ],
    )
    save_duplication(
        ledger, USER, AS_OF,
        portfolio_duplication(result.contributions, result.summary.total_value_inr),
    )
    return SqliteLookThroughProvider(ledger, warehouse)


# --- the boundary must not change a number -----------------------------------


def test_the_provider_does_not_change_a_number(provider, result) -> None:  # type: ignore[no-untyped-def]
    """Issuer for issuer, rupee for rupee, against what the engine computed.

    Asserted per issuer rather than on the total, because a total survives two
    errors that cancel — and the pair that cancels is exactly the pair an
    adapter produces, since it reshapes every row the same way.
    """
    served = provider.exposures(USER, AS_OF)
    computed = {e.issuer_id: e for e in result.exposures}
    assert {e.issuer_id for e in served} == set(computed)
    for exposure in served:
        assert exposure.exposure_inr == computed[exposure.issuer_id].exposure_inr
        assert exposure.exposure_pct == computed[exposure.issuer_id].pct_of_portfolio
        assert exposure.via_funds == computed[exposure.issuer_id].fund_count
        assert isinstance(exposure.exposure_inr, Decimal)


def test_closure_still_holds_on_the_far_side_of_the_boundary(provider) -> None:  # type: ignore[no-untyped-def]
    """§2.2's identity, re-asserted on what M6 will actually receive.

    The engine asserts closure on what it computes. Nothing asserted it on what
    is read back, and a row dropped by a WHERE clause or a filter would be
    invisible to every test upstream of here.
    """
    served = provider.exposures(USER, AS_OF)
    total = sum((e.exposure_inr for e in served), Decimal(0))
    assert abs(total - provider.summary(USER, AS_OF).total_value_inr) <= Decimal(1)


def test_exposures_come_back_largest_first(provider) -> None:  # type: ignore[no-untyped-def]
    """§15.3: descending `exposure_inr`, always — M6 caches on payload order.

    `ORDER BY` cannot do this: the column is `DECIMAL_TEXT`, so SQLite sorts it
    as text and "5000" outranks "25000". The sort is in Python for that reason.
    """
    served = provider.exposures(USER, AS_OF)
    values = [e.exposure_inr for e in served]
    assert values == sorted(values, reverse=True)
    assert len(values) > 2


def test_ties_are_broken_by_issuer_id_not_by_insertion_order(  # type: ignore[no-untyped-def]
    ledger, warehouse, result
) -> None:
    save_lookthrough(ledger, USER, AS_OF, result, DATES)
    ledger.execute(
        "UPDATE lookthrough_exposure SET exposure_inr = '1000'"
        " WHERE user_id = ? AND as_of = ?",
        (str(USER), AS_OF.isoformat()),
    )
    ledger.commit()
    served = SqliteLookThroughProvider(ledger, warehouse).exposures(USER, AS_OF)
    assert [str(e.issuer_id) for e in served] == sorted(
        str(e.issuer_id) for e in served
    )


# --- Zone A x Zone B ---------------------------------------------------------


def test_issuer_names_come_from_zone_a(provider) -> None:  # type: ignore[no-untyped-def]
    assert provider.issuer_name(IssuerId("ACME")) == "Acme Industries Ltd."


def test_an_unknown_issuer_falls_back_to_its_id(provider) -> None:  # type: ignore[no-untyped-def]
    """The id is never wrong. A blank label in a Sankey is a node nobody can
    identify, which is worse than an ugly one."""
    assert provider.issuer_name(IssuerId("BETA")) == "BETA"


def test_every_exposure_carries_a_name(provider) -> None:  # type: ignore[no-untyped-def]
    assert all(e.issuer_name for e in provider.exposures(USER, AS_OF))


# --- §4.2 contributions ------------------------------------------------------


def test_contributions_name_every_fund_that_supplies_an_issuer(provider) -> None:  # type: ignore[no-untyped-def]
    """BETA is held by S1 and S2, so it must come back with both."""
    found = provider.contributions(USER, AS_OF, IssuerId("BETA"))
    assert {str(c.scheme_id) for c in found} == {"S1", "S2"}
    assert all(isinstance(c.exposure_inr, Decimal) for c in found)


def test_contributions_for_an_issuer_sum_to_its_exposure(provider) -> None:  # type: ignore[no-untyped-def]
    """§4.2's audit trail has to reconcile with the thing it audits."""
    exposure = {e.issuer_id: e for e in provider.exposures(USER, AS_OF)}
    for issuer in (IssuerId("BETA"), IssuerId("ACME")):
        routes = provider.contributions(USER, AS_OF, issuer)
        assert sum((c.exposure_inr for c in routes), Decimal(0)) == (
            exposure[issuer].exposure_inr
        )


# --- §8.2 scope --------------------------------------------------------------


def test_a_scoped_request_drops_the_synthetics(provider) -> None:  # type: ignore[no-untyped-def]
    """§8.2's denominator trap: __CASH__ is exposure but never an issuer."""
    equity = provider.exposures(USER, AS_OF, scope="equity")
    assert not any(e.is_synthetic for e in equity)
    assert all(e.instrument_class == "equity" for e in equity)


def test_the_all_scope_keeps_the_synthetics_visible(provider) -> None:  # type: ignore[no-untyped-def]
    """Appendix A pins __UNRESOLVED__ outside `top_n` so missing mass stays on
    screen. Filtering it out of `all` would delete it before M6 ever saw it."""
    everything = provider.exposures(USER, AS_OF, scope="all")
    assert any(e.is_synthetic for e in everything)


# --- §4.3 metrics ------------------------------------------------------------


def test_concentration_round_trips(provider, result) -> None:  # type: ignore[no-untyped-def]
    computed = concentration(result.exposures, "equity")
    served = provider.concentration(USER, AS_OF, "equity")
    assert served.hhi == computed.hhi
    assert served.top10_pct == computed.top10_pct
    assert served.issuer_count == computed.issuer_count


def test_largest_issuer_pct_is_the_top1_figure(provider) -> None:  # type: ignore[no-untyped-def]
    """§4.3 declares both columns. One NULL beside the other populated would
    read as two different quantities."""
    served = provider.concentration(USER, AS_OF, "equity")
    assert served.largest_issuer_pct == served.top1_pct


def test_overlap_comes_back_most_overlapping_first(provider) -> None:  # type: ignore[no-untyped-def]
    pairs = provider.overlap_matrix(USER, AS_OF)
    assert len(pairs) == 3
    assert [p.overlap_pct for p in pairs] == sorted(
        (p.overlap_pct for p in pairs), reverse=True
    )


def test_a_pair_is_found_in_either_argument_order(provider) -> None:  # type: ignore[no-untyped-def]
    forward = provider.pairwise_overlap(USER, AS_OF, S1, S2)
    reverse = provider.pairwise_overlap(USER, AS_OF, S2, S1)
    assert forward is not None
    assert forward == reverse


def test_an_unheld_pair_is_none_not_zero(provider) -> None:  # type: ignore[no-untyped-def]
    assert provider.pairwise_overlap(USER, AS_OF, S1, SchemeId("NOPE")) is None


def test_max_pairwise_overlap_is_the_maximum_of_the_matrix(provider) -> None:  # type: ignore[no-untyped-def]
    """M4's `overlap_max` limit delegates here, so the two must not disagree."""
    pairs = provider.overlap_matrix(USER, AS_OF)
    assert provider.max_pairwise_overlap(USER, AS_OF) == max(
        p.overlap_pct for p in pairs
    )


def test_max_pairwise_overlap_is_none_when_there_is_no_pair(  # type: ignore[no-untyped-def]
    ledger, warehouse
) -> None:
    """A single-fund portfolio has no overlap question. Zero would claim the
    funds were compared and found disjoint."""
    provider = SqliteLookThroughProvider(ledger, warehouse)
    assert provider.max_pairwise_overlap(USER, AS_OF) is None


def test_duplication_round_trips(provider, result) -> None:  # type: ignore[no-untyped-def]
    computed = portfolio_duplication(
        result.contributions, result.summary.total_value_inr
    )
    served = provider.duplication(USER, AS_OF)
    assert served is not None
    assert served[1] == computed.duplicated_inr
    assert served[2] == computed.issuers_multi_fund


# --- §5.1 the assembled result ----------------------------------------------


def test_lookthrough_assembles_the_whole_result_from_storage(provider) -> None:  # type: ignore[no-untyped-def]
    served = provider.lookthrough(USER, AS_OF)
    assert served.exposures
    assert served.contributions
    assert served.summary.total_value_inr == Decimal("175000")


def test_holdings_dates_carry_every_contributing_disclosure(provider) -> None:  # type: ignore[no-untyped-def]
    """D7: §12's `tilts()` picks its AMFI basis with `min(holdings_dates)`, so
    the earliest date has to be in the list rather than averaged away."""
    served = provider.lookthrough(USER, AS_OF)
    assert min(served.holdings_dates) == JUNE
    assert JULY in served.holdings_dates


# --- what is not built says so ----------------------------------------------


def test_summary_raises_rather_than_inventing_a_portfolio(  # type: ignore[no-untyped-def]
    ledger, warehouse
) -> None:
    """Nothing stored is not an empty portfolio. A zero-valued summary would
    render as a real screen reporting that the user owns nothing."""
    provider = SqliteLookThroughProvider(ledger, warehouse)
    with pytest.raises(LookupError, match="no look-through stored"):
        provider.summary(USER, AS_OF)


def test_concentration_raises_when_it_was_never_computed(  # type: ignore[no-untyped-def]
    ledger, warehouse
) -> None:
    provider = SqliteLookThroughProvider(ledger, warehouse)
    with pytest.raises(LookupError, match="no equity concentration"):
        provider.concentration(USER, AS_OF, "equity")


@pytest.mark.parametrize(
    "method,args,expected",
    [
        ("redundancy", (), "M2"),
        ("marginal", (S1,), "M2"),
        ("sector_exposure", ("current",), "taxonomy"),
    ],
)
def test_an_unbuilt_method_names_what_it_is_waiting_on(  # type: ignore[no-untyped-def]
    provider, method, args, expected
) -> None:
    """`[]` would read as "measured, and there is nothing" — the answer
    `PLAN.md` §3.3 and MODULE_6 §3.3 both exist to prevent."""
    with pytest.raises(NotImplementedError, match=expected):
        getattr(provider, method)(USER, AS_OF, *args)


# --- the protocol ------------------------------------------------------------


def test_the_provider_satisfies_the_frozen_protocol(provider) -> None:  # type: ignore[no-untyped-def]
    """Structural typing means nothing checks this unless something asserts it.

    `BUILD_ORDER.md` R1 requires M4's M3-block to be pure delegation, which only
    holds if the real provider is substitutable for the protocol M4 was written
    against.
    """
    served: LookThroughProvider = provider
    assert served.exposures(USER, AS_OF, WeightBasis.DISCLOSED)
