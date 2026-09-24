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

Two things are mostly absent. **Drill-down** (§12.2) has four targets, and
one exists: Holdings links each scheme to `fund_xray_header`, scoped by
`scope_id`. `company_page`, `sector_detail` and `overlap_detail` wait on data
not loaded yet. The per-view URLs are here so every view is linkable and
bookmarkable (§12.1: "costs nothing at the start and is painful to retrofit");
links into screens that do not exist are not.
**Display preferences** (§4.3's `user_display_pref`) are not built either; the
defaults are §9's.
"""

from __future__ import annotations

import html
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from src.common.types import UserId
from src.m6_views.builder import Scope
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.registry import FUND_PAGE, VIEW_DEFS, VIEW_REGISTRY, catalogue
from src.m6_views.render import (
    ECHART_SCRIPTS,
    FILTERS,
    chart_context,
    needs_echarts,
    needs_sankey_script,
)
from src.m6_views.states import error_envelope

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
STATIC = Path(__file__).resolve().parent.parent / "static"

#: §16.5, and `PLAN.md` §5.10 on metric walls. Three questions, and the section
#: ends "Resist adding a fourth." Everything else is one click away in the nav.
LANDING = ("portfolio_summary", "lookthrough_sankey", "overlap_heatmap")

#: A search is a few words; anything longer is not a query anyone typed.
SEARCH_MAX_CHARS = 80

SANKEY_SCRIPTS = (
    '<script src="{root}/static/vendor/d3.v7.min.js"></script>\n'
    '<script src="{root}/static/vendor/d3-sankey.v0.12.3.min.js"></script>\n'
    '<script src="{root}/static/sankey.js"></script>'
)


def chart_scripts(envelopes: list[ViewEnvelope], root: str = "") -> str:
    """The script tags a page's charts need, and none it does not."""
    return "\n".join(
        tags.format(root=root)
        for tags, needed in (
            (SANKEY_SCRIPTS, needs_sankey_script(envelopes)),
            (ECHART_SCRIPTS, needs_echarts(envelopes)),
        )
        if needed
    )


def templates(root: str = "", static: bool = False) -> Jinja2Templates:
    """Jinja with §9's formatters bound as filters.

    Registering them here rather than calling them in the templates is what
    keeps §16.4 true: a template can render a figure but cannot make one, and
    every conversion from `Decimal` to text happens in `format.py`.

    `root` prefixes every link the templates write: empty for this server, the
    repository's name for the public copy on GitHub Pages, which is served from
    a subdirectory. `static` is that copy: no server behind it, so no portfolio,
    no search API and no fragments (DECISIONS V1-72).
    """
    engine = Jinja2Templates(directory=str(TEMPLATES))
    engine.env.filters.update(FILTERS)
    engine.env.globals.update(root=root, static=static)
    return engine


def fund_context(
    build: Callable[[str, Scope, dict[str, Any]], ViewEnvelope],
    scope: Scope,
    window: str | None = None,
    root: str = "",
    adapt: Callable[[ViewEnvelope], ViewEnvelope] = lambda env: env,
) -> dict[str, Any]:
    """Everything `fund.html` needs for one fund, `registry.FUND_PAGE` in order.

    One function for the server's `/fund` route and the public copy's builder,
    so the two cannot draw a different page. `adapt` is the public copy's hook
    for what it must change before rendering (its export links, what it
    withholds).
    """
    params = {"window": window} if window else {}
    envelopes = [
        adapt(build(view_id, scope, params if view_id == "fund_growth" else {}))
        for view_id in FUND_PAGE
    ]
    detail = adapt(build("fund_xray_header", scope, {}))
    head = envelopes[0]
    name = head.payload.get("name") if head.state.value == "ok" else None
    return {
        "title": name or scope.scope_id,
        "panels": [safe_chart_context(env, scope) for env in envelopes],
        "detail": safe_chart_context(detail, scope),
        "chart_scripts": chart_scripts([*envelopes, detail], root),
    }


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
    search: Any,
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

    def _scope(
        user_id: str, as_of: date | None, scope_id: str | None = None
    ) -> Scope:
        return Scope(
            user_id=UserId(user_id),
            as_of=as_of or date.today(),
            scope_type="portfolio",
            scope_id=scope_id,
        )

    def _shell(user_id: str, as_of: date | None, active: str) -> dict[str, Any]:
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
        as_of: date | None = None,
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
                "chart_scripts": chart_scripts(envelopes),
            },
        )

    @router.get("/fund/{scheme_id}", response_class=HTMLResponse)
    async def fund_page(
        request: Request,
        scheme_id: str,
        user_id: str = Query("USER-01"),
        as_of: date | None = None,
        window: str | None = Query(None),
    ) -> Any:
        """One fund, every picture of it. `registry.FUND_PAGE`, top to bottom."""
        scope = _scope(user_id, as_of, scheme_id)
        return engine.TemplateResponse(
            request,
            "fund.html",
            {
                **_shell(user_id, as_of, active=""),
                **fund_context(
                    lambda v, s, p: build(v, s, p, ledger, warehouse), scope, window
                ),
            },
        )

    @router.get("/fragment/{view_id}", response_class=HTMLResponse)
    async def fragment(
        request: Request,
        view_id: str,
        user_id: str = Query("USER-01"),
        as_of: date | None = None,
        scope_id: str | None = Query(None),
        window: str | None = Query(None),
    ) -> Any:
        """One panel, for a period tab to swap in place. Same builder, macro and
        partial as a full page, so the swapped panel cannot differ from it."""
        if view_id not in VIEW_REGISTRY:
            return HTMLResponse(status_code=404, content="")
        scope = _scope(user_id, as_of, scope_id)
        env = build(view_id, scope, {"window": window} if window else {},
                    ledger, warehouse)
        return engine.TemplateResponse(
            request, "fragment.html", safe_chart_context(env, scope)
        )

    @router.get("/search", response_class=HTMLResponse)
    async def search_page(
        request: Request,
        q: str = Query("", max_length=SEARCH_MAX_CHARS),
        user_id: str = Query("USER-01"),
    ) -> Any:
        return engine.TemplateResponse(
            request,
            "search.html",
            {
                **_shell(user_id, None, active=""),
                "query": q,
                "hits": search(q) if q.strip() else [],
            },
        )

    @router.get("/view/{view_id}", response_class=HTMLResponse)
    async def one_view(
        request: Request,
        view_id: str,
        user_id: str = Query("USER-01"),
        as_of: date | None = None,
        top_n: int | None = Query(None),
        scope: str | None = Query(None),
        scope_id: str | None = Query(None),
    ) -> Any:
        if view_id not in VIEW_REGISTRY:
            return HTMLResponse(
                status_code=404,
                # escaped: `view_id` is a path parameter and this is the one
                # response in M6 built as raw HTML rather than rendered by
                # Jinja, whose autoescaping would have covered it. Reflected
                # XSS otherwise -- `!r` quotes the string, it does not escape
                # it (CodeQL #2).
                content=(
                    f"<p>No view {html.escape(repr(view_id))}. "
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
        view_scope = _scope(user_id, as_of, scope_id)
        env = build(view_id, view_scope, params, ledger, warehouse)
        return engine.TemplateResponse(
            request,
            "page.html",
            {
                **_shell(user_id, as_of, active=view_id),
                **safe_chart_context(env, view_scope),
                "chart_scripts": chart_scripts([env]),
            },
        )

    return router


__all__ = [
    "LANDING",
    "SEARCH_MAX_CHARS",
    "STATIC",
    "TEMPLATES",
    "chart_context",
    "chart_scripts",
    "fund_context",
    "make_router",
    "safe_chart_context",
    "templates",
]
