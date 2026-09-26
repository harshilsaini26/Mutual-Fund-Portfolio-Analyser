"""Every view builder, imported so registration happens. MODULE_6.md §5.3.

Registration is a decorator run at import time, so a builder nobody imports is a
builder nobody has. Importing them here and then asserting consistency means a
missing import fails the test suite at collection rather than producing a 404 on
a screen nobody is looking at yet.
"""

from __future__ import annotations

from src.m6_views.builders.fund import (  # noqa: F401
    consistency,
    drawdown,
    growth,
    header,
    nav,
    peers,
    performance,
    portfolio,
    returns,
    xray,
)
from src.m6_views.builders.portfolio import (  # noqa: F401
    concentration,
    duplication,
    funds,
    marginal,
    overlap,
    sankey,
    size,
    summary,
)
from src.m6_views.registry import assert_registry_consistent

assert_registry_consistent()
