"""`concentration_curve` — "How concentrated am I really?". §8.1.

A Lorenz curve with HHI, effective-N and the topN shares beside it. The curve is
what makes the single numbers legible: HHI 0.014 means nothing to most readers,
and "the smallest 100 of your 156 companies are 30% of your money" means
something immediately.

**Every point comes from M3.** `lorenz_points` lives in
`src/m3_lookthrough/concentration.py`, not here, because a Lorenz point is a
cumulative share of a cumulative share and §2.1 forbids the view layer from
deriving a number. This builder chooses a scope, asks, and arranges the answer.

**Gini can legitimately be absent.** V1-21: the curve and its summary are both
undefined once a net-short issuer puts a negative weight in the pool, so the
`all` scope can come back with no curve while `equity` has one. It renders as an
em dash (§9.3), not as zero.
"""

from __future__ import annotations

from typing import Any

from src.common.types import ExposureScope, UserId
from src.m3_lookthrough.concentration import lorenz_points
from src.m6_views.builder import Scope
from src.m6_views.compose import ok_envelope
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.registry import VIEW_DEFS, register
from src.m6_views.states import empty_envelope

VIEW_ID = "concentration_curve"


@register
class ConcentrationCurveBuilder:
    view_id = VIEW_ID

    def __init__(self, deps: Deps) -> None:
        self.lookthrough = deps.lookthrough
        self.ledger = deps.ledger

    def required_sources(self) -> list[str]:
        return VIEW_DEFS[VIEW_ID].requires_fields

    def build(self, scope: Scope, params: dict[str, Any]) -> ViewEnvelope:
        question = VIEW_DEFS[VIEW_ID].question
        user = UserId(scope.user_id)
        pool: ExposureScope = params.get("scope", "equity")

        try:
            metric = self.lookthrough.concentration(user, scope.as_of, pool)
            summary = self.lookthrough.summary(user, scope.as_of)
        except LookupError as exc:
            return empty_envelope(
                VIEW_ID,
                question,
                scope,
                f"Concentration has not been computed for this date ({exc}). "
                f"It is written alongside the look-through — re-run it.",
            )

        if not metric.issuer_count:
            return empty_envelope(
                VIEW_ID,
                question,
                scope,
                f"No {pool} holdings were found on this date, so there is "
                f"nothing to measure concentration across. Try the 'all' scope, "
                f"or load a fund's disclosure.",
            )

        # Passed straight through — filtering and ordering are M3's, and the
        # exposures arrive from the provider already descending by contract.
        curve = lorenz_points(
            self.lookthrough.exposures(user, scope.as_of, scope="all"), pool
        )

        extra = (
            []
            if curve
            else [
                "The concentration curve is not shown because a holding has a "
                "negative net exposure — a fund holds it short. The curve and "
                "its Gini coefficient are only defined for non-negative "
                "holdings."
            ]
        )

        return ok_envelope(
            view_id=VIEW_ID,
            scope=scope,
            payload={
                "scope": pool,
                "curve": [
                    {"issuer_share": x, "exposure_share": y} for x, y in curve
                ],
                "issuer_count": metric.issuer_count,
                "hhi": metric.hhi,
                "effective_n": metric.effective_n,
                "top1_pct": metric.top1_pct,
                "top5_pct": metric.top5_pct,
                "top10_pct": metric.top10_pct,
                "top20_pct": metric.top20_pct,
                "gini": metric.gini,
                "largest_issuer_id": (
                    str(metric.largest_issuer_id)
                    if metric.largest_issuer_id
                    else None
                ),
            },
            quality=summary,
            data_as_of=scope.as_of,
            source_modules=["m3"],
            row_count=metric.issuer_count,
            params=params,
            ledger=self.ledger,
            extra_caveats=extra,
        )


__all__ = ["ConcentrationCurveBuilder"]
