"""`lookthrough_sankey` — the flagship. MODULE_6.md §8.1 and §5.2.

*"What do I actually own, beneath the funds?"* — funds on the left, companies on
the right, flow width in rupees. It explains look-through in one glance, which is
the product thesis, and it is the screen `PLAN.md` §9.10 chose a frontend for.

Two requirements from Appendix A, both load-bearing:

1. **Cap at ~40 companies with an aggregated `__OTHERS__` node.** A Sankey with
   156 right-hand nodes is unreadable, and shipping all of them to hide most in
   the browser would make the CSV disagree with the chart (§11.2).
2. **`__UNRESOLVED__` and `__NO_DISCLOSURE__` are visually distinct nodes, pinned
   outside `top_n`, never dropped.** They are usually small, so plain tail
   aggregation folds exactly the missing mass into a bucket labelled "smaller
   holdings" — §20's runbook lists it as a known failure mode. Missing mass that
   disappears is the one outcome this whole project exists to prevent: the user
   concludes they know what they own when a slice of it was never identified.

Link values are M3's `exposure_inr` per (scheme, issuer), passed through
unchanged. Nothing is summed or divided here — §2.1 — so the widths on screen
are the same rupees the closure test asserts.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.common.types import UserId
from src.m3_lookthrough.providers.lookthrough import Exposure
from src.m6_views.aggregate import (
    SANKEY_NODE_CAP,
    aggregate_tail,
    others_label,
    tail_total,
)
from src.m6_views.builder import Scope
from src.m6_views.compose import ok_envelope
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.registry import VIEW_DEFS, register
from src.m6_views.states import empty_envelope

VIEW_ID = "lookthrough_sankey"
OTHERS = "__OTHERS__"


@register
class LookthroughSankeyBuilder:
    """§5.2's reference implementation, against the real provider."""

    view_id = VIEW_ID

    def __init__(self, deps: Deps) -> None:
        self.lookthrough = deps.lookthrough

    def build(self, scope: Scope, params: dict[str, Any]) -> ViewEnvelope:
        question = VIEW_DEFS[VIEW_ID].question
        top_n = int(params.get("top_n", SANKEY_NODE_CAP))
        user = UserId(scope.user_id)

        try:
            result = self.lookthrough.lookthrough(user, scope.as_of)
        except LookupError:
            return empty_envelope(
                VIEW_ID,
                question,
                scope,
                "No holdings yet. Import a CAS statement and load at least one "
                "fund's portfolio disclosure to see what you own beneath them.",
            )
        if not result.exposures:
            return empty_envelope(
                VIEW_ID,
                question,
                scope,
                "The look-through ran but found no exposures. This happens when "
                "no held scheme has a parsed disclosure yet — disclosures "
                "usually appear 7-15 days after month end.",
            )

        # Already descending by the provider's contract (§15.3), so this does
        # not re-sort. `exposure_inr` is DECIMAL_TEXT and ordering it anywhere
        # near SQL is the V1-18 trap; the provider does it in Python.
        head, tail = aggregate_tail(
            result.exposures,
            top_n,
            value_of=lambda e: e.exposure_inr,
            keep=lambda e: e.is_synthetic,
        )
        shown = {str(e.issuer_id) for e in head}

        nodes: list[dict[str, Any]] = []
        for exposure in head:
            nodes.append(_issuer_node(exposure))
        for scheme_id in sorted({str(c.scheme_id) for c in result.contributions}):
            nodes.append(
                {
                    "id": scheme_id,
                    "label": scheme_id,
                    "kind": "scheme",
                    "side": "left",
                    "pattern": None,
                }
            )

        links = [
            {
                "source": str(c.scheme_id),
                "target": str(c.issuer_id),
                "value": c.exposure_inr,
            }
            for c in result.contributions
            if str(c.issuer_id) in shown
        ]

        truncated = bool(tail)
        if truncated:
            nodes.append(
                {
                    "id": OTHERS,
                    "label": others_label(len(tail)),
                    "kind": "aggregate",
                    "side": "right",
                    "pattern": None,
                }
            )
            folded = {str(e.issuer_id) for e in tail}
            # One link per scheme into the aggregate node, so the tail keeps its
            # provenance: the user can still see WHICH funds the folded rupees
            # came through, which is the audit trail §4.2 exists for.
            by_scheme: dict[str, Decimal] = {}
            for contribution in result.contributions:
                if str(contribution.issuer_id) in folded:
                    key = str(contribution.scheme_id)
                    by_scheme[key] = (
                        by_scheme.get(key, Decimal(0)) + contribution.exposure_inr
                    )
            links.extend(
                {"source": scheme, "target": OTHERS, "value": value}
                for scheme, value in sorted(by_scheme.items())
            )

        return ok_envelope(
            view_id=VIEW_ID,
            scope=scope,
            payload={
                "nodes": nodes,
                "links": links,
                "weight_basis": params.get("weight_basis", "disclosed"),
                "top_n": top_n,
                "others_total_inr": tail_total(tail, lambda e: e.exposure_inr),
            },
            quality=result.summary,
            data_as_of=(
                min(result.holdings_dates)
                if result.holdings_dates
                else scope.as_of
            ),
            source_modules=["m3", "m1", "m0"],
            row_count=len(result.exposures),
            params=params,
            truncated=truncated,
            basis=params.get("weight_basis"),
        )


def _issuer_node(exposure: Exposure) -> dict[str, Any]:
    """One right-hand node.

    Synthetics carry `kind="synthetic"` and a hatch pattern rather than a
    palette colour — §10.2 fixes them to neutral grey, and §10.3 requires the
    pattern so the distinction survives greyscale and colour-blind vision.
    `__UNRESOLVED__` must not be able to pass for a company.
    """
    return {
        "id": str(exposure.issuer_id),
        "label": exposure.issuer_name,
        "kind": "synthetic" if exposure.is_synthetic else "issuer",
        "side": "right",
        "pattern": "hatch" if exposure.is_synthetic else None,
        "value": exposure.exposure_inr,
        "pct": exposure.exposure_pct,
        "via_funds": exposure.via_funds,
        "instrument_class": exposure.instrument_class,
    }


__all__ = ["LookthroughSankeyBuilder"]
