"""`fund_growth`: "What would Rs 10,000 have become?". MODULE_6.md §8.2.

The picture a first-time reader understands without a glossary: one line for
the fund, one for its benchmark with dividends included, both starting at
Rs 10,000 on the same day. The period tabs refetch this panel alone.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from src.common.types import SchemeId
from src.m2_fund.paths import growth_path, price_history
from src.m2_fund.risk import confidence_from_obs
from src.m2_fund.windows import window_start
from src.m6_views.builder import Scope
from src.m6_views.builders.fund.common import (
    NO_FUND,
    FundQuality,
    points,
    rupees,
    tabs,
    thin,
    window_of,
)
from src.m6_views.compose import ok_envelope
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.format import format_date
from src.m6_views.registry import VIEW_DEFS, register
from src.m6_views.states import empty_envelope

VIEW_ID = "fund_growth"


@register
class FundGrowthBuilder:
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
        navs, levels, index_id = price_history(self.market, scheme, scope.as_of)
        key = window_of(params)
        start = (
            date.min if key == "max" or not navs
            else window_start(navs[-1].nav_date, key)
        )
        path = growth_path(navs, levels, start)
        if path is None:
            return empty_envelope(
                VIEW_ID, question, scope,
                f"Fewer than two prices are on record for {scheme} in this "
                f"period. python -m jobs.backfill_scheme_nav --scheme {scheme} "
                f"loads its history.",
            )

        facts = self.market.scheme_facts(scheme)
        name = facts.name if facts else str(scheme)
        bench = facts.benchmark_name if facts else None
        end, fund_end, bench_end = path.points[-1]
        headline = (
            f"₹10,000 put into {name} on {format_date(path.start)} was worth "
            f"{rupees(fund_end)} on {format_date(end)}"
        )
        headline += (
            f"; the same ₹10,000 in the {bench} index, dividends included, "
            f"was worth {rupees(bench_end)}."
            if bench and bench_end is not None
            else "."
        )

        caveats: list[str] = []
        if key != "max" and path.start > start:
            caveats.append(
                f"Prices on record begin on {format_date(path.start)}, after "
                f"this period's start, so both lines begin there."
            )
        if index_id is None:
            caveats.append(
                "No benchmark index is on record for this fund, so only the "
                "fund is drawn."
            )
        elif bench_end is None:
            caveats.append(
                f"Benchmark {index_id} is on record, but its total-return "
                f"series does not cover this period. python -m jobs.fetch_index "
                f"--held loads it."
            )

        kept = thin(path.points)
        series = [
            {"name": name, "role": "fund",
             "points": points([(d, f) for d, f, _ in kept], rupees)},
        ]
        if bench and any(b is not None for _, _, b in kept):
            series.append(
                {"name": f"{bench} (with dividends)", "role": "benchmark",
                 "points": points([(d, b) for d, _, b in kept], rupees)}
            )
        rows = [
            {"date": d, "fund_value_inr": f, "benchmark_value_inr": b}
            for d, f, b in kept
        ]
        return ok_envelope(
            view_id=VIEW_ID,
            scope=scope,
            payload={
                "headline": headline,
                "tabs": tabs(VIEW_ID, str(scheme), key),
                "charts": [
                    {"kind": "line", "title": question, "y": "inr", "series": series}
                ],
                "columns": [
                    {"key": "date", "label": "Date", "kind": "date"},
                    {"key": "fund_value_inr", "label": name, "kind": "inr"},
                    {"key": "benchmark_value_inr", "label": "Benchmark", "kind": "inr"},
                ],
                "rows": rows,
            },
            quality=FundQuality(
                (scope.as_of - end).days, confidence_from_obs((end - path.start).days)
            ),
            data_as_of=end,
            source_modules=["m2", "m0"],
            row_count=len(rows),
            params=params,
            extra_caveats=caveats,
        )


__all__ = ["FundGrowthBuilder"]
