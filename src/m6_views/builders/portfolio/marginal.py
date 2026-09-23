"""`marginal_contribution` — "What does each fund add?". MODULE_6.md §8.1.

MODULE_3 §11's question, and not overlap's: a fund can overlap heavily and
still be the only route to something. Every figure was computed by M3 when the
look-through ran, one extra look-through per fund with that fund left out, and
is only laid out here.

A fund with no disclosure keeps its row, with dashes: what it adds is unknown,
and leaving it out would read as a fund examined and found to add nothing.
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

VIEW_ID = "marginal_contribution"

COLUMNS = [
    ("scheme_id", "Scheme", "text"),
    ("position_inr", "Value", "inr"),
    ("new_issuers", "New companies", "count"),
    ("new_exposure_inr", "New exposure", "inr"),
    ("new_exposure_pct", "New exposure %", "pct"),
    ("hhi_delta", "HHI change", "metric"),
    ("effective_n_delta", "Effective-N change", "metric"),
]

DEFINITION = (
    "New companies are those no other fund you hold provides. HHI change is "
    "the equity concentration with the fund minus without it: negative means "
    "the fund spreads the portfolio, positive means it concentrates it. "
    "Effective-N change is the same comparison in number of equal holdings."
)


@register
class MarginalContributionBuilder:
    view_id = VIEW_ID

    def __init__(self, deps: Deps) -> None:
        self.lookthrough = deps.lookthrough
        self.ledger = deps.ledger

    def required_sources(self) -> list[str]:
        return VIEW_DEFS[VIEW_ID].requires_fields

    def build(self, scope: Scope, params: dict[str, Any]) -> ViewEnvelope:
        question = VIEW_DEFS[VIEW_ID].question
        user = UserId(scope.user_id)
        marginals = self.lookthrough.marginals(user, scope.as_of)
        if not marginals:
            return empty_envelope(
                VIEW_ID,
                question,
                scope,
                f"No marginal contribution was computed for "
                f"{scope.as_of.isoformat()}. Run the look-through, which "
                f"stores it: python -m scripts.show_lookthrough",
            )

        # Written by the same run as the marginals, so present whenever they are.
        summary = self.lookthrough.summary(user, scope.as_of)
        rows = [{k: getattr(m, k) for k, _, _ in COLUMNS} for m in marginals]
        unknown = [m for m in marginals if m.new_issuers is None]
        extra = (
            [
                f"{len(unknown)} of {len(marginals)} funds have no disclosure "
                f"loaded, so what they add cannot be measured; their rows show "
                f"dashes."
            ]
            if unknown
            else []
        )
        return ok_envelope(
            view_id=VIEW_ID,
            scope=scope,
            payload={
                "columns": [
                    {"key": k, "label": label, "kind": kind}
                    for k, label, kind in COLUMNS
                ],
                "rows": rows,
                "definition": DEFINITION,
            },
            quality=summary,
            data_as_of=scope.as_of,
            source_modules=["m3"],
            row_count=len(rows),
            params=params,
            ledger=self.ledger,
            extra_caveats=extra,
        )


__all__ = ["MarginalContributionBuilder"]
