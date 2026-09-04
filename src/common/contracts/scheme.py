"""Scheme master, TER, manager and classification-list types.

Shared by M0, M2, M4. Slice Zero — dataclasses only, no logic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.common.types import IssuerId, ManagerId, McapBucket, SchemeId


@dataclass(frozen=True)
class SchemeRow:
    """MODULE_0.md `scheme`.

    `plan` and `option` are separate fields and are never conflated: same
    portfolio, different expense ratio, different ISIN, different NAV series.
    """

    scheme_id: SchemeId
    scheme_name: str
    plan: str
    option: str
    status: str  # active|merged|wound_up
    amfi_code: str | None
    isin: str | None
    amc_id: str | None
    sebi_category: str | None
    benchmark_id: str | None
    inception_date: date | None
    merged_into: SchemeId | None
    merger_date: date | None
    merger_ratio_num: int | None
    merger_ratio_den: int | None


@dataclass(frozen=True)
class TerPoint:
    """MODULE_0.md `scheme_ter`. TER changes over time, so it is a dated series.

    `ter` is a percentage (0.6200 == 0.62%), not a fraction.
    """

    scheme_id: SchemeId
    valid_from: date
    ter: Decimal
    valid_to: date | None


@dataclass(frozen=True)
class Tenure:
    """MODULE_0.md `scheme_manager_tenure`.

    `confidence` defaults to medium in the DDL because factsheet parsing is
    lossy. It travels with the row so M2 can degrade the manager dossier rather
    than presenting a parsed guess as fact.
    """

    scheme_id: SchemeId
    manager_id: ManagerId
    start_date: date
    confidence: str
    end_date: date | None
    role: str | None  # lead|co|associate|unknown


@dataclass(frozen=True)
class ManagerRow:
    """MODULE_0.md `manager`. `name_norm` exists to dedup one person across AMCs."""

    manager_id: ManagerId
    full_name: str
    name_norm: str
    qualifications: str | None
    experience_start_date: date | None


@dataclass(frozen=True)
class McapList:
    """MODULE_0.md `issuer_classification` (taxonomy='amfi_mcap'), as a snapshot.

    AMFI publishes half-yearly. `effective_date` is stored on every derived
    figure so the number stays reproducible after AMFI revises the list.

    `CLAUDE.md` invariant 6 and `lookthrough.md`: ONE `mcap_basis` across all
    schemes in a single aggregation, or incompatible buckets are being summed.

    The two accessors are plain mapping lookups — the object is a data carrier,
    not a service.
    """

    effective_date: date
    buckets: Mapping[IssuerId, McapBucket]
    mcaps: Mapping[IssuerId, Decimal]

    def bucket_for(self, issuer_id: IssuerId) -> McapBucket | None:
        return self.buckets.get(issuer_id)

    def mcap_for(self, issuer_id: IssuerId) -> Decimal | None:
        return self.mcaps.get(issuer_id)
