"""A fund among the funds that do the same job. DECISIONS V1-77; MODULE_2 §11.

**Peers** are the live funds (`m0_data.universe`) in the same canonical category
(`config/categories.yaml`, V1-76) -- which already makes them Direct plan only,
as §11.2 rule 1 requires, and one share class per fund. For a window, only the
funds whose prices span it are ranked: four years of history is not a five-year
record, whatever the window is called.

**Ranks** use the figures `m2_fund.stats` stored for every fund, so a page reads
its category once instead of every peer's history. The spec's rules, and where
they were silent or wrong:
- each metric has a direction (§11.2 rule 4). Worst fall is stored as a negative
  depth, so the smaller fall is the *higher* number; the spec's `lower_better`
  would rank the deepest fall first. The expense ratio (V1-78) ranks the
  cheapest first, and is read from `scheme_ter` rather than the stored windows.
- ties share a rank, 1, 2, 2, 4 (the spec gave no tie rule);
- fewer than `MIN_PEERS` ranked funds is no rank at all, with the reason (the spec
  set no minimum);
- every rank carries how many were ranked of how many are in the category
  (§11.2 rule 3);
- wound-up funds are not included -- nothing records them yet -- so §11.2 rule 2
  (survivorship) is unmet, and each page says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from src.m0_data.categories import Category, category_of

#: Below this many ranked funds a rank says more about the gaps than the fund.
MIN_PEERS = 5
SURVIVORSHIP_CAVEAT = (
    "Ranks compare the funds open today; funds that closed or merged are not "
    "included, which tends to flatter the category's middle."
)


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    window: str
    field: str
    #: True when the higher stored number ranks first.
    higher_first: bool


METRICS = (
    Metric("return_1y", "Return, 1 year", "1y", "return_ann", True),
    Metric("return_3y", "Return, 3 years", "3y", "return_ann", True),
    Metric("return_5y", "Return, 5 years", "5y", "return_ann", True),
    Metric("volatility_3y", "Volatility, 3 years", "3y", "volatility_ann", False),
    # A negative depth: -0.12 is a smaller fall than -0.35, and ranks first.
    Metric("worst_fall_3y", "Worst fall, 3 years", "3y", "max_dd", True),
    Metric("sharpe_3y", "Return for the risk, 3 years (Sharpe)", "3y", "sharpe", True),
    # Not a window: the newest total TER on record, percent a year.
    Metric("ter", "Expense ratio (TER)", "", "ter", False),
)


@dataclass(frozen=True)
class Rank:
    metric: Metric
    value: Decimal | None
    rank: int | None
    ranked: int
    quartile: int | None
    #: Why there is no rank, when there is none.
    reason: str | None = None


@dataclass(frozen=True)
class Point:
    scheme_id: str
    name: str
    volatility: Decimal
    return_ann: Decimal


@dataclass(frozen=True)
class PeerContext:
    scheme_id: str
    category: Category
    in_category: int
    ranks: list[Rank]
    #: Every peer with three years of prices: the category's risk and return.
    points: list[Point]


def competition_rank(value: Decimal, others: list[Decimal], higher_first: bool) -> int:
    """1 + how many are strictly better: ties share a rank (1, 2, 2, 4)."""
    better = sum(1 for o in others if (o > value if higher_first else o < value))
    return better + 1


def quartile(rank: int, ranked: int) -> int:
    """1 for the first quarter of the ranked funds, 4 for the last."""
    return (rank * 4 + ranked - 1) // ranked


def peer_context(market: Any, scheme_id: str) -> PeerContext | None:
    """This fund's category, its ranks in it, and its category's points.

    None when the fund is not a live fund with a Direct plan: there is no peer
    group to place a Regular-only or closed fund in.
    """
    funds = market.live_funds()
    fund = next((f for f in funds if f.scheme_id == scheme_id), None)
    if fund is None:
        return None
    category = category_of(fund.category)
    members = {
        f.scheme_id: f for f in funds if category_of(f.category).key == category.key
    }
    stats = {(s.scheme_id, s.window_key): s
             for s in market.window_stats(sorted(members))}
    ters = {sid: t.total for sid, t in market.ters(sorted(members), date.max).items()}

    ranks: list[Rank] = []
    for metric in METRICS:
        values = ters if metric.field == "ter" else {
            sid: getattr(stat, metric.field)
            for sid in members
            if (stat := stats.get((sid, metric.window))) is not None
            and stat.spans and getattr(stat, metric.field) is not None
        }
        own = values.get(scheme_id)
        if own is None:
            ranks.append(Rank(metric, None, None, len(values), None,
                              "no expense ratio on record" if metric.field == "ter"
                              else "too little price history for the period"))
        elif len(values) < MIN_PEERS:
            ranks.append(Rank(metric, own, None, len(values), None,
                              f"only {len(values)} of its category's funds"
                              f" {'has' if len(values) == 1 else 'have'} it"))
        else:
            others = [v for sid, v in values.items() if sid != scheme_id]
            position = competition_rank(own, others, metric.higher_first)
            ranks.append(Rank(metric, own, position, len(values),
                              quartile(position, len(values))))

    points = [
        Point(sid, members[sid].name, vol, ret)
        for sid in sorted(members)
        if (stat := stats.get((sid, "3y"))) is not None and stat.spans
        and (vol := stat.volatility_ann) is not None
        and (ret := stat.return_ann) is not None
    ]
    return PeerContext(scheme_id, category, len(members), ranks, points)


__all__ = ["METRICS", "MIN_PEERS", "SURVIVORSHIP_CAVEAT", "Metric", "PeerContext",
           "Point", "Rank", "competition_rank", "peer_context", "quartile"]
