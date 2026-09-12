"""`portfolio_summary` — "Where do I stand?". MODULE_6.md §8.1.

The first of §16.5's three landing questions, and a KPI grid rather than a chart.

**Most of its tiles are empty, and that is the design working.** §4.6's
`portfolio_summary` declares XIRR, TWRR, blended TER, fee drag and realised P&L;
M3 fills none of them, because they belong to M1's returns engine and M2's fee
data, neither of which has run. Every one comes through as `None` and renders as
an em dash (§9.3).

That is not a gap being papered over — it is the alternative being refused. A
zero in the XIRR tile would read as "your portfolio returned nothing", which is a
claim, and a wrong one. `PLAN.md` §4.3 is that a number the product cannot stand
behind is not shown at all.
"""

from __future__ import annotations

from typing import Any

from src.common.types import UserId
from src.m6_views.builder import Scope
from src.m6_views.compose import ok_envelope
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.registry import VIEW_DEFS, register
from src.m6_views.states import empty_envelope

VIEW_ID = "portfolio_summary"


@register
class PortfolioSummaryBuilder:
    """§8.1. M3's stored summary, reshaped into tiles."""

    view_id = VIEW_ID

    def __init__(self, deps: Deps) -> None:
        self.lookthrough = deps.lookthrough
        self.ledger = deps.ledger

    def required_sources(self) -> list[str]:
        return VIEW_DEFS[VIEW_ID].requires_fields

    def build(self, scope: Scope, params: dict[str, Any]) -> ViewEnvelope:
        question = VIEW_DEFS[VIEW_ID].question
        user = UserId(scope.user_id)
        try:
            summary = self.lookthrough.summary(user, scope.as_of)
        except LookupError:
            return empty_envelope(
                VIEW_ID,
                question,
                scope,
                "No portfolio has been computed for this date yet. Import a CAS "
                "statement and run the look-through to populate it.",
            )

        # §5.5's worst-case: the EARLIEST contributing disclosure, so the
        # envelope's `data_as_of` matches the staleness it reports. Taking
        # `scope.as_of` instead would say the holdings are current beside a
        # `staleness_days` of 43, and an envelope that contradicts itself is
        # worse than one that admits age.
        dated = [
            e.holdings_as_of
            for e in self.lookthrough.exposures(user, scope.as_of)
            if e.holdings_as_of is not None
        ]
        data_as_of = min(dated) if dated else scope.as_of

        # Tiles are (key, label, value, kind). `kind` tells the renderer how to
        # format — it does NOT convert anything here, because a builder that
        # formats has decided what the number means. §2.1.
        tiles = [
            ("total_value_inr", "Portfolio value", summary.total_value_inr, "inr"),
            ("fund_value_inr", "Held via funds", summary.fund_value_inr, "inr"),
            ("direct_value_inr", "Held directly", summary.direct_value_inr, "inr"),
            ("invested_net_inr", "Invested (net)", summary.invested_net_inr, "inr"),
            (
                "unrealised_pnl_inr",
                "Unrealised P&L",
                summary.unrealised_pnl_inr,
                "inr_signed",
            ),
            ("portfolio_xirr", "XIRR", summary.portfolio_xirr, "return_ann"),
            ("blended_ter", "Blended TER", summary.blended_ter, "pct"),
            ("scheme_count", "Schemes", summary.scheme_count, "count"),
            ("folio_count", "Folios", summary.folio_count, "count"),
            ("issuer_count", "Companies", summary.issuer_count, "count"),
            ("coverage_pct", "Coverage", summary.coverage_pct, "pct"),
            ("unresolved_pct", "Unresolved", summary.unresolved_pct, "pct"),
        ]

        # Named rather than silently absent. A tile that is missing tells the
        # reader nothing; a tile reading "—" with this note tells them the
        # figure exists as a concept and which part of the system owes it.
        pending = [key for key, _, value, _ in tiles if value is None]
        extra = (
            [
                f"{len(pending)} figure(s) are not computed yet — they come "
                f"from the returns and fee engines, which have not run."
            ]
            if pending
            else []
        )

        return ok_envelope(
            view_id=VIEW_ID,
            scope=scope,
            payload={
                "tiles": [
                    {"key": k, "label": label, "value": v, "kind": kind}
                    for k, label, v, kind in tiles
                ],
                "confidence": summary.confidence,
            },
            quality=summary,
            data_as_of=data_as_of,
            source_modules=["m3", "m1"],
            row_count=len(tiles),
            params=params,
            ledger=self.ledger,
            extra_caveats=extra,
        )


__all__ = ["PortfolioSummaryBuilder"]
