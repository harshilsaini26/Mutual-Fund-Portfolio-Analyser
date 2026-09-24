"""`fund_header`: "What is this fund, and how has it done?". MODULE_6.md §8.2.

The top of the fund page: the fund's name in words rather than an ISIN, the
facts a reader looks for first, and three plain findings, each a sentence and
each descriptive (§2.6): against its benchmark, how steady, and the worst fall.
A tone and a symbol travel with each finding, never colour alone (§10.3).
"""

from __future__ import annotations

from typing import Any

from src.common.types import SchemeId
from src.m2_fund.paths import price_history, rolling_path
from src.m2_fund.windows import NothingToCompute, fund_windows
from src.m6_views.builder import Scope
from src.m6_views.builders.fund.common import NO_FUND, FundQuality
from src.m6_views.compose import ok_envelope
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.format import (
    format_date,
    format_fraction,
    format_inr,
    format_pct,
    format_return,
)
from src.m6_views.registry import VIEW_DEFS, register
from src.m6_views.states import empty_envelope

VIEW_ID = "fund_header"
YEAR = 365
THREE_YEARS = 1095
PERIODS = {"5y": "five years", "3y": "three years", "1y": "one year"}


def _finding(title: str, text: str, tone: str) -> dict[str, str]:
    symbol = {"ahead": "▲", "behind": "▼"}.get(tone, "●")
    return {"title": title, "text": text, "tone": tone, "symbol": symbol}


@register
class FundHeaderBuilder:
    view_id = VIEW_ID

    def __init__(self, deps: Deps) -> None:
        self.market = deps.market

    def build(self, scope: Scope, params: dict[str, Any]) -> ViewEnvelope:
        question = VIEW_DEFS[VIEW_ID].question
        if not scope.scope_id:
            return empty_envelope(VIEW_ID, question, scope, NO_FUND)
        scheme = SchemeId(scope.scope_id)
        facts = self.market.scheme_facts(scheme)
        if facts is None:
            return empty_envelope(
                VIEW_ID, question, scope,
                f"No fund with the ISIN {scheme} is in AMFI's list on record.",
            )

        findings: list[dict[str, str]] = []
        first = last = None
        staleness, confidence = 0, "low"
        try:
            fw = fund_windows(self.market, scheme, scope.as_of)
        except NothingToCompute:
            fw = None
        if fw is not None:
            first, last = fw.navs[0].nav_date, fw.navs[-1].nav_date
            staleness, confidence = fw.staleness_days, fw.confidence
            compared = next(
                ((k, w) for k in PERIODS
                 if (w := fw.windows.get(k)) is not None
                 and w.bench_return_ann is not None and w.obs_days >= YEAR),
                None,
            )
            if compared is not None and compared[1].bench_return_ann is not None:
                key, w = compared
                ahead = w.return_ann > compared[1].bench_return_ann
                findings.append(_finding(
                    f"Over {PERIODS[key]}",
                    f"{format_return(w.return_ann, False)} a year, against "
                    f"{format_return(w.bench_return_ann, False)} for its benchmark",
                    "ahead" if ahead else "behind",
                ))
            navs, levels, _ = price_history(self.market, scheme, scope.as_of)
            rp = rolling_path(navs, levels, THREE_YEARS)
            if rp is not None and rp.pct_ahead is not None:
                findings.append(_finding(
                    "How steady",
                    f"Ahead of its benchmark in {format_pct(rp.pct_ahead, precision=0)} "
                    f"of all three-year stretches",
                    "ahead" if rp.pct_ahead >= 50 else "behind",
                ))
            # A "worst fall" over three weeks of prices is not a finding: the
            # same year of history the benchmark finding asks for.
            longest = fw.windows.get("since_first_nav")
            if (
                longest is not None
                and longest.obs_days >= YEAR
                and longest.drawdown.depth < 0
            ):
                dd = longest.drawdown
                recovered = (
                    f"; back at its high {dd.recovery_days:,} days after the low"
                    if dd.recovery_days is not None
                    else "; not yet back at that high"
                )
                findings.append(_finding(
                    "Worst fall",
                    f"{format_fraction(dd.depth)}, from {format_date(dd.peak)} to "
                    f"{format_date(dd.trough)}{recovered}",
                    "neutral",
                ))

        plan = " · ".join(p.title() for p in (facts.plan, facts.option) if p)
        size = (
            f"{format_inr(facts.aum_inr, precision=0)}, the average for the "
            f"quarter to {format_date(facts.aum_as_of)}"
            if facts.aum_inr is not None
            else None
        )
        rows = [
            {"label": label, "value": value}
            for label, value in (
                ("Fund house", facts.amc_name),
                ("Category", facts.category),
                ("Plan", plan or None),
                ("Launched", format_date(facts.inception) if facts.inception else None),
                ("Fund size", size),
                ("Benchmark", f"{facts.benchmark_name}, with dividends"
                 if facts.benchmark_name else "None on record"),
                ("Prices on record",
                 f"{format_date(first)} to {format_date(last)}" if first else "None"),
            )
            if value
        ]
        return ok_envelope(
            view_id=VIEW_ID,
            scope=scope,
            payload={
                "name": facts.name,
                "subtitle": " · ".join(
                    x for x in (facts.amc_name, facts.category, plan) if x
                ),
                "scheme_id": str(scheme),
                "findings": findings,
                "columns": [
                    {"key": "label", "label": "Fact", "kind": "text"},
                    {"key": "value", "label": "Value", "kind": "text"},
                ],
                "rows": rows
                + [{"label": f["title"], "value": f["text"]} for f in findings],
                "facts": rows,
            },
            quality=FundQuality(staleness, confidence),
            data_as_of=last or scope.as_of,
            source_modules=["m2", "m0"],
            row_count=len(rows),
            params=params,
        )


__all__ = ["FundHeaderBuilder"]
