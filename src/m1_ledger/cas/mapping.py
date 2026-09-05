"""Description -> transaction type. MODULE_1.md §5.5.

The table is `config/txn_types.yaml`, not a literal in this module, because
§5.5 asks for it as data: CAS descriptions are free text authored independently
by CAMS, KFintech and MF Central, and a new wording should not need a release.

The one rule that must never be relaxed: an unmatched description RAISES. §5.5
says so twice, and the reason is in `CLAUDE.md` invariant 5 — a row booked as a
`PURCHASE` because nothing else matched is a wrong number that reconciles,
which is the only kind of wrong number this system cannot detect.
"""

from __future__ import annotations

import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src.m1_ledger.txn import UnmappedTransactionType

CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "txn_types.yaml"


@lru_cache(maxsize=1)
def _config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    if not isinstance(loaded, dict) or "patterns" not in loaded:
        raise ValueError(f"{CONFIG_PATH} is not a type-mapping table")
    return loaded


@lru_cache(maxsize=1)
def type_patterns() -> list[tuple[re.Pattern[str], str]]:
    """Compiled in file order. First match wins, so the order is load-bearing."""
    return [
        (re.compile(row["pattern"], re.I), row["type"]) for row in _config()["patterns"]
    ]


@lru_cache(maxsize=1)
def charge_patterns() -> list[tuple[re.Pattern[str], str]]:
    """Lines that are charges attached to the preceding transaction (§5.4)."""
    return [
        (re.compile(row["pattern"], re.I), row["field"])
        for row in _config().get("charges", [])
    ]


@lru_cache(maxsize=1)
def _directional() -> dict[str, dict[str, str]]:
    return dict(_config().get("directional", {}))


def map_txn_type(desc: str, units: Decimal | None) -> str:
    """MODULE_1.md §5.5, with `_disambiguate` made concrete.

    §5.5 names `_disambiguate` and says it "uses the sign of units to resolve
    ambiguous cases" without defining it. The cases that need it are the ones
    where a single printed wording covers both directions — a merger prints
    identically in the dying scheme and the surviving one, and only the sign of
    the units column says which side of it you are reading.

    A zero or absent unit count is NOT treated as negative. A zero-unit row is
    a distribution (§5.4), and defaulting it to the outbound direction would
    book an IDCW payout as units leaving the folio.
    """
    for pattern, txn_type in type_patterns():
        if pattern.search(desc):
            return _disambiguate(txn_type, units)
    raise UnmappedTransactionType(desc)


def _disambiguate(txn_type: str, units: Decimal | None) -> str:
    rule = _directional().get(txn_type)
    if rule is None or units is None:
        return txn_type
    # Strictly negative, so a zero-unit row keeps the inbound reading. An
    # explicit `units == 0` guard above would be dead code: `0 < 0` is already
    # False. Mutation testing found it, which is the point of running it.
    return rule["negative"] if units < 0 else rule["positive"]
