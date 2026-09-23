"""Slice Zero gate.

These contracts carry no runtime behaviour, so the gate is static: it proves the
invariants that `CLAUDE.md` and `PLAN.md` §8.2 say must never be violated, at the
one point in the project where they are cheap to enforce.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import pathlib
import pkgutil
import typing
from datetime import date
from decimal import Decimal

import pytest

CONTRACT_MODULES = [
    "src.common.types",
    "src.common.contracts",
    "src.common.contracts.entity",
    "src.common.contracts.market",
    "src.common.contracts.quality",
    "src.common.contracts.scheme",
    "src.m0_data.providers.market_data",
    "src.m1_ledger.handoff",
    "src.m3_lookthrough.providers.lookthrough",
    "src.m3_lookthrough.providers.data",
    "src.m6_views.envelope",
    "src.m6_views.builder",
]

PROTOCOLS = {
    "src.m0_data.providers.market_data": "MarketDataProvider",
    "src.m3_lookthrough.providers.lookthrough": "LookThroughProvider",
    "src.m3_lookthrough.providers.data": "LookThroughDataProvider",
    "src.m6_views.builder": "ViewBuilder",
}

# `PLAN.md` §8.2 rule 3. Index = position in M0 -> M1 -> M3 -> M6.
# The gaps are M2, M4 and M5, which are specified but not built. Their indices
# are left free rather than closed up, so adding one later does not renumber
# the modules that already exist.
TIER = {
    "src.common": 0,
    "src.m0_data": 1,
    "src.m1_ledger": 2,
    "src.m3_lookthrough": 4,
    "src.m6_views": 6,
}


def _dataclasses_in(module_name: str) -> list[type]:
    mod = importlib.import_module(module_name)
    return [
        obj
        for _, obj in inspect.getmembers(mod, inspect.isclass)
        if dataclasses.is_dataclass(obj) and obj.__module__ == module_name
    ]


def _all_contract_dataclasses() -> list[type]:
    out: list[type] = []
    for name in CONTRACT_MODULES:
        out.extend(_dataclasses_in(name))
    return out


def test_every_protocol_resolves() -> None:
    """All ten Slice Zero protocols import and are runtime-inspectable."""
    for module_name, proto_name in PROTOCOLS.items():
        mod = importlib.import_module(module_name)
        proto = getattr(mod, proto_name, None)
        assert proto is not None, f"{proto_name} missing from {module_name}"
        assert inspect.isclass(proto)


@pytest.mark.parametrize("klass", _all_contract_dataclasses(), ids=lambda k: k.__name__)
def test_dataclasses_are_frozen(klass: type) -> None:
    """Contracts are values, not mutable state.

    A result object that can be edited in place is a result object whose
    provenance can drift away from its numbers.
    """
    params = typing.cast(typing.Any, klass).__dataclass_params__
    assert params.frozen, f"{klass.__name__} is not frozen"


@pytest.mark.parametrize("klass", _all_contract_dataclasses(), ids=lambda k: k.__name__)
def test_no_float_fields(klass: type) -> None:
    """`PLAN.md` §8.2 rule 1: Decimal for money, units, NAVs and weights.

    Units carry 6 decimals in a CAS. Float accumulation over hundreds of SIP
    instalments fails reconciliation at exactly the tolerance that matters.
    Floats are permitted inside numerical routines (XIRR, covariance,
    regression) and must be converted back at the boundary — this asserts the
    boundary.
    """
    hints = typing.get_type_hints(klass)
    offenders = [
        name
        for name, hint in hints.items()
        if hint is float or float in typing.get_args(hint)
    ]
    assert not offenders, f"{klass.__name__} has float fields: {offenders}"


def test_classification_basis_never_defaults() -> None:
    """`CLAUDE.md` invariant 6: `ClassificationBasis` is required, no default.

    The same exposure produces different tilts depending on which classification
    vintage is applied. Nothing in the data forces a choice, so the API forces it.
    """
    from src.common.types import ClassificationBasis

    for module_name, proto_name in PROTOCOLS.items():
        proto = getattr(importlib.import_module(module_name), proto_name)
        for meth_name, meth in inspect.getmembers(proto, inspect.isfunction):
            try:
                hints = typing.get_type_hints(meth)
            except NameError:  # pragma: no cover - unresolved forward ref
                continue
            sig = inspect.signature(meth)
            for pname, param in sig.parameters.items():
                if hints.get(pname) is ClassificationBasis:
                    assert param.default is inspect.Parameter.empty, (
                        f"{proto_name}.{meth_name} gives {pname} a default"
                    )


def test_rendered_results_carry_caveats() -> None:
    """MODULE_6.md §3.2.

    Caveats are user-facing sentences that must reach `ViewEnvelope` unchanged.
    If a type M6 renders cannot carry them, they get dropped somewhere between
    here and the screen.
    """
    from src.m3_lookthrough.providers.lookthrough import (
        LookThroughResult,
        PortfolioSummary,
    )
    from src.m6_views.envelope import ViewEnvelope

    for klass in (LookThroughResult, PortfolioSummary, ViewEnvelope):
        assert "caveats" in typing.get_type_hints(klass), (
            f"{klass.__name__} cannot carry caveats"
        )


def test_view_envelope_provenance_has_no_defaults() -> None:
    """MODULE_6.md §3.1: provenance and quality are ALL REQUIRED.

    A default would let a view omit its staleness by accident. `PLAN.md` §4.3:
    staleness is displayed, never hidden.
    """
    from src.m6_views.envelope import ViewEnvelope

    required = {
        "as_of",
        "data_as_of",
        "staleness_days",
        "source_modules",
        "confidence",
        "coverage_pct",
        "unresolved_pct",
        "caveats",
    }
    for field in dataclasses.fields(ViewEnvelope):
        if field.name in required:
            assert field.default is dataclasses.MISSING, (
                f"ViewEnvelope.{field.name} has a default"
            )
            assert field.default_factory is dataclasses.MISSING, (
                f"ViewEnvelope.{field.name} has a default_factory"
            )


def test_dependency_direction_is_one_way() -> None:
    """`PLAN.md` §8.2 rule 3. A circular import is a design error.

    M4 and M5 are peers at the same tier: neither may import the other.
    """
    import src

    violations: list[str] = []
    for mod_info in pkgutil.walk_packages(src.__path__, prefix="src."):
        name = mod_info.name
        own_tier = next((t for pkg, t in TIER.items() if name.startswith(pkg)), None)
        if own_tier is None:
            continue
        module = importlib.import_module(name)
        origin = getattr(module, "__file__", None)
        if origin is None:
            continue
        source = pathlib.Path(origin).read_text(encoding="utf-8")
        for pkg, tier in TIER.items():
            if name.startswith(pkg):
                continue
            if f"from {pkg}" in source and tier > own_tier:
                violations.append(f"{name} (tier {own_tier}) imports {pkg} (tier {tier})")

    assert not violations, "upstream imports downstream: " + "; ".join(violations)


def _code_strings(tree: ast.Module) -> list[str]:
    """Every string literal except the docstrings.

    Docstrings are exempt because the modules that avoid a token are the ones
    that have to name it to explain why.
    """
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, holders)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


#: The layers where a date crosses the boundary between stored bytes and the
#: warehouse: M0 archives and parses published files, M1 parses the user's own
#: statements. `m6_views` is deliberately absent — its `strftime` calls render
#: a date for a person to read, which is a display choice, not a fact anything
#: is stored or rebuilt from.
INGEST_PACKAGES = ("src.m0_data", "src.m1_ledger")


@pytest.mark.parametrize("package", INGEST_PACKAGES)
def test_no_month_name_crosses_the_locale(package: str) -> None:
    """`CLAUDE.md` invariant 10, for every fetcher and parser that ingests.

    `%b` and `%B` render and read through `LC_TIME`, and these are the layers
    where that reaches stored data. Three distinct failures, all real before
    the tables went in:

    1.  A fetcher building its query with `strftime("%d-%b-%Y")` sends
        `01-Mrz-2024` on a German machine. Both AMFI and niftyindices answer a
        query they cannot read with a SUCCESS status and no rows, so the job
        records nothing and REPORTS SUCCESS.
    2.  An M0 parser using `strptime` refuses a file the same warehouse
        archived itself, so the rebuild fails by geography.
    3.  `cas/parse.py` calls its date reader from inside the state machine with
        nothing catching it, so a German locale does not degrade the import —
        it aborts it, and a statement that imports on one machine cannot be
        imported on another.

    The explicit month tables in `m0_data/normalise/numbers.py`,
    `m0_data/parse/index/nifty.py`, the two M0 fetchers and `m1_ledger/cas/
    parse.py` are what replace them. This is what stops the next source added
    to either layer from reaching for `%b` again, which is the failure a
    per-module test cannot cover.
    """
    module = importlib.import_module(package)
    root = pathlib.Path(module.__file__ or "").parent
    violations = [
        f"{path.relative_to(root.parent)}: {text!r}"
        for path in sorted(root.rglob("*.py"))
        for text in _code_strings(ast.parse(path.read_text(encoding="utf-8")))
        if "%b" in text or "%B" in text
    ]
    assert not violations, (
        "a month name goes through LC_TIME; use an explicit table: "
        + "; ".join(violations)
    )


def test_decimal_survives_the_boundary() -> None:
    """A construction smoke test: the types accept Decimal and reject nothing."""
    from src.common.contracts.market import NavPoint
    from src.common.types import SchemeId

    point = NavPoint(
        scheme_id=SchemeId("INF209K01Z15"),
        nav_date=date(2026, 7, 31),
        nav=Decimal("142.8391"),
        is_interpolated=False,
    )
    assert isinstance(point.nav, Decimal)
    with pytest.raises(dataclasses.FrozenInstanceError):
        point.nav = Decimal(0)  # type: ignore[misc]
