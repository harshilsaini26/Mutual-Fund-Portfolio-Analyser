"""Regressions for the V1.9 review. Written before the fixes.

Nine of these are in `src/m1_ledger/` and `src/m0_data/`, which `CLAUDE.md`
makes test-first. They are collected here rather than scattered into the
existing files because they share a subject: every one is a case where the code
produced a **plausible** answer instead of a correct one or an absent one, which
is the failure mode this project is built to refuse.

Two are reproduced against real SQLite rather than asserted in the abstract,
because the claim is about what SQLite does with a text-affinity column and an
assertion about that is worth nothing if it is only my belief.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from src.common.types import SchemeId, UserId
from src.m1_ledger.lots import (
    GRANDFATHER_DATE,
    InvalidOpeningTransaction,
    build_book,
)
from src.m1_ledger.reconcile import diagnose, nav_cross_check, reconcile
from src.m1_ledger.returns import Position, Returns, compute_returns, twrr
from src.m1_ledger.txn import Txn

USER = UserId("USER-01")
HDFC = SchemeId("INF179K01UT0")
AS_OF = date(2026, 3, 31)


def txn(
    ref: str,
    kind: str,
    when: date,
    units: str | None,
    amount: str | None,
    nav: str | None = None,
    balance: str | None = None,
    seq: int = 1,
) -> Txn:
    """`txn_id` is a property derived from the content, so it is not passed."""
    return Txn(
        txn_ref=ref,
        user_id=USER,
        folio="F1",
        scheme_id=HDFC,
        scheme_raw_name="HDFC Flexi Cap Fund - Direct Plan - Growth",
        txn_date=when,
        txn_seq=seq,
        txn_type=kind,
        units=Decimal(units) if units is not None else None,
        nav=Decimal(nav) if nav else None,
        amount=Decimal(amount) if amount is not None else None,
        stamp_duty=Decimal(0),
        stt=Decimal(0),
        exit_load=Decimal(0),
        switch_group_id=None,
        reverses_txn_ref=None,
        units_balance_rep=Decimal(balance) if balance else None,
    )


# --- reconciliation: a diagnosis that was always the same --------------------


def test_a_history_that_starts_at_zero_is_not_blamed_on_a_missing_early_cas() -> None:
    """`has_earlier` was `any(t.txn_date < min(those same dates))` — no member of
    a set is below its own minimum, so it was always False and MISSING_EARLY_CAS
    was emitted for every negative delta.

    The witness is in the statement: the first transaction prints a running
    balance. If it equals that transaction's own units, the position began
    there and the gap is elsewhere.
    """
    opened_here = [
        txn("T1", "PURCHASE", date(2024, 1, 1), "100", "100000", "1000", "100"),
        txn("T2", "PURCHASE", date(2024, 6, 1), "50", "55000", "1100", "150"),
    ]
    result = reconcile(
        scheme_id=HDFC, folio="F1", computed=Decimal("150"),
        reported=Decimal("200"), as_of=AS_OF, navs={}, txns=opened_here,
    )
    assert result.status == "fail"
    assert "MISSING_EARLY_CAS" not in result.diagnosis


def test_a_history_that_starts_mid_stream_does_name_the_missing_early_cas() -> None:
    """The first transaction's printed balance exceeds its own units, so units
    existed before anything we hold. That is the actionable case."""
    starts_late = [
        txn("T1", "PURCHASE", date(2024, 1, 1), "100", "100000", "1000", "450"),
    ]
    result = reconcile(
        scheme_id=HDFC, folio="F1", computed=Decimal("100"),
        reported=Decimal("450"), as_of=AS_OF, navs={}, txns=starts_late,
    )
    assert "MISSING_EARLY_CAS" in result.diagnosis


def test_diagnose_still_takes_an_explicit_answer() -> None:
    """The helper's contract is unchanged; only its caller stopped lying to it.
    `None` is the third state — the statement printed no running balance, so
    whether the history starts at zero is unknown."""
    assert diagnose(Decimal("-5"), starts_at_zero=False, txn_count=10) == [
        "MISSING_EARLY_CAS"
    ]
    assert diagnose(Decimal("-5"), starts_at_zero=True, txn_count=10) == ["UNKNOWN"]
    assert diagnose(Decimal("-5"), starts_at_zero=None, txn_count=10) == [
        "MISSING_EARLY_CAS"
    ]


# --- reconciliation: "checked nothing" is not "found no problem" -------------


def test_a_nav_cross_check_with_nothing_to_check_says_so() -> None:
    """`status = "fail" if mismatched else "ok"` could not tell 500 checked and
    0 mismatched from nothing checked at all. V0-05's wrong-plan case — HDFC
    Direct NAVs against a Regular record — reports success under the second."""
    priced = [txn("T1", "PURCHASE", date(2024, 1, 1), "100", "100000", "1000")]
    check = nav_cross_check(priced, navs={})
    assert check.checked == 0
    assert check.status == "unverified"


def test_an_unverifiable_folio_reconciles_as_warn_not_ok() -> None:
    """A `warn` is not a pass. The module already says so for a missing reported
    balance: "there was nothing to check against, so the position is unverified
    rather than verified-correct"."""
    priced = [txn("T1", "PURCHASE", date(2024, 1, 1), "100", "100000", "1000")]
    result = reconcile(
        scheme_id=HDFC, folio="F1", computed=Decimal("100"),
        reported=Decimal("100"), as_of=AS_OF, navs={}, txns=priced,
    )
    assert result.status == "warn"
    assert result.confidence != "high"
    assert "NAV_SERIES_UNVERIFIED" in result.diagnosis


def test_a_folio_whose_navs_all_agree_still_passes() -> None:
    """The other half: a real check that succeeds must stay `ok`/high."""
    priced = [txn("T1", "PURCHASE", date(2024, 1, 1), "100", "100000", "1000")]
    result = reconcile(
        scheme_id=HDFC, folio="F1", computed=Decimal("100"),
        reported=Decimal("100"), as_of=AS_OF,
        navs={date(2024, 1, 1): Decimal("1000")}, txns=priced,
    )
    assert result.status == "ok"
    assert result.confidence == "high"


# --- grandfathering: wired, not merely implemented --------------------------


def test_grandfathering_reaches_the_lot_it_was_built_for() -> None:
    """`build_book` never passed `grandfathered_nav`, so `effective_cost`
    returned early for every lot and §112A relief never applied — while every
    pre-2018 lot was simultaneously marked low confidence.

    The NAV exists: `backfill_nav` clamps `--from` to 31-Jan-2018 precisely to
    fetch it, and `persist.py` already writes the column.
    """
    bought = txn("T1", "PURCHASE", date(2015, 6, 1), "100", "50000", "500")
    sold = txn("T2", "REDEMPTION", date(2024, 6, 1), "-100", "300000", "3000", seq=2)
    book = build_book(
        [bought, sold], grandfathering_navs={str(HDFC): Decimal("2000")}
    )
    consumption = book.all_consumptions()[0]
    assert consumption.cost_basis_method == "grandfathered"
    # max(actual 500, min(FMV 2000, sale 3000)) x 100 units.
    assert consumption.cost_allocated == Decimal("200000.00")
    assert consumption.confidence == "high"


def test_a_pre_cutoff_lot_with_no_fmv_is_still_flagged_rather_than_computed() -> None:
    """§7.5's other half, unchanged: absent the FMV, flag instead of guessing."""
    bought = txn("T1", "PURCHASE", date(2015, 6, 1), "100", "50000", "500")
    sold = txn("T2", "REDEMPTION", date(2024, 6, 1), "-100", "300000", "3000", seq=2)
    consumption = build_book([bought, sold]).all_consumptions()[0]
    assert consumption.cost_basis_method == "actual"
    assert consumption.confidence == "low"


def test_a_post_cutoff_lot_is_never_grandfathered() -> None:
    bought = txn("T1", "PURCHASE", GRANDFATHER_DATE, "100", "50000", "500")
    sold = txn("T2", "REDEMPTION", date(2024, 6, 1), "-100", "300000", "3000", seq=2)
    book = build_book(
        [bought, sold], grandfathering_navs={str(HDFC): Decimal("2000")}
    )
    assert book.all_consumptions()[0].cost_basis_method == "actual"


# --- a purchase with no price ------------------------------------------------


def test_a_purchase_with_neither_amount_nor_nav_raises() -> None:
    """`abs(t.units * (t.nav or 0))` made `cost_total` the stamp duty alone, so
    the lot carried ~zero cost and reported essentially its whole proceeds as a
    capital gain. `CLAUDE.md` invariant 5: raise, don't clamp — a plausible
    wrong answer is worse than a crash."""
    priceless = txn("T1", "PURCHASE", date(2024, 1, 1), "100", None, None)
    with pytest.raises(InvalidOpeningTransaction, match="no amount and no NAV"):
        build_book([priceless])


def test_a_purchase_priced_by_nav_alone_still_works() -> None:
    """The legitimate case the guard must not catch."""
    by_nav = txn("T1", "PURCHASE", date(2024, 1, 1), "100", None, "500")
    assert build_book([by_nav]).all_lots()[0].cost_total == Decimal("50000.00")


# --- returns: cumulative is not annualised ----------------------------------


def test_a_sub_year_twrr_has_no_annualised_figure() -> None:
    """It returned the cumulative value in the annualised slot, and
    `compute_returns` then subtracted it from an annualised XIRR. `MODULE_2.md`
    §7.3 forbids labelling a cumulative figure as annualised, and
    `Returns.twrr_ann` is that label."""
    cumulative, annualised = twrr(Decimal("100"), Decimal("110"), days=60)
    assert cumulative == Decimal("0.1")
    assert annualised is None


def test_a_full_year_twrr_still_annualises() -> None:
    cumulative, annualised = twrr(Decimal("100"), Decimal("121"), days=730)
    assert cumulative == Decimal("0.21")
    assert annualised is not None
    assert Decimal("0.099") < annualised < Decimal("0.101")


def test_timing_effect_is_absent_rather_than_mixing_two_bases() -> None:
    """Subtracting a cumulative TWRR from an annualised XIRR reports timing
    benefit that is purely the unit mismatch. None, and a note saying why."""
    position = Position(
        scheme_id=HDFC, as_of=date(2024, 3, 1), units=Decimal("100"),
        market_value=Decimal("105000"), invested_net=Decimal("100000"),
        first_purchase=date(2024, 1, 1),
    )
    result = compute_returns(
        position,
        [txn("T1", "PURCHASE", date(2024, 1, 1), "100", "100000", "1000")],
        {date(2024, 1, 1): Decimal("1000"), date(2024, 3, 1): Decimal("1050")},
    )
    assert isinstance(result, Returns)
    assert result.twrr_cum is not None
    assert result.twrr_ann is None
    assert result.timing_effect is None
    assert result.obs_note is not None
    assert "annualis" in result.obs_note


def test_a_fully_recovered_position_does_not_invert_its_absolute_return() -> None:
    """`if pos.invested_net` only rejects exactly zero. A position that has
    returned more cash than it took in carries a negative `invested_net`, and
    dividing by it flips the sign of a real profit."""
    recovered = Position(
        scheme_id=HDFC, as_of=AS_OF, units=Decimal("10"),
        market_value=Decimal("20000"), invested_net=Decimal("-5000"),
        first_purchase=date(2020, 1, 1),
    )
    result = compute_returns(recovered, [], {})
    assert result.absolute is None


# --- gaps the mutation harness found in the fixes themselves ----------------


def test_grandfathering_is_only_taken_when_it_helps() -> None:
    """§7.5 is a relief, not a substitution. `effective_cost` returns the
    grandfathered figure only when it EXCEEDS the actual cost — a fund that
    fell between purchase and the cutoff has an FMV below what was paid, and
    using it would manufacture a larger taxable gain than really occurred."""
    bought = txn("T1", "PURCHASE", date(2015, 6, 1), "100", "50000", "500")
    sold = txn("T2", "REDEMPTION", date(2024, 6, 1), "-100", "300000", "3000", seq=2)
    book = build_book(
        # FMV 300 < the 500 actually paid.
        [bought, sold], grandfathering_navs={str(HDFC): Decimal("300")}
    )
    consumption = book.all_consumptions()[0]
    assert consumption.cost_basis_method == "actual"
    assert consumption.cost_allocated == Decimal("50000.00")


def test_the_grandfathered_basis_is_capped_by_the_sale_price() -> None:
    """`max(actual, min(FMV, sale))`. Dropping the inner `min` lets a fund that
    peaked in Jan 2018 and has since fallen claim a cost basis above what the
    units actually fetched — a fabricated capital LOSS."""
    bought = txn("T1", "PURCHASE", date(2015, 6, 1), "100", "50000", "500")
    sold = txn("T2", "REDEMPTION", date(2024, 6, 1), "-100", "80000", "800", seq=2)
    book = build_book(
        [bought, sold], grandfathering_navs={str(HDFC): Decimal("2000")}
    )
    consumption = book.all_consumptions()[0]
    # min(FMV 2000, sale 800) = 800, and max(cost 500, 800) x 100 units.
    assert consumption.cost_allocated == Decimal("80000.00")
    assert consumption.gain_amount == Decimal("0.00")


def test_the_cutoff_nav_is_read_on_the_day_never_near_it() -> None:
    """§7.5's substitution is the fair market value ON 31 January 2018. The
    nearest earlier price is a different number with no statutory standing, so
    a scheme that did not price that day is absent and its lots are flagged
    rather than valued from a neighbour."""
    from src.m1_ledger.persist import _grandfathering_navs

    missed_the_day = {str(HDFC): {date(2018, 1, 30): Decimal("1999")}}
    assert _grandfathering_navs(missed_the_day) == {}

    priced = {str(HDFC): {GRANDFATHER_DATE: Decimal("2000")}}
    assert _grandfathering_navs(priced) == {str(HDFC): Decimal("2000")}


def test_a_reversed_pair_does_not_reach_the_reconciliation_buckets() -> None:
    """`build_book` drops reversals before the engine sees anything and
    `nav_cross_check` drops them internally, so bucketing the raw list was the
    one place they survived — a reversed redemption's printed running balance
    could become `units_reported`."""
    from src.m1_ledger.reconcile import reconcile_all

    bought = txn("T1", "PURCHASE", date(2024, 1, 1), "100", "100000", "1000", "100")
    # Booked, then voided. `drop_reversed` removes BOTH sides (§3.2), so the
    # engine holds 100 units and the statement's standing balance is 100 — the
    # 60 printed against the voided entry must not be read as the latest.
    wrong = txn("T2", "REDEMPTION", date(2024, 2, 1), "-40", "44000", "1100", "60", seq=2)
    undo = Txn(
        txn_ref="T3", user_id=USER, folio="F1", scheme_id=HDFC,
        scheme_raw_name="HDFC Flexi Cap Fund - Direct Plan - Growth",
        txn_date=date(2024, 2, 2), txn_seq=3, txn_type="REVERSAL",
        units=Decimal("40"), nav=Decimal("1100"), amount=Decimal("44000"),
        stamp_duty=Decimal(0), stt=Decimal(0), exit_load=Decimal(0),
        switch_group_id=None, reverses_txn_ref="T2",
        # No printed balance on the reversal line, which is what statements
        # commonly do — and what makes this discriminating: with the reversed
        # pair still in the bucket, the latest entry that HAS a balance is the
        # voided redemption's 60.
        units_balance_rep=None,
    )
    book = build_book([bought, wrong, undo])
    results = reconcile_all(book, [bought, wrong, undo], {}, AS_OF)

    assert len(results) == 1
    # 60 is the voided entry's balance; 100 is what stands after the reversal.
    assert results[0].units_reported == Decimal("100")
    assert results[0].delta_units == Decimal(0)
