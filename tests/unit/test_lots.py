"""The FIFO lot engine against the V0.1 golden fixture.

`CLAUDE.md` working agreement: write the test first for anything in
`src/m1_ledger/`. This file was written before the engine.

FIFO is statutory for Indian mutual fund units — not a design choice — so these
are conformance tests, not preference tests. Expected values come from
`tests/fixtures/v0_ledger/expected.yaml`, hand-computed independently of the
engine. If the two disagree, that disagreement is the finding.
"""

from __future__ import annotations

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
HDFC = "PLACEHOLDER:HDFC-FLEXICAP-REG-G"
ABSL = "PLACEHOLDER:ABSL-LARGEMID-REG-G"
ICICI = "PLACEHOLDER:ICICI-MULTIASSET-REG-G"


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
    assert lot.cost_total == Decimal("10000.50")
    assert lot.cost_total > Decimal("10000.00")
    assert lot.cost_per_unit == Decimal("1000.050000")


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
    lot = next(x for x in book.all_lots() if x.scheme_id == ICICI)
    assert lot.acquisition_date == date(2026, 1, 15)
    assert lot.origin == "switch_in"
    absl = next(x for x in book.all_lots() if x.golden_ref == "L7")
    assert lot.acquisition_date > absl.acquisition_date


def test_idcw_reinvest_creates_a_lot_and_idcw_payout_does_not(book: LotBook) -> None:
    """MODULE_1.md §3.2: both effects must fire for a reinvestment."""
    reinvest = [x for x in book.all_lots() if x.origin == "idcw_reinvest"]
    assert len(reinvest) == 1
    assert reinvest[0].acquisition_date == date(2025, 8, 20)
    assert reinvest[0].units_original == Decimal("4.000000")
    assert not [x for x in book.all_lots() if x.open_txn_ref == "T011"]


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
    assert [c.gain_type for c in cons] == ["LTCG", "STCG", "STCG"]
    assert [c.holding_days for c in cons] == [380, 349, 320]
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
    for txn_ref in ("T009", "T013"):
        dates = [c.acquisition_date for c in book.consumptions_for(txn_ref)]
        assert dates == sorted(dates)


def test_net_proceeds_are_reduced_by_exit_load_and_stt(
    book: LotBook, expected: dict[str, Any]
) -> None:
    """Gross is not what the investor receives, and cost is not what they paid."""
    want = expected["totals"]["T009"]
    cons = book.consumptions_for("T009")
    total_net = sum((c.proceeds_net for c in cons), Decimal(0))
    assert total_net == Decimal(want["net_total"])
    assert total_net == (
        Decimal(want["gross"]) - Decimal(want["exit_load"]) - Decimal(want["stt"])
    )
    assert total_net < Decimal(want["gross"])


def test_switch_out_is_taxable_even_though_no_cash_moved(book: LotBook) -> None:
    """MODULE_1.md §3.2. Cash-neutral, tax-positive.

    Modelling a switch as a single transfer under-reports capital gains.
    """
    cons = book.consumptions_for("T013")
    assert len(cons) == 1
    assert cons[0].gain_type == "LTCG"
    assert cons[0].gain_amount == Decimal("2498.7700")
    assert cons[0].gain_amount > 0


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
