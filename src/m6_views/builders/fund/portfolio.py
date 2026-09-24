"""`fund_portfolio`: "What does this fund own?". MODULE_6.md §8.1, one fund.

Four pictures of one disclosure: the asset mix as a ring, the companies as
tiles sized by weight, the equity by the sector the fund house printed, and the
equity by AMFI size bucket. The sums are M3's (`composition`); this lays them
out, folds the smallest tiles into one counted "others" tile (§11.2), and keeps
`__UNRESOLVED__` visible however small (Appendix A).
"""

from __future__ import annotations

from typing import Any

from src.common.types import SchemeId
from src.m3_lookthrough.engine import IssuerWeight
from src.m6_views.aggregate import aggregate_tail, others_label, tail_total
from src.m6_views.builder import Scope
from src.m6_views.builders.fund.common import (
    CLASS_NAMES,
    CLASS_PHRASES,
    NO_FUND,
    SIZE_NAMES,
    FundQuality,
)
from src.m6_views.compose import ok_envelope
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.format import format_date, format_pct
from src.m6_views.registry import VIEW_DEFS, register
from src.m6_views.states import empty_envelope

VIEW_ID = "fund_portfolio"
TILES = 50
SECTORS = 10
UNRESOLVED = "__UNRESOLVED__"
#: Days after which a monthly disclosure is behind (MODULE_3 §5.5's threshold).
STALE_DAYS = 45


def _class(klass: str) -> str:
    return CLASS_NAMES.get(klass, klass)


def _pct(v: Any) -> str:
    return format_pct(v, precision=2)


@register
class FundPortfolioBuilder:
    view_id = VIEW_ID

    def __init__(self, deps: Deps) -> None:
        self.lookthrough = deps.lookthrough

    def required_sources(self) -> list[str]:
        return VIEW_DEFS[VIEW_ID].requires_fields

    def build(self, scope: Scope, params: dict[str, Any]) -> ViewEnvelope:
        question = VIEW_DEFS[VIEW_ID].question
        if not scope.scope_id:
            return empty_envelope(VIEW_ID, question, scope, NO_FUND)
        scheme = SchemeId(scope.scope_id)
        found = self.lookthrough.fund_composition(scheme, scope.as_of)
        if found is None:
            return empty_envelope(
                VIEW_ID, question, scope,
                "No portfolio is loaded for this fund. Portfolios come from each "
                "fund house's monthly disclosure; HDFC, ICICI Prudential, Kotak, "
                "Nippon India and PPFAS are read directly, and any other fund can "
                "be added with python -m jobs.fetch_groww.",
            )
        fund, disclosed, tier = found
        name = self.lookthrough.issuer_name

        positive = [h for h in fund.holdings if h.weight > 0]
        head, tail = aggregate_tail(
            positive, TILES, lambda h: h.weight,
            keep=lambda h: str(h.issuer_id) == UNRESOLVED,
        )
        tiles: list[dict[str, Any]] = [
            {"name": name(h.issuer_id), "value": str(h.weight), "label": _pct(h.weight),
             "group": _class(h.instrument_class)}
            for h in head
        ]
        if tail:
            rest = tail_total(tail, lambda h: h.weight)
            # Drawn grey: a bucket of many holdings, not one large holding.
            tiles.append({"name": others_label(len(tail)), "value": str(rest),
                          "label": _pct(rest), "group": "Others", "others": True})

        # Read in pairs: the ring beside the size bars, the tiles on their own
        # row, then the sectors.
        charts: list[dict[str, Any]] = [
            {"kind": "donut", "title": "Asset mix",
             "slices": [{"name": _class(k), "value": str(v), "label": format_pct(v)}
                        for k, v in fund.by_class if v > 0]},
        ]
        if fund.size:
            charts.append({
                "kind": "hbar", "title": "Shares by company size",
                "bars": [
                    {"name": SIZE_NAMES.get(t.dimension_value, t.dimension_value),
                     "value": str(t.exposure_pct),
                     "label": format_pct(t.exposure_pct)}
                    for t in fund.size
                ],
            })
        charts.append({"kind": "treemap", "title": "Largest holdings", "cells": tiles})
        if fund.by_sector:
            shown, rest_sectors = fund.by_sector[:SECTORS], fund.by_sector[SECTORS:]
            bars = [
                {"name": s, "value": str(v), "label": format_pct(v)} for s, v in shown
            ]
            if rest_sectors:
                rest = tail_total(rest_sectors, lambda kv: kv[1])
                bars.append({"name": f"{len(rest_sectors)} other sectors",
                             "value": str(rest), "label": format_pct(rest)})
            charts.append({"kind": "hbar", "title": "Shares by sector", "bars": bars})

        mix = ", ".join(
            f"{format_pct(v)} {CLASS_PHRASES.get(k, k)}"
            for k, v in fund.by_class
            if v > 0
        )
        headline = (
            f"{len(fund.holdings):,} holdings in the portfolio disclosed for "
            f"{format_date(disclosed)}: {mix}."
        )
        caveats: list[str] = []
        negative = [h for h in fund.holdings if h.weight < 0]
        if negative:
            caveats.append(
                f"{len(negative)} holding{'s' if len(negative) != 1 else ''} with a "
                f"negative weight (net payables or written options) are listed in "
                f"the table and not drawn in the pictures."
            )
        if tier == "aggregator":
            caveats.append(
                "This portfolio comes from an aggregator's page, which carries no "
                "ISINs, so more of it is unresolved than the fund house's own file "
                "would leave."
            )
        if fund.by_sector:
            caveats.append(
                "Sectors are as the fund house labels them; two fund houses may "
                "name the same sector differently."
            )

        unresolved = next(
            (h.weight for h in fund.holdings if str(h.issuer_id) == UNRESOLVED), None
        )
        stale = (scope.as_of - disclosed).days
        rows = [_row(h, name(h.issuer_id)) for h in fund.holdings]
        return ok_envelope(
            view_id=VIEW_ID,
            scope=scope,
            payload={
                "headline": headline,
                "charts": charts,
                "columns": [
                    {"key": "holding", "label": "Holding", "kind": "text"},
                    {"key": "kind", "label": "Kind", "kind": "text"},
                    {"key": "weight_pct", "label": "Share of fund", "kind": "pct"},
                ],
                "rows": rows,
            },
            quality=FundQuality(
                stale, "high" if stale <= STALE_DAYS else "medium",
                unresolved_pct=unresolved,
            ),
            data_as_of=disclosed,
            source_modules=["m3", "m0"],
            row_count=len(rows),
            params=params,
            extra_caveats=caveats,
        )


def _row(h: IssuerWeight, label: str) -> dict[str, Any]:
    return {"holding": label, "kind": _class(h.instrument_class), "weight_pct": h.weight}


__all__ = ["FundPortfolioBuilder"]
