"""`mcap_allocation` — "What is my size profile?". MODULE_6.md §8.1.

Equity exposure by AMFI's large/mid/small list, computed by M3 under ONE list
chosen by date (invariant 6). Present tense, so the CURRENT basis: the list in
force on the date asked about, per MODULE_3 §12.2 rule 2.

The unranked row stays: equity AMFI does not rank — foreign listings, mostly —
is shown as itself rather than folded into small cap or dropped.
"""

from __future__ import annotations

from typing import Any

from src.common.types import ClassificationBasis, UserId
from src.m6_views.builder import Scope
from src.m6_views.compose import ok_envelope
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.format import format_date
from src.m6_views.registry import VIEW_DEFS, register
from src.m6_views.states import empty_envelope

VIEW_ID = "mcap_allocation"

COLUMNS = [
    ("bucket", "Size", "text"),
    ("exposure_inr", "Equity exposure", "inr"),
    ("exposure_pct", "Share of equity", "pct"),
]

LABELS = {
    "large": "Large cap",
    "mid": "Mid cap",
    "small": "Small cap",
    "unranked": "Not ranked by AMFI",
}


@register
class SizeProfileBuilder:
    view_id = VIEW_ID

    def __init__(self, deps: Deps) -> None:
        self.lookthrough = deps.lookthrough

    def build(self, scope: Scope, params: dict[str, Any]) -> ViewEnvelope:
        question = VIEW_DEFS[VIEW_ID].question
        user = UserId(scope.user_id)
        try:
            tilts = self.lookthrough.tilts(
                user, scope.as_of, "mcap", ClassificationBasis.CURRENT
            )
        except LookupError as e:
            return empty_envelope(VIEW_ID, question, scope, str(e))
        if not tilts:
            return empty_envelope(
                VIEW_ID,
                question,
                scope,
                f"No equity exposure is stored for {scope.as_of.isoformat()}. "
                f"Run the look-through: python -m scripts.show_lookthrough",
            )

        summary = self.lookthrough.summary(user, scope.as_of)
        rows = [
            {
                "bucket": LABELS[t.dimension_value],
                "exposure_inr": t.exposure_inr,
                "exposure_pct": t.exposure_pct,
            }
            for t in tilts
        ]
        return ok_envelope(
            view_id=VIEW_ID,
            scope=scope,
            payload={
                "columns": [
                    # A share of the whole: the table draws it as a bar too.
                    {"key": k, "label": label, "kind": kind, "bar": k == "exposure_pct"}
                    for k, label, kind in COLUMNS
                ],
                "rows": rows,
                "definition": (
                    f"Equity exposure by AMFI's market-cap list of "
                    f"{format_date(tilts[0].mcap_basis)}, the one in force on "
                    f"{format_date(scope.as_of)}. Large cap is the 100 largest "
                    f"companies by average market capitalisation, mid cap the "
                    f"next 150, small cap the rest. Not ranked is equity the "
                    f"list does not cover, such as foreign listings."
                ),
            },
            quality=summary,
            data_as_of=scope.as_of,
            source_modules=["m3", "m0"],
            row_count=len(rows),
            params=params,
        )


__all__ = ["SizeProfileBuilder"]
