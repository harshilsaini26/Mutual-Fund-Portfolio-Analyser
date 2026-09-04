"""Shared leaf types — the value objects two or more modules both need.

Module-owned types stay in their owning module (`Exposure` in M3, `RiskSnapshot`
in M4, `PositionContext` in M1). Only types crossing more than one boundary live
here, so that M5 never has to import from M2 to read a price.

`PLAN.md` §8.2 rule 3: dependency direction is one-way,
M0 -> M1 -> M2 -> M3 -> M4/M5 -> M6. This package sits below all of them.
"""

from src.common.contracts.entity import (
    Constituent,
    DirectHolding,
    Holding,
    IssuerWeight,
    MergerLink,
    SchemeRef,
    SchemeWeight,
)
from src.common.contracts.market import (
    IdcwEvent,
    IndexMeta,
    IndexPoint,
    NavPoint,
    PricePoint,
    RfPoint,
)
from src.common.contracts.quality import CoverageStat, DataQuality, DisclosureQuality
from src.common.contracts.scheme import (
    ManagerRow,
    McapList,
    SchemeRow,
    Tenure,
    TerPoint,
)

__all__ = [
    "Constituent",
    "CoverageStat",
    "DataQuality",
    "DirectHolding",
    "DisclosureQuality",
    "Holding",
    "IdcwEvent",
    "IndexMeta",
    "IndexPoint",
    "IssuerWeight",
    "ManagerRow",
    "McapList",
    "MergerLink",
    "NavPoint",
    "PricePoint",
    "RfPoint",
    "SchemeRef",
    "SchemeRow",
    "SchemeWeight",
    "Tenure",
    "TerPoint",
]
