"""Print a fund's return windows and risk statistics. Not M6 — a terminal report.

    python -m scripts.show_fund_xray --scheme INF179K01UT0
    python -m scripts.show_fund_xray --scheme INF179K01UT0 --as-of 2026-03-31

The windows come from M2's `fund_windows`, the same call the fund page makes,
so the terminal and the browser cannot disagree. This script only prints, and
contains no SQL of its own (invariant 3).

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
from datetime import date
from decimal import Decimal

from src.common.decimals import connect
from src.common.types import SchemeId
from src.m0_data.config import warehouse_path
from src.m0_data.providers.warehouse import WarehouseMarketDataProvider
from src.m2_fund.windows import (
    NothingToCompute,
    ReturnWindow,
    fund_windows,
    rolling_returns,
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
    # Per row, not once for the series: a fund backfilled densely for years
    # and interpolated through the last twelve months reports ~2% overall
    # while its 1y row is mostly filled, and that row's volatility is the
    # understated one.
    filled = " *" if w.interpolated_pct > 0 else ""
    return (
        f"  {w.window_key:16} {pct(w.return_ann)} {pct(w.return_cum)}"
        f" {pct(w.volatility_ann)} {pct(dd.depth)}"
        f"  {w.obs_count:>5}  {w.confidence:<6} {recovered}{filled}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scheme", required=True, help="scheme_id (an ISIN)")
    ap.add_argument("--as-of", help="YYYY-MM-DD; defaults to the latest NAV on record")
    args = ap.parse_args()

    conn = connect(warehouse_path())
    try:
        md = WarehouseMarketDataProvider(conn)
        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
        try:
            fw = fund_windows(md, SchemeId(args.scheme), as_of)
        except NothingToCompute as e:
            print(f"FUND X-RAY  {args.scheme}   as of {as_of}")
            print(f"  {e}")
            return
        upto, index_id = fw.navs, fw.benchmark_id

        print(f"FUND X-RAY  {args.scheme}   as of {upto[-1].nav_date}")
        print(
            f"  adjusted NAV, {len(upto):,} points"
            f" from {upto[0].nav_date} to {upto[-1].nav_date}"
        )
        print()
        print(f"  {'window':16} {'ann':>9} {'cumulative':>9} {'vol':>9} {'max dd':>9}"
              f"  {'obs':>5}  {'conf':<6} drawdown")
        print("  " + "-" * 86)

        for key, w in fw.windows.items():
            print(line(w) if w else f"  {key:16} insufficient history")
        shown = [w for w in fw.windows.values() if w]
        whole = fw.windows["since_first_nav"]

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

        rolls = [
            r for y in (1, 3, 5)
            if (r := rolling_returns(upto, 365 * y)) is not None
        ]
        if rolls:
            print()
            print(f"  {'rolling':16} {'worst':>9} {'median':>9} {'best':>9}"
                  f"  {'windows':>7}  positive")
            print("  " + "-" * 62)
            for r in rolls:
                print(
                    f"  {r.horizon_days // 365}y{'':14}{pct(r.worst)} {pct(r.median)}"
                    f" {pct(r.best)}  {r.windows:>7}  {r.pct_positive:>6.1f}%"
                )

        if any(w.interpolated_pct > 0 for w in shown):
            print()
            print("  * some NAV points in that row were interpolated, not")
            print("  fetched. A filled series is a straight line, which has")
            print("  no variance, so its volatility reads low.")

        rated = [w for w in shown if w.sharpe is not None]
        if rated:
            print()
            print(f"  {'risk-adjusted':16} {'sharpe':>9} {'sortino':>9}  {'rf':>6}")
            print("  " + "-" * 46)
            for w in rated:
                so = f'{w.sortino:>9.2f}' if w.sortino is not None else f'{chr(45):>9}'
                print(
                    f"  {w.window_key:16} {w.sharpe:>9.2f} {so}"
                    f"  {w.risk_free_pct:>5.2f}%"
                )

        versus = [w for w in shown if w.beta is not None]
        if versus:
            print()
            print(f"  vs {str(index_id)[:28]:28} {'bench':>8} {'beta':>6}"
                  f" {'t.err':>7} {'alpha':>7} {'up':>6} {'down':>6}")
            print("  " + "-" * 74)
            for w in versus:
                dash7, dash6 = f"{chr(45):>7}", f"{chr(45):>6}"
                al = (f"{w.alpha_ann * 100:>6.2f}%"
                      if w.alpha_ann is not None else dash7)
                up = f"{w.up_capture:>6.2f}" if w.up_capture is not None else dash6
                dn = f"{w.down_capture:>6.2f}" if w.down_capture is not None else dash6
                te = (f"{w.tracking_error * 100:>6.2f}%"
                      if w.tracking_error is not None else dash7)
                bm = (f"{w.bench_return_ann * 100:>7.2f}%"
                      if w.bench_return_ann is not None else f"{chr(45):>8}")
                print(f"  {w.window_key:31} {bm} {w.beta:>6.2f} {te} {al} {up} {dn}")
        elif index_id:
            print()
            print(f"  benchmark {index_id} is on record, but too little of its")
            print("  series overlaps these windows to compare against --")
            print("  python -m jobs.fetch_index --held")
        else:
            print()
            print("  no benchmark on record for this scheme: alpha, beta, tracking")
            print("  error and capture are not computed. 1,573 of 19,598 schemes")
            print("  carry one; an active fund's name does not name its index.")
        if not rated:
            print("  config/risk_free.yaml carries no rate for these windows.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
