"""Print the portfolio's look-through. Not M6 — a terminal report.

    python -m scripts.show_lookthrough                 # positions from Zone B
    python -m scripts.show_lookthrough --equal 100000  # or a flat weighting

Reads issuer weights from Zone A and positions from the encrypted Zone B
ledger, materialising `scheme_issuer_weight` first for any scheme that has a
disclosure and no weights yet.

`--equal` exists because the Zone B ledger needs a key and may hold nothing:
it values every disclosed scheme at the same amount so the exposure *shape* is
readable without a real ledger. It says so in the output, because a portfolio
report that is not your portfolio must never look like one.
"""

from __future__ import annotations

import argparse
import getpass
from datetime import date
from decimal import Decimal

from src.common.decimals import connect
from src.common.types import SchemeId
from src.m0_data.config import warehouse_path
from src.m1_ledger.db import connect_ledger, ledger_path
from src.m3_lookthrough.concentration import concentration
from src.m3_lookthrough.engine import Position, compute_lookthrough
from src.m3_lookthrough.weights import (
    latest_as_of,
    load_issuer_weights,
    materialise_weights,
)

TOP_N = 20


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--equal", help="value every disclosed scheme at this amount, in rupees"
    )
    parser.add_argument("--user", default="USER-01")
    args = parser.parse_args()

    conn = connect(str(warehouse_path()))
    schemes = [
        SchemeId(r[0])
        for r in conn.execute(
            "SELECT DISTINCT scheme_id FROM holding_disclosure WHERE is_current = 1"
        )
    ]
    weights_by_scheme = {}
    as_of_by_scheme: dict[SchemeId, date] = {}
    for scheme_id in schemes:
        as_of = latest_as_of(conn, scheme_id)
        if as_of is None:
            continue
        if not load_issuer_weights(conn, scheme_id, as_of):
            materialise_weights(conn, scheme_id, as_of)
        found = load_issuer_weights(conn, scheme_id, as_of)
        if found:
            weights_by_scheme[scheme_id] = found
            as_of_by_scheme[scheme_id] = as_of

    positions, basis = _positions(args, weights_by_scheme)
    if not positions:
        raise SystemExit(
            "no positions. Import a CAS, or pass --equal 100000 to see the shape."
        )

    as_of = max(as_of_by_scheme.values()) if as_of_by_scheme else date.today()
    result = compute_lookthrough(positions, weights_by_scheme, as_of)
    summary = result.summary
    names = _issuer_names(conn, [str(e.issuer_id) for e in result.exposures[:TOP_N]])

    print(f"\nLOOK-THROUGH  ({basis})   disclosures as of {as_of}")
    print(f"{'issuer':<44}{'exposure Rs':>18}{'%':>9}{'funds':>7}")
    print("-" * 78)
    for exposure in result.exposures[:TOP_N]:
        label = names.get(str(exposure.issuer_id), str(exposure.issuer_id))
        print(
            f"{label[:44]:<44}{exposure.exposure_inr:>18,.2f}"
            f"{exposure.pct_of_portfolio:>8.2f}%{exposure.fund_count:>7}"
        )

    got = sum((e.exposure_inr for e in result.exposures), Decimal(0))
    print("-" * 78)
    print(
        f"{'CLOSURE':<44}{got:>18,.2f}"
        f"{'':>9}{'':>7}\n"
        f"{'  portfolio value':<44}{summary.total_value_inr:>18,.2f}\n"
        f"{'  delta':<44}{got - summary.total_value_inr:>18,.2f}   "
        f"{'OK' if abs(got - summary.total_value_inr) <= 1 else 'VIOLATION'}"
    )
    print(
        f"\ncoverage {summary.coverage_pct}%  ·  unresolved "
        f"{summary.unresolved_pct}%  ·  {summary.issuer_count} real issuers "
        f"across {len(positions)} funds"
    )
    equity = concentration(result.exposures, "equity")
    if equity.issuer_count:
        print(
            f"equity concentration: HHI {equity.hhi}  effective-N "
            f"{equity.effective_n}  top-10 {equity.top10_pct}%"
        )
    for caveat in result.caveats:
        print(f"\n  ! {caveat}")
    print()


def _positions(
    args: argparse.Namespace, weights_by_scheme: dict[SchemeId, object]
) -> tuple[list[Position], str]:
    if args.equal:
        value = Decimal(args.equal)
        return (
            [Position(s, value) for s in weights_by_scheme],
            f"ILLUSTRATIVE — every fund valued at Rs {value:,.2f}, NOT your ledger",
        )
    db = ledger_path()
    if not db.exists():
        raise SystemExit(
            f"no ledger at {db}. Import a CAS first, or pass --equal 100000."
        )
    ledger = connect_ledger(str(db), key=getpass.getpass("Zone B ledger key: "))
    rows = ledger.execute(
        "SELECT scheme_id, market_value FROM position"
        " WHERE user_id = ? AND market_value IS NOT NULL",
        (args.user,),
    ).fetchall()
    return (
        [Position(SchemeId(r[0]), r[1]) for r in rows],
        "your ledger",
    )


def _issuer_names(conn: object, issuer_ids: list[str]) -> dict[str, str]:
    """Best-effort display names. Falls back to the id, which is never wrong."""
    if not issuer_ids:
        return {}
    placeholders = ",".join("?" * len(issuer_ids))
    rows = conn.execute(  # type: ignore[attr-defined]
        f"SELECT issuer_id, canonical_name FROM issuer"
        f" WHERE issuer_id IN ({placeholders})",
        issuer_ids,
    ).fetchall()
    return {r[0]: r[1] or r[0] for r in rows}


if __name__ == "__main__":
    main()
