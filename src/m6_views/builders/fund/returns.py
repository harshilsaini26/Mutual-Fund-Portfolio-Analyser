"""`fund_returns`: "How much has it returned, against its benchmark?". §8.2.

Paired bars per period, fund beside benchmark, from the same `fund_windows` the
detail table and `scripts/show_fund_xray.py` read, so the three cannot disagree.
A period shorter than a year shows its cumulative return and says so: an
annualised four-month figure multiplies the number in the reader's head.
"""

from __future__ import annotations

from typing import Any

from src.common.types import SchemeId
from src.m2_fund.windows import NothingToCompute, ReturnWindow, fund_windows
from src.m6_views.builder import Scope
from src.m6_views.builders.fund.common import NO_FUND, FundQuality
from src.m6_views.compose import ok_envelope
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.format import format_date, format_return
from src.m6_views.registry import VIEW_DEFS, register
from src.m6_views.states import empty_envelope

VIEW_ID = "fund_returns"
YEAR = 365
PERIODS = {"1y": "1 year", "3y": "3 years", "5y": "5 years"}


def _figures(w: ReturnWindow) -> tuple[Any, Any, str, bool]:
    """(fund, benchmark, what they are, annualised)."""
    if w.obs_days < YEAR:
        return w.return_cum, None, "in total", False
    return w.return_ann, w.bench_return_ann, "a year", True


@register
class FundReturnsBuilder:
    view_id = VIEW_ID

    def __init__(self, deps: Deps) -> None:
        self.market = deps.market

    def build(self, scope: Scope, params: dict[str, Any]) -> ViewEnvelope:
        question = VIEW_DEFS[VIEW_ID].question
        if not scope.scope_id:
            return empty_envelope(VIEW_ID, question, scope, NO_FUND)
        scheme = SchemeId(scope.scope_id)
        try:
            fw = fund_windows(self.market, scheme, scope.as_of)
        except NothingToCompute as e:
            return empty_envelope(VIEW_ID, question, scope, str(e))

        facts = self.market.scheme_facts(scheme)
        bench = facts.benchmark_name if facts else None
        since = f"Since {format_date(fw.navs[0].nav_date)}"
        labels = {**PERIODS, "since_first_nav": since}
        shown = [(k, w) for k, w in fw.windows.items() if w is not None]

        fund_values: list[list[str | None]] = []
        bench_values: list[list[str | None]] = []
        rows: list[dict[str, Any]] = []
        for key, w in shown:
            fund, benchmark, _, annualised = _figures(w)
            fund_values.append([str(fund), format_return(fund, annualised)])
            bench_values.append(
                [str(benchmark), format_return(benchmark, annualised)]
                if benchmark is not None else [None, "—"]
            )
            rows.append({
                "period": labels[key],
                "fund_return": fund,
                "benchmark_return": benchmark,
                "annualised": "yes" if annualised else "no, cumulative",
                "days": w.obs_days,
            })

        series: list[dict[str, Any]] = [
            {"name": facts.name if facts else str(scheme), "role": "fund",
             "values": fund_values}
        ]
        if bench and any(v[0] is not None for v in bench_values):
            series.append({"name": f"{bench} (with dividends)", "role": "benchmark",
                           "values": bench_values})

        lead = next(
            (
                (k, found)
                for k in ("5y", "3y", "1y")
                if (found := fw.windows.get(k)) is not None and found.obs_days >= YEAR
            ),
            shown[-1],
        )
        fund, benchmark, unit, _ = _figures(lead[1])
        headline = (
            f"Over {labels[lead[0]].lower()} it returned "
            f"{format_return(fund, False)} {unit}"
        )
        headline += (
            f"; the {bench} index, dividends included, returned "
            f"{format_return(benchmark, False)} {unit}."
            if bench and benchmark is not None
            else "."
        )

        return ok_envelope(
            view_id=VIEW_ID,
            scope=scope,
            payload={
                "headline": headline,
                "charts": [{
                    "kind": "bar", "title": question, "y": "fraction",
                    "categories": [labels[k] for k, _ in shown], "series": series,
                }],
                "columns": [
                    {"key": "period", "label": "Period", "kind": "text"},
                    {"key": "fund_return", "label": "Fund", "kind": "fraction"},
                    {"key": "benchmark_return", "label": "Benchmark", "kind": "fraction"},
                    {"key": "annualised", "label": "A year?", "kind": "text"},
                    {"key": "days", "label": "Days", "kind": "count"},
                ],
                "rows": rows,
            },
            quality=FundQuality(fw.staleness_days, fw.confidence),
            data_as_of=fw.navs[-1].nav_date,
            source_modules=["m2", "m0"],
            row_count=len(rows),
            params=params,
        )


__all__ = ["FundReturnsBuilder"]
