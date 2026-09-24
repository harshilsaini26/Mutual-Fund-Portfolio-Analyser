"""`fund_header`: "What is this fund, and how has it done?". MODULE_6.md §8.2.

The top of the fund page: the fund's name in words rather than an ISIN, a
strip of KPI tiles (DECISIONS V1-74) -- returns against the benchmark, the worst
fall, volatility and size -- and the one finding no tile can hold: how steady
the fund has been across every three-year stretch. Each is descriptive (§2.6),
and a tone always travels with a symbol and words, never colour alone (§10.3).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from src.common.contracts.market import NavPoint
from src.common.types import SchemeId
from src.m2_fund.paths import price_history, rolling_path
from src.m2_fund.windows import (
    FundWindows,
    NothingToCompute,
    fund_windows,
    spans,
    window_start,
)
from src.m6_views.aggregate import lttb_indices
from src.m6_views.builder import Scope
from src.m6_views.builders.fund.common import NO_FUND, FundQuality
from src.m6_views.compose import ok_envelope
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.format import (
    format_date,
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


#: Points in a tile's sparkline: a shape, not a chart. LTTB keeps the trough.
SPARK_POINTS = 48
RETURN_TILES = (("1y", "1 year"), ("3y", "3 years"), ("5y", "5 years"))


def _finding(title: str, text: str, tone: str) -> dict[str, str]:
    symbol = {"ahead": "▲", "behind": "▼"}.get(tone, "●")
    return {"title": title, "text": text, "tone": tone, "symbol": symbol}


def _spark(navs: list[NavPoint], start: date) -> list[Decimal]:
    """The prices behind one window, thinned to a sparkline's worth."""
    values = [n.nav for n in navs if n.nav_date >= start]
    if len(values) < 2:
        return []
    return [values[i] for i in lttb_indices(values, SPARK_POINTS)]


def _tile(key: str, label: str, value: Any, kind: str, context: str | None,
          tone: str | None = None) -> dict[str, Any]:
    """One KPI tile. The figure is formatted by `fmt_tile`; the context line is
    a sentence written here. A tone always travels with its symbol (§10.3)."""
    return {
        "key": key, "label": label, "value": value, "kind": kind,
        "context": context, "tone": tone,
        "symbol": {"ahead": "▲", "behind": "▼"}.get(tone or ""),
        "spark": [],
    }


def _tiles(fw: FundWindows | None, facts: Any) -> list[dict[str, Any]]:
    """The fund page's KPI strip (DECISIONS V1-74): returns over one, three and
    five years, the worst fall, volatility and size.

    Beside each return goes the benchmark's own figure, never a difference
    between the two: that would be a number the page derived, and §2.1 keeps
    figures where they were computed.
    """
    tiles: list[dict[str, Any]] = []
    for key, label in RETURN_TILES:
        w = fw.windows.get(key) if fw else None
        if w is None or not spans(w.obs_days, key):
            tiles.append(_tile(f"return_{key}", f"Return, {label}", None, "return_ann",
                               f"Less than {label} of prices on record"))
            continue
        context, tone = None, None
        if w.bench_return_ann is not None:
            tone = "ahead" if w.return_ann > w.bench_return_ann else "behind"
            context = (
                f"{'Ahead of' if tone == 'ahead' else 'Behind'} its benchmark, "
                f"{format_return(w.bench_return_ann, True)}"
            )
        tile = _tile(f"return_{key}", f"Return, {label}", w.return_ann, "return_ann",
                     context, tone)
        if fw is not None:
            tile["spark"] = _spark(fw.navs, window_start(fw.navs[-1].nav_date, key))
        tiles.append(tile)

    longest = fw.windows.get("since_first_nav") if fw else None
    if longest is not None and longest.obs_days >= YEAR and longest.drawdown.depth < 0:
        dd = longest.drawdown
        tiles.append(_tile(
            "worst_fall", "Worst fall", dd.depth, "fraction",
            f"{format_date(dd.peak)} to {format_date(dd.trough)}; "
            + (f"back at its high {dd.recovery_days:,} days after the low"
               if dd.recovery_days is not None else "not yet back at that high"),
        ))
    steady = next(
        ((k, w) for k, w in ((k, fw.windows.get(k)) for k in ("3y", "1y"))
         if w and spans(w.obs_days, k)),
        None,
    ) if fw else None
    if steady is not None:
        key, w = steady
        tiles.append(_tile("volatility", "Volatility", w.volatility_ann, "fraction",
                           f"Annualised, from {PERIODS[key]} of daily prices"))
    tiles.append(_tile(
        "fund_size", "Fund size", facts.aum_inr, "inr_round",
        f"Quarterly average to {format_date(facts.aum_as_of)}"
        if facts.aum_inr is not None else "Not on record",
    ))
    return tiles


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
            # The returns against the benchmark and the worst fall are tiles
            # now; a finding repeating a tile's figure would say it twice.
            navs, levels, _ = price_history(self.market, scheme, scope.as_of)
            rp = rolling_path(navs, levels, THREE_YEARS)
            if rp is not None and rp.pct_ahead is not None:
                findings.append(_finding(
                    "How steady",
                    f"Ahead of its benchmark in {format_pct(rp.pct_ahead, precision=0)} "
                    f"of all three-year stretches",
                    "ahead" if rp.pct_ahead >= 50 else "behind",
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
                "tiles": _tiles(fw, facts),
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
