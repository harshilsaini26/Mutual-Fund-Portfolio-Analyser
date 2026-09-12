"""The HTTP API. MODULE_6.md §15.

**Loopback only, no auth** (§15.3). Single user, bound to 127.0.0.1, remote
access via Tailscale if ever wanted. Adding authentication before any non-
loopback binding is its own decision requiring its own review — not something to
bolt on when the bind address changes.

**A builder that raises must not take down the screen.** `PLAN.md` §4.9 is
"degrade one panel, never the screen", so the route catches and returns an
`error` envelope with the exception's class and message. Never a 500, never a
stack trace: a traceback in a UI teaches nothing and looks like a crash.

**The connections must be opened with `check_same_thread=False`, and every use
takes `DB_LOCK`.** This was got wrong first: the original reasoning was that
`async def` routes run on the event loop's single thread, so the default
same-thread check would be satisfied. It is not — the loop runs in whatever
thread the server started it in, which is never the thread that opened the
database. The first API test failed with a `ProgrammingError` swallowed into an
`error` envelope, which is exactly how a wrong assumption hides when the error
path is well-behaved.

So the opt-out is explicit at the call site (`create_app` opens nothing;
`jobs/serve.py` passes `check_same_thread=False`) and the safety it removes is
put back with a lock. sqlite3's check exists because concurrent use corrupts
cursor state; serialising every request restores that guarantee without
pretending the threading model is simpler than it is. For a single user on
loopback, serialised access costs nothing.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date
from typing import Any

from fastapi import FastAPI, Query, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from src.common.types import UserId
from src.m6_views.api.pages import STATIC, make_router
from src.m6_views.builder import Scope
from src.m6_views.builders import (  # noqa: F401  — import registers the builders
    portfolio,
)
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.export.csv import ENCODING, filename_for, to_csv
from src.m6_views.registry import VIEW_DEFS, VIEW_REGISTRY, catalogue
from src.m6_views.serialise import serialise
from src.m6_views.states import error_envelope

#: Serialises every database use. See the module docstring: the connections are
#: opened with the same-thread check off, and this is what replaces it.
DB_LOCK = threading.Lock()

#: §15.3. Never widen this without the review §15.3 asks for.
BIND_HOST = "127.0.0.1"
BIND_PORT = 8765


def build_view(
    view_id: str,
    scope: Scope,
    params: dict[str, Any],
    ledger: sqlite3.Connection,
    warehouse: sqlite3.Connection,
) -> ViewEnvelope:
    """Construct the builder, run it, and never let it raise past here.

    Every builder takes one `Deps`, so this does not need to know which view
    wants which provider — that branch is what forced `Deps` into existence.
    """
    question = VIEW_DEFS[view_id].question
    try:
        with DB_LOCK:
            builder = VIEW_REGISTRY[view_id](Deps.over(ledger, warehouse))
            return builder.build(scope, params)
    except Exception as exc:
        return error_envelope(view_id, question, scope, exc)


def create_app(
    ledger: sqlite3.Connection, warehouse: sqlite3.Connection
) -> FastAPI:
    """A factory, so tests drive the same app over temporary databases."""
    app = FastAPI(title="MF look-through", docs_url="/api/docs")
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    def _scope(user_id: str, as_of: str | None, scope_id: str | None) -> Scope:
        return Scope(
            user_id=UserId(user_id),
            as_of=date.fromisoformat(as_of) if as_of else date.today(),
            scope_type="portfolio",
            scope_id=scope_id,
        )

    @app.get("/api/views")
    async def list_views() -> list[dict[str, Any]]:
        """§15.1. The catalogue, in landing order."""
        return catalogue()

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        """§15.3: report freshness so the UI can say "last updated" honestly
        rather than implying the data is live."""
        with DB_LOCK:
            row = ledger.execute(
                "SELECT max(as_of), max(computed_at) FROM portfolio_summary"
            ).fetchone()
            disclosures = warehouse.execute(
                "SELECT count(*), max(as_of_date) FROM holding_disclosure"
                " WHERE is_current = 1"
            ).fetchone()
        return {
            "status": "ok",
            "lookthrough_as_of": row[0] if row else None,
            "lookthrough_computed_at": row[1] if row else None,
            "current_disclosures": disclosures[0] if disclosures else 0,
            "latest_disclosure": disclosures[1] if disclosures else None,
            "views_registered": len(VIEW_REGISTRY),
        }

    @app.get("/api/views/{view_id}")
    async def get_view(
        view_id: str,
        user_id: str = Query("USER-01"),
        as_of: str | None = Query(None),
        scope_id: str | None = Query(None),
        top_n: int | None = Query(None),
        scope: str | None = Query(None),
    ) -> JSONResponse:
        if view_id not in VIEW_REGISTRY:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "unknown_view",
                    "detail": f"No view {view_id!r}. See /api/views.",
                    "available": sorted(VIEW_REGISTRY),
                },
            )
        params: dict[str, Any] = {}
        if top_n is not None:
            params["top_n"] = top_n
        if scope is not None:
            params["scope"] = scope
        envelope = build_view(
            view_id, _scope(user_id, as_of, scope_id), params, ledger, warehouse
        )
        return JSONResponse(content=serialise(envelope))

    @app.get("/api/export/{view_id}.csv")
    async def export_csv(
        view_id: str,
        user_id: str = Query("USER-01"),
        as_of: str | None = Query(None),
        scope_id: str | None = Query(None),
        top_n: int | None = Query(None),
        scope: str | None = Query(None),
        full: int = Query(0),
    ) -> Response:
        """§13. Every view exports — §2.5 calls it a trust feature and an
        escape hatch, signalling the data is not trapped in this UI."""
        if view_id not in VIEW_REGISTRY:
            return JSONResponse(
                status_code=404, content={"error": "unknown_view"}
            )
        params: dict[str, Any] = {}
        if scope is not None:
            params["scope"] = scope
        # §13.2's exception: `full=1` bypasses `top_n` so the escape hatch
        # returns every row, and the header and filename both say it did.
        params["top_n"] = 10**9 if full else (top_n if top_n is not None else 40)
        envelope = build_view(
            view_id, _scope(user_id, as_of, scope_id), params, ledger, warehouse
        )
        body = to_csv(envelope, full=bool(full))
        return Response(
            content=body.encode(ENCODING),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="'
                    f'{filename_for(envelope, full=bool(full))}"'
                )
            },
        )

    # The HTML surface, over the same envelopes the JSON routes return. Mounted
    # last so `/api/*` always wins: a view named `views` could otherwise be
    # shadowed by the page router's `/view/{view_id}`.
    app.include_router(make_router(ledger, warehouse, build_view))
    return app


__all__ = ["BIND_HOST", "BIND_PORT", "build_view", "create_app"]
