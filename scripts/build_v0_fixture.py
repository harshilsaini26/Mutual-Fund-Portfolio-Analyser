"""Generate the V0.1 transaction fixture from the real NAV series.

Repricing onto real NAVs costs the eyeball-check that clean synthetic numbers
gave, so two things preserve verifiability instead:

  - **Rupee amounts stay round.** You pay Rs 10,000 and receive whatever units
    that buys, which is what actually happens. Amounts and dates stay checkable
    by inspection; only unit counts are computed.
  - **This script is not the engine.** `scripts/verify_v0_ledger.py` recomputes
    the expected output longhand and imports nothing from `src.m1_ledger`.

Gain type and FIFO order depend on dates, not amounts, so they remain verifiable
by reading the dates alone.

Usage:
    python -m scripts.build_v0_fixture
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from src.common.decimals import MONEY_Q, UNITS_Q
from tests.fakes.loader import load_yaml

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "v0_ledger"

HDFC = "INF179K01UT0"
ICICI = "INF109K01761"
KOTAK = "INF174KA1EZ1"

NAMES = {
    HDFC: "HDFC Flexi Cap Fund - Growth Option - Direct Plan",
    ICICI: "ICICI Prudential Multi Asset Allocation Fund - Growth",
    KOTAK: "Kotak Pioneer Fund- Direct Plan- Growth Option",
}
FOLIOS = {HDFC: "F0001/22", ICICI: "F0003/01", KOTAK: "F0004/18"}

#: Stamp duty on purchases, 0.005%, levied since July 2020. Deducted from the
#: payment before units are allotted, so units are bought with slightly less
#: than was paid — and the lot's cost is the full payment
#: (MODULE_1.md §7.2: cost_total = amount + stamp_duty).
STAMP_DUTY_RATE = Decimal("0.00005")

#: STT on equity-oriented redemptions and switch-outs, 0.001%.
STT_RATE = Decimal("0.00001")

#: Exit load is PER SCHEME, not a global constant. HDFC and ICICI charge 1%;
#: Kotak Pioneer charges 0.5%. An earlier draft hardcoded 1% for everything,
#: which would have silently overcharged Kotak by a factor of two — the kind of
#: thing a single-fund fixture cannot catch.
EXIT_LOAD = {
    HDFC: (Decimal("0.01"), 365),
    ICICI: (Decimal("0.01"), 365),
    KOTAK: (Decimal("0.005"), 365),
}


def load_navs() -> dict[str, dict[date, Decimal]]:
    raw = load_yaml(FIXTURES / "nav_series.yaml")["series"]
    return {
        scheme_id: {d: Decimal(str(v)) for d, v in block["navs"].items()}
        for scheme_id, block in raw.items()
    }


def trading_day_on_or_after(navs: dict[date, Decimal], target: date) -> date:
    """First day with a published NAV on or after `target`.

    A SIP mandate dated the 1st does not transact on the 1st when that is a
    weekend or market holiday; it transacts on the next business day at that
    day's NAV.
    """
    for offset in range(15):
        day = target + timedelta(days=offset)
        if day in navs:
            return day
    raise KeyError(f"no NAV within 15 days of {target}")


def buy(navs: dict[date, Decimal], target: date, payment: Decimal) -> dict[str, object]:
    """A purchase of `payment` rupees on the first trading day from `target`."""
    day = trading_day_on_or_after(navs, target)
    nav = navs[day]
    duty = (payment * STAMP_DUTY_RATE).quantize(MONEY_Q)
    invested = payment - duty
    return {
        "date": day,
        "nav": nav,
        "units": (invested / nav).quantize(UNITS_Q),
        "amount": invested,
        "stamp_duty": duty,
    }


def exit_load_for(
    scheme_id: str,
    lots: list[dict[str, object]],
    redeem_units: Decimal,
    redeem_day: date,
    redeem_nav: Decimal,
) -> tuple[Decimal, Decimal]:
    """Exit load on the units actually inside the load window, in FIFO order.

    Returns (load_amount, units_inside_window). Charging the load on the whole
    redemption would overcharge any position holding units past the window.
    """
    rate, window = EXIT_LOAD[scheme_id]
    inside = Decimal(0)
    remaining = redeem_units
    for lot in lots:
        if remaining <= 0:
            break
        take = min(Decimal(str(lot["units"])), remaining)
        if (redeem_day - lot["date"]).days <= window:  # type: ignore[operator]
            inside += take
        remaining -= take
    return (inside * redeem_nav * rate).quantize(MONEY_Q), inside


class Rows:
    """Accumulates CSV lines in the txn table's column order."""

    COLUMNS = (
        "txn_ref",
        "user_id",
        "folio",
        "scheme_id",
        "scheme_raw_name",
        "txn_date",
        "txn_seq",
        "txn_type",
        "units",
        "nav",
        "amount",
        "stamp_duty",
        "stt",
        "exit_load",
        "switch_group_id",
        "reverses_txn_ref",
        "units_balance_rep",
    )

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._n = 0
        #: Running unit balance per (folio, scheme), as a statement prints it.
        self._balance: dict[tuple[str, str], Decimal] = {}

    def add(self, scheme_id: str, **kw: object) -> str:
        self._n += 1
        ref = f"T{self._n:03d}"
        kw.setdefault("user_id", "USER-01")
        kw |= {
            "txn_ref": ref,
            "folio": FOLIOS[scheme_id],
            "scheme_id": scheme_id,
            "scheme_raw_name": NAMES[scheme_id],
        }
        # A statement prints the balance after every entry, INCLUDING a
        # reversal — which is how a reader sees the bounced instalment undone.
        key = (str(kw["folio"]), scheme_id)
        units = kw.get("units")
        if units not in (None, "", 0):
            self._balance[key] = self._balance.get(key, Decimal(0)) + Decimal(str(units))
        kw["units_balance_rep"] = self._balance.get(key, Decimal(0))
        self.lines.append(",".join(str(kw.get(c, "")) for c in self.COLUMNS))
        return ref


def main() -> None:
    navs = load_navs()
    h, i, k = navs[HDFC], navs[ICICI], navs[KOTAK]
    rows = Rows()

    # === HDFC: six monthly SIP instalments of Rs 10,000 ====================
    sips = [buy(h, date(2024, m, 1), Decimal("10000.00")) for m in range(1, 7)]
    for s in sips:
        rows.add(
            HDFC,
            txn_date=s["date"],
            txn_seq=0,
            txn_type="SIP",
            units=s["units"],
            nav=s["nav"],
            amount=-Decimal(str(s["amount"])),
            stamp_duty=s["stamp_duty"],
            stt=0,
            exit_load=0,
        )

    # A seventh instalment that bounced, plus its reversal. Neither reaches the
    # engine — MODULE_1.md §3.2: excluded, not netted.
    bounced = buy(h, date(2024, 7, 1), Decimal("10000.00"))
    ref_bounced = rows.add(
        HDFC,
        txn_date=bounced["date"],
        txn_seq=0,
        txn_type="SIP",
        units=bounced["units"],
        nav=bounced["nav"],
        amount=-Decimal(str(bounced["amount"])),
        stamp_duty=bounced["stamp_duty"],
        stt=0,
        exit_load=0,
    )
    rows.add(
        HDFC,
        txn_date=bounced["date"] + timedelta(days=4),  # type: ignore[operator]
        txn_seq=0,
        txn_type="REVERSAL",
        units=-Decimal(str(bounced["units"])),
        nav=bounced["nav"],
        amount=bounced["amount"],
        stamp_duty=0,
        stt=0,
        exit_load=0,
        reverses_txn_ref=ref_bounced,
    )

    # A redemption straddling the one-year boundary: the first instalment is
    # past 365 days, the rest are not, so ONE transaction yields both LTCG and
    # STCG. Exit load 1%, on the units inside the window only.
    r_day = trading_day_on_or_after(h, date(2025, 1, 15))
    r_units, r_nav = Decimal("20.000000"), h[r_day]
    r_gross = (r_units * r_nav).quantize(MONEY_Q)
    r_load, r_inside = exit_load_for(HDFC, sips, r_units, r_day, r_nav)
    rows.add(
        HDFC,
        txn_date=r_day,
        txn_seq=0,
        txn_type="REDEMPTION",
        units=-r_units,
        nav=r_nav,
        amount=r_gross,
        stamp_duty=0,
        stt=(r_gross * STT_RATE).quantize(MONEY_Q),
        exit_load=r_load,
    )

    # === HDFC -> ICICI switch, on a day BOTH schemes priced =================
    s_day = trading_day_on_or_after(i, trading_day_on_or_after(h, date(2026, 1, 15)))
    s_units, s_nav = Decimal("10.000000"), h[s_day]
    s_gross = (s_units * s_nav).quantize(MONEY_Q)
    s_stt = (s_gross * STT_RATE).quantize(MONEY_Q)
    s_proceeds = s_gross - s_stt
    in_nav = i[s_day]
    in_duty = (s_proceeds * STAMP_DUTY_RATE).quantize(MONEY_Q)
    in_amount = s_proceeds - in_duty

    rows.add(
        HDFC,
        txn_date=s_day,
        txn_seq=0,
        txn_type="SWITCH_OUT",
        units=-s_units,
        nav=s_nav,
        amount=s_gross,
        stamp_duty=0,
        stt=s_stt,
        exit_load=0,
        switch_group_id="SW001",
    )
    rows.add(
        ICICI,
        txn_date=s_day,
        txn_seq=1,
        txn_type="SWITCH_IN",
        units=(in_amount / in_nav).quantize(UNITS_Q),
        nav=in_nav,
        amount=-in_amount,
        stamp_duty=in_duty,
        stt=0,
        exit_load=0,
        switch_group_id="SW001",
    )

    # === Kotak Pioneer: a 0.5% exit load and a NAV two orders smaller =======
    # NAV here is ~Rs 20-40 against HDFC's ~Rs 2,000, so Rs 50,000 buys
    # hundreds of units rather than a handful. Unit quantisation to 6dp bites
    # differently at that scale, and the exit load is HALF the other schemes'.
    k1 = buy(k, date(2024, 11, 1), Decimal("50000.00"))
    k2 = buy(k, date(2025, 8, 1), Decimal("25000.00"))
    for kb in (k1, k2):
        rows.add(
            KOTAK,
            txn_date=kb["date"],
            txn_seq=0,
            txn_type="PURCHASE",
            units=kb["units"],
            nav=kb["nav"],
            amount=-Decimal(str(kb["amount"])),
            stamp_duty=kb["stamp_duty"],
            stt=0,
            exit_load=0,
        )

    # April 2026: the first purchase is long-term AND outside the load window;
    # the second is neither. Mixed gain types plus a partial load, at 0.5%.
    kr_day = trading_day_on_or_after(k, date(2026, 4, 15))
    kr_units, kr_nav = Decimal("1800.000000"), k[kr_day]
    kr_gross = (kr_units * kr_nav).quantize(MONEY_Q)
    kr_load, kr_inside = exit_load_for(KOTAK, [k1, k2], kr_units, kr_day, kr_nav)
    rows.add(
        KOTAK,
        txn_date=kr_day,
        txn_seq=0,
        txn_type="REDEMPTION",
        units=-kr_units,
        nav=kr_nav,
        amount=kr_gross,
        stamp_duty=0,
        stt=(kr_gross * STT_RATE).quantize(MONEY_Q),
        exit_load=kr_load,
    )

    header = ",".join(Rows.COLUMNS)
    preamble = f"""> V0.1 transactions, priced on the REAL NAV series in nav_series.yaml.
> Generated by scripts/build_v0_fixture.py — regenerate, do not hand-edit.
>
> Payments are round rupee amounts; unit counts fall out of the real NAV, which
> is what happens when you invest Rs 10,000. Dates are real trading days: a
> mandate dated the 1st transacts on the next business day when the 1st is a
> weekend or market holiday.
>
> Plans match their NAV workbooks exactly. HDFC and Kotak are DIRECT; ICICI is
> REGULAR. A Direct series under a Regular record differs by ~1%/year and only
> a VALUE reconciliation catches it (DECISIONS V0-05).
>
> Cases covered:
>   - six SIP lots, each carrying its own tax clock
>   - a redemption straddling the 365-day boundary: one transaction, both LTCG
>     and STCG, because gain type is a property of the LOT, not of the sale
>   - exit load charged only on units inside the window, at TWO different rates
>     (1% HDFC, 0.5% Kotak) — a per-scheme term, not a constant
>   - a lot split across two transactions, landing on opposite sides of the year
>   - a bounced SIP excluded with its reversal, not netted
>   - a switch: taxed on the way out, clock restarting on the way in
>   - Kotak's NAV is ~Rs 20-40 against HDFC's ~Rs 2,000, so unit quantisation is
>     exercised across two orders of magnitude
>   - Kotak's series ends 2026-09-03, a day before the others: real cross-fund
>     staleness at any portfolio as-of date
>
> NOT covered here, deliberately: IDCW. Every scheme with a real NAV series is a
> GROWTH option, which distributes nothing, so an IDCW row would conjure units
> and cash the NAV never paid for (DECISIONS V0-09). Those paths are tested with
> synthetic transactions instead.
>
> ICICI's tax class is unknown (DECISIONS V0-03), so nothing is redeemed from
> it — a gain there cannot be classified without inventing a threshold.
>
{header}"""

    (FIXTURES / "transactions.csv").write_text(
        preamble + "\n" + "\n".join(rows.lines) + "\n", encoding="utf-8"
    )

    print(f"Generated transactions.csv — {len(rows.lines)} rows\n")
    print("HDFC redemption")
    print(f"  {r_day} nav={r_nav} gross={r_gross}")
    print(f"  exit load {r_load} at 1.0% on {r_inside} of {r_units} units")
    print("Kotak redemption")
    print(f"  {kr_day} nav={kr_nav} gross={kr_gross}")
    print(f"  exit load {kr_load} at 0.5% on {kr_inside} of {kr_units} units")
    print("\nKotak lots")
    for n, kb in enumerate((k1, k2), 1):
        days = (kr_day - kb["date"]).days  # type: ignore[operator]
        print(
            f"  K{n}: {kb['date']} nav={kb['nav']} units={kb['units']} "
            f"days={days} -> {'LTCG' if days > 365 else 'STCG'}"
            f"  load_window={'inside' if days <= 365 else 'outside'}"
        )


if __name__ == "__main__":
    main()
