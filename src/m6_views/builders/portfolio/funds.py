"""`fund_list` — "What do I hold?". MODULE_6.md §8.1.

The plainest view here, and the one that has to be exactly right: it is the
screen a user checks their own statement against. If a unit count disagrees with
their CAS, nothing else on the site gets trusted.

**Reconciliation status travels per row, not as a page banner.** `MODULE_1.md`
§12.1 makes data quality a property of the fact. A folio that failed
reconciliation is degraded *in its own row*, beside the numbers it makes
doubtful, rather than behind a header saying "some data may be stale" that
applies to everything and therefore to nothing.

**M6 reads `position` through a provider, never directly** — §1.3 rule 2.
`SqlitePositionProvider` is the way in, and it derives nothing: every column here
was written by M1's `rebuild()`.
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

VIEW_ID = "fund_list"

COLUMNS = [
    ("scheme_id", "Scheme", "text"),
    ("folio", "Folio", "text"),
    ("units", "Units", "units"),
    ("nav", "NAV", "nav"),
    ("nav_date", "NAV date", "date"),
    ("market_value", "Value", "inr"),
    ("invested_net", "Invested (net)", "inr"),
    ("unrealised_pnl", "Unrealised P&L", "inr_signed"),
    ("weight_in_portfolio", "Weight", "pct"),
    ("confidence", "Confidence", "confidence"),
]


@register
class FundListBuilder:
    view_id = VIEW_ID

    def __init__(self, deps: Deps) -> None:
        self.positions = deps.positions
        self.lookthrough = deps.lookthrough
        self.ledger = deps.ledger

    def required_sources(self) -> list[str]:
        return VIEW_DEFS[VIEW_ID].requires_fields

    def build(self, scope: Scope, params: dict[str, Any]) -> ViewEnvelope:
        question = VIEW_DEFS[VIEW_ID].question
        user = UserId(scope.user_id)
        held = self.positions.positions(user, scope.as_of)
        if not held:
            latest = self.positions.latest_as_of(user)
            hint = (
                f" The most recent rebuild is dated {latest.isoformat()} — try "
                f"that date."
                if latest is not None
                else " Import a CAS statement to create it."
            )
            return empty_envelope(
                VIEW_ID,
                question,
                scope,
                f"No positions were rebuilt for {scope.as_of.isoformat()}.{hint}",
            )

        rows = [
            {
                "scheme_id": str(p.scheme_id),
                "folio": p.folio,
                "units": p.units,
                "nav": p.nav,
                "nav_date": p.nav_date,
                "market_value": p.market_value,
                "invested_net": p.invested_net,
                "unrealised_pnl": p.unrealised_pnl,
                "realised_pnl_todate": p.realised_pnl_todate,
                "weight_in_portfolio": p.weight_in_portfolio,
                # Per row, per §12.1. The renderer must show this beside the
                # numbers rather than aggregating it away.
                "reconciled": p.reconciled,
                "confidence": p.confidence,
            }
            for p in held
        ]

        extra: list[str] = []
        unreconciled = [p for p in held if not p.reconciled]
        if unreconciled:
            extra.append(
                f"{len(unreconciled)} of {len(held)} positions did not "
                f"reconcile against the statement's own closing balance; their "
                f"figures are marked in the table."
            )
        unvalued = [p for p in held if p.market_value is None]
        if unvalued:
            extra.append(
                f"{len(unvalued)} position(s) had no NAV on this date, so they "
                f"carry no value here and are not in the portfolio total."
            )

        try:
            summary = self.lookthrough.summary(user, scope.as_of)
        except LookupError:
            return empty_envelope(
                VIEW_ID,
                question,
                scope,
                "Positions exist but no look-through has been computed for this "
                "date, so their coverage and staleness are unknown. Re-run the "
                "look-through.",
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
            },
            quality=summary,
            data_as_of=scope.as_of,
            source_modules=["m1", "m0"],
            row_count=len(rows),
            params=params,
            ledger=self.ledger,
            extra_caveats=extra,
        )


__all__ = ["FundListBuilder"]
