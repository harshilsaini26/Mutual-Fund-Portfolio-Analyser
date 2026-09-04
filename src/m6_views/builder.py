"""`ViewBuilder` and `Scope` — M6's per-view contract.

MODULE_6.md §5.1, transcribed. Slice Zero — Protocol stub and dataclass, no logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from src.common.types import AnalysisScope, UserId
from src.m6_views.envelope import ViewEnvelope


@dataclass(frozen=True)
class Scope:
    """What a view is being asked about."""

    user_id: UserId
    as_of: date
    scope_type: AnalysisScope  # portfolio|scheme|issuer|sector|manager
    scope_id: str | None = None


class ViewBuilder(Protocol):
    """One builder per view. Builders are independent of each other.

    Frozen in Slice Zero per `BUILD_ORDER.md` R5 so the view catalogue becomes
    parallel work rather than a sequence.
    """

    view_id: str

    def required_sources(self) -> list[str]:
        """Provider methods this builder calls — drives `upstream_hash`.

        Declared rather than inferred, so a cache key cannot silently go stale
        when a builder starts reading a new source.
        """
        ...

    def build(self, scope: Scope, params: dict[str, Any]) -> ViewEnvelope:
        """Must return an envelope in every case, including failure.

        A builder that cannot produce data returns `state='empty'` or
        `'suppressed'` with a `state_reason` — it does not raise past this
        boundary. `PLAN.md` §4.9: degrade one panel, never the screen.
        """
        ...
