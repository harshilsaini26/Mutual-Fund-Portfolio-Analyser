"""A fund among the funds that do the same job. `src/m2_fund/peers.py`, `stats.py`.

DECISIONS V1-77 fills MODULE_2 §11's gaps: ties share a rank, worst fall ranks
the smaller fall first, a window the prices do not span is not ranked, and a
category of fewer than five ranked funds gives no rank. Each is pinned here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from src.common.decimals import connect
from src.m0_data.providers.warehouse import (
    Ter,
    WarehouseMarketDataProvider,
    WindowStat,
)
from src.m0_data.universe import Fund
from src.m2_fund.peers import METRICS, competition_rank, peer_context, quartile
from src.m2_fund.stats import rebuild_fund_stats

from tests.conftest import migrated

FLEXI_OLD = "Equity Scheme - Flexi Cap Fund"
FLEXI_NEW = "Equity Schemes - Flexi Cap Fund"


def test_ties_share_a_rank() -> None:
    values = [Decimal("5"), Decimal("4"), Decimal("4"), Decimal("3")]
    ranks = [competition_rank(v, values[:i] + values[i + 1:], True)
             for i, v in enumerate(values)]
    assert ranks == [1, 2, 2, 4]


def test_lower_first_ranks_the_steadier_fund_first() -> None:
    steadier, others = Decimal("0.10"), [Decimal("0.20"), Decimal("0.15")]
    assert competition_rank(steadier, others, False) == 1


def test_the_smaller_fall_ranks_first() -> None:
    """Stored as a negative depth: the spec's `lower_better` would put the deepest
    fall at the top."""
    worst_fall = next(m for m in METRICS if m.key == "worst_fall_3y")
    smaller, deeper = Decimal("-0.10"), [Decimal("-0.30")]
    assert competition_rank(smaller, deeper, worst_fall.higher_first) == 1


def test_quartiles() -> None:
    assert [quartile(r, 8) for r in range(1, 9)] == [1, 1, 2, 2, 3, 3, 4, 4]
    assert quartile(1, 5) == 1 and quartile(5, 5) == 4


@dataclass
class _Market:
    funds: list[Fund]
    stats: list[WindowStat]

    def live_funds(self) -> list[Fund]:
        return self.funds

    def window_stats(self, scheme_ids: list[str]) -> list[WindowStat]:
        return [s for s in self.stats if s.scheme_id in scheme_ids]

    def ters(self, scheme_ids: list[str], on: date) -> dict[str, Ter]:
        return {sid: Ter(sid, on, Decimal(len(sid)) / 10, None)
                for sid in scheme_ids if sid.startswith("F")}


def _fund(sid: str, category: str) -> Fund:
    return Fund(sid, None, f"Fund {sid}", category, "direct", "growth", "amc")


def _stat(sid: str, window: str, ret: str, *, spans: bool = True) -> WindowStat:
    return WindowStat(sid, window, date(2026, 9, 23), 1100, spans, Decimal(ret),
                      Decimal("0.15"), Decimal("-0.2"), Decimal("1.0"))


def test_one_category_under_two_headings_is_one_peer_group() -> None:
    funds = [_fund(f"F{i}", FLEXI_OLD if i % 2 else FLEXI_NEW) for i in range(6)]
    funds.append(_fund("SMALL", "Equity Scheme - Small Cap Fund"))
    stats = [_stat(f.scheme_id, "3y", f"0.{10 + i}") for i, f in enumerate(funds)]
    found = peer_context(_Market(funds, stats), "F0")
    assert found is not None
    assert found.in_category == 6  # the small-cap fund is not a peer
    three_years = next(r for r in found.ranks if r.metric.key == "return_3y")
    assert (three_years.rank, three_years.ranked) == (6, 6)
    assert len(found.points) == 6


def test_a_window_the_prices_do_not_span_is_not_ranked() -> None:
    funds = [_fund(f"F{i}", FLEXI_NEW) for i in range(6)]
    stats = [_stat(f.scheme_id, "5y", "0.12", spans=f.scheme_id != "F0") for f in funds]
    found = peer_context(_Market(funds, stats), "F0")
    assert found is not None
    five = next(r for r in found.ranks if r.metric.key == "return_5y")
    assert five.rank is None and five.ranked == 5
    assert five.reason and "history" in five.reason


def test_too_few_peers_gives_no_rank_and_says_why() -> None:
    funds = [_fund(f"F{i}", FLEXI_NEW) for i in range(3)]
    stats = [_stat(f.scheme_id, "1y", "0.1") for f in funds]
    found = peer_context(_Market(funds, stats), "F0")
    assert found is not None
    one = next(r for r in found.ranks if r.metric.key == "return_1y")
    assert one.rank is None and one.reason and "only 3" in one.reason


def test_a_fund_that_is_not_live_has_no_peer_group() -> None:
    assert peer_context(_Market([_fund("F0", FLEXI_NEW)], []), "GONE") is None


def test_the_stored_figures_are_replaced_not_added_to(tmp_path: Path) -> None:
    db = tmp_path / "w.db"
    migrated(db)
    conn = connect(str(db))
    as_of = date(2026, 9, 23)
    conn.execute(
        "INSERT INTO scheme (scheme_id, amfi_code, scheme_name, plan, option, amc_id,"
        " sebi_category, status, last_seen) VALUES ('INF000000A01', '1', 'Fund A',"
        " 'direct', 'growth', 'amc', ?, 'active', ?)", (FLEXI_NEW, as_of),
    )
    day, nav = as_of - timedelta(days=800), Decimal("100")
    while day <= as_of:
        if day.weekday() < 5:
            nav += Decimal("0.05") if day.day % 3 else Decimal("-0.08")
            conn.execute(
                "INSERT INTO nav_daily (scheme_id, nav_date, nav, nav_adj)"
                " VALUES (?,?,?,?)", ("INF000000A01", day, nav, nav),
            )
        day += timedelta(days=1)
    conn.commit()

    first = rebuild_fund_stats(conn, as_of)
    again = rebuild_fund_stats(conn, as_of)
    stored = WarehouseMarketDataProvider(conn).window_stats(["INF000000A01"])
    conn.close()
    assert first == again == len(stored) > 0
    by_window = {s.window_key: s for s in stored}
    assert by_window["1y"].spans and not by_window["3y"].spans
    assert by_window["since_first_nav"].spans


def test_a_fund_with_an_unusable_price_is_reported_and_the_rest_go_on(
    tmp_path: Path,
) -> None:
    """Found by the first full build: one fund priced 0.000000 on one day stopped
    every fund's figures. `fund_windows` is right to refuse it (invariant 5); the
    rebuild is wrong to let one refusal cost the other 1,863 funds."""
    db = tmp_path / "w.db"
    migrated(db)
    conn = connect(str(db))
    as_of = date(2026, 9, 23)
    for sid, bad in (("INF000000A01", False), ("INF000000B01", True)):
        conn.execute(
            "INSERT INTO scheme (scheme_id, amfi_code, scheme_name, plan, option,"
            " amc_id, sebi_category, status, last_seen) VALUES (?, ?, ?, 'direct',"
            " 'growth', ?, ?, 'active', ?)", (sid, sid[-3:], sid, sid, FLEXI_NEW, as_of),
        )
        day, nav = as_of - timedelta(days=400), Decimal("100")
        while day <= as_of:
            nav += Decimal("0.05")
            price = Decimal("0") if bad and day == as_of - timedelta(days=100) else nav
            conn.execute("INSERT INTO nav_daily (scheme_id, nav_date, nav, nav_adj)"
                         " VALUES (?,?,?,?)", (sid, day, price, price))
            day += timedelta(days=1)
    conn.commit()

    said: list[str] = []
    rows = rebuild_fund_stats(conn, as_of, progress=said.append)
    stored = {s.scheme_id for s in
              WarehouseMarketDataProvider(conn).window_stats(["INF000000A01",
                                                              "INF000000B01"])}
    conn.close()
    assert rows > 0 and stored == {"INF000000A01"}
    assert any("INF000000B01" in line and "no figures" in line for line in said)
