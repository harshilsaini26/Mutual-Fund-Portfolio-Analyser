"""YAML fixture loading for the `Fake*` providers.

`BUILD_ORDER.md` R1 step 2: the fakes read YAML fixtures, so every module's test
suite runs with no database. That makes the vertical slices independently
testable in any order — the whole point of contracts-first.

The one hazard here is the loader. PyYAML's default resolver turns `142.8391`
into a Python `float`, which would put binary floating point at the very
boundary `PLAN.md` §8.2 rule 1 exists to defend. `DecimalSafeLoader` below
resolves those scalars to `Decimal` instead, so a fixture cannot introduce the
error even if someone writes an unquoted number.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml


class FixtureError(RuntimeError):
    """A fake was asked for something the fixture does not describe.

    Raised rather than returning None so a test failure names the missing
    fixture row instead of surfacing as an unrelated assertion three layers up.
    """


def fixture_key(mapping: Any, key: Any) -> Any:
    """Look up `key` in a fixture mapping that may key dates either way.

    PyYAML resolves a bare `2026-07-31` to a `datetime.date`, but a quoted one
    stays a string, and both spellings appear in real fixtures. Returns `Any`
    because fixture contents genuinely are untyped — the typing boundary is
    where the fakes construct dataclasses, not here.
    """
    if mapping is None:
        return None
    if key in mapping:
        return mapping[key]
    return mapping.get(str(key))


class DecimalSafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    """A SafeLoader that never produces a `float`.

    Every scalar YAML would resolve to `tag:yaml.org,2002:float` is constructed
    as `Decimal` instead. `.inf`, `-.inf` and `.nan` are rejected outright:
    none of them is a valid money, unit, NAV or weight, and letting one through
    would propagate silently through an aggregation.
    """


def _construct_decimal(loader: yaml.SafeLoader, node: yaml.Node) -> Decimal:
    raw = str(loader.construct_scalar(node))
    cleaned = raw.replace("_", "")
    lowered = cleaned.lower().lstrip("+-")
    if lowered in {".inf", ".nan", "inf", "nan"}:
        raise yaml.constructor.ConstructorError(
            None,
            None,
            f"non-finite value {raw!r} in a fixture; not a valid money/unit/NAV",
            node.start_mark,
        )
    try:
        return Decimal(cleaned)
    except InvalidOperation:  # e.g. YAML's sexagesimal float form
        raise yaml.constructor.ConstructorError(
            None, None, f"cannot parse {raw!r} as Decimal", node.start_mark
        ) from None


DecimalSafeLoader.add_constructor("tag:yaml.org,2002:float", _construct_decimal)


def load_yaml(path: Path) -> dict[str, Any]:
    """Load one fixture file. Returns {} for an empty document."""
    with path.open(encoding="utf-8") as fh:
        data = yaml.load(fh, Loader=DecimalSafeLoader)
    return data or {}


def as_decimal(v: Any) -> Decimal | None:
    """Fixture value to Decimal, preserving None.

    Accepts the Decimal the loader produced, or a quoted string. Rejects float
    explicitly rather than coercing: a float reaching here means the loader was
    bypassed, and silently rounding it would hide that.
    """
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, float):
        raise TypeError(
            f"float {v!r} in fixture data; load via DecimalSafeLoader, "
            "or quote the value in YAML"
        )
    return Decimal(str(v))


def as_date(v: Any) -> _dt.date | None:
    """Fixture value to date. PyYAML resolves ISO dates natively."""
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    return _dt.date.fromisoformat(str(v))


def as_datetime(v: Any) -> _dt.datetime | None:
    """Fixture value to datetime."""
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        return v
    if isinstance(v, _dt.date):
        return _dt.datetime(v.year, v.month, v.day)
    return _dt.datetime.fromisoformat(str(v))


class FixtureStore:
    """A directory of YAML fixtures, loaded once and shared by the fakes.

    One store backs every `Fake*` provider in a test, so the schemes M1 sees,
    the holdings M3 dissolves and the prices M4 reads all describe the same
    portfolio. Separate stores per fake would let them drift apart, and a fake
    that disagrees with itself is worse than no fake.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[str, dict[str, Any]] = {}

    def section(self, name: str) -> dict[str, Any]:
        """Load `<root>/<name>.yaml`, cached. Missing file yields {}."""
        if name not in self._cache:
            path = self.root / f"{name}.yaml"
            self._cache[name] = load_yaml(path) if path.exists() else {}
        return self._cache[name]

    def table(self, section: str, key: str) -> list[dict[str, Any]]:
        """One list-of-rows out of a section. Missing key yields []."""
        rows = self.section(section).get(key) or []
        if not isinstance(rows, list):
            raise TypeError(f"{section}.{key} is {type(rows).__name__}, expected a list")
        return rows


def default_store() -> FixtureStore:
    """The fixture set committed under `tests/fixtures/slice_zero/`."""
    root = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "slice_zero"
    return FixtureStore(root)
