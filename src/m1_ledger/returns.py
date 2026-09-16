"""Returns engine — XIRR, TWRR and the timing effect.

MODULE_1.md §9. Four numbers, each answering a different question:

    XIRR           what the *investor* earned, including their timing
    TWRR           what the *fund* delivered, independent of cashflows
    timing effect  XIRR - TWRR: whether the contribution pattern helped
    absolute       plain profit over money invested

`PLAN.md` §8.2 rule 1 permits float inside a numerical routine and requires the
conversion back at the boundary. The solver here is the only float in the
ledger; every value crossing in or out of this module is a `Decimal`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.common.decimals import annualise
from src.common.types import SchemeId
from src.m1_ledger.txn import (
    CASH_NEUTRAL_TYPES,
    CLOSING_TYPES,
    OPENING_TYPES,
    Txn,
    drop_reversed,
)

#: Excel's XIRR discounts on a 365-day year, and `PLAN.md` §7 V0 checks our
#: answer against it to four decimal places. A 365.25 basis moves the result
#: inside that tolerance, so this one cannot move.
XIRR_DAYS_PER_YEAR = Decimal(365)

#: The SAME 365, departing from MODULE_1.md §9.3's 365.25. DECISIONS V0-17.
#:
#: `timing_effect` subtracts one of these figures from the other, and a
#: difference of two near-equal numbers carries the whole basis mismatch into
#: the result instead of diluting it. §9.3's 365.25 guards against calendar
#: drift over decades, which is not what this number is for.
TWRR_DAYS_PER_YEAR = XIRR_DAYS_PER_YEAR

_MAX_NEWTON_STEPS = 50
_MAX_BISECTION_STEPS = 200
_NPV_TOLERANCE = 1e-7
_RATE_FLOOR = -0.9999


@dataclass(frozen=True)
class Position:
    """What a returns calculation needs about a holding."""

    scheme_id: SchemeId
    as_of: date
    units: Decimal
    market_value: Decimal
    invested_net: Decimal
    first_purchase: date


@dataclass(frozen=True)
class Returns:
    """All four numbers. Compute every one; label each precisely.

    Any of them may be None, and None means "not defined for this position",
    not zero. MODULE_1.md §9.2: displaying "—" with an explanation beats
    displaying 847%.
    """

    xirr: Decimal | None
    twrr_cum: Decimal | None
    twrr_ann: Decimal | None
    timing_effect: Decimal | None
    absolute: Decimal | None
    obs_note: str | None = None


def build_cashflows(
    txns: list[Txn],
    scope: str,
    terminal_value: Decimal,
    as_of: date,
) -> list[tuple[date, Decimal]]:
    """External cashflows, plus the closing value as the final inflow.

    §9.1. `scope` is locked by `PLAN.md` §9.6: `scheme` includes switch legs,
    since leaving scheme A is a real exit from A; `portfolio` excludes them as
    internal transfers. Both must be labelled in any UI or they will not tie
    out and it reads as a bug.

    One correction against §9.1 (V0-07): `IDCW_REINVEST` is in `OPENING_TYPES`,
    so the spec books it as `-abs(amount)` with no offsetting inflow. No
    external cash moves on a reinvestment, so that overstates money invested
    and understates XIRR. Excluded at both scopes.
    """
    flows: list[tuple[date, Decimal]] = []

    for t in drop_reversed(txns):
        # A reinvestment is cash-neutral at every scope. See V0-07.
        if t.txn_type == "IDCW_REINVEST":
            continue
        if scope == "portfolio" and t.txn_type in CASH_NEUTRAL_TYPES:
            continue

        if t.txn_type in OPENING_TYPES:
            if t.amount is not None:
                flows.append((t.txn_date, -abs(t.amount)))
        elif t.txn_type in CLOSING_TYPES:
            if t.amount is not None:
                flows.append((t.txn_date, abs(t.amount) - t.exit_load - t.stt))
        elif t.txn_type == "IDCW_PAYOUT":
            if t.amount is not None:
                flows.append((t.txn_date, abs(t.amount)))

    flows.append((as_of, terminal_value))
    return sorted(flows, key=lambda f: f[0])


def npv(flows: list[tuple[date, Decimal]], rate: Decimal) -> Decimal:
    """Net present value of `flows` at `rate`, on a 365-day year.

    Exposed because it is the definition XIRR solves against: asserting
    `npv(flows, xirr(flows)) ~ 0` verifies a rate without needing a second
    implementation to agree with it.
    """
    if not flows:
        return Decimal(0)
    t0 = flows[0][0]
    total = Decimal(0)
    for when, amount in flows:
        years = Decimal((when - t0).days) / XIRR_DAYS_PER_YEAR
        total += Decimal(str(float(amount) / (1.0 + float(rate)) ** float(years)))
    return total


def xirr(flows: list[tuple[date, Decimal]], guess: float = 0.15) -> Decimal | None:
    """Money-weighted return: the rate at which NPV is zero.

    §9.2. Returns None rather than a garbage number: XIRR is genuinely
    undefined for some flow patterns, and no sign change means no root.

    Newton-Raphson fails on real patterns — large late contributions, multiple
    sign changes — so the bisection fallback is not optional. Float lives inside
    this function only; the answer crosses back as Decimal.
    """
    if len(flows) < 2:
        return None
    signs = {1 if a > 0 else -1 for _, a in flows if a != 0}
    if len(signs) < 2:
        return None

    t0 = flows[0][0]
    years = [float((d - t0).days) / float(XIRR_DAYS_PER_YEAR) for d, _ in flows]
    amounts = [float(a) for _, a in flows]

    def _npv(rate: float) -> float:
        total = 0.0
        for a, t in zip(amounts, years, strict=True):
            total += a / (1.0 + rate) ** t
        return total

    def _dnpv(rate: float) -> float:
        total = 0.0
        for a, t in zip(amounts, years, strict=True):
            total += -t * a / (1.0 + rate) ** (t + 1.0)
        return total

    # --- Newton-Raphson ---
    rate = guess
    for _ in range(_MAX_NEWTON_STEPS):
        try:
            value = _npv(rate)
        except (OverflowError, ZeroDivisionError):
            break
        if abs(value) < _NPV_TOLERANCE:
            return Decimal(str(rate))
        try:
            slope = _dnpv(rate)
        except (OverflowError, ZeroDivisionError):
            break
        if abs(slope) < 1e-12:
            break
        stepped = rate - value / slope
        if stepped <= _RATE_FLOOR:
            stepped = (rate + _RATE_FLOOR) / 2.0  # damp toward the floor
        if abs(stepped - rate) < 1e-9:
            return Decimal(str(stepped))
        rate = stepped

    # --- bisection fallback ---
    lo, hi = _RATE_FLOOR, 10.0
    try:
        if _npv(lo) * _npv(hi) > 0:
            return None
    except (OverflowError, ZeroDivisionError):
        return None
    for _ in range(_MAX_BISECTION_STEPS):
        mid = (lo + hi) / 2.0
        if _npv(lo) * _npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-10:
            break
    return Decimal(str((lo + hi) / 2.0))


def twrr(
    nav_start: Decimal | None, nav_end: Decimal | None, days: int
) -> tuple[Decimal | None, Decimal | None]:
    """Time-weighted return: what the fund delivered, ignoring cashflows.

    §9.3. At position level this collapses to a NAV ratio, because M0 supplies
    a daily IDCW-adjusted series — a direct payoff from building `nav_adj`
    correctly.

    Under one year there IS no annualised figure: the second element is `None`,
    not a copy of the cumulative one (`MODULE_2.md` §7.3). Not cosmetic —
    `compute_returns` subtracts `twrr_ann` from an annualised XIRR, so a
    four-month position reported +11 points of "timing benefit" that was
    entirely the unit mismatch between the operands.
    """
    if nav_start is None or nav_end is None or nav_start <= 0 or days <= 0:
        return None, None

    growth = nav_end / nav_start
    cumulative = growth - 1
    years = Decimal(days) / TWRR_DAYS_PER_YEAR
    if years < 1:
        return cumulative, None

    # Shared with M2 via `src/common/`, because invariant 3 forbids M1
    # importing M2. Was a float round-trip here, which agreed to ~1e-7 but
    # converted twice on a money path invariant 1 keeps in Decimal.
    return cumulative, annualise(growth, days)


def nav_on_or_before(navs: dict[date, Decimal], on: date) -> Decimal | None:
    """Latest published NAV on or before `on`.

    Funds do not price on weekends or market holidays, so an as-of date can
    legitimately have no NAV of its own. Rolling forward would use a price that
    did not exist yet.
    """
    candidates = [d for d in navs if d <= on]
    return navs[max(candidates)] if candidates else None


def compute_returns(
    pos: Position,
    txns: list[Txn],
    navs: dict[date, Decimal],
    scope: str = "scheme",
) -> Returns:
    """All four numbers for one position. MODULE_1.md §9.3.

    NAVs must come from the IDCW-adjusted series. For a Growth option the two
    are identical because nothing is distributed; for an IDCW plan, raw NAV
    drops on every payout and returns computed on it are wrong.
    """
    flows = build_cashflows(txns, scope, pos.market_value, pos.as_of)
    rate = xirr(flows)

    nav_start = nav_on_or_before(navs, pos.first_purchase)
    nav_end = nav_on_or_before(navs, pos.as_of)
    days = (pos.as_of - pos.first_purchase).days
    twrr_cum, twrr_ann = twrr(nav_start, nav_end, days)

    # Both figures are on a 365-day basis (DECISIONS V0-17), so this
    # subtraction is exact rather than approximate. MODULE_1.md §9.3 puts TWRR
    # on 365.25, which would put 0.068% of a year straight into a difference
    # of two near-equal numbers.
    timing = rate - twrr_ann if rate is not None and twrr_ann is not None else None

    # `> 0`, not truthiness. A position that has returned more cash than it
    # took in carries a NEGATIVE `invested_net` (the column is net of
    # redemption proceeds), and dividing by it flips the sign of a real profit —
    # a falsy check only rejects exactly zero.
    absolute = (
        (pos.market_value - pos.invested_net) / pos.invested_net
        if pos.invested_net > 0
        else None
    )

    note = None
    if rate is None:
        note = "XIRR is undefined for this cashflow pattern."
    elif nav_start is None:
        note = "No NAV at first purchase; TWRR unavailable."
    elif twrr_ann is None:
        note = (
            "Held under a year, so the fund return is cumulative and not "
            "annualised; the timing effect needs both on the same basis."
        )

    return Returns(
        xirr=rate,
        twrr_cum=twrr_cum,
        twrr_ann=twrr_ann,
        timing_effect=timing,
        absolute=absolute,
        obs_note=note,
    )
