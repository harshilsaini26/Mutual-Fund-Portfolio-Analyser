"""`fund_nav`: "How has its price moved since the first one on record?".

The published NAV, not the dividend-adjusted one the other charts use: this is
the price a statement shows, from its first day on record to its latest
(DECISIONS V1-81). Downsampled for the drawing only (§11.3); the highest and
lowest are read from every price.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from src.common.types import SchemeId
from src.m2_fund.risk import confidence_from_obs
from src.m6_views.aggregate import lttb_indices
from src.m6_views.builder import Scope
from src.m6_views.builders.fund.common import NO_FUND, FundQuality, points
from src.m6_views.compose import ok_envelope
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.format import format_date, format_inr
from src.m6_views.registry import VIEW_DEFS, register
from src.m6_views.states import empty_envelope

VIEW_ID = "fund_nav"


def _nav(v: Decimal | None) -> str:
    return format_inr(v, precision=4, compact=False)


@register
class FundNavBuilder:
    view_id = VIEW_ID

    def __init__(self, deps: Deps) -> None:
        self.market = deps.market

    def build(self, scope: Scope, params: dict[str, Any]) -> ViewEnvelope:
        question = VIEW_DEFS[VIEW_ID].question
        if not scope.scope_id:
            return empty_envelope(VIEW_ID, question, scope, NO_FUND)
        scheme = SchemeId(scope.scope_id)
        navs = self.market.nav_series(scheme, date.min, scope.as_of, adjusted=False)
        if len(navs) < 2:
            return empty_envelope(
                VIEW_ID, question, scope,
                f"Fewer than two prices are on record for {scheme}. "
                f"python -m jobs.backfill_scheme_nav --scheme {scheme} loads them.",
            )
        first, last = navs[0], navs[-1]
        high = max(navs, key=lambda p: p.nav)
        low = min(navs, key=lambda p: p.nav)
        headline = (
            f"Its NAV was {_nav(first.nav)} on {format_date(first.nav_date)} and "
            f"{_nav(last.nav)} on {format_date(last.nav_date)}. The highest was "
            f"{_nav(high.nav)} on {format_date(high.nav_date)}, the lowest "
            f"{_nav(low.nav)} on {format_date(low.nav_date)}."
        )
        kept = [navs[i] for i in lttb_indices([p.nav for p in navs])]
        facts = self.market.scheme_facts(scheme)
        rows = [{"date": p.nav_date, "nav": p.nav} for p in kept]
        return ok_envelope(
            view_id=VIEW_ID,
            scope=scope,
            payload={
                "headline": headline,
                "charts": [{
                    "kind": "line", "title": question, "y": "inr",
                    "series": [{
                        "name": facts.name if facts else str(scheme), "role": "fund",
                        "points": points([(p.nav_date, p.nav) for p in kept], _nav),
                    }],
                }],
                "columns": [
                    {"key": "date", "label": "Date", "kind": "date"},
                    {"key": "nav", "label": "NAV", "kind": "nav"},
                ],
                "rows": rows,
            },
            quality=FundQuality(
                (scope.as_of - last.nav_date).days,
                confidence_from_obs((last.nav_date - first.nav_date).days),
            ),
            data_as_of=last.nav_date,
            source_modules=["m0"],
            row_count=len(rows),
            params=params,
        )


__all__ = ["FundNavBuilder"]
