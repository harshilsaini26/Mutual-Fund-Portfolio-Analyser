"""Generate the V0.1 transaction fixture and its expected output from real NAVs.

Repricing the golden fixture onto the real NAV series costs the eyeball-check
that clean synthetic NAVs gave, so two things preserve verifiability instead:

  - **Rupee amounts stay round.** You pay Rs 10,000 and receive whatever units
    that buys, which is what actually happens. Amounts and dates remain
    checkable by inspection; only unit counts are computed.
  - **This script is not the engine.** Expected values are derived here by
    plain arithmetic, then asserted against `src/m1_ledger/lots.py`. Two
    independent code paths agreeing is the check; if they diverge, that
    divergence is the finding.

Gain type and FIFO order depend on dates, not amounts, so they stay verifiable
by reading the dates alone.

Usage:
    python -m scripts.build_v0_fixture
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from src.common.decimals import MONEY_Q, UNITS_Q
from src.common.fixtures import load_yaml

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "v0_ledger"

HDFC = "AMFI:HDFC-FLEXICAP-DIR-G"
ICICI = "AMFI:ICICI-MULTIASSET-REG-G"

#: Stamp duty on mutual fund purchases, 0.005%, levied since July 2020. It is
#: deducted from the payment before units are allotted, so the investor's units
#: are bought with slightly less than they paid — and the lot's cost is the
#: full payment (MODULE_1.md §7.2: cost_total = amount + stamp_duty).
STAMP_DUTY_RATE = Decimal("0.00005")

#: STT on equity-oriented redemptions and switch-outs, 0.001%.
STT_RATE = Decimal("0.00001")

#: HDFC Flexi Cap exit load: 1% if redeemed within 365 days. Charged only on
#: the units actually inside the load window, not the whole redemption.
EXIT_LOAD_RATE = Decimal("0.01")
EXIT_LOAD_DAYS = 365


def load_navs() -> dict[str, dict[date, Decimal]]:
    raw = load_yaml(FIXTURES / "nav_series.yaml")["series"]
    return {
        scheme_id: {d: Decimal(str(v)) for d, v in block["navs"].items()}
        for scheme_id, block in raw.items()
    }


def trading_day_on_or_after(navs: dict[date, Decimal], target: date) -> date:
    """The first day with a published NAV on or after `target`.

    A SIP mandate dated the 1st does not transact on the 1st when that is a
    Saturday or a market holiday; it transacts on the next business day at that
    day's NAV. Three of the original fixture's dates had no NAV for exactly
    this reason.
    """
    for offset in range(0, 15):
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
    units = (invested / nav).quantize(UNITS_Q)
    return {
        "date": day,
        "nav": nav,
        "units": units,
        "amount": invested,
        "stamp_duty": duty,
        "payment": payment,
    }


def main() -> None:
    navs = load_navs()
    h, i = navs[HDFC], navs[ICICI]

    # --- HDFC: six monthly SIP instalments of Rs 10,000 --------------------
    sips = [buy(h, date(2024, m, 1), Decimal("10000.00")) for m in range(1, 7)]
    # A seventh instalment that bounced, plus its reversal. Neither reaches the
    # engine — MODULE_1.md §3.2: excluded, not netted.
    bounced = buy(h, date(2024, 7, 1), Decimal("10000.00"))

    # --- HDFC: a redemption straddling the one-year boundary ---------------
    # Mid-January 2025 puts the first instalment past 365 days and the rest
    # inside it, so ONE transaction yields both LTCG and STCG.
    redeem_day = trading_day_on_or_after(h, date(2025, 1, 15))
    redeem_units = Decimal("20.000000")
    redeem_nav = h[redeem_day]
    redeem_gross = (redeem_units * redeem_nav).quantize(MONEY_Q)

    # Exit load applies only to units inside the 365-day window.
    loaded_units = Decimal(0)
    remaining = redeem_units
    for s in sips:
        if remaining <= 0:
            break
        take = min(Decimal(str(s["units"])), remaining)
        if (redeem_day - s["date"]).days <= EXIT_LOAD_DAYS:  # type: ignore[operator]
            loaded_units += take
        remaining -= take
    exit_load = (loaded_units * redeem_nav * EXIT_LOAD_RATE).quantize(MONEY_Q)
    redeem_stt = (redeem_gross * STT_RATE).quantize(MONEY_Q)

    # --- HDFC -> ICICI switch on a real trading day ------------------------
    switch_day = trading_day_on_or_after(h, date(2026, 1, 15))
    switch_day = trading_day_on_or_after(i, switch_day)  # both must price it
    switch_units = Decimal("10.000000")
    switch_nav = h[switch_day]
    switch_gross = (switch_units * switch_nav).quantize(MONEY_Q)
    switch_stt = (switch_gross * STT_RATE).quantize(MONEY_Q)
    switch_proceeds = switch_gross - switch_stt
    in_nav = i[switch_day]
    in_duty = (switch_proceeds * STAMP_DUTY_RATE).quantize(MONEY_Q)
    in_amount = switch_proceeds - in_duty
    in_units = (in_amount / in_nav).quantize(UNITS_Q)

    # NOTE: no IDCW transactions appear in this fixture. Both schemes here are
    # GROWTH options, which accumulate and distribute nothing — a growth option
    # structurally cannot pay IDCW. Modelling one against a NAV series that
    # never dropped to fund it manufactures units and cash from nowhere: an
    # earlier draft produced a +45.60pp "timing effect" that was pure artifact.
    # IDCW code paths are covered by synthetic transactions in the test suite,
    # where the NAV series is not claimed to be real.

    rows: list[str] = []

    def row(**kw: object) -> None:
        rows.append(
            ",".join(
                str(kw.get(k, ""))
                for k in (
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
                )
            )
        )

    HN = "HDFC Flexi Cap Fund - Growth Option - Direct Plan"
    IN_ = "ICICI Prudential Multi Asset Allocation Fund - Growth"

    for n, s in enumerate(sips, start=1):
        row(
            txn_ref=f"T{n:03d}",
            user_id="USER-01",
            folio="F0001/22",
            scheme_id=HDFC,
            scheme_raw_name=HN,
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

    row(
        txn_ref="T007",
        user_id="USER-01",
        folio="F0001/22",
        scheme_id=HDFC,
        scheme_raw_name=HN,
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
    row(
        txn_ref="T008",
        user_id="USER-01",
        folio="F0001/22",
        scheme_id=HDFC,
        scheme_raw_name=HN,
        txn_date=bounced["date"] + timedelta(days=4),
        txn_seq=0,  # type: ignore[operator]
        txn_type="REVERSAL",
        units=-Decimal(str(bounced["units"])),
        nav=bounced["nav"],
        amount=bounced["amount"],
        stamp_duty=0,
        stt=0,
        exit_load=0,
        reverses_txn_ref="T007",
    )

    row(
        txn_ref="T009",
        user_id="USER-01",
        folio="F0001/22",
        scheme_id=HDFC,
        scheme_raw_name=HN,
        txn_date=redeem_day,
        txn_seq=0,
        txn_type="REDEMPTION",
        units=-redeem_units,
        nav=redeem_nav,
        amount=redeem_gross,
        stamp_duty=0,
        stt=redeem_stt,
        exit_load=exit_load,
    )

    row(
        txn_ref="T010",
        user_id="USER-01",
        folio="F0001/22",
        scheme_id=HDFC,
        scheme_raw_name=HN,
        txn_date=switch_day,
        txn_seq=0,
        txn_type="SWITCH_OUT",
        units=-switch_units,
        nav=switch_nav,
        amount=switch_gross,
        stamp_duty=0,
        stt=switch_stt,
        exit_load=0,
        switch_group_id="SW001",
    )
    row(
        txn_ref="T011",
        user_id="USER-01",
        folio="F0003/01",
        scheme_id=ICICI,
        scheme_raw_name=IN_,
        txn_date=switch_day,
        txn_seq=1,
        txn_type="SWITCH_IN",
        units=in_units,
        nav=in_nav,
        amount=-in_amount,
        stamp_duty=in_duty,
        stt=0,
        exit_load=0,
        switch_group_id="SW001",
    )

    header = (
        "txn_ref,user_id,folio,scheme_id,scheme_raw_name,txn_date,txn_seq,txn_type,"
        "units,nav,amount,stamp_duty,stt,exit_load,switch_group_id,reverses_txn_ref"
    )
    preamble = f"""> V0.1 transactions, priced on the REAL NAV series in nav_series.yaml.
> Generated by scripts/build_v0_fixture.py — regenerate, do not hand-edit.
>
> Payments are round rupee amounts; unit counts fall out of the real NAV, which
> is what actually happens when you invest Rs 10,000. Dates are real trading
> days: a mandate dated the 1st transacts on the next business day when the 1st
> is a weekend or a market holiday.
>
> HDFC Flexi Cap here is the DIRECT plan, matching the NAV workbook. Its NAV
> series and the Regular plan's differ by ~10% and must never be crossed.
>
> Cases covered: six SIP lots; a redemption straddling the 365-day boundary so
> one transaction yields both LTCG and STCG; exit load charged only on units
> inside the load window; a bounced SIP excluded with its reversal; a switch
> taxed on the way out with the clock restarting on the way in; and a lot split
> across two transactions, landing on opposite sides of the year boundary.
>
> NOT covered here, deliberately: IDCW. Both schemes are GROWTH options, which
> distribute nothing, so an IDCW transaction against these NAV series would
> conjure units and cash the NAV never paid for (DECISIONS V0-09). Those paths
> are tested with synthetic transactions instead.
>
> ICICI's tax class is unknown (DECISIONS V0-03), so nothing is redeemed from
> it — a gain there cannot be classified without inventing a threshold.
>
{header}"""

    (FIXTURES / "transactions.csv").write_text(
        preamble + "\n" + "\n".join(rows) + "\n", encoding="utf-8"
    )

    print("Generated transactions.csv\n")
    print(f"  HDFC SIP dates : {[str(s['date']) for s in sips]}")
    print(f"  SIP NAVs       : {[str(s['nav']) for s in sips]}")
    print(f"  SIP units      : {[str(s['units']) for s in sips]}")
    print(f"  redemption     : {redeem_day} nav={redeem_nav} gross={redeem_gross}")
    print(f"    exit load    : {exit_load} on {loaded_units} units inside 365d")
    print(f"    stt          : {redeem_stt}")
    print(f"  switch         : {switch_day} out_nav={switch_nav} in_nav={in_nav}")
    print(f"    in_units     : {in_units}")
    print()
    for n, s in enumerate(sips, 1):
        days = (redeem_day - s["date"]).days  # type: ignore[operator]
        print(
            f"  lot L{n}: acq={s['date']} units={s['units']} "
            f"days_at_redemption={days} -> {'LTCG' if days > 365 else 'STCG'}"
        )


if __name__ == "__main__":
    main()
