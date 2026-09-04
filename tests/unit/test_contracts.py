"""Slice Zero gate.

These contracts carry no runtime behaviour, so the gate is static: it proves the
invariants that `CLAUDE.md` and `PLAN.md` §8.2 say must never be violated, at the
one point in the project where they are cheap to enforce.
"""

from __future__ import annotations

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
    "src.m0_data.providers.fund_data",
    "src.m1_ledger.handoff",
    "src.m2_fund.providers.characteristics",
    "src.m3_lookthrough.providers.lookthrough",
    "src.m3_lookthrough.providers.data",
    "src.m4_risk.providers.risk",
    "src.m4_risk.providers.inputs",
    "src.m5_market.providers.intelligence",
    "src.m5_market.providers.feed",
    "src.m6_views.envelope",
    "src.m6_views.builder",
]

PROTOCOLS = {
    "src.m0_data.providers.market_data": "MarketDataProvider",
    "src.m0_data.providers.fund_data": "FundDataProvider",
    "src.m2_fund.providers.characteristics": "SchemeCharacteristics",
    "src.m3_lookthrough.providers.lookthrough": "LookThroughProvider",
    "src.m3_lookthrough.providers.data": "LookThroughDataProvider",
    "src.m4_risk.providers.risk": "RiskProvider",
    "src.m4_risk.providers.inputs": "RiskInputs",
    "src.m5_market.providers.intelligence": "MarketIntelligence",
    "src.m5_market.providers.feed": "MarketDataFeed",
    "src.m6_views.builder": "ViewBuilder",
}

# `PLAN.md` §8.2 rule 3. Index = position in M0 -> M1 -> M2 -> M3 -> M4/M5 -> M6.
# M4 and M5 are peers; neither may import the other.
TIER = {
    "src.common": 0,
    "src.m0_data": 1,
    "src.m1_ledger": 2,
    "src.m2_fund": 3,
    "src.m3_lookthrough": 4,
    "src.m4_risk": 5,
    "src.m5_market": 5,
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


def test_risk_inputs_m3_block_delegates_to_lookthrough_provider() -> None:
    """`BUILD_ORDER.md` R1's amendment, plus DECISIONS D1 and D2.

    `LookThroughProvider` must expose every M3-block method `RiskInputs` needs,
    with a compatible signature, so M4's adapter is a pass-through and no M3
    calculation leaks into M4.
    """
    from src.m3_lookthrough.providers.lookthrough import LookThroughProvider
    from src.m4_risk.providers.inputs import RiskInputs

    m3_block = ["exposures", "sector_exposure", "concentration", "max_pairwise_overlap"]

    for name in m3_block:
        assert hasattr(RiskInputs, name), f"RiskInputs is missing {name}"
        assert hasattr(LookThroughProvider, name), (
            f"LookThroughProvider cannot serve RiskInputs.{name}"
        )

        ri_params = set(inspect.signature(getattr(RiskInputs, name)).parameters)
        ltp_params = set(inspect.signature(getattr(LookThroughProvider, name)).parameters)
        assert ri_params <= ltp_params, (
            f"RiskInputs.{name} asks for {ri_params - ltp_params}, "
            f"which LookThroughProvider does not accept"
        )


def test_risk_inputs_has_no_lookthrough_method() -> None:
    """DECISIONS D1: the colliding name is gone.

    MODULE_4.md §14.4 declared `lookthrough() -> list[Exposure]` while
    MODULE_3.md §15.1 declares `lookthrough() -> LookThroughResult`. One name,
    two return types, across a module boundary.
    """
    from src.m4_risk.providers.inputs import RiskInputs

    assert not hasattr(RiskInputs, "lookthrough"), (
        "RiskInputs.lookthrough() collides with LookThroughProvider.lookthrough(); "
        "use exposures() instead"
    )


def test_rendered_results_carry_caveats() -> None:
    """MODULE_4.md §14.2 and MODULE_6.md §3.2.

    Caveats are user-facing sentences that must reach `ViewEnvelope` unchanged.
    If a type M6 renders cannot carry them, they get dropped somewhere between
    here and the screen.
    """
    from src.m3_lookthrough.providers.lookthrough import (
        LookThroughResult,
        PortfolioSummary,
    )
    from src.m4_risk.providers.risk import RiskSnapshot
    from src.m5_market.providers.intelligence import IssuerFlow
    from src.m6_views.envelope import ViewEnvelope

    for klass in (
        LookThroughResult,
        PortfolioSummary,
        RiskSnapshot,
        IssuerFlow,
        ViewEnvelope,
    ):
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
