"""The look-through engine. MODULE_3.md §2, §5, §8, §9.

Written before the implementation, per `CLAUDE.md`'s working agreement.

§2.2 calls closure *"the single most important test in the module"*, and names
the three ways it breaks: raw `pct_to_nav` used instead of `pct_normalised`,
unresolved holdings dropped by an inner join, and schemes with no disclosure
contributing silently zero. Each has a test here, because a look-through that
quietly under-reports is worse than none — the user concludes they are less
concentrated than they are.

Everything aggregates in Python with `Decimal`. `CLAUDE.md` invariant 1's second
clause forbids doing it in SQL, and this module is precisely where that would
have been tempting.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from src.common.types import IssuerId, SchemeId
from src.m3_lookthrough.concentration import concentration, filter_scope
from src.m3_lookthrough.engine import (
    ClosureViolation,
    IssuerWeight,
    Position,
    WeightsNotNormalised,
    assert_closure,
    assert_weights_sum_to_100,
    compute_lookthrough,
)
from src.m3_lookthrough.overlap import pairwise_overlap

AS_OF = date(2026, 7, 31)
UNRESOLVED = IssuerId("__UNRESOLVED__")
NO_DISCLOSURE = IssuerId("__NO_DISCLOSURE__")


def w(issuer: str, weight: str, klass: str = "equity") -> IssuerWeight:
    return IssuerWeight(IssuerId(issuer), Decimal(weight), klass)


def pos(scheme: str, value: str) -> Position:
    return Position(SchemeId(scheme), Decimal(value))


# --- §2.2 closure ------------------------------------------------------------


def test_closure_holds_exactly_on_a_simple_portfolio() -> None:
    """§2.2's identity: Σ exposure == Σ position value."""
    positions = [pos("S1", "100000"), pos("S2", "50000")]
    weights = {
        SchemeId("S1"): [w("ACME", "60"), w("BETA", "40")],
        SchemeId("S2"): [w("BETA", "30"), w("GAMMA", "70")],
    }
    result = compute_lookthrough(positions, weights, AS_OF)

    total = sum((e.exposure_inr for e in result.exposures), Decimal(0))
    assert total == Decimal("150000")
    assert result.summary.total_value_inr == Decimal("150000")

    by_issuer = {str(e.issuer_id): e.exposure_inr for e in result.exposures}
    assert by_issuer["ACME"] == Decimal("60000")      # 60% of 100000
    assert by_issuer["BETA"] == Decimal("55000")      # 40% of 100000 + 30% of 50000
    assert by_issuer["GAMMA"] == Decimal("35000")     # 70% of 50000


def test_an_issuer_held_by_two_funds_aggregates_and_counts_both() -> None:
    """§2.3: the exposure unit is the issuer, and `fund_count` says how many."""
    positions = [pos("S1", "100000"), pos("S2", "100000")]
    weights = {
        SchemeId("S1"): [w("ACME", "100")],
        SchemeId("S2"): [w("ACME", "100")],
    }
    result = compute_lookthrough(positions, weights, AS_OF)
    assert len(result.exposures) == 1
    only = result.exposures[0]
    assert only.exposure_inr == Decimal("200000")
    assert only.fund_count == 2
    assert only.pct_of_portfolio == Decimal("100")


def test_a_scheme_with_no_disclosure_becomes_a_visible_bucket() -> None:
    """§5.4. It contributes its full value to `__NO_DISCLOSURE__`.

    The alternative — an inner join that drops it — keeps closure *looking*
    fine while the portfolio silently shrinks, which is the failure §2.2 exists
    to prevent.
    """
    positions = [pos("S1", "100000"), pos("S_UNKNOWN", "40000")]
    weights = {SchemeId("S1"): [w("ACME", "100")]}
    result = compute_lookthrough(positions, weights, AS_OF)

    by_issuer = {str(e.issuer_id): e for e in result.exposures}
    assert str(NO_DISCLOSURE) in by_issuer
    assert by_issuer[str(NO_DISCLOSURE)].exposure_inr == Decimal("40000")
    assert by_issuer[str(NO_DISCLOSURE)].is_synthetic

    assert sum((e.exposure_inr for e in result.exposures), Decimal(0)) == Decimal(
        "140000"
    )
    # §5.4's coverage: only schemes that produced real weights count as covered.
    assert result.summary.covered_value_inr == Decimal("100000")
    assert result.summary.coverage_pct == Decimal("71.428571")
    assert any("disclosure" in c.lower() for c in result.caveats)


def test_unresolved_holdings_stay_visible(  # §5.3
) -> None:
    """§5.3: never filter `__UNRESOLVED__`. Missing mass must stay on the page."""
    positions = [pos("S1", "100000")]
    weights = {
        SchemeId("S1"): [w("ACME", "95"), w("__UNRESOLVED__", "5", "unknown")]
    }
    result = compute_lookthrough(positions, weights, AS_OF)

    by_issuer = {str(e.issuer_id): e for e in result.exposures}
    assert str(UNRESOLVED) in by_issuer
    assert by_issuer[str(UNRESOLVED)].exposure_inr == Decimal("5000")
    assert by_issuer[str(UNRESOLVED)].is_synthetic
    assert result.summary.unresolved_pct == Decimal("5")
    # §5.3 rule 4: a caveat once it exceeds 2%.
    assert any("unresolved" in c.lower() for c in result.caveats)


def test_closure_is_asserted_not_merely_available() -> None:
    """§5.2: *"run this on every computation, not just in tests."*"""
    with pytest.raises(ClosureViolation, match="delta"):
        assert_closure(Decimal("99998"), Decimal("100000"))
    assert_closure(Decimal("100000.50"), Decimal("100000"))  # inside ±1


def test_weights_that_do_not_sum_to_100_are_refused() -> None:
    """§5.2. If this fires the bug is in M0's `normalise_weights`, not here."""
    with pytest.raises(WeightsNotNormalised):
        assert_weights_sum_to_100(
            [w("ACME", "60"), w("BETA", "30")], SchemeId("S1"), AS_OF
        )
    assert_weights_sum_to_100(
        [w("ACME", "60"), w("BETA", "40")], SchemeId("S1"), AS_OF
    )


def test_the_engine_refuses_weights_that_do_not_normalise() -> None:
    """The assertion is wired into the engine, not left for a caller to remember."""
    positions = [pos("S1", "100000")]
    weights = {SchemeId("S1"): [w("ACME", "60"), w("BETA", "30")]}
    with pytest.raises(WeightsNotNormalised):
        compute_lookthrough(positions, weights, AS_OF)


def test_a_zero_unit_position_is_excluded() -> None:
    """§5.1 filters to `units > 0`; a closed position is not an exposure."""
    positions = [pos("S1", "100000"), pos("S2", "0")]
    weights = {
        SchemeId("S1"): [w("ACME", "100")],
        SchemeId("S2"): [w("BETA", "100")],
    }
    result = compute_lookthrough(positions, weights, AS_OF)
    assert {str(e.issuer_id) for e in result.exposures} == {"ACME"}


# --- §8 concentration --------------------------------------------------------


def test_synthetics_are_excluded_from_the_denominator() -> None:
    """§8.2, "the denominator trap".

    Including `__CASH__` inflates effective-N and understates concentration —
    precisely the number the product exists to surface. Here the real issuers
    are 50/50 within their own pool, so effective-N is 2, not 3.
    """
    positions = [pos("S1", "100000")]
    weights = {
        SchemeId("S1"): [
            w("ACME", "40"), w("BETA", "40"), w("__CASH__", "20", "cash")
        ]
    }
    result = compute_lookthrough(positions, weights, AS_OF)
    assert len(result.exposures) == 3

    metrics = concentration(result.exposures, "all")
    assert metrics.issuer_count == 2
    assert metrics.hhi == Decimal("0.5")
    assert metrics.effective_n == Decimal(2)
    assert metrics.top1_pct == Decimal(50)
    assert str(metrics.largest_issuer_id) in ("ACME", "BETA")


def test_scope_filters_to_an_instrument_class() -> None:
    """§8.2. `equity` is the headline; `all` is the audit view."""
    exposures = compute_lookthrough(
        [pos("S1", "100000")],
        {SchemeId("S1"): [
            w("ACME", "50"), w("GILT", "30", "debt"), w("__CASH__", "20", "cash")
        ]},
        AS_OF,
    ).exposures
    assert {str(e.issuer_id) for e in filter_scope(exposures, "equity")} == {"ACME"}
    assert {str(e.issuer_id) for e in filter_scope(exposures, "debt")} == {"GILT"}
    assert {str(e.issuer_id) for e in filter_scope(exposures, "all")} == {
        "ACME", "GILT"
    }
    with pytest.raises(ValueError):
        filter_scope(exposures, "nonsense")


def test_concentration_of_a_single_issuer_is_total() -> None:
    """One issuer: HHI 1, effective-N 1. The degenerate case should not divide by zero."""
    result = compute_lookthrough(
        [pos("S1", "100000")], {SchemeId("S1"): [w("ACME", "100")]}, AS_OF
    )
    metrics = concentration(result.exposures, "all")
    assert metrics.hhi == Decimal(1)
    assert metrics.effective_n == Decimal(1)
    assert metrics.top1_pct == Decimal(100)


def test_concentration_of_an_empty_pool_does_not_divide_by_zero() -> None:
    """A portfolio of nothing but cash has no equity concentration to report."""
    result = compute_lookthrough(
        [pos("S1", "100000")],
        {SchemeId("S1"): [w("__CASH__", "100", "cash")]},
        AS_OF,
    )
    metrics = concentration(result.exposures, "equity")
    assert metrics.issuer_count == 0
    assert metrics.hhi is None
    assert metrics.effective_n is None


def test_top_n_saturates_rather_than_overruns() -> None:
    """`top10_pct` of a three-issuer portfolio is 100, not an IndexError."""
    result = compute_lookthrough(
        [pos("S1", "300000")],
        {SchemeId("S1"): [w("A", "50"), w("B", "30"), w("C", "20")]},
        AS_OF,
    )
    metrics = concentration(result.exposures, "all")
    assert metrics.top1_pct == Decimal(50)
    assert metrics.top5_pct == Decimal(100)
    assert metrics.top10_pct == Decimal(100)
    assert metrics.top20_pct == Decimal(100)


# --- §9.1 overlap ------------------------------------------------------------


def test_identical_funds_overlap_completely() -> None:
    """The upper bound, and the sanity check that the formula is min-based."""
    a = [w("ACME", "60"), w("BETA", "40")]
    result = pairwise_overlap(SchemeId("S1"), SchemeId("S2"), AS_OF, AS_OF, a, a)
    assert result.overlap_pct == Decimal(100)
    assert result.jaccard == Decimal(1)
    assert result.common_issuers == 2
    assert result.union_issuers == 2
    assert result.aligned


def test_disjoint_funds_do_not_overlap() -> None:
    a = [w("ACME", "100")]
    b = [w("BETA", "100")]
    result = pairwise_overlap(SchemeId("S1"), SchemeId("S2"), AS_OF, AS_OF, a, b)
    assert result.overlap_pct == Decimal(0)
    assert result.jaccard == Decimal(0)
    assert result.common_issuers == 0
    assert result.union_issuers == 2


def test_partial_overlap_takes_the_minimum_of_each_shared_weight() -> None:
    """§9.1. The shared exposure is what BOTH funds hold, so `min` per issuer."""
    a = [w("ACME", "70"), w("BETA", "30")]
    b = [w("ACME", "20"), w("GAMMA", "80")]
    result = pairwise_overlap(SchemeId("S1"), SchemeId("S2"), AS_OF, AS_OF, a, b)
    assert result.overlap_pct == Decimal(20)
    assert result.common_issuers == 1
    assert result.union_issuers == 3
    assert result.jaccard == Decimal(1) / Decimal(3)


def test_overlap_is_computed_on_issuers_not_isins() -> None:
    """§2.3's warning, made a test.

    One fund holds the issuer's equity, the other its debt. Keying on ISIN
    would call that no overlap; keying on the issuer sees the same corporate
    exposure, which is the answer the user needs.
    """
    equity = [w("ACME", "100", "equity")]
    debt = [w("ACME", "100", "debt")]
    result = pairwise_overlap(
        SchemeId("S1"), SchemeId("S2"), AS_OF, AS_OF, equity, debt
    )
    assert result.overlap_pct == Decimal(100)
    assert result.common_issuers == 1


def test_synthetics_do_not_create_false_overlap() -> None:
    """Two unrelated funds both hold cash. That is not a shared holding.

    Counting `__CASH__` would give every pair of funds a floor of overlap and
    make the metric useless at exactly the low end where it should reassure.
    """
    a = [w("ACME", "80"), w("__CASH__", "20", "cash")]
    b = [w("BETA", "80"), w("__CASH__", "20", "cash")]
    result = pairwise_overlap(SchemeId("S1"), SchemeId("S2"), AS_OF, AS_OF, a, b)
    assert result.overlap_pct == Decimal(0)
    assert result.common_issuers == 0


def test_a_date_gap_is_reported_rather_than_hidden() -> None:
    """§9.1 carries `as_of_gap_days` and `aligned` so a soft comparison says so."""
    a = [w("ACME", "100")]
    result = pairwise_overlap(
        SchemeId("S1"), SchemeId("S2"), AS_OF, date(2026, 6, 30), a, a
    )
    assert result.as_of_gap_days == 31
    assert not result.aligned


def test_scheme_ids_are_ordered_so_a_pair_has_one_row() -> None:
    """§9.1 stores `min`/`max`, so (A,B) and (B,A) are the same key."""
    a = [w("ACME", "100")]
    forward = pairwise_overlap(SchemeId("Z"), SchemeId("A"), AS_OF, AS_OF, a, a)
    assert (str(forward.scheme_a), str(forward.scheme_b)) == ("A", "Z")


# --- on real materialised weights -------------------------------------------


def test_closure_holds_on_the_real_warehouse() -> None:
    """§2.2 on real weight vectors, not constructed ones.

    Skips without a loaded warehouse, like `test_cas_real`: the point is to run
    the closure identity against the 79- and 102-issuer weight sets that came
    out of two real disclosures, where an off-by-one in the collapse or a
    dropped synthetic would show up and a three-issuer fixture would not.

    One held scheme deliberately has no disclosure, because that is the real
    portfolio's actual shape — three funds held, disclosures parsed for one of
    them — and §5.4's bucket is what keeps closure intact through it.
    """
    from src.common.decimals import connect
    from src.m0_data.config import warehouse_path
    from src.m3_lookthrough.weights import latest_as_of, load_issuer_weights

    db = warehouse_path()
    if not db.exists():
        pytest.skip("no warehouse; run jobs.load_holdings first")
    conn = connect(str(db))

    weights_by_scheme = {}
    for scheme_id in (SchemeId("INF179K01UT0"), SchemeId("INF204K01E54")):
        as_of = latest_as_of(conn, scheme_id)
        if as_of is None:
            continue
        found = load_issuer_weights(conn, scheme_id, as_of)
        if found:
            weights_by_scheme[scheme_id] = found
    if not weights_by_scheme:
        pytest.skip("no materialised weights; run scripts.show_lookthrough")

    positions = [pos(str(s), "1000000.55") for s in weights_by_scheme]
    positions.append(pos("INF109K01761", "250000.45"))  # held, no disclosure

    result = compute_lookthrough(positions, weights_by_scheme, AS_OF)

    expected = sum((p.value_inr for p in positions), Decimal(0))
    got = sum((e.exposure_inr for e in result.exposures), Decimal(0))
    assert abs(got - expected) <= Decimal("1.00"), f"closure off by {got - expected}"

    # The undisclosed fund is visible rather than absent.
    buckets = {str(e.issuer_id) for e in result.exposures}
    assert str(NO_DISCLOSURE) in buckets
    assert result.summary.coverage_pct < Decimal(100)

    # And the real weight sets each normalise, or the engine would have raised.
    for scheme_id, found in weights_by_scheme.items():
        assert_weights_sum_to_100(found, scheme_id, AS_OF)


def test_the_prefix_helper_agrees_with_the_authoritative_flag() -> None:
    """§2.4: *"always filter via `issuer.is_synthetic` ... the flag is authoritative."*

    The engine is a pure function and never sees the issuer table, so it uses
    §2.4's own prefix helper. That is only safe while the two agree — and if a
    real issuer were ever named `__something__`, or a synthetic seeded without
    the flag, concentration would silently include or exclude the wrong rows.
    This is the check that the shortcut is still true of the data.
    """
    from src.common.decimals import connect
    from src.m0_data.config import warehouse_path
    from src.m3_lookthrough.engine import is_synthetic

    db = warehouse_path()
    if not db.exists():
        pytest.skip("no warehouse")
    conn = connect(str(db))
    rows = conn.execute("SELECT issuer_id, is_synthetic FROM issuer").fetchall()
    if not rows:
        pytest.skip("no issuers loaded")

    disagreements = [
        (issuer_id, flag)
        for issuer_id, flag in rows
        if is_synthetic(str(issuer_id)) != bool(flag)
    ]
    assert not disagreements, (
        f"{len(disagreements)} issuers where the __name__ convention and "
        f"issuer.is_synthetic disagree: {disagreements[:5]}"
    )


def test_the_engine_asserts_closure_on_every_computation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.2: *"run this on every computation, not just in tests."*

    Mutation testing found this gap: deleting the `assert_closure` call from
    `compute_lookthrough` survived a green suite, because every fixture here
    closes by construction and the safety net never fires. That makes it a net
    nobody has checked is attached.

    Asserting the call happens is the test that catches its removal, which is
    the property §5.2 actually asks for — the net matters precisely for the
    inputs no test anticipated.
    """
    from src.m3_lookthrough import engine

    calls: list[tuple[Decimal, Decimal]] = []
    real = engine.assert_closure

    def spy(got: Decimal, expected: Decimal) -> None:
        calls.append((got, expected))
        real(got, expected)

    monkeypatch.setattr(engine, "assert_closure", spy)
    compute_lookthrough(
        [pos("S1", "100000")], {SchemeId("S1"): [w("ACME", "100")]}, AS_OF
    )
    assert calls, "compute_lookthrough returned without asserting closure"
    assert calls[0] == (Decimal("100000"), Decimal("100000"))
