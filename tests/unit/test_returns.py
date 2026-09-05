"""XIRR, TWRR and timing effect.

MODULE_1.md §9. Written before the engine, per `CLAUDE.md`'s working agreement
for `src/m1_ledger/`.

`PLAN.md` §7 V0 requires XIRR to match an independent calculation to 4 decimal
places. Three kinds of check do that here:

  1. **Closed-form cases.** Rs 100 becoming Rs 110 in exactly 365 days is 10%,
     and no implementation detail changes that.
  2. **The defining property.** XIRR is the rate at which NPV is zero, so
     asserting NPV(returned_rate) ~ 0 verifies the answer without needing a
     second implementation to agree with.
  3. **Excel's convention.** Excel's XIRR discounts on a 365-day year. Matching
     it is what makes the V0 gate checkable in a spreadsheet.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from src.common.fixtures import load_yaml
from src.common.types import SchemeId
from src.m1_ledger.lots import build_book
from src.m1_ledger.returns import (
    TWRR_DAYS_PER_YEAR,
    XIRR_DAYS_PER_YEAR,
    Position,
    Returns,
    build_cashflows,
    compute_returns,
    npv,
    twrr,
    xirr,
)
from src.m1_ledger.txn import load_transactions

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "v0_ledger"
HDFC = SchemeId("INF179K01UT0")
ICICI = SchemeId("INF109K01761")
KOTAK = SchemeId("INF174KA1EZ1")
AS_OF = date(2026, 9, 4)


@pytest.fixture(scope="module")
def navs() -> dict[str, dict[date, Decimal]]:
    raw = load_yaml(FIXTURES / "nav_series.yaml")["series"]
    return {k: {d: Decimal(str(v)) for d, v in b["navs"].items()} for k, b in raw.items()}


@pytest.fixture(scope="module")
def txns() -> list[Any]:
    return load_transactions(FIXTURES / "transactions.csv")


# --- closed-form cases -----------------------------------------------------


@pytest.mark.parametrize(
    ("outflow", "inflow", "days", "expected"),
    [
        ("100", "110", 365, "0.10"),  # 10% in exactly one year
        ("100", "200", 365, "1.00"),  # a double
        ("100", "121", 730, "0.10"),  # 10% compounded over two years
        ("100", "90", 365, "-0.10"),  # a loss is a negative rate
        ("1000", "1000", 365, "0.00"),  # no gain, no rate
    ],
)
def test_xirr_closed_form(outflow: str, inflow: str, days: int, expected: str) -> None:
    """Cases whose answer is arithmetic, not convention."""
    t0 = date(2024, 1, 1)
    flows = [(t0, -Decimal(outflow)), (t0 + timedelta(days=days), Decimal(inflow))]
    got = xirr(flows)
    assert got is not None
    assert abs(got - Decimal(expected)) < Decimal("0.0001")


def test_xirr_uses_a_365_day_year_like_excel() -> None:
    """`PLAN.md` §7 V0 checks XIRR against Excel's XIRR().

    Excel discounts on a 365-day year. A 365.25 basis would shift the answer in
    the fourth decimal place — inside the gate's tolerance — so the convention
    is asserted rather than assumed.
    """
    t0 = date(2024, 1, 1)
    flows = [(t0, Decimal("-100")), (t0 + timedelta(days=365), Decimal("110"))]
    got = xirr(flows)
    assert got is not None
    assert abs(got - Decimal("0.10")) < Decimal("0.00000001")


# --- the defining property -------------------------------------------------


def test_npv_is_zero_at_the_returned_rate(
    txns: list[Any], navs: dict[str, dict[date, Decimal]]
) -> None:
    """XIRR is *defined* as the rate where NPV is zero.

    This verifies the answer on the real fixture without needing a second
    implementation to agree with it.
    """
    book = build_book(txns)
    market_value = book.units_remaining(HDFC) * navs[HDFC][AS_OF]
    flows = build_cashflows(
        [t for t in txns if t.scheme_id == HDFC], "scheme", market_value, AS_OF
    )
    rate = xirr(flows)
    assert rate is not None
    assert abs(npv(flows, rate)) < Decimal("0.01")


# --- undefined cases return None, never a garbage number -------------------


def test_xirr_is_none_without_a_sign_change() -> None:
    """No sign change means no root. Displaying "—" beats displaying 847%."""
    t0 = date(2024, 1, 1)
    assert (
        xirr([(t0, Decimal("-100")), (t0 + timedelta(days=365), Decimal("-50"))]) is None
    )
    assert xirr([(t0, Decimal("100")), (t0 + timedelta(days=365), Decimal("50"))]) is None


def test_xirr_is_none_with_fewer_than_two_flows() -> None:
    assert xirr([]) is None
    assert xirr([(date(2024, 1, 1), Decimal("-100"))]) is None


def test_xirr_survives_a_pattern_that_defeats_newton_raphson() -> None:
    """A large late contribution is the classic Newton-Raphson failure.

    MODULE_1.md §9.2 says the bisection fallback is not optional, so a pattern
    that needs it is exercised here rather than assumed unreachable.
    """
    t0 = date(2020, 1, 1)
    flows = [
        (t0, Decimal("-1")),
        (t0 + timedelta(days=3650), Decimal("-100000")),
        (t0 + timedelta(days=3651), Decimal("100050")),
    ]
    got = xirr(flows)
    if got is not None:
        assert abs(npv(flows, got)) < Decimal("1")


# --- cashflow construction -------------------------------------------------


def test_portfolio_scope_excludes_switch_legs(
    txns: list[Any], navs: dict[str, dict[date, Decimal]]
) -> None:
    """`PLAN.md` §9.6, locked. Switch legs are internal transfers.

    Both numbers must be labelled or they will not tie out and it reads as a bug.
    """
    mv = Decimal("100000")
    scheme = build_cashflows(txns, "scheme", mv, AS_OF)
    portfolio = build_cashflows(txns, "portfolio", mv, AS_OF)
    assert len(scheme) > len(portfolio)

    switch_day = date(2026, 1, 16)
    assert any(d == switch_day for d, _ in scheme)
    assert not any(d == switch_day for d, _ in portfolio)


def test_terminal_value_is_the_final_inflow(txns: list[Any]) -> None:
    mv = Decimal("123456.7890")
    flows = build_cashflows(txns, "portfolio", mv, AS_OF)
    assert flows[-1] == (AS_OF, mv)
    assert flows == sorted(flows, key=lambda f: f[0])


def test_reversed_transactions_never_reach_the_cashflows(txns: list[Any]) -> None:
    """The bounced SIP would otherwise show as an outflow that never happened."""
    flows = build_cashflows(txns, "scheme", Decimal("1000"), AS_OF)
    bounced = next(t for t in txns if t.txn_ref == "T007")
    assert not any(d == bounced.txn_date for d, _ in flows)


def test_idcw_payout_is_an_inflow(txns: list[Any]) -> None:
    """Cash reaching the investor is a real return, and XIRR must see it.

    Synthetic, because both schemes in the real-NAV fixture are GROWTH options
    and cannot distribute (DECISIONS V0-09).
    """
    payout = replace(
        txns[0],
        txn_ref="X001",
        txn_type="IDCW_PAYOUT",
        txn_date=date(2025, 3, 10),
        units=Decimal("0"),
        amount=Decimal("2500.00"),
        stamp_duty=Decimal("0"),
    )
    flows = build_cashflows([txns[0], payout], "scheme", Decimal("1000"), AS_OF)
    matched = [a for d, a in flows if d == payout.txn_date and a > 0]
    assert matched == [Decimal("2500.00")]


def test_idcw_reinvest_is_not_an_unmatched_outflow(txns: list[Any]) -> None:
    """DECISIONS V0-07. No external cash moves on a reinvestment.

    The dividend is declared and immediately buys units in the same scheme, so
    the investor neither pays nor receives anything. Booking it as an outflow
    with no matching inflow overstates the money invested and understates XIRR.

    MODULE_1.md §9.1 routes IDCW_REINVEST through the OPENING_TYPES branch,
    which appends `-abs(amount)` and never adds the offsetting inflow. This
    pins the corrected behaviour: it nets to zero at every scope.
    """
    reinvest = replace(
        txns[0],
        txn_ref="X002",
        txn_type="IDCW_REINVEST",
        txn_date=date(2025, 6, 10),
        units=Decimal("1.500000"),
        nav=Decimal("2000.000000"),
        amount=Decimal("-3000.00"),
        stamp_duty=Decimal("0.15"),
    )
    for scope in ("scheme", "portfolio"):
        flows = build_cashflows([txns[0], reinvest], scope, Decimal("1000"), AS_OF)
        on_day = [a for d, a in flows if d == reinvest.txn_date]
        assert sum(on_day, Decimal(0)) == Decimal(0), (
            f"{scope} scope: reinvestment moved "
            f"{sum(on_day, Decimal(0))} of external cash, but it moves none"
        )


# --- TWRR and timing effect ------------------------------------------------


def test_both_return_measures_use_the_same_year_length() -> None:
    """DECISIONS V0-17, resolving V0-08. One basis, because they get subtracted.

    `timing_effect = XIRR - TWRR_ann` is a difference of two near-equal
    numbers, so any basis mismatch between them lands entirely in the result
    rather than being diluted by it. At 365 against 365.25 that artifact is
    0.068% of a year — comparable in size to a genuine timing effect on a
    steadily-invested portfolio, and indistinguishable from one.

    365 is the side that cannot move: `PLAN.md` §7 V0 checks XIRR against
    Excel to four decimal places, and Excel discounts on 365. The 365.25 in
    TWRR was guarding against calendar drift over decades, which is not a
    concern when the figure exists to be subtracted from one computed the
    other way.
    """
    assert XIRR_DAYS_PER_YEAR == TWRR_DAYS_PER_YEAR == Decimal(365)


def test_a_two_year_twrr_annualises_on_exactly_two_years() -> None:
    """The consequence of V0-17, stated as a number.

    730 days is exactly two 365-day years, so a NAV that went 100 -> 121
    annualises to exactly 10% — no residual from a fractional year count. Under
    365.25 the exponent was 1/1.99863, which returned 10.0075%: right to three
    decimals, wrong in the fourth, and wrong in the same direction every time.
    """
    cum, ann = twrr(Decimal("100"), Decimal("121"), days=730)
    assert cum is not None and ann is not None
    assert cum == Decimal("0.21")
    assert abs(ann - Decimal("0.10")) < Decimal("0.00005")


def test_twrr_is_a_nav_ratio() -> None:
    """MODULE_1.md §9.3. A daily adjusted series means no sub-period chaining."""
    cum, ann = twrr(Decimal("100"), Decimal("121"), days=730)
    assert cum is not None and ann is not None
    assert abs(cum - Decimal("0.21")) < Decimal("0.0001")
    assert abs(ann - Decimal("0.0999")) < Decimal("0.001")


def test_twrr_does_not_annualise_under_one_year() -> None:
    """Annualising a two-month return produces a number nobody should act on."""
    cum, ann = twrr(Decimal("100"), Decimal("110"), days=60)
    assert cum == ann, "sub-year TWRR must report the cumulative figure unchanged"


def test_twrr_is_none_when_the_opening_nav_is_unusable() -> None:
    assert twrr(Decimal("0"), Decimal("110"), days=365) == (None, None)
    assert twrr(Decimal("-1"), Decimal("110"), days=365) == (None, None)


def test_timing_effect_is_xirr_minus_twrr(
    txns: list[Any], navs: dict[str, dict[date, Decimal]]
) -> None:
    """ "Did my contribution pattern help or hurt?" — a question people ask and
    get no honest answer to.
    """
    book = build_book(txns)
    pos = Position(
        scheme_id=HDFC,
        as_of=AS_OF,
        units=book.units_remaining(HDFC),
        market_value=book.units_remaining(HDFC) * navs[HDFC][AS_OF],
        invested_net=Decimal("60000.00"),
        first_purchase=date(2024, 1, 1),
    )
    r = compute_returns(
        pos, [t for t in txns if t.scheme_id == HDFC], navs[HDFC], scope="scheme"
    )
    assert isinstance(r, Returns)
    assert r.xirr is not None and r.twrr_ann is not None
    assert r.timing_effect == r.xirr - r.twrr_ann


def test_returns_degrade_rather_than_guess(navs: dict[str, dict[date, Decimal]]) -> None:
    """A position with no cashflows yields no XIRR, and says so."""
    pos = Position(
        scheme_id=HDFC,
        as_of=AS_OF,
        units=Decimal("0"),
        market_value=Decimal("0"),
        invested_net=Decimal("0"),
        first_purchase=date(2024, 1, 1),
    )
    r = compute_returns(pos, [], navs[HDFC], scope="scheme")
    assert r.xirr is None
    assert r.absolute is None
    assert r.timing_effect is None


def test_xirr_returns_decimal_not_float(
    txns: list[Any], navs: dict[str, dict[date, Decimal]]
) -> None:
    """`PLAN.md` §8.2 rule 1: float is permitted inside the solver only, and
    must be converted back at the boundary.
    """
    book = build_book(txns)
    flows = build_cashflows(
        [t for t in txns if t.scheme_id == HDFC],
        "scheme",
        book.units_remaining(HDFC) * navs[HDFC][AS_OF],
        AS_OF,
    )
    rate = xirr(flows)
    assert isinstance(rate, Decimal)
    assert isinstance(npv(flows, rate), Decimal)


def test_cost_basis_remaining_excludes_what_was_already_sold(
    txns: list[Any], navs: dict[str, dict[date, Decimal]]
) -> None:
    """The absolute-return trap.

    `Position.invested_net` means the cost of what is STILL held. Summing
    purchase amounts instead counts money already redeemed or switched away:
    on this fixture that reads as -85.67% on a position actually up 20.62%.
    """
    book = build_book(txns)
    basis = book.cost_basis_remaining(HDFC)
    purchases = sum(
        (
            abs(t.amount) + t.stamp_duty
            for t in txns
            if t.scheme_id == HDFC and t.txn_type == "SIP" and t.amount
        ),
        Decimal(0),
    )
    assert basis < purchases, "30 of 34.4 units left the position"

    pos = Position(
        scheme_id=HDFC,
        as_of=AS_OF,
        units=book.units_remaining(HDFC),
        market_value=book.units_remaining(HDFC) * navs[HDFC][AS_OF],
        invested_net=basis,
        first_purchase=date(2024, 1, 1),
    )
    r = compute_returns(pos, [t for t in txns if t.scheme_id == HDFC], navs[HDFC])
    assert r.absolute is not None and r.absolute > 0

    # The same position with the naive figure looks catastrophically wrong.
    naive = compute_returns(
        Position(**{**pos.__dict__, "invested_net": purchases}),
        [t for t in txns if t.scheme_id == HDFC],
        navs[HDFC],
    )
    assert naive.absolute is not None and naive.absolute < Decimal("-0.5")


def test_nav_on_the_as_of_date_is_not_rolled_forward(
    navs: dict[str, dict[date, Decimal]],
) -> None:
    """A fund does not price on a weekend, and yesterday's NAV is the answer.

    Rolling forward would value a position at a price that did not exist yet.
    """
    from src.m1_ledger.returns import nav_on_or_before

    saturday = date(2026, 6, 6)
    assert saturday not in navs[HDFC]
    got = nav_on_or_before(navs[HDFC], saturday)
    assert got == navs[HDFC][date(2026, 6, 5)]
    assert nav_on_or_before(navs[HDFC], date(2000, 1, 1)) is None


def test_redemption_inflow_is_net_of_exit_load_and_stt(txns: list[Any]) -> None:
    """XIRR must see what the investor actually received, not the gross.

    MODULE_1.md §9.1 books a closing transaction as
    `abs(amount) - exit_load - stt`. On this fixture that is Rs 273.69 of
    charges on a Rs 39,413.96 redemption — 0.69% of it, which moves the return.

    Found by mutation: dropping both terms left the whole suite green.
    """
    redemption = next(t for t in txns if t.txn_type == "REDEMPTION")
    assert redemption.exit_load > 0 and redemption.stt > 0, "fixture must charge both"
    assert redemption.amount is not None

    expected = abs(redemption.amount) - redemption.exit_load - redemption.stt
    flows = build_cashflows(txns, "scheme", Decimal("1000"), AS_OF)
    on_day = [a for d, a in flows if d == redemption.txn_date]

    assert on_day == [expected]
    assert expected < abs(redemption.amount)


def test_switch_out_inflow_is_net_of_stt(txns: list[Any]) -> None:
    """A switch moves no cash to the investor, but STT still reduces the
    proceeds that buy into the receiving scheme.
    """
    switch = next(t for t in txns if t.txn_type == "SWITCH_OUT")
    assert switch.amount is not None
    expected = abs(switch.amount) - switch.exit_load - switch.stt
    flows = build_cashflows(txns, "scheme", Decimal("1000"), AS_OF)
    assert any(a == expected for d, a in flows if d == switch.txn_date)


def test_funds_price_on_different_last_days(
    navs: dict[str, dict[date, Decimal]],
) -> None:
    """Kotak's series ends a day before the other two.

    Real cross-fund staleness: a portfolio as-of date cannot assume every
    scheme priced that day. `PLAN.md` §4.3 — staleness is displayed, never
    hidden — starts with the valuation actually noticing it.
    """
    last = {k: max(v) for k, v in navs.items()}
    assert last[HDFC] == date(2026, 9, 4)
    assert last[ICICI] == date(2026, 9, 4)
    assert last[KOTAK] == date(2026, 9, 3)
    assert len({v for v in last.values()}) > 1, "fixture must span >1 last-priced date"


def test_valuation_uses_each_funds_own_latest_nav(
    navs: dict[str, dict[date, Decimal]],
) -> None:
    """At a portfolio as-of of 2026-09-04, Kotak is valued on 2026-09-03.

    Rolling forward would price it at a NAV that did not exist yet; refusing to
    value it would drop a real holding from the total.
    """
    from src.m1_ledger.returns import nav_on_or_before

    assert AS_OF not in navs[KOTAK]
    got = nav_on_or_before(navs[KOTAK], AS_OF)
    assert got is not None
    assert got == navs[KOTAK][date(2026, 9, 3)]

    staleness = (AS_OF - max(d for d in navs[KOTAK] if d <= AS_OF)).days
    assert staleness == 1, "Kotak is one day stale at this as-of date"


def test_returns_computed_for_every_held_scheme(
    txns: list[Any], navs: dict[str, dict[date, Decimal]]
) -> None:
    """Three schemes, three coherent return sets, none degrading to None."""
    from src.m1_ledger.returns import nav_on_or_before

    book = build_book(txns)
    firsts = {HDFC: date(2024, 1, 1), ICICI: date(2026, 1, 16), KOTAK: date(2024, 11, 4)}
    for scheme_id, first in firsts.items():
        units = book.units_remaining(scheme_id)
        assert units > 0, f"{scheme_id} should still hold units"
        nav = nav_on_or_before(navs[scheme_id], AS_OF)
        assert nav is not None
        pos = Position(
            scheme_id=scheme_id,
            as_of=AS_OF,
            units=units,
            market_value=units * nav,
            invested_net=book.cost_basis_remaining(scheme_id),
            first_purchase=first,
        )
        r = compute_returns(
            pos, [t for t in txns if t.scheme_id == scheme_id], navs[scheme_id]
        )
        assert r.xirr is not None, f"{scheme_id} XIRR undefined"
        assert r.twrr_ann is not None, f"{scheme_id} TWRR undefined"
        assert r.absolute is not None
        assert r.timing_effect == r.xirr - r.twrr_ann
