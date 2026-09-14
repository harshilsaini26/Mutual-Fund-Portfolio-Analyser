"""Render states and suppression. MODULE_6.md §7 and §3.3.

**A blank chart teaches the user the tool is broken. An explained absence
teaches them how the tool works.** Every non-`ok` envelope carries a
`state_reason` naming what is missing and what produces it; `"No data"` is not
acceptable.

Four states, genuinely different facts:

  ok          data present and above the confidence floor
  empty       nothing computed yet, or the user holds nothing
  suppressed  computed, but below `min_confidence` — a number that would mislead
  error       the builder raised

`suppressed` is for figures that are ACTIVELY misleading, not merely uncertain;
everything else renders with caveats. There is no `hide_low_confidence`
preference (§4.4): a toggle that hides weak data makes a cleaner product that is
quietly less truthful.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.common.types import ViewState
from src.m6_views.builder import Scope
from src.m6_views.envelope import ViewEnvelope

#: §7.1. Ordered so a floor can be compared numerically.
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


def should_suppress(envelope_confidence: str, min_confidence: str) -> bool:
    """§7.1. True when the view's floor is above what the data supports."""
    return (
        CONFIDENCE_ORDER[envelope_confidence] < CONFIDENCE_ORDER[min_confidence]
    )


def _blank(
    view_id: str,
    question: str,
    scope: Scope,
    state: ViewState,
    reason: str,
) -> ViewEnvelope:
    """The shared shape of every non-`ok` envelope.

    `coverage_pct` and `unresolved_pct` are None rather than zero: nothing was
    measured, and zero coverage is a different claim from unmeasured coverage.
    `staleness_days` is 0 because there is no data to be stale — the field is
    non-optional on the contract, and this is the one case where that pinches.
    """
    if not reason.strip():
        raise ValueError(
            f"{view_id}: a {state.value} envelope needs a state_reason naming "
            f"what is missing and what produces it (MODULE_6 §3.3)"
        )
    return ViewEnvelope(
        view_id=view_id,
        question=question,
        as_of=scope.as_of,
        data_as_of=scope.as_of,
        staleness_days=0,
        source_modules=[],
        confidence="low",
        coverage_pct=None,
        unresolved_pct=None,
        caveats=[],
        state=state,
        state_reason=reason,
        payload={},
        row_count=0,
        truncated=False,
        computed_at=datetime.now(UTC),
        export_url="",
        upstream_hash="",
    )


def empty_envelope(
    view_id: str, question: str, scope: Scope, reason: str
) -> ViewEnvelope:
    """§7.3. Nothing to show yet — say what would produce it."""
    return _blank(view_id, question, scope, ViewState.EMPTY, reason)


def suppressed_envelope(
    view_id: str, question: str, scope: Scope, reason: str
) -> ViewEnvelope:
    """§7.3. Computed and withheld — say why, and what is available instead."""
    return _blank(view_id, question, scope, ViewState.SUPPRESSED, reason)


def error_envelope(
    view_id: str, question: str, scope: Scope, exc: Exception
) -> ViewEnvelope:
    """§7.3. The builder raised. Show the error CLASS, never a stack trace.

    `PLAN.md` §4.9: degrade one panel, never the screen. A builder that raises
    past its own boundary would take down the page with it, so the API route
    calls this instead of returning a 500.

    The exception's own message is included because these are single-user,
    loopback-bound, and the reader is the person who can fix it — but the
    traceback is not, because a stack trace in a UI teaches nothing and looks
    like a crash.
    """
    return _blank(
        view_id,
        question,
        scope,
        ViewState.ERROR,
        f"This view could not be built: {type(exc).__name__}. {exc}",
    )


__all__ = [
    "CONFIDENCE_ORDER",
    "empty_envelope",
    "error_envelope",
    "should_suppress",
    "suppressed_envelope",
]
