"""Server-side aggregation. MODULE_6.md §11.2.

**Do not ship 350 nodes and hide 310 in the browser.** It wastes payload, and it
makes export inconsistent with display — the CSV would contain rows the chart
never showed, which breaks the audit trail `PLAN.md` §4.2 is built on. The
aggregation happens here, once, and both the chart and the CSV read the result.

`downsample_lttb` (§11.3) is **not** here. It is for time series, and the V1
launch surface has no time-series view — nothing would call it. It lands with the
first chart that plots a series, along with §11.3's assertion that a drawdown
trough survives the downsample.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import TypeVar

T = TypeVar("T")

#: §11.1's budgets. Above these a chart stops being readable before it stops
#: being fast, which is why they are low.
SANKEY_NODE_CAP = 40
TREEMAP_CELL_CAP = 100
HEATMAP_CELL_CAP = 400


def aggregate_tail(
    rows: Sequence[T],
    top_n: int,
    value_of: Callable[[T], Decimal],
    keep: Callable[[T], bool] | None = None,
) -> tuple[list[T], list[T]]:
    """Split into what is shown and what is folded into `__OTHERS__`.

    Returns `(head, tail)` rather than a pre-built others-row, because what the
    others-row should look like differs per chart — a Sankey node and a treemap
    cell are not the same shape — while the *split* is identical. The caller
    sums the tail with `value_of`.

    `keep` pins rows into the head regardless of rank. That exists for one
    reason: Appendix A requires `__UNRESOLVED__` and `__NO_DISCLOSURE__` to stay
    **visible nodes, pinned outside `top_n`**. They are usually small, so plain
    tail aggregation would fold exactly the missing mass the user most needs to
    see into a bucket labelled "smaller holdings" — §20's runbook lists it as a
    known failure mode.

    Callers pass rows already sorted. Sorting here would hide whether they were,
    and `exposure_inr` is `DECIMAL_TEXT` — ordering it is the V1-18 trap.
    """
    if keep is None:
        head, tail = list(rows[:top_n]), list(rows[top_n:])
        return head, tail

    pinned = [r for r in rows if keep(r)]
    rest = [r for r in rows if not keep(r)]
    return pinned + rest[:top_n], rest[top_n:]


def tail_total(tail: Sequence[T], value_of: Callable[[T], Decimal]) -> Decimal:
    """Python `Decimal`, never `sum()` over a SQL aggregate — invariant 1.

    §19.5 asserts `Σ head + tail_total == Σ everything`: aggregation may reduce
    what is drawn, never what is counted.
    """
    return sum((value_of(r) for r in tail), Decimal(0))


def others_label(n: int) -> str:
    """§11.2's label. Descriptive and countable — "smaller holdings" alone would
    not tell the reader how much was folded away."""
    return f"{n} smaller holding{'s' if n != 1 else ''}"


__all__ = [
    "HEATMAP_CELL_CAP",
    "SANKEY_NODE_CAP",
    "TREEMAP_CELL_CAP",
    "aggregate_tail",
    "others_label",
    "tail_total",
]
