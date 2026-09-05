"""Recompute the V0.1 expected output, independently of the lot engine.

Reads `transactions.csv` with the stdlib CSV module and does the FIFO
arithmetic longhand. It imports nothing from `src.m1_ledger`, so agreement
between this and the engine is a real check rather than a tautology.

`PLAN.md` §8.3 calls for expected output "checked against an independent
calculation". Repricing onto the real NAV series means the numbers are no
longer round, so this script replaces eyeball verification — the arithmetic is
simple enough to read, and it is written a second time here on purpose.

Usage:
    python -m scripts.verify_v0_ledger          # print and rewrite expected.yaml
    python -m scripts.verify_v0_ledger --check  # print only, exit 1 on drift
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "v0_ledger"

Q4 = Decimal("0.0001")
Q6 = Decimal("0.000001")
LTCG_DAYS = 365

OPENING = {"PURCHASE", "SIP", "SWITCH_IN", "STP_IN", "IDCW_REINVEST", "MERGER_IN"}
CLOSING = {"REDEMPTION", "SWITCH_OUT", "STP_OUT", "SWP", "MERGER_OUT"}


def read_rows() -> list[dict[str, str]]:
    with (FIXTURES / "transactions.csv").open(encoding="utf-8") as fh:
        body = [ln for ln in fh if not ln.lstrip().startswith(">")]
    return list(csv.DictReader(body))


def d(value: str) -> Decimal:
    return Decimal(value) if value not in ("", None) else Decimal(0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    rows = read_rows()

    # Reversals and what they reverse never reach the engine.
    reversed_refs = {r["reverses_txn_ref"] for r in rows if r["reverses_txn_ref"]}
    live = [
        r
        for r in rows
        if r["txn_type"] != "REVERSAL" and r["txn_ref"] not in reversed_refs
    ]
    live.sort(key=lambda r: (r["txn_date"], int(r["txn_seq"]), r["txn_ref"]))

    lots: list[dict[str, object]] = []
    consumptions: dict[str, list[dict[str, object]]] = {}
    idcw: list[dict[str, object]] = []

    for r in live:
        t = r["txn_type"]
        when = date.fromisoformat(r["txn_date"])

        if t == "IDCW_PAYOUT":
            idcw.append(
                {
                    "txn_ref": r["txn_ref"],
                    "date": when,
                    "amount": d(r["amount"]),
                    "creates_lot": False,
                }
            )
            continue

        if t in OPENING:
            units = d(r["units"])
            cost_total = (abs(d(r["amount"])) + d(r["stamp_duty"])).quantize(Q4)
            lots.append(
                {
                    "ref": r["txn_ref"],
                    "scheme": r["scheme_id"],
                    "folio": r["folio"],
                    "acq": when,
                    "units": units,
                    "remaining": units,
                    "cost_total": cost_total,
                    "cpu": (cost_total / units).quantize(Q6),
                    "origin": t.lower(),
                }
            )
            if t == "IDCW_REINVEST":
                idcw.append(
                    {
                        "txn_ref": r["txn_ref"],
                        "date": when,
                        "amount": abs(d(r["amount"])),
                        "creates_lot": True,
                    }
                )
            continue

        if t not in CLOSING:
            raise SystemExit(f"unhandled {t}")

        to_close = abs(d(r["units"]))
        gross = abs(d(r["amount"]))
        net_total = gross - d(r["exit_load"]) - d(r["stt"])
        npu = (net_total / to_close).quantize(Q6)

        out = []
        pool = sorted(
            (
                x
                for x in lots
                if x["scheme"] == r["scheme_id"]
                and x["folio"] == r["folio"]
                and Decimal(str(x["remaining"])) > 0
            ),
            key=lambda x: (x["acq"], x["ref"]),
        )
        for lot in pool:
            if to_close <= 0:
                break
            take = min(Decimal(str(lot["remaining"])), to_close)
            days = (when - lot["acq"]).days  # type: ignore[operator]
            cost = (Decimal(str(lot["cpu"])) * take).quantize(Q4)
            proceeds = (take * npu).quantize(Q4)
            out.append(
                {
                    "lot_ref": lot["ref"],
                    "units": take,
                    "days": days,
                    "gain_type": "LTCG" if days > LTCG_DAYS else "STCG",
                    "cost": cost,
                    "proceeds_net": proceeds,
                    "gain": (proceeds - cost).quantize(Q4),
                }
            )
            lot["remaining"] = Decimal(str(lot["remaining"])) - take
            to_close -= take
        if to_close > 0:
            raise SystemExit(f"{r['txn_ref']}: short by {to_close} units")
        consumptions[r["txn_ref"]] = out

    # Label lots L1..Ln in acquisition order, matching the engine's golden refs.
    ordered = sorted(lots, key=lambda x: (x["acq"], x["ref"]))
    label = {lot["ref"]: f"L{n}" for n, lot in enumerate(ordered, 1)}

    signed = Decimal(0)
    for r in live:
        if not r["units"]:
            continue
        if r["txn_type"] in OPENING:
            signed += abs(d(r["units"]))
        elif r["txn_type"] in CLOSING:
            signed -= abs(d(r["units"]))
    held = sum((Decimal(str(x["remaining"])) for x in lots), Decimal(0))

    by_scheme: dict[str, Decimal] = {}
    for lot in lots:
        key = str(lot["scheme"])
        by_scheme[key] = by_scheme.get(key, Decimal(0)) + Decimal(str(lot["remaining"]))

    # --- report -----------------------------------------------------------
    print("LOTS")
    for lot in ordered:
        print(
            f"  {label[lot['ref']]:3} {lot['scheme'][:28]:30} acq={lot['acq']} "
            f"units={lot['units']} cost={lot['cost_total']} cpu={lot['cpu']}"
        )
    print("\nCONSUMPTIONS")
    for ref, out in consumptions.items():
        print(f"  {ref}")
        for c in out:
            print(
                f"    {label[str(c['lot_ref'])]:3} units={c['units']} "
                f"days={c['days']:4} {c['gain_type']} cost={c['cost']} "
                f"net={c['proceeds_net']} gain={c['gain']}"
            )
        print(
            f"    TOTAL net={sum(Decimal(str(c['proceeds_net'])) for c in out)} "
            f"gain={sum(Decimal(str(c['gain'])) for c in out)}"
        )
    print(f"\nCONSERVATION signed={signed} held={held} equal={signed == held}")

    # --- write ------------------------------------------------------------
    lines = [
        "# Golden expected output for transactions.csv — PLAN.md §8.3.",
        "#",
        "# GENERATED by scripts/verify_v0_ledger.py, which recomputes the FIFO",
        "# arithmetic longhand and imports nothing from src.m1_ledger. Agreement",
        "# between this file and the engine is therefore a real check.",
        "#",
        "# Do not edit to make a failing test pass — if the two disagree, one of",
        "# them is wrong and the disagreement is the finding.",
        "",
        "lots:",
    ]
    for lot in ordered:
        lines.append(
            f'  - {{lot_id: {label[lot["ref"]]}, scheme: "{lot["scheme"]}", '
            f"acquisition_date: {lot['acq']}, origin: {lot['origin']}, "
            f'units_original: "{lot["units"]}", cost_total: "{lot["cost_total"]}", '
            f'cost_per_unit: "{lot["cpu"]}"}}'
        )
    lines += ["", "lots_not_created_from: [T007, T008]", "", "consumptions:"]
    for ref, out in consumptions.items():
        lines.append(f"  {ref}:")
        for c in out:
            lines.append(
                f"    - {{lot_id: {label[str(c['lot_ref'])]}, "
                f'units_consumed: "{c["units"]}", holding_days: {c["days"]}, '
                f'gain_type: {c["gain_type"]}, cost_allocated: "{c["cost"]}", '
                f'proceeds_net: "{c["proceeds_net"]}", gain_amount: "{c["gain"]}"}}'
            )
    lines += ["", "totals:"]
    for r in live:
        if r["txn_type"] not in CLOSING:
            continue
        ref = r["txn_ref"]
        cost_sum = sum(Decimal(str(c["cost"])) for c in consumptions[ref])
        gain_sum = sum(Decimal(str(c["gain"])) for c in consumptions[ref])
        gross = abs(d(r["amount"]))
        net = gross - d(r["exit_load"]) - d(r["stt"])
        lines += [
            f"  {ref}:",
            f'    gross: "{gross}"',
            f'    exit_load: "{d(r["exit_load"])}"',
            f'    stt: "{d(r["stt"])}"',
            f'    net_total: "{net}"',
            f'    cost_allocated: "{cost_sum}"',
            f'    gain_amount: "{gain_sum}"',
        ]
    lines += ["", "units_remaining:"]
    for scheme_id, units in sorted(by_scheme.items()):
        lines.append(f'  "{scheme_id}": "{units}"')
    lines += [
        "",
        "# PLAN.md §8.3 invariant 1.",
        "conservation:",
        f'  signed_txn_units: "{signed}"',
        f'  sum_units_remaining: "{held}"',
        "",
        "# Slab income. IDCW_REINVEST appears here AND in `lots` — both effects fire.",
        "idcw_income:",
    ]
    for row in idcw:
        lines.append(
            f"  - {{txn_ref: {row['txn_ref']}, txn_date: {row['date']}, "
            f'amount: "{row["amount"]}", creates_lot: {str(row["creates_lot"]).lower()}}}'
        )
    lines.append("")

    text = "\n".join(lines)
    target = FIXTURES / "expected.yaml"
    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != text:
            print(
                "\nDRIFT: expected.yaml does not match the recomputation", file=sys.stderr
            )
            sys.exit(1)
        print("\nexpected.yaml matches the independent recomputation")
    else:
        target.write_text(text, encoding="utf-8")
        print(f"\nwrote {target}")


if __name__ == "__main__":
    main()
