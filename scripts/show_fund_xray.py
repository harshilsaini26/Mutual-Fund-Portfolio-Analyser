"""Print a fund's return windows and risk statistics. Not M6 — a terminal report.

    python -m scripts.show_fund_xray --scheme INF179K01UT0
    python -m scripts.show_fund_xray --scheme INF179K01UT0 --as-of 2026-03-31

Reads adjusted NAV through `WarehouseMarketDataProvider`, so this script
contains no SQL of its own (invariant 3) and M2 contains none either.

Read-only: it opens the warehouse, computes, prints, and writes nothing. M3's
look-through materialises its weights because recomputing them costs a second;
a return window is one indexed query and a single pass, so there is nothing
worth persisting yet.

Windows that the series cannot support are printed as such rather than
omitted — of 19,598 schemes only 1,786 carry three years of NAV, and a report
that silently showed fewer rows for a short-history fund would read as though
the fund had been examined and found unremarkable.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date
from decimal import Decimal

from src.common.decimals import connect
from src.common.types import SchemeId
from src.m0_data.config import warehouse_path
from src.m0_data.providers.warehouse import WarehouseMarketDataProvider
from src.m2_fund.windows import (
    WINDOW_YEARS,
    ReturnWindow,
    compute_return_window,
    window_start,
)


def pct(value: Decimal) -> str:
    return f"{value * 100:>8.2f}%"


def line(w: ReturnWindow) -> str:
    dd = w.drawdown
    recovered = (
        f"recovered in {dd.recovery_days}d"
        if dd.recovery_days is not None
        else "not recovered" if dd.depth < 0 else "-"
    )
    return (
        f"  {w.window_key:16} {pct(w.return_ann)} {pct(w.return_cum)}"
        f" {pct(w.volatility_ann)} {pct(dd.depth)}"
        f"  {w.obs_count:>5}  {w.confidence:<6} {recovered}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scheme", required=True, help="scheme_id (an ISIN)")
    ap.add_argument("--as-of", help="YYYY-MM-DD; defaults to the latest NAV on record")
    args = ap.parse_args()

    conn = connect(warehouse_path())
    try:
        # The provider reads rows by column name, as jobs/import_cas.py does.
        conn.row_factory = sqlite3.Row
        md = WarehouseMarketDataProvider(conn)
        scheme_id = SchemeId(args.scheme)

        full = md.nav_series(scheme_id, date.min, date.today(), adjusted=True)
        if len(full) < 2:
            print(f"{args.scheme}: {len(full)} NAV points — nothing to compute.")
            print("Load history first:")
            print(f"  python -m jobs.backfill_scheme_nav --scheme {args.scheme}")
            return

        as_of = date.fromisoformat(args.as_of) if args.as_of else full[-1].nav_date

        print(f"FUND X-RAY  {args.scheme}   as of {as_of}")
        print(
            f"  adjusted NAV, {len(full):,} points"
            f" from {full[0].nav_date} to {full[-1].nav_date}"
        )

        # A series that does not move is not a fund that made nothing. It is an
        # IDCW plan whose distributions are not on record: `scheme_idcw` is
        # empty, so `nav_adj` equals raw NAV and the entire return -- which was
        # paid out rather than accrued -- is invisible. 4,595 of the 15,006
        # schemes with NAV are IDCW options, so this is not a rare shape.
        # Printing 0.00% for them would be a confident wrong answer.
        if len({p.nav for p in full}) < 3:
            distinct = len({p.nav for p in full})
            plural = "" if distinct == 1 else "s"
            print()
            print(f"  NAV takes {distinct} distinct value{plural} across the whole")
            print("  series, so no return can be read from it.")
            print()
            print("  This is an IDCW plan whose distributions are not loaded:")
            print("  scheme_idcw is empty, so nav_adj == nav and the return that")
            print("  was paid out does not appear. Refusing to report 0.00%.")
            return

        print()
        print(f"  {'window':16} {'ann':>9} {'cumulative':>9} {'vol':>9} {'max dd':>9}"
              f"  {'obs':>5}  {'conf':<6} drawdown")
        print("  " + "-" * 86)

        for key in WINDOW_YEARS:
            navs = md.nav_series(
                scheme_id, window_start(as_of, key), as_of, adjusted=True
            )
            w = compute_return_window(navs, key)
            short = f"  {key:16} insufficient history ({len(navs)} points)"
            print(line(w) if w else short)

        whole = compute_return_window(
            [p for p in full if p.nav_date <= as_of], "since_first_nav"
        )
        if whole:
            print(line(whole))

        if whole and whole.drawdown.depth < 0:
            # The dates are what make the depth checkable against market
            # history; a bare percentage is not something a reader can verify.
            dd = whole.drawdown
            back = f"back {dd.recovery}" if dd.recovery else "not yet recovered"
            print()
            print(
                f"  worst fall {dd.peak} -> {dd.trough}"
                f" ({dd.duration_days}d), {back}"
            )

        filled = whole.interpolated_pct if whole else Decimal(0)
        if filled > 0:
            print(f"\n  {filled:.2f}% of NAV points are interpolated, not fetched —")
            print("  a filled series is a straight line, which understates volatility.")

        print("\n  no benchmark on record: alpha, beta, tracking error and capture are")
        print("  not computed. no risk-free series: Sharpe and Sortino are not computed.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
