"""`fund_xray_header` — "How has this fund done, and against what?". §8.1.

The browser half of `scripts/show_fund_xray.py`. Both call M2's `fund_windows`,
so the page and the terminal cannot disagree; this file only lays the result
out, one row per window.

Coverage and unresolved share are look-through properties, not a fund's, so
they are None here — "not applicable", as the envelope defines it — rather than
a 100% that would claim something was checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.common.types import SchemeId
from src.m2_fund.windows import NothingToCompute, ReturnWindow, fund_windows
from src.m6_views.builder import Scope
from src.m6_views.builders.fund.common import (
    INDEX_PROXY,
    INDEX_WITHHELD,
    withholds_index,
)
from src.m6_views.compose import ok_envelope
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.format import format_date
from src.m6_views.registry import VIEW_DEFS, register
from src.m6_views.states import empty_envelope

VIEW_ID = "fund_xray_header"

COLUMNS = [
    ("window_key", "Window", "text"),
    ("obs_days", "Days", "count"),
    ("return_ann", "Return", "return_ann"),
    ("return_cum", "Cumulative", "fraction"),
    ("volatility_ann", "Volatility", "fraction"),
    ("max_dd", "Max drawdown", "fraction"),
    ("confidence", "Confidence", "text"),
    ("sharpe", "Sharpe", "ratio"),
    ("sortino", "Sortino", "ratio"),
    ("risk_free_pct", "Risk-free", "pct"),
    ("bench_return_ann", "Benchmark", "return_ann"),
    ("beta", "Beta", "ratio"),
    ("tracking_error", "Tracking error", "fraction"),
    ("alpha_ann", "Alpha p.a.", "fraction"),
    ("up_capture", "Up capture", "ratio"),
    ("down_capture", "Down capture", "ratio"),
    ("interpolated_pct", "Filled NAV", "pct"),
]


@dataclass(frozen=True)
class _Quality:
    worst_staleness_days: int
    confidence: str
    caveats: list[str]
    coverage_pct: None = None
    unresolved_pct: None = None


def _row(key: str, w: ReturnWindow | None) -> dict[str, Any]:
    """Every column present, None where the window has no figure, so a short
    history shows as dashes rather than as a missing row."""
    row: dict[str, Any] = {k: getattr(w, k, None) for k, _, _ in COLUMNS}
    row["window_key"] = key
    row["max_dd"] = w.drawdown.depth if w else None
    return row


@register
class FundXrayBuilder:
    view_id = VIEW_ID

    def __init__(self, deps: Deps) -> None:
        self.market = deps.market

    def build(self, scope: Scope, params: dict[str, Any]) -> ViewEnvelope:
        question = VIEW_DEFS[VIEW_ID].question
        if not scope.scope_id:
            return empty_envelope(
                VIEW_ID,
                question,
                scope,
                "No fund chosen. Open one from the Holdings table, or add "
                "scope_id=<ISIN> to this page's address.",
            )
        scheme = SchemeId(scope.scope_id)
        try:
            fw = fund_windows(self.market, scheme, scope.as_of)
        except NothingToCompute as e:
            return empty_envelope(VIEW_ID, question, scope, str(e))

        shown = [w for w in fw.windows.values() if w]
        caveats: list[str] = []
        if withholds_index(self.market):
            caveats.append(INDEX_PROXY if fw.benchmark_id else INDEX_WITHHELD)
        elif fw.benchmark_id is None:
            caveats.append(
                "No benchmark index is on record for this scheme, so alpha, "
                "beta, tracking error and capture are not computed."
            )
        elif not any(w.beta is not None for w in shown):
            caveats.append(
                f"Benchmark {fw.benchmark_id} is on record, but too little of "
                f"its total-return series overlaps these windows to compare. "
                f"python -m jobs.fetch_index --held loads it."
            )
        if any(w.interpolated_pct for w in shown):
            caveats.append(
                "Some NAV points were interpolated, not fetched (Filled NAV). "
                "A filled stretch is a straight line, so volatility there "
                "reads low."
            )

        rows = [_row(key, w) for key, w in fw.windows.items()]
        return ok_envelope(
            view_id=VIEW_ID,
            scope=scope,
            payload={
                "columns": [
                    {"key": k, "label": label, "kind": kind}
                    for k, label, kind in COLUMNS
                ],
                "rows": rows,
                "definition": (
                    f"{scheme}: NAV adjusted for distributions,"
                    f" {len(fw.navs):,} points from {fw.navs[0].nav_date}"
                    f" to {fw.navs[-1].nav_date}. Benchmark:"
                    f" {fw.benchmark_id or 'none on record'}."
                    + (f" Launched {format_date(fw.inception)}." if fw.inception else "")
                ),
            },
            quality=_Quality(fw.staleness_days, fw.confidence, []),
            data_as_of=fw.navs[-1].nav_date,
            source_modules=["m2", "m0"],
            row_count=len(rows),
            params=params,
            extra_caveats=caveats,
        )


__all__ = ["FundXrayBuilder"]
