"""Turning an envelope into what a template needs. MODULE_6.md §16.

Three jobs:

**Formatting** (§9) — every figure a reader sees becomes a string here, in
Python. §16.4: the frontend may sort a table and toggle a series; it may not
compute a percentage.

**Geometry** — the heatmap grid and Lorenz path are pixel coordinates, not
figures. §2.1 bans deriving a reported figure; scaling one M3 already computed
into a viewBox is the presentation-only arithmetic it permits.

**Chart selection** — one template per `chart_type`.

The Sankey is the narrow exception: `d3-sankey` needs numbers to compute widths,
so the payload crosses as JSON with Decimals still as strings (§15.2). Every
number the USER sees in that view is still formatted here, including the
accessible table beneath the diagram.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from src.m6_views.envelope import ViewEnvelope
from src.m6_views.format import (
    DASH,
    format_date,
    format_inr,
    format_pct,
    format_staleness,
)
from src.m6_views.registry import VIEW_DEFS
from src.m6_views.serialise import jsonable

#: `chart_type` -> the partial that draws it. A view whose chart type is not here
#: has no way to render, which the startup check would not catch — so
#: `test_every_registered_view_has_a_chart_template` does.
CHART_TEMPLATES = {
    "kpi": "charts/kpi.html",
    "table": "charts/table.html",
    "sankey": "charts/sankey.html",
    "heatmap": "charts/heatmap.html",
    "lorenz": "charts/lorenz.html",
    "echart": "charts/echart.html",
    "fundcard": "charts/fundcard.html",
}

#: ECharts draws every `echart` view; `charts.js` hands it each payload.
#: `{root}` is the site's URL prefix: empty on the server, the repository's
#: name on the public copy (`jobs/publish_site.py`).
ECHART_SCRIPTS = (
    '<script src="{root}/static/vendor/echarts.v6.1.0.min.js"></script>\n'
    '<script src="{root}/static/charts.js"></script>'
)


# --- formatting filters ------------------------------------------------------


def fmt_date(value: date | str | None) -> str:
    if isinstance(value, str):
        value = date.fromisoformat(value)
    return format_date(value)


def fmt_datetime(value: datetime | str | None) -> str:
    """Freshness in the masthead. Minutes, because the page is not live and the
    difference between "12:27" and "12:27:13" is not information."""
    if value is None:
        return DASH
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.strftime("%d %b %Y, %H:%M")


def fmt_pct(value: Decimal | None) -> str:
    return format_pct(value)


def fmt_staleness(value: int | None) -> str:
    return format_staleness(value)


def fmt_inr_full(value: Decimal | None) -> str:
    """Tables and exports use full grouping (§9.1). Never mixed with the compact
    form inside one view — two cells reading "₹1.50 Cr" and "₹1,50,00,000.00"
    are the same number and look like two."""
    return format_inr(value, compact=False)


def fmt_metric(value: Decimal | None) -> str:
    """HHI, effective-N, Gini. Six decimals is what M3 quantized them to; showing
    all six in a tile is noise, and rounding them here is presentation."""
    if value is None:
        return DASH
    return f"{value.quantize(Decimal('0.0001')):f}".rstrip("0").rstrip(".")


def _fmt_value(tile: dict[str, Any], compact: bool) -> str:
    """One dispatch on `kind`, so a figure is formatted in exactly one place.

    A `None` is an em dash in every kind (§9.3) — never a zero. The distinction
    is the product's whole posture: an absent XIRR means nobody computed it, and
    a 0.0% would be a claim that the portfolio returned nothing.
    """
    value = tile.get("value")
    kind = tile.get("kind", "text")
    if value is None:
        return DASH
    if kind == "inr":
        return format_inr(Decimal(str(value)), compact=compact)
    if kind == "inr_signed":
        rendered = format_inr(abs(Decimal(str(value))), compact=compact)
        return f"+{rendered}" if Decimal(str(value)) >= 0 else f"-{rendered}"
    if kind == "pct":
        return format_pct(Decimal(str(value)))
    if kind == "return_ann":
        return format_pct(Decimal(str(value)) * 100, signed=True) + " p.a."
    if kind == "fraction":  # M2's figures are fractions: 0.0842 -> 8.42%
        return format_pct(Decimal(str(value)) * 100, precision=2)
    if kind == "ratio":
        return f"{Decimal(str(value)):.2f}"
    if kind == "metric":
        return fmt_metric(Decimal(str(value)))
    if kind in ("units", "nav"):
        return f"{Decimal(str(value)):,.4f}".rstrip("0").rstrip(".")
    if kind == "date":
        return fmt_date(value)
    if kind == "count":
        return f"{int(value):,}"
    return str(value)


def fmt_tile(tile: dict[str, Any]) -> str:
    """A KPI tile: the compact form, `Rs 1.50 Cr`. §9.1."""
    return _fmt_value(tile, compact=True)


def fmt_cell(tile: dict[str, Any]) -> str:
    """A table cell: full Indian grouping, `Rs 1,50,00,000.00`. §9.1.

    The two exist separately because §9.1's rule is "axis labels and KPI tiles
    use compact; tables and exports use full grouping — **never mix within one
    view**", and a single filter with a default would be mixed by accident on
    the first table somebody adds. Found by a test: the holdings table was
    rendering `Rs 1.00 L` where a statement says `1,00,000.00`, which is the
    one screen a user checks against their own paperwork.
    """
    return _fmt_value(tile, compact=False)


FILTERS = {
    "fmt_date": fmt_date,
    "fmt_datetime": fmt_datetime,
    "fmt_pct": fmt_pct,
    "fmt_staleness": fmt_staleness,
    "fmt_inr_full": fmt_inr_full,
    "fmt_metric": fmt_metric,
    "fmt_tile": fmt_tile,
    "fmt_cell": fmt_cell,
}


# --- geometry ----------------------------------------------------------------

CELL = 64
MARGIN_LEFT = 150
MARGIN_TOP = 70
LABEL_CHARS = 14


def heatmap_grid(env: ViewEnvelope) -> dict[str, Any]:
    """§9.3's matrix, laid out as a lower triangle of scheme pairs.

    The fill is a single-hue ramp on overlap, and **colour is never the only
    channel**: every cell also carries its percentage as text (§10.3), so the
    chart reads correctly in greyscale and to a screen reader through the table
    beneath it.

    An unaligned pair gets a dashed stroke and says how many days apart the two
    disclosures were, because §9.3 forbids presenting a cross-date comparison as
    though it were a same-date one.
    """
    cells_in = env.payload.get("cells", [])
    schemes: list[str] = env.payload.get("schemes", [])
    index = {scheme: i for i, scheme in enumerate(schemes)}

    # A lower triangle, so the last scheme is never a column and the first is
    # never a row. Labelling all n on both axes puts a heading over an empty
    # strip, which reads as a missing cell rather than as an axis that stops.
    columns = [
        {
            "text": scheme[:LABEL_CHARS],
            "x": MARGIN_LEFT + i * CELL + CELL // 2,
            "y": MARGIN_TOP - 12,
        }
        for scheme, i in index.items()
        if i < len(schemes) - 1
    ]
    rows = [
        {
            "text": scheme[:LABEL_CHARS],
            "x": MARGIN_LEFT - 10,
            "y": MARGIN_TOP + i * CELL + CELL // 2 + 4,
        }
        for scheme, i in index.items()
        if i > 0
    ]

    cells = []
    for cell in cells_in:
        a, b = index[cell["scheme_a"]], index[cell["scheme_b"]]
        row, column = max(a, b), min(a, b)
        pct = Decimal(str(cell["overlap_pct"]))
        cells.append(
            {
                "x": MARGIN_LEFT + column * CELL,
                "y": MARGIN_TOP + row * CELL,
                "cx": MARGIN_LEFT + column * CELL + CELL // 2,
                "cy": MARGIN_TOP + row * CELL + CELL // 2 + 4,
                "fill": _ramp(pct),
                "label": format_pct(pct, precision=0),
                "aligned": bool(cell["aligned"]),
                "title": _cell_title(cell, pct),
            }
        )

    span = max(len(schemes), 1)
    return {
        "cells": cells,
        "columns": columns,
        "rows": rows,
        "size": CELL - 4,
        "width": MARGIN_LEFT + span * CELL + 20,
        "height": MARGIN_TOP + span * CELL + 20,
    }


def _cell_title(cell: dict[str, Any], pct: Decimal) -> str:
    """The hover text. Descriptive only — it states the overlap and, when the
    dates differ, that they differ. `PLAN.md` §3.3 applies to tooltips too."""
    base = (
        f"{cell['scheme_a']} and {cell['scheme_b']}: {format_pct(pct)} overlap, "
        f"{cell['common_issuers']} shared companies of {cell['union_issuers']}"
    )
    if cell.get("overlap_value_inr") is not None:
        base += f", {fmt_inr_full(Decimal(str(cell['overlap_value_inr'])))} held by both"
    if not cell["aligned"]:
        base += (
            f". Disclosures are {cell['as_of_gap_days']} days apart, so this is "
            f"an approximate comparison."
        )
    return base


def _ramp(pct: Decimal) -> str:
    """A five-step single-hue ramp. Steps rather than a continuum because the
    eye cannot read a continuous scale off a small square anyway, and the text
    label carries the precision."""
    steps = [
        (Decimal(5), "#eef3f8"),
        (Decimal(15), "#cfe0ef"),
        (Decimal(30), "#9ec4e0"),
        (Decimal(50), "#5b9bd0"),
    ]
    for bound, colour in steps:
        if pct < bound:
            return colour
    return "#2b6ca8"


def lorenz_path(env: ViewEnvelope) -> str:
    """The curve as an SVG path, scaled into a 320x320 box.

    Empty when M3 returned no curve — V1-21 makes it undefined for a pool holding
    a net short. The template renders the metrics without a chart and the
    builder's caveat says why.
    """
    points = env.payload.get("curve", [])
    if not points:
        return ""
    commands = []
    for i, point in enumerate(points):
        x = 20 + Decimal(str(point["issuer_share"])) * 280
        y = 300 - Decimal(str(point["exposure_share"])) * 280
        commands.append(f"{'M' if i == 0 else 'L'}{x:.2f},{y:.2f}")
    return " ".join(commands)


def embeddable_json(payload: dict[str, Any]) -> str:
    """JSON that is safe to place inside a `<script>` element.

    **`json.dumps` escapes quotes and backslashes. It does not escape `<` or
    `/`.** An issuer name containing `</script>` closes the element and
    everything after it parses as HTML — demonstrated live, same-origin with
    `/api/*`, so it could read the whole portfolio and post it anywhere.

    The input is REMOTE: issuer names come from AMC disclosure files fetched
    over the internet, so a hostile or compromised disclosure is the attack.

    The escapes are the same characters to a JSON parser, so this changes the
    encoding and not the data. U+2028 and U+2029 are included because they are
    literal line terminators in JavaScript source.
    """
    return (
        json.dumps(jsonable(payload))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(chr(0x2028), "\\u2028")
        .replace(chr(0x2029), "\\u2029")
    )


def sankey_labels(env: ViewEnvelope) -> dict[str, str]:
    """Node id -> display name, for the accessible table under the diagram."""
    return {n["id"]: n.get("label", n["id"]) for n in env.payload.get("nodes", [])}


# --- assembly ----------------------------------------------------------------


def chart_context(env: ViewEnvelope) -> dict[str, Any]:
    """Everything the chosen partial needs, and nothing it does not.

    A view in a non-`ok` state gets no chart context at all: the container
    renders the placeholder instead of calling the partial, and computing a grid
    for a payload that is `{}` would only produce a shape nobody draws.
    """
    chart_type = VIEW_DEFS[env.view_id].chart_type
    context: dict[str, Any] = {
        "env": env,
        "chart_template": CHART_TEMPLATES[chart_type],
        "labels": {},
        "grid": {},
        "curve_path": "",
        "payload_json": "{}",
    }
    if env.state.value != "ok":
        return context
    if chart_type == "heatmap":
        context["grid"] = heatmap_grid(env)
    elif chart_type == "lorenz":
        context["curve_path"] = lorenz_path(env)
    elif chart_type == "echart":
        # Only what the drawing needs: positions and the labels the reader sees.
        # The table under the chart is rendered here, in Python, from `rows`.
        context["payload_json"] = embeddable_json(
            {"charts": env.payload.get("charts", [])}
        )
    elif chart_type == "sankey":
        context["labels"] = sankey_labels(env)
        # Decimals stay strings across this boundary — §15.2. `sankey.js` parses
        # them only where a pixel width is being computed.
        context["payload_json"] = embeddable_json(env.payload)
    return context


def needs_echarts(envelopes: list[ViewEnvelope]) -> bool:
    """ECharts is 1.1 MB. It loads where a view draws with it and nowhere else."""
    return any(
        VIEW_DEFS[e.view_id].chart_type == "echart" and e.state.value == "ok"
        for e in envelopes
    )


def needs_sankey_script(envelopes: list[ViewEnvelope]) -> bool:
    """d3 is 280 KB. It loads on the pages that draw a Sankey and nowhere else."""
    return any(
        VIEW_DEFS[e.view_id].chart_type == "sankey" and e.state.value == "ok"
        for e in envelopes
    )


__all__ = [
    "CHART_TEMPLATES",
    "ECHART_SCRIPTS",
    "FILTERS",
    "chart_context",
    "embeddable_json",
    "fmt_cell",
    "fmt_tile",
    "heatmap_grid",
    "lorenz_path",
    "needs_echarts",
    "needs_sankey_script",
    "sankey_labels",
]
