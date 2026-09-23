"""The view registry. MODULE_6.md §4.1 and §5.3.

`VIEW_DEFS` is the catalogue; `VIEW_REGISTRY` is the set of builders that exist.
**A startup check asserts the two match exactly**, both ways: a definition with
no builder is a screen that cannot render, a builder with no definition is code
nothing routes to. §5.3 calls a mismatch a deployment error.

**`question` is a required field**, a forcing-function rather than
documentation: §2.3 says *"if the question can't be stated in a sentence, the
view shouldn't exist"*, and a view without one cannot be constructed.

**Only the views that can be built are here** — nine of §8.1's eleven. The
other two need M5's sector taxonomy and company data, and are ABSENT
rather than registered with a stub returning empty: an absent view is an
honest gap the consistency check enforces, where a registered one that always
returns `empty` is a broken feature pretending to be a data problem.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from src.m6_views.builder import ViewBuilder

if TYPE_CHECKING:
    from src.m6_views.deps import Deps


@dataclass(frozen=True)
class ViewDef:
    """§4.1's `view_definition` row, as code. The database copy is seeded from
    this, never the other way round — the catalogue is a fact about the code."""

    view_id: str
    view_name: str
    module_source: str  # m1|m2|m3|m4|m5
    question: str  # the ONE question
    chart_type: str
    default_scope: str
    requires_fields: list[str]  # provider methods needed
    drill_targets: dict[str, str] | None = None
    min_confidence: str = "low"
    supports_export: bool = True
    sort_order: int = 0

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise ValueError(
                f"{self.view_id}: a view that cannot state its question does "
                f"not get registered (MODULE_6 §2.3)"
            )


#: §8.1, restricted to what M1 and M3 can serve today. `sort_order` is the
#: landing surface's reading order: §16.5's three questions first — where do I
#: stand, what do I own, what is duplicated — then everything else one click
#: down. §16.5 says to resist adding a fourth to the top; the ordering is how
#: that resistance is expressed rather than a preference.
VIEW_DEFS: dict[str, ViewDef] = {
    "portfolio_summary": ViewDef(
        view_id="portfolio_summary",
        view_name="Portfolio summary",
        module_source="m3",
        question="Where do I stand?",
        chart_type="kpi",
        default_scope="portfolio",
        requires_fields=["m3.summary", "m3.exposures"],
        drill_targets={"scheme": "fund_list"},
        sort_order=10,
    ),
    "lookthrough_sankey": ViewDef(
        view_id="lookthrough_sankey",
        view_name="Look-through exposure",
        module_source="m3",
        question="What do I actually own, beneath the funds?",
        chart_type="sankey",
        default_scope="portfolio",
        requires_fields=["m3.lookthrough", "m3.contributions"],
        drill_targets={"issuer": "lookthrough_sankey", "scheme": "fund_list"},
        sort_order=20,
    ),
    "overlap_heatmap": ViewDef(
        view_id="overlap_heatmap",
        view_name="Fund overlap",
        module_source="m3",
        question="Am I paying twice for the same thing?",
        chart_type="heatmap",
        default_scope="portfolio",
        requires_fields=["m3.overlap_matrix"],
        drill_targets={"scheme": "fund_list"},
        sort_order=30,
    ),
    "duplication_summary": ViewDef(
        view_id="duplication_summary",
        view_name="Duplication",
        module_source="m3",
        question="How much of my money is doubled up?",
        chart_type="kpi",
        default_scope="portfolio",
        requires_fields=["m3.duplication"],
        sort_order=40,
    ),
    "concentration_curve": ViewDef(
        view_id="concentration_curve",
        view_name="Concentration",
        module_source="m3",
        question="How concentrated am I really?",
        chart_type="lorenz",
        default_scope="portfolio",
        requires_fields=["m3.concentration", "m3.exposures"],
        sort_order=50,
    ),
    "mcap_allocation": ViewDef(
        view_id="mcap_allocation",
        view_name="Size profile",
        module_source="m3",
        question="What is my size profile?",
        chart_type="table",
        default_scope="portfolio",
        requires_fields=["m3.tilts", "m3.summary"],
        sort_order=55,
    ),
    "marginal_contribution": ViewDef(
        view_id="marginal_contribution",
        view_name="What each fund adds",
        module_source="m3",
        question="What does each fund add?",
        chart_type="table",
        default_scope="portfolio",
        requires_fields=["m3.marginal", "m3.summary"],
        drill_targets={"scheme": "fund_xray_header"},
        sort_order=45,
    ),
    "fund_list": ViewDef(
        view_id="fund_list",
        view_name="Holdings",
        module_source="m1",
        question="What do I hold?",
        chart_type="table",
        default_scope="portfolio",
        requires_fields=["m1.positions"],
        drill_targets={"scheme": "lookthrough_sankey"},
        sort_order=60,
    ),
    "fund_xray_header": ViewDef(
        view_id="fund_xray_header",
        view_name="Fund x-ray",
        module_source="m2",
        question="How has this fund done, and against what?",
        chart_type="table",
        default_scope="scheme",
        requires_fields=["m2.fund_windows"],
        sort_order=70,
    ),
}

BuilderT = TypeVar("BuilderT", bound=ViewBuilder)

#: view_id -> the builder class, which is exactly a `Deps -> ViewBuilder`
#: callable. Typed as the callable rather than as `type[ViewBuilder]` because
#: constructing a Protocol type is not something mypy can check — and what the
#: router actually needs is "give me a builder for these deps".
VIEW_REGISTRY: dict[str, Callable[[Deps], ViewBuilder]] = {}


def register(builder_cls: type[BuilderT]) -> type[BuilderT]:
    """§5.3. Registration is at import time, via decorator."""
    view_id = builder_cls.view_id
    if view_id in VIEW_REGISTRY:
        raise ValueError(f"{view_id} is registered twice")
    VIEW_REGISTRY[view_id] = builder_cls
    return builder_cls


def assert_registry_consistent() -> None:
    """§5.3. Both directions, and the message names which way it failed.

    Called at import of `src.m6_views.builders`, so a mismatch fails the test
    suite rather than the screen. The two failures mean different things and a
    combined "sets differ" message would send the reader looking in the wrong
    place.
    """
    defined, built = set(VIEW_DEFS), set(VIEW_REGISTRY)
    if missing_builder := defined - built:
        raise RuntimeError(
            f"view_definition rows with no builder: {sorted(missing_builder)}. "
            f"Either build them or remove the definition — a defined view that "
            f"cannot render is a broken screen."
        )
    if missing_def := built - defined:
        raise RuntimeError(
            f"builders with no view_definition: {sorted(missing_def)}. "
            f"Nothing routes to them."
        )


def seed_view_definitions(conn: sqlite3.Connection) -> int:
    """Write `VIEW_DEFS` into Zone A. §4.1: seeded from code.

    Replace rather than merge: the catalogue is derived from the code, so a view
    deleted here must disappear there. Same rule as every other derived table in
    this project (`CLAUDE.md` invariant 10), and the same reason V1-18 and V1-20
    both record — an upsert leaves behind what the new set dropped.
    """
    conn.execute("DELETE FROM view_definition WHERE is_builtin = 1")
    for view in VIEW_DEFS.values():
        conn.execute(
            "INSERT INTO view_definition ("
            " view_id, view_name, module_source, question, chart_type,"
            " default_scope, requires_fields, drill_targets, min_confidence,"
            " supports_export, is_builtin, sort_order"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
            (
                view.view_id, view.view_name, view.module_source, view.question,
                view.chart_type, view.default_scope,
                json.dumps(view.requires_fields),
                json.dumps(view.drill_targets) if view.drill_targets else None,
                view.min_confidence, 1 if view.supports_export else 0,
                view.sort_order,
            ),
        )
    conn.commit()
    return len(VIEW_DEFS)


def catalogue() -> list[dict[str, Any]]:
    """`GET /api/views`. Ordered by `sort_order`, which is the landing order."""
    return [
        {
            "view_id": v.view_id,
            "view_name": v.view_name,
            "question": v.question,
            "chart_type": v.chart_type,
            "module_source": v.module_source,
            "default_scope": v.default_scope,
            "min_confidence": v.min_confidence,
            "supports_export": v.supports_export,
            "drill_targets": v.drill_targets or {},
        }
        for v in sorted(VIEW_DEFS.values(), key=lambda d: (d.sort_order, d.view_id))
    ]


__all__ = [
    "VIEW_DEFS",
    "VIEW_REGISTRY",
    "ViewDef",
    "assert_registry_consistent",
    "catalogue",
    "register",
    "seed_view_definitions",
]
