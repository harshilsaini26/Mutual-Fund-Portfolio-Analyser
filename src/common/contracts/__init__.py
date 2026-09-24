"""Shared leaf types — the value objects two or more modules both need.

Module-owned types stay in their owning module (`Exposure` in M3,
`RiskSnapshot` in M4). Only types crossing more than one boundary live
here, so that M5 never has to import from M2 to read a price.

`PLAN.md` §8.2 rule 3: dependency direction is one-way,
M0 -> M1 -> M2 -> M3 -> M4/M5 -> M6. This package sits below all of them.
"""

from src.common.contracts.entity import (
    Holding,
    IssuerWeight,
    MergerLink,
    SchemeRef,
)
from src.common.contracts.market import IdcwEvent, IndexPoint, NavPoint

__all__ = [
    "Holding",
    "IdcwEvent",
    "IndexPoint",
    "IssuerWeight",
    "MergerLink",
    "NavPoint",
    "SchemeRef",
]
