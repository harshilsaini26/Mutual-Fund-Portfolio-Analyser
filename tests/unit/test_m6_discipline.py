"""The two rules that keep M6 honest. MODULE_6.md §19.3 and §19.6.

Neither test checks a feature. They check that a property of the code stays
true as it grows, which is the only kind of check that survives contact with a
future contributor who has not read the spec.

**§19.3 — no computation in the view layer.** A number derived in a builder is a
second source of truth that nobody can reconcile against the first, and it will
not appear in any export, audit or reconciliation. §2.1's example of the mistake
is literally `pct = holding.value / total * 100`. The check is a static scan for
division, which is imperfect and catches the common case; §19.3 says so and pairs
it with review discipline.

**§19.6 — descriptive language only.** `PLAN.md` §3.3. The product states what is
true about the data and never what to do about it. Two funds holding the same
company is often deliberate, a concentrated portfolio is sometimes the whole
point, and this product does not know which. The lint runs over every string that
reaches a user.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.types import IssuerId, SchemeId, UserId
from src.m0_data.schema.apply import apply_migrations
from src.m1_ledger.db import apply_ledger_schema, connect_ledger
from src.m3_lookthrough.engine import IssuerWeight, Position, compute_lookthrough
from src.m3_lookthrough.persist import save_lookthrough
from src.m6_views.builder import Scope
from src.m6_views.builders import portfolio  # noqa: F401  — registers builders
from src.m6_views.deps import Deps
from src.m6_views.registry import VIEW_DEFS, VIEW_REGISTRY

ROOT = Path(__file__).resolve().parents[2]
BUILDERS = ROOT / "src" / "m6_views" / "builders"


# --- §19.3 no computation in the view layer ----------------------------------


def _builder_files() -> list[Path]:
    return sorted(p for p in BUILDERS.rglob("*.py") if p.name != "__init__.py")


def test_there_are_builders_to_check() -> None:
    """A static check over an empty set passes vacuously and says nothing."""
    assert len(_builder_files()) >= 6


@pytest.mark.parametrize("path", _builder_files(), ids=lambda p: p.name)
def test_no_builder_divides_a_provider_sourced_value(path: Path) -> None:
    """§19.3. Division is how a derived percentage gets made.

    The Lorenz curve is the case that proves the rule: its points are cumulative
    shares, so computing them here would need two divisions. `lorenz_points`
    lives in `src/m3_lookthrough/concentration.py` for exactly that reason, and
    this test is what stopped it being written in the builder.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    divisions = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div | ast.FloorDiv)
    ]
    assert not divisions, (
        f"{path.name} divides at line(s) {divisions}. A number derived in a "
        f"view cannot be audited — move it to the module that owns it."
    )


@pytest.mark.parametrize("path", _builder_files(), ids=lambda p: p.name)
def test_no_builder_uses_a_float(path: Path) -> None:
    """`CLAUDE.md` invariant 1 reaches the view layer too. A float literal in a
    builder means somebody did arithmetic on money and left the boundary."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    floats = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    ]
    assert not floats, f"{path.name} has float literal(s) at {floats}"


@pytest.mark.parametrize("path", _builder_files(), ids=lambda p: p.name)
def test_no_builder_reaches_past_its_providers(path: Path) -> None:
    """§1.3 rule 2: no direct SQL against analytics tables.

    A builder that can write `SELECT` has stopped depending on a contract and
    started depending on a schema, and the next migration breaks a screen
    instead of failing a test.
    """
    source = path.read_text(encoding="utf-8")
    for verb in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
        assert verb not in source, f"{path.name} contains raw SQL ({verb.strip()})"


# --- §19.6 descriptive language only -----------------------------------------

#: §19.6, verbatim. Every one is investment advice rather than a description.
PRESCRIPTIVE_PATTERNS = [
    r"\byou should\b",
    r"\bwe recommend\b",
    r"\bconsider (buying|selling|switching)\b",
    r"\bbetter (choice|option)\b",
    r"\bavoid\b",
    r"\bbest fund\b",
    r"\btoo (risky|concentrated|expensive)\b",
    r"\bought to\b",
]

USER = UserId("USER-01")
AS_OF = date(2026, 9, 4)
JULY = date(2026, 7, 31)
S1 = SchemeId("S1")


@pytest.fixture
def populated(tmp_path: Path) -> Deps:
    """A minimal but non-empty store, so the `ok` strings are exercised too —
    an empty database only produces `state_reason`s."""
    from src.common.decimals import connect

    warehouse_db = str(tmp_path / "warehouse.db")
    apply_migrations(warehouse_db)
    warehouse: sqlite3.Connection = connect(warehouse_db)
    ledger = connect_ledger(str(tmp_path / "personal.db"), key="test-key")
    apply_ledger_schema(ledger)
    result = compute_lookthrough(
        [Position(S1, Decimal("100000"))],
        {
            S1: [
                IssuerWeight(IssuerId("ACME"), Decimal("60"), "equity"),
                IssuerWeight(IssuerId("BETA"), Decimal("40"), "equity"),
            ]
        },
        AS_OF,
    )
    save_lookthrough(ledger, USER, AS_OF, result, {S1: JULY})
    return Deps.over(ledger, warehouse)


def all_user_facing_strings(deps: Deps) -> list[str]:
    """Every string that can reach a screen: questions, names, caveats, reasons,
    tile and column labels, and any prose a payload carries."""
    out: list[str] = []
    for view in VIEW_DEFS.values():
        out.extend([view.question, view.view_name])

    scope = Scope(user_id=USER, as_of=AS_OF, scope_type="portfolio")
    for view_id in VIEW_REGISTRY:
        env = VIEW_REGISTRY[view_id](deps).build(scope, {})
        out.extend(env.caveats)
        if env.state_reason:
            out.append(env.state_reason)
        for key in ("tiles", "columns"):
            for item in env.payload.get(key, []):
                out.append(str(item.get("label", "")))
        if "definition" in env.payload:
            out.append(str(env.payload["definition"]))
    return [s for s in out if s]


def test_there_are_strings_to_lint(populated: Deps) -> None:
    assert len(all_user_facing_strings(populated)) > 20


def test_no_prescriptive_language(populated: Deps) -> None:
    """`PLAN.md` §3.3. The product describes; it does not advise."""
    offences = [
        (pattern, text)
        for text in all_user_facing_strings(populated)
        for pattern in PRESCRIPTIVE_PATTERNS
        if re.search(pattern, text, re.I)
    ]
    assert not offences, f"prescriptive language: {offences}"


def test_every_state_reason_names_something_actionable(tmp_path: Path) -> None:
    """§7.3: `state_reason` must name the fix. "No data" is not acceptable, and
    a reason nobody can act on is the same thing at greater length.

    Counted, not just asserted. A loop that skips everything passes silently,
    which is the failure the M3 mutation harness had in V1.6 — an un-run check
    reported as a passing one.
    """
    from src.common.decimals import connect

    warehouse_db = str(tmp_path / "bare_warehouse.db")
    apply_migrations(warehouse_db)
    empty = connect_ledger(str(tmp_path / "bare.db"), key="test-key")
    apply_ledger_schema(empty)

    scope = Scope(user_id=USER, as_of=AS_OF, scope_type="portfolio")
    deps = Deps.over(empty, connect(warehouse_db))
    checked = 0
    for view_id in VIEW_REGISTRY:
        env = VIEW_REGISTRY[view_id](deps).build(scope, {})
        assert env.state_reason, f"{view_id} has no data and no explanation"
        assert len(env.state_reason.split()) >= 8, view_id
        checked += 1
    assert checked == len(VIEW_REGISTRY)
