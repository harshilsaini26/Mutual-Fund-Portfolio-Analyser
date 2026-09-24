"""`ViewEnvelope` — the contract every M6 view returns.

MODULE_6.md §3.1, transcribed. Slice Zero — dataclass only, no logic.

`BUILD_ORDER.md` R5: once this and `assemble_caveats` are frozen, every
individual view is an independent unit of work. M6 has the highest leaf ratio in
the corpus (41% dangling concepts) — that is the signature of a list, not a
structure, and the views can be built in whatever order a slice needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from src.common.types import ViewState


@dataclass(frozen=True)
class ViewEnvelope:
    """One answer to one question, with everything needed to judge it.

    Provenance and quality fields are ALL REQUIRED, with no defaults: a default
    would let a view omit its staleness by accident, and §4.3 says staleness is
    displayed, never hidden.

    `caveats` are user-facing sentences assembled upstream and passed through
    unchanged — not flags, not log lines. A caveat that exists upstream and is
    missing here is a bug (§19.2 tests it), e.g.:

      "Holdings as of 31 Jul 2026; 43 days stale."
      "1.4% of your exposure could not be identified to a company."

    `state_reason` is required whenever `state` is not `ok`, and "No data" is
    not an acceptable one — name what is missing and what produces it.

    Every string here is descriptive, never prescriptive (§3.3). CI lints it.
    """

    # identity
    view_id: str
    question: str  # the ONE question this view answers

    # provenance — ALL REQUIRED, no defaults
    as_of: date  # the analysis date
    data_as_of: date  # may differ: holdings staleness
    staleness_days: int
    source_modules: list[str]  # e.g. ['m3', 'm1', 'm0']

    # quality — ALL REQUIRED
    confidence: str  # high|medium|low
    coverage_pct: Decimal | None  # None = not applicable to this view
    unresolved_pct: Decimal | None
    caveats: list[str]  # RENDERED, not logged

    # state
    state: ViewState
    state_reason: str | None  # required when state != ok

    # payload
    payload: dict[str, Any]

    # audit
    row_count: int
    truncated: bool  # True when M6 aggregated an 'others' bucket
    computed_at: datetime
    export_url: str  # CSV on every view — a trust feature and an escape hatch
