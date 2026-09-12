"""`overlap_heatmap` — "Am I paying twice for the same thing?". §8.1.

The third of §16.5's landing questions, and usually the launch story: a portfolio
of five large-cap funds tends to be one fund bought five times.

**Unaligned cells are marked.** §9.3: overlap is exactly the metric where a
month's trading moves the answer, so a pair whose two disclosures are from
different dates carries `aligned = false` and its own gap in days. Rendering it
identically to a same-date cell would present a soft comparison as a hard one —
§9.3's *"never silently compare mismatched dates"*.
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

VIEW_ID = "overlap_heatmap"


@register
class OverlapHeatmapBuilder:
    view_id = VIEW_ID

    def __init__(self, deps: Deps) -> None:
        self.lookthrough = deps.lookthrough
        self.ledger = deps.ledger

    def required_sources(self) -> list[str]:
        return VIEW_DEFS[VIEW_ID].requires_fields

    def build(self, scope: Scope, params: dict[str, Any]) -> ViewEnvelope:
        question = VIEW_DEFS[VIEW_ID].question
        user = UserId(scope.user_id)
        pairs = self.lookthrough.overlap_matrix(user, scope.as_of)
        if not pairs:
            return empty_envelope(
                VIEW_ID,
                question,
                scope,
                "Overlap needs at least two funds with parsed disclosures. "
                "Load another fund's portfolio to compare them.",
            )

        try:
            summary = self.lookthrough.summary(user, scope.as_of)
        except LookupError:
            return empty_envelope(
                VIEW_ID,
                question,
                scope,
                "The overlap matrix exists but the portfolio summary does not, "
                "so there is nothing to report its coverage against. Re-run the "
                "look-through.",
            )

        schemes = sorted(
            {str(p.scheme_a) for p in pairs} | {str(p.scheme_b) for p in pairs}
        )
        cells = [
            {
                "scheme_a": str(p.scheme_a),
                "scheme_b": str(p.scheme_b),
                "overlap_pct": p.overlap_pct,
                "overlap_equity_pct": p.overlap_equity_pct,
                "overlap_value_inr": p.overlap_value_inr,
                "common_issuers": p.common_issuers,
                "union_issuers": p.union_issuers,
                "jaccard": p.jaccard,
                # The renderer must treat these as a distinct visual state, not
                # a tooltip. §9.3 rule 3.
                "aligned": p.aligned,
                "as_of_gap_days": p.as_of_gap_days,
                "as_of_a": p.as_of_a,
                "as_of_b": p.as_of_b,
            }
            for p in pairs
        ]

        unaligned = [c for c in cells if not c["aligned"]]
        extra = (
            [
                f"{len(unaligned)} of {len(cells)} fund pairs are compared "
                f"across different disclosure dates, so those figures are "
                f"approximate."
            ]
            if unaligned
            else []
        )

        return ok_envelope(
            view_id=VIEW_ID,
            scope=scope,
            payload={"schemes": schemes, "cells": cells},
            quality=summary,
            data_as_of=min(p.as_of_a for p in pairs),
            source_modules=["m3", "m0"],
            row_count=len(cells),
            params=params,
            ledger=self.ledger,
            extra_caveats=extra,
        )


__all__ = ["OverlapHeatmapBuilder"]
