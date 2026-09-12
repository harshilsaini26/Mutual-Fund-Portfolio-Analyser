"""The HTML surface. MODULE_6.md §16.

Server-rendered Jinja over the same envelopes `/api/views/{view_id}` returns.
There is no second data path: the page and the JSON come from one `build_view`
call each, so a chart cannot show a figure the API disagrees with.

**Every chart renders inside `view_container`** (§16.3). The spec enforces that
with an ESLint rule over React components; here the macro is the only thing that
opens a `section.view`, and `tests/unit/test_m6_render.py` asserts the property
on the rendered HTML — every `[data-chart]` element must be a descendant of a
`section.view`. Testing the output rather than the source means the rule survives
a refactor that moves the templates around.

Two things are deliberately absent. **Drill-down** (§12.2) has targets —
`company_page`, `sector_detail`, `fund_xray_header`, `overlap_detail` — and none
of them exists, because none of M2 or M5 does. The per-view URLs are here so
every view is linkable and bookmarkable (§12.1: "costs nothing at the start and
is painful to retrofit"); the links into screens that do not exist are not.
**Display preferences** (§4.3's `user_display_pref`) are not built either; the
defaults are §9's.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from src.common.types import UserId
from src.m6_views.builder import Scope
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.registry import VIEW_DEFS, VIEW_REGISTRY, catalogue
from src.m6_views.render import FILTERS, chart_context, needs_sankey_script
from src.m6_views.states import error_envelope

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
STATIC = Path(__file__).resolve().parent.parent / "static"

#: §16.5, and `PLAN.md` §5.10 on metric walls. Three questions, and the section
#: ends "Resist adding a fourth." Everything else is one click away in the nav.
LANDING = ("portfolio_summary", "lookthrough_sankey", "overlap_heatmap")

SANKEY_SCRIPTS = (
    '<script src="/static/vendor/d3.v7.min.js"></script>\n'
    '<script src="/static/vendor/d3-sankey.v0.12.3.min.js"></script>\n'
    '<script src="/static/sankey.js"></script>'
)


def templates() -> Jinja2Templates:
    """Jinja with §9's formatters bound as filters.

    Registering them here rather than calling them in the templates is what
    keeps §16.4 true: a template can render a figure but cannot make one, and
    every conversion from `Decimal` to text happens in `format.py`.
    """
    engine = Jinja2Templates(directory=str(TEMPLATES))
    engine.env.filters.update(FILTERS)
    return engine


def safe_chart_context(env: ViewEnvelope, scope: Scope) -> dict[str, Any]:
    """`chart_context`, inside the same boundary the builder has.

    `build_view` catches everything a builder raises and returns an `error`
    envelope — and then the route called `chart_context(env)` unguarded, in a
    list comprehension over all three landing panels. `heatmap_grid` indexes
    `index[cell["scheme_a"]]` and the formatters coerce with
    `Decimal(str(...))`, so one malformed stored payload took down the page
    carrying the summary and the Sankey with it.

    `PLAN.md` §4.9 is "degrade one panel, never the screen", and the boundary
    stopped one call short of that.
    """
    try:
        return chart_context(env)
    except Exception as exc:
        return chart_context(
            error_envelope(env.view_id, env.question, scope, exc)
        )


def make_router(
    ledger: sqlite3.Connection,
    warehouse: sqlite3.Connection,
    build: Any,
    health: Any,
) -> APIRouter:
    """`build` is `app.build_view` and `health` is `app.health_snapshot`, both
    passed in rather than imported.

    They carry the lock and the never-raise boundary, and taking them as
    arguments keeps this module free of the connection plumbing — the pages
    need envelopes, not databases. It is also what keeps `app -> pages` a
    one-way import: `pages` importing back from `app` is a cycle, since `app`
    imports this module to mount the router.
    """
    router = APIRouter()
    engine = templates()

    def _scope(user_id: str, as_of: str | None) -> Scope:
        return Scope(
            user_id=UserId(user_id),
            as_of=date.fromisoformat(as_of) if as_of else date.today(),
            scope_type="portfolio",
        )

    def _shell(user_id: str, as_of: str | None, active: str) -> dict[str, Any]:
        query = f"?user_id={user_id}"
        if as_of:
            query += f"&as_of={as_of}"
        return {
            "catalogue": catalogue(),
            "health": health(ledger, warehouse),
            "qs": query,
            "active": active,
        }

    @router.get("/", response_class=HTMLResponse)
    async def landing(
        request: Request,
        user_id: str = Query("USER-01"),
        as_of: str | None = Query(None),
    ) -> Any:
        scope = _scope(user_id, as_of)
        envelopes: list[ViewEnvelope] = [
            build(view_id, scope, {}, ledger, warehouse) for view_id in LANDING
        ]
        return engine.TemplateResponse(
            request,
            "landing.html",
            {
                **_shell(user_id, as_of, active=""),
                "panels": [safe_chart_context(env, scope) for env in envelopes],
                "chart_scripts": (
                    SANKEY_SCRIPTS if needs_sankey_script(envelopes) else ""
                ),
            },
        )

    @router.get("/view/{view_id}", response_class=HTMLResponse)
    async def one_view(
        request: Request,
        view_id: str,
        user_id: str = Query("USER-01"),
        as_of: str | None = Query(None),
        top_n: int | None = Query(None),
        scope: str | None = Query(None),
    ) -> Any:
        if view_id not in VIEW_REGISTRY:
            return HTMLResponse(
                status_code=404,
                content=(
                    f"<p>No view {view_id!r}. "
                    f"Known views: {', '.join(sorted(VIEW_DEFS))}.</p>"
                ),
            )
        params: dict[str, Any] = {}
        if top_n is not None:
            params["top_n"] = top_n
        if scope is not None:
            params["scope"] = scope
        # Named apart from the `scope` query parameter, which is the exposure
        # pool ("equity", "all") and not an analysis scope at all.
        view_scope = _scope(user_id, as_of)
        env = build(view_id, view_scope, params, ledger, warehouse)
        return engine.TemplateResponse(
            request,
            "page.html",
            {
                **_shell(user_id, as_of, active=view_id),
                **safe_chart_context(env, view_scope),
                "chart_scripts": (
                    SANKEY_SCRIPTS if needs_sankey_script([env]) else ""
                ),
            },
        )

    return router


__all__ = [
    "LANDING",
    "STATIC",
    "TEMPLATES",
    "chart_context",
    "make_router",
    "safe_chart_context",
    "templates",
]
