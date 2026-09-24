"""Envelope serialisation. MODULE_6.md §15.2.

**Every Decimal goes over the wire as a string.** JSON has one number type and it
is a double, so serialising `Decimal("133164.47")` as a JSON number reintroduces
in the last three feet exactly the precision problem the entire backend was built
to avoid — `DECIMAL_TEXT`, the adapter registry, the ban on SQL aggregation, all
of it. A string crosses intact and the renderer formats it.

The shape groups the envelope's flat fields into `provenance`, `quality` and
`meta`. That is §15.2's, and it is deliberate: a client destructuring
`envelope.quality` gets coverage, confidence and caveats together, so dropping
one is a visible omission rather than a forgotten field.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from src.m6_views.envelope import ViewEnvelope


def jsonable(value: Any) -> Any:
    """Recursively make a payload JSON-safe without losing precision.

    `Decimal` -> string, dates -> ISO. Containers are walked because payloads
    are nested — a Sankey's links are a list of dicts of Decimals, and a
    top-level conversion would miss every one of them.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    return value


def serialise(env: ViewEnvelope) -> dict[str, Any]:
    """§15.2. The one wire format, used by the API and by JSON export."""
    return {
        "view_id": env.view_id,
        "question": env.question,
        "provenance": {
            "as_of": env.as_of.isoformat(),
            "data_as_of": env.data_as_of.isoformat(),
            "staleness_days": env.staleness_days,
            "source_modules": env.source_modules,
            "computed_at": env.computed_at.isoformat(),
        },
        "quality": {
            "confidence": env.confidence,
            "coverage_pct": (
                str(env.coverage_pct) if env.coverage_pct is not None else None
            ),
            "unresolved_pct": (
                str(env.unresolved_pct)
                if env.unresolved_pct is not None
                else None
            ),
            "caveats": env.caveats,
        },
        "state": (
            env.state.value if hasattr(env.state, "value") else env.state
        ),
        "state_reason": env.state_reason,
        "payload": jsonable(env.payload),
        "meta": {
            "row_count": env.row_count,
            "truncated": env.truncated,
            "export_url": env.export_url,
        },
    }


__all__ = ["jsonable", "serialise"]
