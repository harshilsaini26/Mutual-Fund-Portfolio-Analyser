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
    MIN_DISTINCT_NAVS,
    WINDOW_YEARS,
    ReturnWindow,
    compute_return_window,
    rolling_returns,
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


def benchmark_for(conn: sqlite3.Connection, scheme_id: str) -> tuple[str, str, list]:
    """`(index_id, index_name, levels)` for a scheme, or `("", "", [])`.

    Read here rather than through `WarehouseMarketDataProvider` because that
    provider's `index_levels` belongs to S11/S12's own slice; this script is a
    terminal report and reads what it needs. The SQL is the one exception this
    file already makes for the warehouse it opens.
    """
    row = conn.execute(
        "SELECT b.index_id, b.index_name FROM scheme s"
        " JOIN benchmark_index b ON b.index_id = s.benchmark_id"
        " WHERE s.scheme_id = ?",
        (scheme_id,),
    ).fetchone()
    if row is None:
        return "", "", []
    levels = conn.execute(
        "SELECT level_date, level FROM index_level"
        " WHERE index_id = ? ORDER BY level_date",
        (row[0],),
    ).fetchall()
    return str(row[0]), str(row[1]), [(r[0], r[1]) for r in levels]


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
        index_id, index_name, levels = benchmark_for(conn, str(scheme_id))

        full = md.nav_series(scheme_id, date.min, date.today(), adjusted=True)
        if len(full) < 2:
            print(f"{args.scheme}: {len(full)} NAV points — nothing to compute.")
            print("Load history first:")
            print(f"  python -m jobs.backfill_scheme_nav --scheme {args.scheme}")
            return

        as_of = date.fromisoformat(args.as_of) if args.as_of else full[-1].nav_date
        # One slice, used by every figure below. Rolling returns once took
        # `full`, so --as-of moved the table and not the distribution.
        upto = [p for p in full if p.nav_date <= as_of]

        print(f"FUND X-RAY  {args.scheme}   as of {as_of}")

        # Before the IDCW guard, because an as-of that predates the series
        # leaves `upto` empty and that is not an IDCW plan -- it is a date
        # with no data behind it. The guard below would otherwise diagnose a
        # Growth fund as one whose distributions are unloaded.
        if len(upto) < 2:
            print(f"  {len(upto)} NAV points on or before {as_of}, of"
                  f" {len(full):,} from {full[0].nav_date} to {full[-1].nav_date}.")
            print("  Nothing to compute: pick a later --as-of.")
            return

        print(
            f"  adjusted NAV, {len(upto):,} points"
            f" from {upto[0].nav_date} to {upto[-1].nav_date}"
        )

        # A series that does not move is not a fund that made nothing. It is an
        # IDCW plan whose distributions are not on record: `scheme_idcw` is
        # empty, so `nav_adj` equals raw NAV and the entire return -- which was
        # paid out rather than accrued -- is invisible. 4,595 of the 15,006
        # schemes with NAV are IDCW options, so this is not a rare shape.
        # Printing 0.00% for them would be a confident wrong answer.
        if len({p.nav for p in upto}) < MIN_DISTINCT_NAVS:
            distinct = len({p.nav for p in upto})
            plural = "" if distinct == 1 else "s"
            print()
            print(f"  NAV takes {distinct} distinct value{plural} across the"
                  f" {len(upto):,} points to {as_of},")
            print("  so no return can be read from it.")
            print()
            print("  This is an IDCW plan whose distributions are not loaded:")
            print("  scheme_idcw is empty, so nav_adj == nav and the return that")
            print("  was paid out does not appear. Refusing to report 0.00%.")
            return

        print()
        print(f"  {'window':16} {'ann':>9} {'cumulative':>9} {'vol':>9} {'max dd':>9}"
              f"  {'obs':>5}  {'conf':<6} drawdown")
        print("  " + "-" * 86)

        shown: list[ReturnWindow] = []
        for key in WINDOW_YEARS:
            navs = md.nav_series(
                scheme_id, window_start(as_of, key), as_of, adjusted=True
            )
            w = compute_return_window(navs, key, levels, index_id)
            short = f"  {key:16} insufficient history ({len(navs)} points)"
            print(line(w) if w else short)
            if w:
                shown.append(w)

        whole = compute_return_window(upto, "since_first_nav", levels, index_id)
        if whole:
            print(line(whole))
            shown.append(whole)

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
            print(f"  vs {index_name[:28]:28} {'bench':>8} {'beta':>6}"
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
                print(
                    f"  {w.window_key:31} {w.bench_return_ann * 100:>7.2f}%"
                    f" {w.beta:>6.2f} {te} {al} {up} {dn}"
                )
        elif index_id:
            print()
            print(f"  benchmark {index_name} is on record but has no levels over")
            print("  these windows -- python -m jobs.fetch_index --held")
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
