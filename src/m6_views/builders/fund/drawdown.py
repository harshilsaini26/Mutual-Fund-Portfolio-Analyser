"""`fund_drawdown`: "How far has it fallen, and how long did it take to recover?".

MODULE_6.md §8.4's drawdown curve, for one fund: each day, how far below its
highest price so far it stood, with its benchmark's line beside it for the same
days. The deepest point is marked, and the headline says whether and when the
fund climbed back.
"""

from __future__ import annotations

from typing import Any

from src.common.contracts.market import NavPoint
from src.common.types import SchemeId
from src.m2_fund.paths import drawdown_path, price_history
from src.m2_fund.risk import confidence_from_obs, max_drawdown
from src.m6_views.builder import Scope
from src.m6_views.builders.fund.common import NO_FUND, FundQuality, points, thin
from src.m6_views.compose import ok_envelope
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.format import format_date, format_fraction
from src.m6_views.registry import VIEW_DEFS, register
from src.m6_views.states import empty_envelope

VIEW_ID = "fund_drawdown"


def _below(v: Any) -> str:
    return f"{format_fraction(v)} below its high"


@register
class FundDrawdownBuilder:
    view_id = VIEW_ID

    def __init__(self, deps: Deps) -> None:
        self.market = deps.market

    def required_sources(self) -> list[str]:
        return VIEW_DEFS[VIEW_ID].requires_fields

    def build(self, scope: Scope, params: dict[str, Any]) -> ViewEnvelope:
        question = VIEW_DEFS[VIEW_ID].question
        if not scope.scope_id:
            return empty_envelope(VIEW_ID, question, scope, NO_FUND)
        scheme = SchemeId(scope.scope_id)
        navs, levels, _ = price_history(self.market, scheme, scope.as_of)
        if len(navs) < 2:
            return empty_envelope(
                VIEW_ID, question, scope,
                f"Fewer than two prices are on record for {scheme}. "
                f"python -m jobs.backfill_scheme_nav --scheme {scheme} loads them.",
            )
        fund = drawdown_path(navs)
        worst = max_drawdown(navs)
        index = dict(drawdown_path(
            [NavPoint(scheme, d, level, False) for d, level in sorted(levels.items())]
        ))
        kept = thin([(d, v, index.get(d)) for d, v in fund])

        facts = self.market.scheme_facts(scheme)
        name = facts.name if facts else str(scheme)
        bench = facts.benchmark_name if facts else None
        if worst.depth < 0:
            headline = (
                f"Its deepest fall was {format_fraction(worst.depth)}, from "
                f"{format_date(worst.peak)} to {format_date(worst.trough)}."
            )
            headline += (
                f" It was back at that high on {format_date(worst.recovery)}, "
                f"{worst.recovery_days:,} days after the low."
                if worst.recovery and worst.recovery_days is not None
                else " It has not been back at that high since."
            )
        else:
            headline = "Its price has not fallen below an earlier high in this record."

        series = [{"name": name, "role": "fund",
                   "points": points([(d, f) for d, f, _ in kept], _below)}]
        if bench and any(b is not None for _, _, b in kept):
            series.append({"name": f"{bench} (with dividends)", "role": "benchmark",
                           "points": points([(d, b) for d, _, b in kept], _below)})
        rows = [
            {"date": d, "fund_below_high": f, "benchmark_below_high": b}
            for d, f, b in kept
        ]
        end = navs[-1].nav_date
        return ok_envelope(
            view_id=VIEW_ID,
            scope=scope,
            payload={
                "headline": headline,
                "charts": [{
                    "kind": "area", "title": question, "y": "fraction",
                    "series": series,
                    "marks": [{
                        "at": worst.trough.isoformat(), "value": str(worst.depth),
                        "label": f"Deepest: {format_fraction(worst.depth)} on "
                                 f"{format_date(worst.trough)}",
                    }] if worst.depth < 0 else [],
                }],
                "columns": [
                    {"key": "date", "label": "Date", "kind": "date"},
                    {"key": "fund_below_high", "label": "Fund below its high",
                     "kind": "fraction"},
                    {"key": "benchmark_below_high", "label": "Benchmark below its high",
                     "kind": "fraction"},
                ],
                "rows": rows,
            },
            quality=FundQuality(
                (scope.as_of - end).days,
                confidence_from_obs((end - navs[0].nav_date).days),
            ),
            data_as_of=end,
            source_modules=["m2", "m0"],
            row_count=len(rows),
            params=params,
        )


__all__ = ["FundDrawdownBuilder"]
