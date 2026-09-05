"""The FIFO lot engine against the V0.1 golden fixture.

`CLAUDE.md` working agreement: write the test first for anything in
`src/m1_ledger/`. This file was written before the engine.

FIFO is statutory for Indian mutual fund units — not a design choice — so these
are conformance tests, not preference tests. Expected values come from
`tests/fixtures/v0_ledger/expected.yaml`, hand-computed independently of the
engine. If the two disagree, that disagreement is the finding.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from src.common.fixtures import load_yaml
from src.m1_ledger.lots import (
    InsufficientUnits,
    LotBook,
    build_book,
    gain_type_for,
)
from src.m1_ledger.txn import Txn, load_transactions

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "v0_ledger"
HDFC = "AMFI:HDFC-FLEXICAP-DIR-G"
ICICI = "AMFI:ICICI-MULTIASSET-REG-G"
KOTAK = "AMFI:KOTAK-PIONEER-DIR-G"


@pytest.fixture(scope="module")
def txns() -> list[Txn]:
    return load_transactions(FIXTURES / "transactions.csv")


@pytest.fixture(scope="module")
def expected() -> dict[str, Any]:
    return load_yaml(FIXTURES / "expected.yaml")


@pytest.fixture(scope="module")
def book(txns: list[Txn]) -> LotBook:
    return build_book(txns)


# --- parsing ---------------------------------------------------------------


def test_all_transactions_parse(txns: list[Txn]) -> None:
    """14 rows, comment lines skipped, every money field a Decimal."""
    assert len(txns) == 14
    assert {t.txn_ref for t in txns} == {f"T{i:03d}" for i in range(1, 15)}
    for t in txns:
        assert t.units is None or isinstance(t.units, Decimal)
        assert t.amount is None or isinstance(t.amount, Decimal)
        assert isinstance(t.stamp_duty, Decimal)


def test_txn_id_is_deterministic_and_idempotent(txns: list[Txn]) -> None:
    """MODULE_1.md §5.6. Re-importing an overlapping statement must not
    duplicate rows, and every new CAS re-covers old periods.
    """
    first = {t.txn_ref: t.txn_id for t in txns}
    again = {
        t.txn_ref: t.txn_id for t in load_transactions(FIXTURES / "transactions.csv")
    }
    assert first == again
    assert len(set(first.values())) == len(first), "txn_id collision"


# --- lot construction ------------------------------------------------------


def test_lots_match_the_golden_file(book: LotBook, expected: dict[str, Any]) -> None:
    """Every lot: acquisition date, units, cost, cost per unit."""
    by_ref = {lot.golden_ref: lot for lot in book.all_lots()}
    assert len(by_ref) == len(expected["lots"])

    for row in expected["lots"]:
        lot = by_ref[row["lot_id"]]
        assert lot.acquisition_date == row["acquisition_date"]
        assert lot.units_original == Decimal(row["units_original"])
        assert lot.cost_total == Decimal(row["cost_total"])
        assert lot.cost_per_unit == Decimal(row["cost_per_unit"])
        assert lot.origin == row["origin"]


def test_stamp_duty_raises_cost_above_amount_paid(book: LotBook) -> None:
    """A lot's cost basis is what you paid PLUS stamp duty.

    Omitting it understates cost and so overstates the gain — in the taxpayer's
    disfavour, and wrong.
    """
    lot = next(x for x in book.all_lots() if x.golden_ref == "L1")
    txn = next(
        t for t in load_transactions(FIXTURES / "transactions.csv") if t.txn_ref == "T001"
    )
    # Rs 10,000 paid: Rs 0.50 to stamp duty, Rs 9,999.50 actually invested.
    # The lot's cost is the full payment, so it exceeds what bought the units.
    assert txn.amount is not None
    assert abs(txn.amount) == Decimal("9999.5000")
    assert txn.stamp_duty == Decimal("0.5000")
    assert lot.cost_total == abs(txn.amount) + txn.stamp_duty
    assert lot.cost_total == Decimal("10000.0000")
    assert lot.cost_per_unit > abs(txn.amount) / lot.units_original


def test_each_sip_instalment_is_its_own_lot(book: LotBook) -> None:
    """MODULE_1.md §3.1. Six instalments, six lots, six acquisition dates.

    Averaging them into one lot destroys the per-instalment tax clock.
    """
    sip_lots = [x for x in book.all_lots() if x.origin == "sip"]
    assert len(sip_lots) == 6
    assert len({x.acquisition_date for x in sip_lots}) == 6


def test_reversed_pair_creates_no_lot(book: LotBook) -> None:
    """MODULE_1.md §3.2. Excluded, not netted.

    Netting would leave a phantom zero-unit lot sitting in the FIFO queue.
    """
    opened_by = {lot.open_txn_ref for lot in book.all_lots()}
    assert "T007" not in opened_by
    assert "T008" not in opened_by
    assert len([x for x in book.all_lots() if x.scheme_id == HDFC]) == 6


def test_switch_in_restarts_the_holding_clock(
    book: LotBook, expected: dict[str, Any]
) -> None:
    """MODULE_1.md §3.2. The receiving leg's acquisition date is the switch date.

    Carrying the original date across would convert a short holding into a long
    one and understate tax.
    """
    lot = next(x for x in book.all_lots() if x.origin == "switch_in")
    assert lot.acquisition_date == date(2026, 1, 16)
    # The units it replaced were bought in 2024 and were long-term by then;
    # the new lot starts its clock from zero.
    source = next(x for x in book.all_lots() if x.golden_ref == "L1")
    assert lot.acquisition_date > source.acquisition_date
    assert (lot.acquisition_date - source.acquisition_date).days > 365


def test_idcw_reinvest_creates_a_lot_and_idcw_payout_does_not() -> None:
    """MODULE_1.md §3.2: both effects must fire for a reinvestment.

    Built from synthetic transactions rather than the real-NAV fixture. Both
    schemes there are GROWTH options, which distribute nothing, so an IDCW
    row against those NAV series would be structurally impossible — see
    DECISIONS V0-09.
    """
    base = load_transactions(FIXTURES / "transactions.csv")[0]
    payout = replace(
        base,
        txn_ref="X001",
        txn_type="IDCW_PAYOUT",
        txn_date=date(2025, 3, 10),
        units=Decimal("0"),
        amount=Decimal("2500.00"),
        stamp_duty=Decimal("0"),
    )
    reinvest = replace(
        base,
        txn_ref="X002",
        txn_type="IDCW_REINVEST",
        txn_date=date(2025, 6, 10),
        units=Decimal("1.500000"),
        nav=Decimal("2000.000000"),
        amount=Decimal("-3000.00"),
        stamp_duty=Decimal("0.15"),
    )
    book = build_book([base, payout, reinvest])

    lots = [x for x in book.all_lots() if x.origin == "idcw_reinvest"]
    assert len(lots) == 1, "the reinvestment must create a lot"
    assert lots[0].acquisition_date == date(2025, 6, 10), "with a FRESH date"
    assert lots[0].units_original == Decimal("1.500000")
    # The payout is income only: no units, no lot.
    assert not [x for x in book.all_lots() if x.open_txn_ref == "X001"]


# --- FIFO consumption ------------------------------------------------------


def test_consumptions_match_the_golden_file(
    book: LotBook, expected: dict[str, Any]
) -> None:
    for txn_ref, rows in expected["consumptions"].items():
        actual = book.consumptions_for(txn_ref)
        assert len(actual) == len(rows), f"{txn_ref}: wrong consumption count"
        for got, want in zip(actual, rows, strict=True):
            assert got.lot_golden_ref == want["lot_id"]
            assert got.units_consumed == Decimal(want["units_consumed"])
            assert got.holding_days == want["holding_days"]
            assert got.gain_type == want["gain_type"]
            assert got.cost_allocated == Decimal(want["cost_allocated"])
            assert got.proceeds_net == Decimal(want["proceeds_net"])
            assert got.gain_amount == Decimal(want["gain_amount"])


def test_one_redemption_produces_both_gain_types(book: LotBook) -> None:
    """The case a per-transaction classifier gets wrong.

    T009 spans the 365-day boundary: lot 1 is 380 days (LTCG), lots 2 and 3 are
    349 and 320 (STCG). Gain type is a property of the LOT, not the sale.
    """
    cons = book.consumptions_for("T009")
    assert [c.gain_type for c in cons] == ["LTCG", "STCG", "STCG", "STCG"]
    assert [c.holding_days for c in cons] == [380, 349, 320, 289]
    assert cons[0].holding_days > 365 >= cons[1].holding_days


@pytest.mark.parametrize(
    ("days", "expected_type"),
    [
        (1, "STCG"),
        (364, "STCG"),
        (365, "STCG"),  # exactly one year is still SHORT term
        (366, "LTCG"),  # the first long-term day
        (400, "LTCG"),
    ],
)
def test_ltcg_boundary_is_strictly_beyond_365_days(days: int, expected_type: str) -> None:
    """The off-by-one that moves real money.

    Equity gains are long-term only when held for MORE than a year. At exactly
    365 days the gain is short-term and taxed at 20%, not 12.5%.

    The golden fixture cannot catch this — its holding periods are 380, 349,
    320 and 584 days, so none of them sits on the boundary. Found by mutating
    `>` to `>=` and watching the whole suite still pass.
    """
    assert gain_type_for(days, "equity") == expected_type


def test_non_equity_holding_threshold_is_not_invented() -> None:
    """CLAUDE.md invariant 8, extended to the threshold that selects a rate.

    Debt and hybrid thresholds are versioned in config/tax_rules.yaml and
    human-verified. Guessing one here would silently misclassify every debt
    gain, so it raises instead.
    """
    with pytest.raises(NotImplementedError):
        gain_type_for(400, "debt")


def test_fifo_order_is_oldest_acquisition_first(book: LotBook) -> None:
    """MODULE_1.md §7.3. Monotonic in acquisition_date — PLAN.md §8.3 invariant 3."""
    for txn_ref in ("T009", "T010", "T014"):
        dates = [c.acquisition_date for c in book.consumptions_for(txn_ref)]
        assert dates == sorted(dates)


def test_net_proceeds_are_reduced_by_exit_load_and_stt(
    book: LotBook, expected: dict[str, Any]
) -> None:
    """Gross is not what the investor receives, and cost is not what they paid."""
    want = expected["totals"]["T009"]
    cons = book.consumptions_for("T009")
    total_net = sum((c.proceeds_net for c in cons), Decimal(0))
    stated_net = (
        Decimal(want["gross"]) - Decimal(want["exit_load"]) - Decimal(want["stt"])
    )
    assert stated_net == Decimal(want["net_total"])
    assert total_net < Decimal(want["gross"]), "exit load and STT must bite"

    # Per-lot proceeds do NOT sum exactly to the transaction total.
    # MODULE_1.md §7.2 quantises net_per_unit to 6dp and re-multiplies,
    # which cannot reproduce the total. Bounded at one paisa per row.
    # DECISIONS V0-06 — the spec does not say who absorbs the residual.
    residual = abs(stated_net - total_net)
    assert residual <= Decimal("0.0001") * len(cons)
    assert residual > 0, (
        "no residual here — if the algorithm changed, V0-06 may be resolved"
    )


def test_switch_out_is_taxable_even_though_no_cash_moved(book: LotBook) -> None:
    """MODULE_1.md §3.2. Cash-neutral, tax-positive.

    Modelling a switch as a single transfer under-reports capital gains.
    """
    cons = book.consumptions_for("T010")
    assert cons, "the switch produced no taxable consumption"
    assert all(c.gain_type == "LTCG" for c in cons)
    assert sum((c.gain_amount for c in cons), Decimal(0)) > 0
    # No cash reached the investor, yet a gain is realised and taxable.
    switch = next(
        t for t in load_transactions(FIXTURES / "transactions.csv") if t.txn_ref == "T010"
    )
    assert switch.txn_type == "SWITCH_OUT"


def test_insufficient_units_raises_and_never_clamps() -> None:
    """MODULE_1.md §7.4. It means a missing CAS period.

    Clamping produces a plausible-looking wrong ledger, which is worse than a
    crash — CLAUDE.md invariant 5.
    """
    rows = load_transactions(FIXTURES / "transactions.csv")
    oversized = [t for t in rows if t.txn_ref in {"T001", "T002"}] + [
        t._replace_units_for_test(Decimal("-999.000000"))
        for t in rows
        if t.txn_ref == "T009"
    ]
    with pytest.raises(InsufficientUnits) as exc:
        build_book(oversized)
    assert exc.value.shortfall > 0


# --- the property invariants (PLAN.md §8.3) --------------------------------


def test_invariant_1_unit_conservation(
    book: LotBook, txns: list[Txn], expected: dict[str, Any]
) -> None:
    """Sum of lot.units_remaining == sum of signed transaction units."""
    want = expected["conservation"]
    assert book.total_units_remaining() == Decimal(want["sum_units_remaining"])
    assert book.signed_txn_units(txns) == Decimal(want["signed_txn_units"])
    assert book.total_units_remaining() == book.signed_txn_units(txns)


def test_invariant_2_cost_conservation(book: LotBook) -> None:
    """Cost is neither created nor destroyed.

    Remaining cost basis plus cost already allocated to consumptions equals the
    total cost of every lot ever opened.
    """
    remaining = sum(
        (lot.cost_per_unit * lot.units_remaining for lot in book.all_lots()),
        Decimal(0),
    )
    allocated = sum((c.cost_allocated for c in book.all_consumptions()), Decimal(0))
    opened = sum((lot.cost_total for lot in book.all_lots()), Decimal(0))
    # Penny-level rounding is expected: cost_per_unit is quantised to 6dp and
    # re-multiplied. The tolerance is one paisa per lot, not a free pass.
    assert abs((remaining + allocated) - opened) <= Decimal("0.01") * len(
        list(book.all_lots())
    )


def test_invariant_3_fifo_ordering_is_monotonic(book: LotBook) -> None:
    for txn_ref in book.closing_txn_refs():
        seq = [c.acquisition_date for c in book.consumptions_for(txn_ref)]
        assert seq == sorted(seq)


def test_invariant_5_rebuild_is_deterministic(txns: list[Txn]) -> None:
    """PLAN.md §8.3. A full rebuild reproduces byte-identical derived output.

    Guarantees the derived tables are droppable — CLAUDE.md invariant 10.
    """
    a = build_book(txns).fingerprint()
    b = build_book(load_transactions(FIXTURES / "transactions.csv")).fingerprint()
    assert a == b


def test_units_remaining_per_scheme(book: LotBook, expected: dict[str, Any]) -> None:
    for scheme_id, want in expected["units_remaining"].items():
        assert book.units_remaining(scheme_id) == Decimal(want)


def test_a_lot_can_be_split_across_two_transactions(book: LotBook) -> None:
    """L4 is partly consumed by the redemption and finished by the switch.

    Its two halves land on opposite sides of the one-year boundary — 289
    days at the redemption, 655 at the switch — so the same lot yields STCG
    once and LTCG later. A lot is not classified when it is opened.
    """
    first = next(c for c in book.consumptions_for("T009") if c.lot_golden_ref == "L4")
    second = next(c for c in book.consumptions_for("T010") if c.lot_golden_ref == "L4")
    assert first.gain_type == "STCG"
    assert second.gain_type == "LTCG"
    assert first.lot_id == second.lot_id

    lot = next(x for x in book.all_lots() if x.golden_ref == "L4")
    assert first.units_consumed + second.units_consumed == lot.units_original
    assert lot.units_remaining == Decimal(0)
    assert lot.is_closed


def test_scheme_master_matches_the_nav_series_plan() -> None:
    """DECISIONS V0-05. The ~1%/year silent error, pinned.

    The scheme record's plan and the NAV workbook's plan must agree. They
    did not when the data arrived: a Direct-plan NAV series was supplied
    against a Regular-plan scheme record, 10.13% apart on 2026-09-04.
    """
    master = load_yaml(FIXTURES / "scheme_master.yaml")
    series = load_yaml(FIXTURES / "nav_series.yaml")["series"]
    by_id = {s["scheme_id"]: s for s in master["schemes"]}

    for scheme_id, block in series.items():
        assert scheme_id in by_id, f"{scheme_id} has a NAV series but no record"
        recorded = by_id[scheme_id]["plan"]
        from_file = block["plan"].split()[0].lower()  # "Direct Plan" -> direct
        assert recorded == from_file, (
            f"{scheme_id}: scheme record says {recorded!r} but the NAV "
            f"workbook says {block['plan']!r}. Different share classes, "
            f"different NAV series — never cross them."
        )


def test_exit_load_is_per_scheme_not_a_constant(txns: list[Txn]) -> None:
    """Kotak charges 0.5%; HDFC charges 1%. Exit load is a scheme term.

    A fixture with one rate cannot catch code that hardcodes it — an earlier
    draft of the generator did exactly that, and would have doubled Kotak's
    load. This asserts the two transactions carry genuinely different rates.
    """
    hdfc_redemption = next(t for t in txns if t.txn_ref == "T009")
    kotak_redemption = next(t for t in txns if t.txn_ref == "T014")
    assert hdfc_redemption.scheme_id == HDFC
    assert kotak_redemption.scheme_id == KOTAK

    master = load_yaml(FIXTURES / "scheme_master.yaml")
    rates = {s["scheme_id"]: Decimal(s["exit_load_pct"]) for s in master["schemes"]}
    assert rates[HDFC] == Decimal("1.0")
    assert rates[KOTAK] == Decimal("0.5")
    assert rates[KOTAK] * 2 == rates[HDFC]

    # Both charged something, and neither charged the whole redemption.
    for t in (hdfc_redemption, kotak_redemption):
        assert t.amount is not None
        assert 0 < t.exit_load < abs(t.amount)


def test_exit_load_spares_units_past_the_window(txns: list[Txn]) -> None:
    """Charging the load on the whole redemption overcharges long holders.

    Kotak's redemption takes 1,567 units held 527 days (outside the window,
    unloaded) and 232 units held 257 days (inside, loaded). The load must be
    0.5% of the 232 units only.
    """
    t = next(x for x in txns if x.txn_ref == "T014")
    assert t.nav is not None
    loaded_units = Decimal("232.483697")
    assert t.exit_load == (loaded_units * t.nav * Decimal("0.005")).quantize(
        Decimal("0.0001")
    )
    # A naive load on all 1,800 units would be nearly eight times larger.
    naive = (Decimal("1800") * t.nav * Decimal("0.005")).quantize(Decimal("0.0001"))
    assert naive > t.exit_load * 7


def test_gain_type_is_per_lot_in_the_kotak_redemption(book: LotBook) -> None:
    """A second scheme, a second mixed-gain redemption, a different load rate.

    Two schemes showing the same behaviour is what distinguishes a rule from a
    coincidence of one fixture's dates.
    """
    cons = book.consumptions_for("T014")
    assert [c.gain_type for c in cons] == ["LTCG", "STCG"]
    assert cons[0].holding_days == 527
    assert cons[1].holding_days == 257


def test_cost_allocation_drifts_at_small_nav_and_large_unit_counts(
    book: LotBook,
) -> None:
    """DECISIONS V0-10. Scale-dependent, and invisible on one fund.

    `cost_per_unit` is quantised to 6dp and re-multiplied by the unit count.
    At HDFC's scale — ~6 units at ~Rs 1,700 — that reproduces the lot cost
    exactly. At Kotak's — ~1,567 units at ~Rs 32 — it overshoots by 2 paisa, so
    a FULLY consumed lot is allocated more cost than it ever had, and the gain
    is understated by the same amount.
    """
    kotak_lot = next(
        x for x in book.all_lots() if x.scheme_id == KOTAK and x.units_remaining == 0
    )
    allocated = sum(
        (
            c.cost_allocated
            for c in book.all_consumptions()
            if c.lot_id == kotak_lot.lot_id
        ),
        Decimal(0),
    )
    assert kotak_lot.is_closed, "this lot must be fully consumed for the test to bite"
    assert allocated != kotak_lot.cost_total, (
        "no drift here — if the algorithm changed, V0-10 may be resolved"
    )
    assert abs(allocated - kotak_lot.cost_total) <= Decimal("0.001")

    # The same arithmetic on an HDFC lot is exact, which is why one fund could
    # never have surfaced this.
    hdfc_lot = next(x for x in book.all_lots() if x.golden_ref == "L1")
    hdfc_alloc = sum(
        (
            c.cost_allocated
            for c in book.all_consumptions()
            if c.lot_id == hdfc_lot.lot_id
        ),
        Decimal(0),
    )
    assert hdfc_alloc == hdfc_lot.cost_total
