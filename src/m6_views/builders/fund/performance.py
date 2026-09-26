"""`fund_performance`: "How has it done against its benchmark, for the risk taken?".

The fund page's Performance section (DECISIONS V1-81, after Fundoo's): each
measure down the side, each period across, and one line on what the measure
says. Every figure is M2's `fund_windows`, the same the "Every figure" table
and `scripts/show_fund_xray.py` read, so the three cannot disagree.

Average maturity and yield to maturity are not here: fund houses publish them
in monthly factsheets, which no source this site loads carries. A debt fund's
page says so rather than leaving the reader to wonder.
"""

from __future__ import annotations

from typing import Any

from src.common.types import SchemeId
from src.m0_data.categories import category_of
from src.m2_fund.windows import NothingToCompute, ReturnWindow, fund_windows, spans
from src.m6_views.builder import Scope
from src.m6_views.builders.fund.common import (
    INDEX_PROXY,
    INDEX_WITHHELD,
    NO_FUND,
    FundQuality,
    withholds_index,
)
from src.m6_views.compose import ok_envelope
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.format import format_date, format_fraction, format_return
from src.m6_views.registry import VIEW_DEFS, register
from src.m6_views.states import empty_envelope

VIEW_ID = "fund_performance"
PERIODS = {"1y": "1 year", "3y": "3 years", "5y": "5 years"}

#: (row, label, kind, what it says). `kind` formats the row's cells.
MEASURES = (
    ("return_ann", "Return a year", "fraction",
     "Annualised, with dividends reinvested."),
    ("bench_return_ann", "Benchmark a year", "fraction",
     "The benchmark over the same days, dividends included."),
    ("lead", "Ahead of benchmark", "fraction",
     "The fund's return a year less the benchmark's."),
    ("volatility_ann", "Volatility", "fraction",
     "How widely its daily price swings, scaled to a year."),
    ("max_dd", "Deepest fall", "fraction",
     "The largest fall from a high within the period."),
    ("sharpe", "Sharpe ratio", "ratio",
     "Return above cash for each unit of volatility."),
    ("sortino", "Sortino ratio", "ratio",
     "Return above cash for each unit of downward swings only."),
    ("treynor", "Treynor ratio", "fraction",
     "Return above cash for each unit of beta."),
    ("alpha_ann", "Alpha a year", "fraction",
     "Return beyond what its beta to the benchmark accounts for."),
    ("beta", "Beta", "ratio",
     "How far it moved for each 1% the benchmark moved."),
    ("tracking_error", "Tracking error", "fraction",
     "How far its daily returns strayed from the benchmark's, a year."),
    ("information_ratio", "Information ratio", "ratio",
     "Its lead over the benchmark for each unit of tracking error."),
    ("up_capture", "Up capture", "fraction",
     "Its gain on the days the benchmark rose, as a share of the benchmark's."),
    ("down_capture", "Down capture", "fraction",
     "Its loss on the days the benchmark fell, as a share of the benchmark's."),
)

NO_YIELD = (
    "Average maturity and yield to maturity are not shown: fund houses publish "
    "them in their monthly factsheets, which this site does not load yet."
)


def _value(w: ReturnWindow, key: str) -> Any:
    if key == "max_dd":
        return w.drawdown.depth
    if key == "lead":
        bench = w.bench_return_ann
        return w.return_ann - bench if bench is not None else None
    return getattr(w, key)


@register
class FundPerformanceBuilder:
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

        # A fixed window only when the prices span it, as in `fund_returns`.
        shown = {k: w for k, w in fw.windows.items()
                 if k in PERIODS and w is not None and spans(w.obs_days, k)}
        if not shown:
            return empty_envelope(
                VIEW_ID, question, scope,
                f"Prices for {scheme} begin on {format_date(fw.navs[0].nav_date)}: "
                f"less than a year, so no yearly measure is shown yet.",
            )
        rows = [
            {"measure": label, "kind": kind, "meaning": meaning,
             **{k: _value(w, key) for k, w in shown.items()}}
            for key, label, kind, meaning in MEASURES
        ]

        facts = self.market.scheme_facts(scheme)
        bench = facts.benchmark_name if facts else None
        lead_key = next(k for k in ("5y", "3y", "1y") if k in shown)
        w = shown[lead_key]
        headline = (
            f"Over {PERIODS[lead_key]} it returned {format_return(w.return_ann, False)}"
            f" a year and fell at most {format_fraction(w.drawdown.depth)}"
        )
        if bench and w.bench_return_ann is not None:
            headline += (
                f"; {bench} returned {format_return(w.bench_return_ann, False)}"
                f" a year over the same days"
            )
            if w.beta is not None:
                headline += f", with a beta of {w.beta:.2f}"
        headline += "."

        caveats: list[str] = []
        if withholds_index(self.market):
            caveats.append(INDEX_PROXY if fw.benchmark_id else INDEX_WITHHELD)
        elif fw.benchmark_id is None:
            caveats.append(
                "No benchmark index is on record for this fund, so the rows "
                "that compare it with one are empty."
            )
        category = category_of(facts.category) if facts and facts.category else None
        if category and "debt" in (category.family, category.key.split("/")[-1]):
            caveats.append(NO_YIELD)

        rf = w.risk_free_pct
        return ok_envelope(
            view_id=VIEW_ID,
            scope=scope,
            payload={
                "headline": headline,
                "definition": (
                    "Cash is the risk-free rate in force when each period began"
                    + (f" ({rf}% a year for {PERIODS[lead_key]})." if rf is not None
                       else ".")
                ),
                "columns": [
                    {"key": "measure", "label": "Measure", "kind": "text"},
                    *({"key": k, "label": PERIODS[k], "kind": "row"} for k in shown),
                    {"key": "meaning", "label": "What it says", "kind": "text"},
                ],
                "rows": rows,
            },
            quality=FundQuality(fw.staleness_days, fw.confidence),
            data_as_of=fw.navs[-1].nav_date,
            source_modules=["m2", "m0"],
            row_count=len(rows),
            params=params,
            extra_caveats=caveats,
        )


__all__ = ["FundPerformanceBuilder"]
