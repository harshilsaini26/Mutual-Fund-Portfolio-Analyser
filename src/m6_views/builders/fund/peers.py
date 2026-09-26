"""`fund_peers`: "How does it compare with funds that do the same job?".

DECISIONS V1-77. Where a fund's figures sit among the live funds of its
canonical category (`m2_fund.peers`): a sentence, a picture of the category's
risk against its return with this fund marked, and its rank on each measure.
Every figure was computed and ranked in M2; this file only words and arranges
them (§2.1). Descriptive throughout (§2.6): a rank says where a fund stood over
a period, not where it will stand.
"""

from __future__ import annotations

from typing import Any

from src.m2_fund.peers import SURVIVORSHIP_CAVEAT, Point, Rank, peer_context
from src.m6_views.builder import Scope
from src.m6_views.builders.fund.common import NO_FUND, FundQuality
from src.m6_views.compose import ok_envelope
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.format import (
    DASH,
    format_fraction,
    format_inr,
    format_ordinal,
    format_pct,
    format_return,
)
from src.m6_views.registry import VIEW_DEFS, register
from src.m6_views.states import empty_envelope

VIEW_ID = "fund_peers"
#: Counted from rank 1, and neutral: "top" would read as "most volatile" beside
#: a volatility rank, where rank 1 is the steadiest. `RANK_ORDER` says which end.
QUARTERS = {1: "first quarter", 2: "second quarter", 3: "third quarter",
            4: "fourth quarter"}
RANK_ORDER = (
    "Rank 1 is the highest return, the lowest volatility, the smallest fall, the "
    "highest return for the risk and the lowest expense ratio."
)
#: How the headline names a period, and the history that period needs.
PERIOD_WORDS = {"1y": ("one-year", "a year of prices"),
                "3y": ("three-year", "three years of prices")}
PERIOD_LABELS = {"1y": "1 year", "3y": "3 years", "5y": "5 years"}
#: Below this many ranked funds, a rank is shown with medium confidence.
SOLID_PEERS = 10


def rank_words(rank: Rank) -> str:
    """"7th of 34", or why there is no rank."""
    if rank.rank is None:
        return rank.reason or DASH
    return f"{format_ordinal(rank.rank)} of {rank.ranked}"


def figure_words(rank: Rank) -> str:
    if rank.value is None:
        return DASH
    if rank.metric.field == "return_ann":
        return format_return(rank.value, True)
    if rank.metric.field == "sharpe":
        return f"{rank.value:.2f}"
    if rank.metric.field == "ter":  # percent already (V1-78)
        return format_pct(rank.value, precision=2)
    return format_fraction(rank.value)


@register
class FundPeersBuilder:
    view_id = VIEW_ID

    def __init__(self, deps: Deps) -> None:
        self.market = deps.market

    def build(self, scope: Scope, params: dict[str, Any]) -> ViewEnvelope:
        question = VIEW_DEFS[VIEW_ID].question
        if not scope.scope_id:
            return empty_envelope(VIEW_ID, question, scope, NO_FUND)
        found = peer_context(self.market, scope.scope_id)
        if found is None:
            return empty_envelope(
                VIEW_ID, question, scope,
                "This fund has no peer group: only funds open today with a Direct "
                "plan are compared, one share class each.",
            )
        if not any(r.value is not None for r in found.ranks):
            return empty_envelope(
                VIEW_ID, question, scope,
                "Its figures are not computed yet: they come from the daily build, "
                "once the fund has a year of prices.",
            )

        category = found.category.name
        three = next(r for r in found.ranks if r.metric.key == "return_3y")
        one = next(r for r in found.ranks if r.metric.key == "return_1y")
        lead = three if three.rank is not None else one
        if lead.rank is not None:
            period, history = PERIOD_WORDS[lead.metric.window]
            headline = (
                f"Its {period} return, {figure_words(lead)}, ranks {rank_words(lead)} "
                f"funds in its category, {category}, with {history}: the "
                f"{QUARTERS[lead.quartile or 4]}."
            )
        else:
            headline = (
                f"{found.in_category} funds share its category, {category}; too "
                f"few have the history to rank it yet."
            )

        # The picture and the table under it are the same points: every fund in
        # the category with three years of prices, this one marked in words too.
        points = sorted(found.points, key=lambda p: p.return_ann, reverse=True)
        own = [p for p in points if p.scheme_id == found.scheme_id]
        charts: list[dict[str, Any]] = []
        if len(points) > len(own):

            # [volatility, return, name, caption, size]: the size sets a bubble's
            # area (V1-80, after Fundoo) and is said in the caption too (§10.3).
            def point(p: Point) -> list[str]:
                size = (f"; size {format_inr(p.aum, precision=0, compact=True)}"
                        if p.aum is not None else "")
                return [
                    str(p.volatility), str(p.return_ann), p.name,
                    f"{format_return(p.return_ann, True)}; volatility "
                    f"{format_fraction(p.volatility)}{size}",
                    "" if p.aum is None else str(p.aum),
                ]

            series = [{"name": "Other funds in its category", "role": "peer",
                       "points": [point(p) for p in points if p not in own]}]
            series += [{"name": p.name, "role": "fund", "mark": "This fund",
                        "points": [point(p)]} for p in own]
            charts.append({
                "kind": "scatter",
                "title": "Three years: return against volatility",
                "x": "fraction", "y": "fraction",
                "x_name": "Volatility, a year", "y_name": "Return, a year",
                "series": series,
            })
        rows = [
            {
                "fund": f"{p.name} (this fund)" if p in own else p.name,
                "return_3y": str(p.return_ann),
                "volatility_3y": str(p.volatility),
            }
            for p in points
        ]

        caveats = [RANK_ORDER, SURVIVORSHIP_CAVEAT]
        if charts and not own:
            caveats.insert(0, "This fund has less than three years of prices, so it "
                              "is not in the picture of its category.")
        if found.category.note:
            caveats.insert(0, found.category.note)
        latest = max(
            (s.as_of for s in self.market.window_stats([found.scheme_id])),
            default=scope.as_of,
        )
        return ok_envelope(
            view_id=VIEW_ID,
            scope=scope,
            payload={
                "headline": headline,
                "category": category,
                "in_category": found.in_category,
                # "Returns compared to peers" (V1-80, after Fundoo): each period's
                # lowest and highest in the category, and where this fund sits.
                # Positions along the bar are drawn in `render.range_bars`.
                "ranges": [
                    {
                        "label": PERIOD_LABELS[r.metric.window],
                        "low": str(r.low), "high": str(r.high), "value": str(r.value),
                        "low_label": figure_words(Rank(r.metric, r.low, None, 0, None)),
                        "high_label": figure_words(Rank(r.metric, r.high, None, 0, None)),
                        "value_label": figure_words(r),
                        "rank": rank_words(r),
                        "quarter": QUARTERS[r.quartile] if r.quartile else "",
                        "ranked": r.ranked,
                    }
                    for r in found.ranks
                    if r.metric.field == "return_ann" and r.rank is not None
                    and r.low is not None and r.high is not None
                ],
                "ranks": [
                    {
                        "label": r.metric.label,
                        "value": rank_words(r) if r.rank is not None else DASH,
                        "note": (f"{figure_words(r)}, {QUARTERS[r.quartile]}"
                                 if r.quartile else rank_words(r)),
                    }
                    for r in found.ranks
                ],
                "charts": charts,
                "columns": [
                    {"key": "fund", "label": "Fund", "kind": "text"},
                    {"key": "return_3y", "label": "Return, 3 years",
                     "kind": "return_ann"},
                    {"key": "volatility_3y", "label": "Volatility, 3 years",
                     "kind": "fraction"},
                ],
                "rows": rows,
                # For the front page's table.
                "rank_3y": {
                    "quartile": three.quartile,
                    "label": rank_words(three) if three.rank is not None else DASH,
                    "quarter": QUARTERS[three.quartile] if three.quartile else "",
                },
            },
            quality=FundQuality(
                (scope.as_of - latest).days,
                "high" if lead.ranked >= SOLID_PEERS and found.category.note is None
                else "medium",
                caveats,
            ),
            data_as_of=latest,
            source_modules=["m2", "m0"],
            row_count=len(rows),
            params=params,
        )


__all__ = ["FundPeersBuilder", "figure_words", "rank_words"]
