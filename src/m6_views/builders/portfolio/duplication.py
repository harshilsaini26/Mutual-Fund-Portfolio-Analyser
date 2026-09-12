"""`duplication_summary` — "How much of my money is doubled up?". §8.1.

Pairwise overlap says whether two funds are similar. This says what it costs the
portfolio, in rupees, which is the form the user actually asks the question in.

**Descriptive, never a recommendation.** `MODULE_3.md` §9.4 is explicit that
`total minus largest` is "the exposure you would still have if you kept only the
largest provider of that issuer" and that stating it is not advice to
consolidate. Two funds holding the same company is often deliberate. `PLAN.md`
§3.3 and §19.6's lint both apply to every string in this file.
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

VIEW_ID = "duplication_summary"


@register
class DuplicationSummaryBuilder:
    view_id = VIEW_ID

    def __init__(self, deps: Deps) -> None:
        self.lookthrough = deps.lookthrough
        self.ledger = deps.ledger

    def required_sources(self) -> list[str]:
        return VIEW_DEFS[VIEW_ID].requires_fields

    def build(self, scope: Scope, params: dict[str, Any]) -> ViewEnvelope:
        question = VIEW_DEFS[VIEW_ID].question
        user = UserId(scope.user_id)
        found = self.lookthrough.duplication(user, scope.as_of)
        if found is None:
            return empty_envelope(
                VIEW_ID,
                question,
                scope,
                "Duplication has not been computed for this date. It is written "
                "alongside the look-through — re-run it to populate this.",
            )
        duplicated_pct, duplicated_inr, multi_fund, max_funds = found

        try:
            summary = self.lookthrough.summary(user, scope.as_of)
        except LookupError:
            return empty_envelope(
                VIEW_ID,
                question,
                scope,
                "The duplication figures exist but the portfolio summary does "
                "not, so there is nothing to report their coverage against. "
                "Re-run the look-through.",
            )

        tiles = [
            (
                "duplicated_pct",
                "Held through more than one fund",
                duplicated_pct,
                "pct",
            ),
            ("duplicated_inr", "In rupees", duplicated_inr, "inr"),
            ("issuers_multi_fund", "Companies affected", multi_fund, "count"),
            (
                "max_funds_per_issuer",
                "Most funds holding one company",
                max_funds,
                "count",
            ),
        ]

        return ok_envelope(
            view_id=VIEW_ID,
            scope=scope,
            payload={
                "tiles": [
                    {"key": k, "label": label, "value": v, "kind": kind}
                    for k, label, v, kind in tiles
                ],
                # The definition travels with the number. Six months later the
                # reader will not remember whether this counted the issuer's
                # whole exposure or only the part beyond its largest provider,
                # and the two differ by roughly a factor of two.
                "definition": (
                    "The exposure beyond the single largest fund providing each "
                    "company. Synthetic holdings and directly held shares are "
                    "excluded."
                ),
            },
            quality=summary,
            data_as_of=scope.as_of,
            source_modules=["m3"],
            row_count=len(tiles),
            params=params,
            ledger=self.ledger,
        )


__all__ = ["DuplicationSummaryBuilder"]
