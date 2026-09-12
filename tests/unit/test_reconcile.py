"""Reconciliation — the V0 gate.

MODULE_1.md §11, written before the implementation per `CLAUDE.md`'s working
agreement for `src/m1_ledger/`.

`PLAN.md` §7 V0 does not ship until every folio passes: a ledger that is 99%
right produces analytics that are confidently wrong, which is worse than none,
because you will act on them.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.fixtures import load_yaml
from src.common.types import SchemeId
from src.m1_ledger.lots import build_book
from src.m1_ledger.reconcile import (
    UNIT_TOL,
    VALUE_TOL,
    apply_gate,
    diagnose,
    nav_cross_check,
    reconcile,
    reconcile_all,
)
from src.m1_ledger.txn import Txn, load_transactions

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "v0_ledger"
HDFC = SchemeId("INF179K01UT0")
ICICI = SchemeId("INF109K01761")
KOTAK = SchemeId("INF174KA1EZ1")
AS_OF = date(2026, 9, 4)


@pytest.fixture(scope="module")
def txns() -> list[Txn]:
    return load_transactions(FIXTURES / "transactions.csv")


@pytest.fixture(scope="module")
def navs() -> dict[str, dict[date, Decimal]]:
    raw = load_yaml(FIXTURES / "nav_series.yaml")["series"]
    return {k: {d: Decimal(str(v)) for d, v in b["navs"].items()} for k, b in raw.items()}


# --- tolerances ------------------------------------------------------------


def test_tolerances_match_the_spec() -> None:
    """MODULE_1.md §11.1 and PLAN.md §7 V0, verbatim."""
    assert UNIT_TOL == Decimal("0.001")
    assert VALUE_TOL == Decimal("0.005")


# --- the happy path --------------------------------------------------------


def test_every_folio_reconciles(
    txns: list[Txn], navs: dict[str, dict[date, Decimal]]
) -> None:
    """The V0 gate. Every folio, units AND value, or V0 does not ship."""
    book = build_book(txns)
    results = reconcile_all(book, txns, navs, AS_OF)
    assert len(results) == 3
    for r in results:
        assert r.status == "ok", f"{r.folio}/{r.scheme_id}: {r.diagnosis}"
        assert abs(r.delta_units) <= UNIT_TOL
        assert r.confidence == "high"

    gate = apply_gate(results)
    assert gate.passed
    assert gate.failures == []


def test_computed_units_equal_the_statement_balance(
    txns: list[Txn], navs: dict[str, dict[date, Decimal]]
) -> None:
    """Reconciliation compares against an INDEPENDENT figure.

    Without the statement's printed balance there is nothing to reconcile to,
    only the ledger's internal self-consistency — which proves nothing about a
    missing CAS period.
    """
    book = build_book(txns)
    for r in reconcile_all(book, txns, navs, AS_OF):
        assert r.units_reported is not None
        assert r.units_computed == r.units_reported
        assert r.delta_units == Decimal(0)


# --- failure paths ---------------------------------------------------------


def _with_inflated_balance(
    txns: list[Txn], scheme_id: SchemeId, extra: Decimal
) -> list[Txn]:
    """The signature of a missing early statement.

    NOT simulated by deleting a transaction: the lot engine correctly refuses
    to process later transactions that depend on the deleted units, raising
    InsufficientUnits before reconciliation is ever reached. A missing CAS
    period presents as a reported balance the ledger cannot explain — units
    the statement knows about and the ledger has never seen.
    """
    out = []
    for t in txns:
        if t.scheme_id == scheme_id and t.units_balance_rep is not None:
            out.append(replace(t, units_balance_rep=t.units_balance_rep + extra))
        else:
            out.append(t)
    return out


def test_missing_early_statement_is_detected_and_diagnosed(
    txns: list[Txn], navs: dict[str, dict[date, Decimal]]
) -> None:
    """Computed < reported means units exist that no imported statement explains.

    MODULE_1.md §11.2: the system must say WHY, not merely that. The actionable
    output is "import the statement covering before <date>", not "mismatch".
    """
    inflated = _with_inflated_balance(txns, HDFC, Decimal("2.500000"))
    book = build_book(inflated)
    results = [
        r for r in reconcile_all(book, inflated, navs, AS_OF) if r.scheme_id == HDFC
    ]
    assert len(results) == 1
    r = results[0]
    assert r.status == "fail"
    assert r.delta_units == Decimal("-2.500000")
    assert "MISSING_EARLY_CAS" in r.diagnosis
    assert r.confidence == "low"


def test_a_failure_excludes_the_position_from_aggregates(
    txns: list[Txn], navs: dict[str, dict[date, Decimal]]
) -> None:
    """MODULE_1.md §11.3. Low-confidence positions are EXCLUDED, not shown."""
    inflated = _with_inflated_balance(txns, HDFC, Decimal("2.500000"))
    book = build_book(inflated)
    results = reconcile_all(book, inflated, navs, AS_OF)
    gate = apply_gate(results)

    assert not gate.passed
    assert [f.scheme_id for f in gate.failures] == [HDFC]
    assert HDFC in gate.excluded_scheme_ids
    # The other two folios are untouched: one bad folio does not condemn them,
    # and a surplus elsewhere must never net away a shortfall here.
    assert {r.scheme_id for r in results if r.status == "ok"} == {ICICI, KOTAK}


def test_no_reported_balance_warns_rather_than_passing(
    txns: list[Txn], navs: dict[str, dict[date, Decimal]]
) -> None:
    """A folio with nothing to check against is not a folio that reconciled.

    Returning 'ok' here would let an unverifiable position into the aggregates
    wearing high confidence.
    """
    stripped = [replace(t, units_balance_rep=None) for t in txns]
    book = build_book(stripped)
    for r in reconcile_all(book, stripped, navs, AS_OF):
        assert r.status == "warn"
        assert "NO_REPORTED_BALANCE" in r.diagnosis
        assert r.confidence == "medium"


def test_unit_tolerance_is_enforced_at_the_boundary(
    txns: list[Txn], navs: dict[str, dict[date, Decimal]]
) -> None:
    """0.001 units passes; anything beyond it does not."""
    book = build_book(txns)
    base = next(r for r in reconcile_all(book, txns, navs, AS_OF) if r.scheme_id == HDFC)
    computed = base.units_computed

    inside = reconcile(
        HDFC, "F0001/22", computed, computed - Decimal("0.001"), AS_OF, navs[HDFC], []
    )
    outside = reconcile(
        HDFC, "F0001/22", computed, computed - Decimal("0.002"), AS_OF, navs[HDFC], []
    )
    assert inside.status == "ok"
    assert outside.status == "fail"


# --- the value check, and what it cannot do --------------------------------


def test_spec_value_ratio_alone_still_cannot_see_a_wrong_nav_series(
    navs: dict[str, dict[date, Decimal]],
) -> None:
    """DECISIONS V0-12, first half: WHY the spec's check needed replacing.

    §11.1 computes `v_c = computed * nav` and `v_r = reported * nav` with the
    SAME nav, so it cancels:

        d_val = |computed-reported| * nav / (reported * nav)
              = |computed-reported| / reported

    With no transactions there is no witness to cross-check against, so this is
    the spec's arithmetic in isolation: the value ratio is identical whichever
    NAV series is attached, and is therefore blind to the substitution.
    """
    units = Decimal("4.417226")
    right = reconcile(HDFC, "F0001/22", units, units, AS_OF, navs[HDFC], [])
    wrong = reconcile(HDFC, "F0001/22", units, units, AS_OF, navs[KOTAK], [])

    assert right.delta_value_pct == wrong.delta_value_pct == Decimal(0)
    assert right.nav_check is None and wrong.nav_check is None


def test_a_wrong_nav_series_now_fails_reconciliation(
    txns: list[Txn], navs: dict[str, dict[date, Decimal]]
) -> None:
    """DECISIONS V0-12, second half: the gate now catches it.

    Units are perfectly correct and the value ratio is zero — the exact shape
    of the real V0-05 mismatch, where an HDFC Direct NAV series sat against a
    Regular scheme record 10.13% away. Under the spec's two checks that passes.

    With the NAV cross-check as a gate condition it fails, and says why.
    """
    hdfc = [t for t in txns if t.scheme_id == HDFC]
    computed = build_book(txns).units_remaining(HDFC)

    good = reconcile(HDFC, "F0001/22", computed, computed, AS_OF, navs[HDFC], hdfc)
    assert good.status == "ok"
    assert good.nav_check is not None and good.nav_check.mismatched == 0

    # Same units, same reported balance, a different scheme's NAV series.
    bad = reconcile(HDFC, "F0001/22", computed, computed, AS_OF, navs[KOTAK], hdfc)
    assert bad.status == "fail"
    assert bad.delta_units == Decimal(0), "units are not what went wrong"
    assert bad.delta_value_pct == Decimal(0), "nor is the spec's value ratio"
    assert "WRONG_NAV_SERIES" in bad.diagnosis
    assert "LIKELY_PLAN_MISMATCH_DIRECT_VS_REGULAR" in bad.diagnosis
    assert not apply_gate([bad]).passed


def test_nav_cross_check_catches_what_the_value_check_cannot(
    txns: list[Txn], navs: dict[str, dict[date, Decimal]]
) -> None:
    """The check that actually delivers §11.1's stated purpose.

    A statement prints the NAV each transaction was priced at. Comparing it to
    our resolved scheme's NAV on the same date detects a wrong-plan resolution
    directly — which is how the real Direct-vs-Regular mismatch in V0-05 would
    have been caught rather than passing both spec checks.
    """
    hdfc_txns = [t for t in txns if t.scheme_id == HDFC]

    ok = nav_cross_check(hdfc_txns, navs[HDFC])
    assert ok.matched > 0
    assert ok.mismatched == 0
    assert ok.status == "ok"

    # The same transactions checked against a different scheme's NAV series:
    # every printed NAV now disagrees with what we would have used.
    bad = nav_cross_check(hdfc_txns, navs[KOTAK])
    assert bad.mismatched == bad.checked
    assert bad.status == "fail"
    assert bad.worst_deviation_pct > Decimal("50")


def test_nav_cross_check_tolerates_a_missing_nav(
    txns: list[Txn], navs: dict[str, dict[date, Decimal]]
) -> None:
    """A day the fund did not price is not a mismatch, it is an absence."""
    kotak = [t for t in txns if t.scheme_id == KOTAK]
    sparse = {d: v for d, v in navs[KOTAK].items() if d != kotak[0].txn_date}
    result = nav_cross_check(kotak, sparse)
    assert result.unavailable >= 1
    assert result.mismatched == 0


# --- the diagnostic ladder -------------------------------------------------


def test_diagnose_names_a_cause_rather_than_returning_empty() -> None:
    """MODULE_1.md §11.2. 'UNKNOWN' is a diagnosis; silence is not."""
    assert diagnose(Decimal("-5"), starts_at_zero=False, txn_count=10) == [
        "MISSING_EARLY_CAS"
    ]
    assert "MISSING_REDEMPTION" in diagnose(
        Decimal("5"), starts_at_zero=True, txn_count=10
    )
    assert diagnose(Decimal("-5"), starts_at_zero=True, txn_count=10) == ["UNKNOWN"]


def test_diagnose_flags_float_contamination_on_a_long_history() -> None:
    """A tiny delta across hundreds of transactions is the float signature.

    Exactly the drift `test_sip_accumulation_float_drifts_decimal_does_not`
    demonstrates: below the 0.001 gate, so it never trips the tolerance — it
    just makes the number quietly wrong.
    """
    found = diagnose(Decimal("0.0004"), starts_at_zero=True, txn_count=240)
    assert "FLOAT_CONTAMINATION" in found
    # The same delta on a short history is not evidence of accumulation.
    assert "FLOAT_CONTAMINATION" not in diagnose(
        Decimal("0.0004"), starts_at_zero=True, txn_count=5
    )


def test_folios_reconcile_separately_and_never_net_against_each_other(
    txns: list[Txn], navs: dict[str, dict[date, Decimal]]
) -> None:
    """`PLAN.md` §9.7. Folios aggregate for display; lots and tax stay scoped.

    The same fund held in two folios, one reporting 2 units more than the
    ledger and the other 2 units fewer. Netted across folios the delta is
    exactly zero and both errors vanish; scoped per folio, both fail.

    Found by mutation — dropping the folio predicate left the whole suite
    green, because every folio in the fixture holds a different scheme.
    """
    sip = next(t for t in txns if t.txn_ref == "T001")
    a = replace(sip, txn_ref="X01", folio="F-AAA/01")
    b = replace(sip, txn_ref="X02", folio="F-BBB/02")

    over = replace(a, units_balance_rep=(a.units or Decimal(0)) + Decimal("2"))
    under = replace(b, units_balance_rep=(b.units or Decimal(0)) - Decimal("2"))

    book = build_book([over, under])
    results = reconcile_all(book, [over, under], navs, AS_OF)

    assert len(results) == 2, "one result per folio, not one per scheme"
    assert {r.folio for r in results} == {"F-AAA/01", "F-BBB/02"}
    assert all(r.status == "fail" for r in results)
    assert sorted(r.delta_units for r in results) == [Decimal("-2"), Decimal("2")]

    # The very thing folio scoping prevents: the errors sum to nothing.
    assert sum((r.delta_units for r in results), Decimal(0)) == Decimal(0)
    assert not apply_gate(results).passed


@pytest.mark.parametrize(
    ("deviation_pct", "expect_match"),
    [
        ("0.0", True),
        ("0.4", True),  # inside tolerance: statement rounding
        ("0.6", False),  # beyond it: a different scheme
        ("1.0", False),  # a young Direct/Regular pair sits about here
    ],
)
def test_nav_match_tolerance_at_the_boundary(
    txns: list[Txn],
    navs: dict[str, dict[date, Decimal]],
    deviation_pct: str,
    expect_match: bool,
) -> None:
    """Where "the statement rounded" ends and "wrong scheme" begins.

    The fixture's wrong-series deviation is ~5,600%, so every tolerance from
    0.5% to 100% classifies it identically — widening the constant 200x changed
    no outcome and no test noticed. Found by mutation.

    The boundary matters in practice: Direct and Regular plans of a recently
    launched fund differ by roughly the annual TER gap, so within the first year
    the deviation is around 1%. Set the tolerance too wide and the ~1%/year
    silent error walks straight through the gate it was built to stop.
    """
    scale = Decimal(1) + Decimal(deviation_pct) / 100
    shifted = [
        replace(t, nav=t.nav * scale) if t.nav is not None else t
        for t in txns
        if t.scheme_id == HDFC
    ]
    result = nav_cross_check(shifted, navs[HDFC])

    assert result.checked > 0
    if expect_match:
        assert result.mismatched == 0, f"{deviation_pct}% should be within tolerance"
        assert result.status == "ok"
    else:
        assert result.mismatched == result.checked
        assert result.status == "fail"
