"""Building an `ok` envelope. MODULE_6.md §3 and §5.2.

Every builder ends the same way: take the portfolio's quality figures, assemble
the caveats, stamp the provenance, wrap the payload. Doing that in six places
would give six chances to forget `unresolved_pct`, and a provenance field that is
missing from one view is exactly the quiet erosion §2.2 makes the fields
non-defaultable to prevent.

It lives outside `builders/` on purpose: §19.3's static check walks every file
under `builders/` looking for arithmetic on provider-sourced values, and a shared
helper there would either trip it or force the check to grow an exception. There
is no arithmetic here either — it is assignment and string assembly — but keeping
the check's scope exactly "the view code" is worth more than the convenience.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from typing import Any

from src.common.types import ViewState
from src.m6_views.builder import Scope
from src.m6_views.caveats import Quality, assemble_caveats
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.hashing import upstream_hash
from src.m6_views.registry import VIEW_DEFS


def export_url(view_id: str, scope: Scope, params: dict[str, Any]) -> str:
    """§2.5: CSV on every view — a trust feature and an escape hatch.

    It signals the user's data is not trapped in this UI, which for a
    self-hosted single-user tool is most of why they would trust it at all.
    """
    query = f"?user_id={scope.user_id}&as_of={scope.as_of.isoformat()}"
    if scope.scope_id:
        query += f"&scope_id={scope.scope_id}"
    for key, value in sorted(params.items()):
        query += f"&{key}={value}"
    return f"/api/export/{view_id}.csv{query}"


def ok_envelope(
    *,
    view_id: str,
    scope: Scope,
    payload: dict[str, Any],
    quality: Quality,
    data_as_of: date,
    source_modules: list[str],
    row_count: int,
    params: dict[str, Any],
    ledger: sqlite3.Connection | None = None,
    truncated: bool = False,
    extra_caveats: list[str] | None = None,
    basis: str | None = None,
) -> ViewEnvelope:
    """§3.1. Everything needed to judge the numbers, beside the numbers.

    `staleness_days` falls back to 0 only when the quality object reports None,
    and `confidence` comes from upstream unchanged — M6 does not grade data, it
    renders the grade. §14.1 rule 3 makes confidence the weakest link rather than
    an average, and that reduction happened in M3.
    """
    view = VIEW_DEFS[view_id]
    return ViewEnvelope(
        view_id=view_id,
        question=view.question,
        as_of=scope.as_of,
        data_as_of=data_as_of,
        staleness_days=quality.worst_staleness_days or 0,
        source_modules=source_modules,
        confidence=quality.confidence,
        coverage_pct=quality.coverage_pct,
        unresolved_pct=quality.unresolved_pct,
        caveats=assemble_caveats(
            quality=quality,
            view_id=view_id,
            basis=basis,
            truncated=truncated,
            extra=extra_caveats,
            holdings_as_of=data_as_of,
        ),
        state=ViewState.OK,
        state_reason=None,
        payload=payload,
        row_count=row_count,
        truncated=truncated,
        computed_at=datetime.now(UTC),
        export_url=export_url(view_id, scope, params),
        upstream_hash=upstream_hash(
            view_id, scope, params, view.requires_fields, ledger
        ),
    )


__all__ = ["export_url", "ok_envelope"]
