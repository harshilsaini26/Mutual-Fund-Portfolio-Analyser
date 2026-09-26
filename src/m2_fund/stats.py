"""Every live fund's figures per window, computed once per build. DECISIONS V1-77.

`fund_windows` over each fund in `m0_data.universe.live_funds`, stored in
`fund_window_stat` so a peer rank or a category picture reads ~1,800 rows rather
than every peer's whole history on every page.

A derived table (invariant 10): the whole set is replaced, in one transaction,
on each rebuild. Only the fund's own figures are kept; a window whose prices do
not span it is stored with `spans = 0`, so nothing downstream can mistake four
years of history for five.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime

from src.common.types import SchemeId
from src.m0_data.providers.market_data import MarketDataProvider
from src.m0_data.providers.warehouse import WarehouseMarketDataProvider
from src.m0_data.universe import live_funds
from src.m2_fund.windows import (
    WINDOW_YEARS,
    NonPositiveLevel,
    NonPositiveNav,
    NothingToCompute,
    fund_windows,
    spans,
)


def rebuild_fund_stats(
    conn: sqlite3.Connection,
    as_of: date,
    market: MarketDataProvider | None = None,
    progress: Callable[[str], None] = lambda line: None,
) -> int:
    """Replace `fund_window_stat` with every live fund's windows. Returns rows.

    A fund whose prices cannot be used -- a zero or negative price on record,
    which `fund_windows` refuses rather than clamps (invariant 5) -- has no
    figures, and is reported through `progress` by name; the rest go on.
    """
    market = market or WarehouseMarketDataProvider(conn)
    now = datetime.now(UTC)
    rows: list[tuple[object, ...]] = []
    funds = live_funds(conn)
    for n, fund in enumerate(funds, start=1):
        try:
            fw = fund_windows(market, SchemeId(fund.scheme_id), as_of)
        except NothingToCompute:
            continue
        except (NonPositiveNav, NonPositiveLevel) as exc:
            progress(f"  ! {fund.scheme_id}: no figures, {exc}")
            continue
        end = fw.navs[-1].nav_date
        for key, w in fw.windows.items():
            if w is None:
                continue
            covered = key not in WINDOW_YEARS or spans(w.obs_days, key)
            rows.append((
                fund.scheme_id, key, end, w.obs_days, int(covered), w.return_ann,
                w.return_cum, w.volatility_ann, w.drawdown.depth, w.sharpe, now,
            ))
        if n % 250 == 0:
            progress(f"  {n:,} of {len(funds):,} funds")
    conn.execute("DELETE FROM fund_window_stat")
    conn.executemany(
        "INSERT INTO fund_window_stat (scheme_id, window_key, as_of, obs_days, spans,"
        " return_ann, return_cum, volatility_ann, max_dd, sharpe, computed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)


__all__ = ["rebuild_fund_stats"]
