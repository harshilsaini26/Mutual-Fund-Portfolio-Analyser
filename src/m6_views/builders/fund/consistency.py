"""`fund_consistency`: "Was the return steady, or a few good years?".

MODULE_6.md §8.2's `rolling_distribution`, drawn as a line a reader can follow:
the return of every three-year stretch (one year where the history is shorter),
by the day it ended, beside the benchmark's for the same stretch. MODULE_2.md
§4.1: a single point-to-point return is an artefact of two dates; the spread
of all of them is the honest version.
"""

from __future__ import annotations

from typing import Any

from src.common.types import SchemeId
from src.m2_fund.paths import price_history, rolling_path
from src.m6_views.builder import Scope
from src.m6_views.builders.fund.common import NO_FUND, FundQuality, points, thin
from src.m6_views.compose import ok_envelope
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.format import format_date, format_pct, format_return
from src.m6_views.registry import VIEW_DEFS, register
from src.m6_views.states import empty_envelope

VIEW_ID = "fund_consistency"
#: Three years where the history allows, else one: (days, words).
HORIZONS = ((1095, "three-year"), (365, "one-year"))


def _a_year(v: Any) -> str:
    return format_return(v, annualised=True)


@register
class FundConsistencyBuilder:
    view_id = VIEW_ID

    def __init__(self, deps: Deps) -> None:
        self.market = deps.market

    def build(self, scope: Scope, params: dict[str, Any]) -> ViewEnvelope:
        question = VIEW_DEFS[VIEW_ID].question
        if not scope.scope_id:
            return empty_envelope(VIEW_ID, question, scope, NO_FUND)
        scheme = SchemeId(scope.scope_id)
        navs, levels, _ = price_history(self.market, scheme, scope.as_of)
        found = next(
            ((rp, words) for days, words in HORIZONS
             if (rp := rolling_path(navs, levels, days)) is not None),
            None,
        )
        if found is None:
            return empty_envelope(
                VIEW_ID, question, scope,
                "Too little price history to compare stretches of time: this "
                "needs at least a year and a quarter of prices on record.",
            )
        rp, words = found
        facts = self.market.scheme_facts(scheme)
        name = facts.name if facts else str(scheme)
        bench = facts.benchmark_name if facts else None

        first, last = rp.points[0][0], rp.points[-1][0]
        headline = (
            f"Across every {words} stretch ending between {format_date(first)} "
            f"and {format_date(last)}, it returned between "
            f"{format_return(rp.worst, False)} and {format_return(rp.best, False)} "
            f"a year; the middle value was {format_return(rp.median, False)}."
        )
        if rp.pct_ahead is not None:
            headline += (
                f" It was ahead of its benchmark in "
                f"{format_pct(rp.pct_ahead, precision=0)} of those stretches."
            )

        kept = thin(rp.points)
        series = [{"name": name, "role": "fund",
                   "points": points([(d, f) for d, f, _ in kept], _a_year)}]
        if bench and any(b is not None for _, _, b in kept):
            series.append({"name": f"{bench} (with dividends)", "role": "benchmark",
                           "points": points([(d, b) for d, _, b in kept], _a_year)})
        rows = [
            {"stretch_ending": d, "fund_return": f, "benchmark_return": b}
            for d, f, b in kept
        ]
        return ok_envelope(
            view_id=VIEW_ID,
            scope=scope,
            payload={
                "headline": headline,
                "charts": [{
                    "kind": "line", "title": question, "y": "fraction",
                    "zero_line": True, "series": series,
                }],
                "columns": [
                    {"key": "stretch_ending", "label": "Stretch ending", "kind": "date"},
                    {"key": "fund_return", "label": "Fund, a year", "kind": "fraction"},
                    {"key": "benchmark_return", "label": "Benchmark, a year",
                     "kind": "fraction"},
                ],
                "rows": rows,
            },
            quality=FundQuality(
                (scope.as_of - navs[-1].nav_date).days,
                "high" if words == "three-year" else "medium",
            ),
            data_as_of=navs[-1].nav_date,
            source_modules=["m2", "m0"],
            row_count=len(rows),
            params=params,
        )


__all__ = ["FundConsistencyBuilder"]
