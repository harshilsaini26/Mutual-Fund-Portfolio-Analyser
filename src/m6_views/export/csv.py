"""CSV export with a provenance header. MODULE_6.md §13.

**The header is mandatory, and it is the point.** §13.1: provenance in the file
is what makes the number defensible six months later, when the user finds the CSV
in a downloads folder and cannot remember its basis. A bare table of rupees with
no as-of date, no coverage and no caveats is exactly the decontextualised number
this project exists to avoid — and a file outlives the screen it came from.

**The CSV shows what the chart showed** (§13.2), including the same tail
aggregation, so display and export agree. The one exception is the explicit
"export full list" path: when `truncated`, `full=1` bypasses `top_n`, the
filename says `_full`, and the header says so too.

UTF-8 **with BOM**, because Excel opens a BOM-less UTF-8 file as the system
codepage and turns every ₹ into mojibake. Every other consumer ignores it.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.m6_views.envelope import ViewEnvelope
from src.m6_views.format import format_date, format_pct

#: Excel-compatible. `utf-8-sig` writes it; readers that do not want it strip it.
ENCODING = "utf-8-sig"


def provenance_header(env: ViewEnvelope, full: bool = False) -> list[str]:
    """§13.1's comment block, in the spec's order.

    Every line here is also a field on the envelope. §19.x asserts they match —
    a header that drifts from the payload it describes is worse than no header,
    because it is believed.
    """
    lines = [
        f"# View: {env.view_id}",
        f"# Question: {env.question}",
        f"# As of: {env.as_of.isoformat()}",
        f"# Holdings as of: {format_date(env.data_as_of)}"
        f" ({env.staleness_days} days old)",
        f"# Coverage: {format_pct(env.coverage_pct)} of portfolio value"
        f" | Unresolved: {format_pct(env.unresolved_pct)}"
        f" | Confidence: {env.confidence}",
        f"# Source modules: {', '.join(env.source_modules) or 'none'}",
    ]
    if env.truncated and not full:
        lines.append(
            "# Note: smaller rows are grouped, matching the chart."
            " Re-export with full=1 for every row."
        )
    if full:
        lines.append("# Note: full list — this file has rows the chart grouped.")
    if env.caveats:
        lines.append("# Notes:")
        # NEVER truncated (§6.1 rule 5). If there are six, six appear.
        lines.extend(f"#   - {c}" for c in env.caveats)
    lines.append(f"# Generated: {datetime.now().astimezone().isoformat()}")
    return lines


def _cell(value: Any) -> str:
    """`Decimal` as its own digits, never a float. `None` as empty.

    An em dash is right on screen and wrong in a CSV: a spreadsheet reads it as
    text and the column stops being numeric. §9.3's dash is a *rendering* rule,
    and this file is not a rendering.
    """
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def rows_for(env: ViewEnvelope) -> tuple[list[str], list[dict[str, Any]]]:
    """Flatten a payload to (columns, rows). One shape per `chart_type`.

    Charts differ in payload shape and a generic flattener would either miss
    fields or invent a nesting convention nobody reads. This is a small
    dispatch, on purpose.
    """
    payload = env.payload
    if "tiles" in payload:
        return (
            ["key", "label", "value"],
            [
                {"key": t["key"], "label": t["label"], "value": t["value"]}
                for t in payload["tiles"]
            ],
        )
    if "rows" in payload:
        columns = [c["key"] for c in payload.get("columns", [])]
        rows = payload["rows"]
        if not columns and rows:
            columns = list(rows[0])
        return columns, list(rows)
    if "cells" in payload:
        rows = payload["cells"]
        return (list(rows[0]) if rows else []), list(rows)
    if "links" in payload:
        # The Sankey exports its LINKS, not its nodes: a link is the auditable
        # unit — this fund gave you this much of this company — and the nodes
        # are a projection of it.
        labels = {n["id"]: n.get("label", n["id"]) for n in payload["nodes"]}
        return (
            ["scheme_id", "issuer_id", "issuer_name", "exposure_inr"],
            [
                {
                    "scheme_id": link["source"],
                    "issuer_id": link["target"],
                    "issuer_name": labels.get(link["target"], link["target"]),
                    "exposure_inr": link["value"],
                }
                for link in payload["links"]
            ],
        )
    if "curve" in payload:
        return (
            ["issuer_share", "exposure_share"],
            list(payload["curve"]),
        )
    return [], []


def to_csv(env: ViewEnvelope, full: bool = False) -> str:
    """§13. The header block, then the same rows the chart drew."""
    buffer = io.StringIO()
    for line in provenance_header(env, full):
        buffer.write(line + "\n")

    columns, rows = rows_for(env)
    if not columns:
        return buffer.getvalue()

    writer = csv.DictWriter(
        buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({c: _cell(row.get(c)) for c in columns})
    return buffer.getvalue()


def filename_for(env: ViewEnvelope, full: bool = False) -> str:
    """§13.2: a full export is labelled as one, so the two files never get
    confused for each other after the fact."""
    suffix = "_full" if full else ""
    return f"{env.view_id}_{env.as_of.isoformat()}{suffix}.csv"


__all__ = ["ENCODING", "filename_for", "provenance_header", "rows_for", "to_csv"]
