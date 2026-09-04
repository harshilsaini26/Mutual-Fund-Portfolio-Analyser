"""Shared scalar types, identifier NewTypes, and scope aliases.

Slice Zero — contracts only. No logic lives here.

`PLAN.md` §9.1 is still open on the `scheme_id` format (ISIN-based vs opaque UUID).
Both are `str`, so the `NewType` below makes that decision mechanical rather than a
project-wide grep.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, NewType

# --- identifiers -----------------------------------------------------------
# All are `str` today. NewType costs nothing at runtime and lets mypy catch a
# scheme_id passed where an issuer_id belongs.

UserId = NewType("UserId", str)
SchemeId = NewType("SchemeId", str)
IssuerId = NewType("IssuerId", str)
ManagerId = NewType("ManagerId", str)
Isin = NewType("Isin", str)
IndexId = NewType("IndexId", str)
SectorId = NewType("SectorId", str)
FactorId = NewType("FactorId", str)
ScenarioId = NewType("ScenarioId", str)
PeerGroupId = NewType("PeerGroupId", str)
UniverseId = NewType("UniverseId", str)
SourceFileId = NewType("SourceFileId", str)

# --- synthetic issuers (MODULE_0.md §4.3) ----------------------------------
# `PLAN.md` §4.10: never silently drop data. Missing mass stays visible as a
# line item rather than vanishing from a join.

UNRESOLVED = IssuerId("__UNRESOLVED__")
NO_DISCLOSURE = IssuerId("__NO_DISCLOSURE__")
CASH = IssuerId("__CASH__")
DERIV = IssuerId("__DERIV__")
DIRECT = SchemeId("__DIRECT__")
PORTFOLIO = "__PORTFOLIO__"

# --- scope aliases (DECISIONS D4) ------------------------------------------
# `scope` means four different things across the corpus, and two of them share
# the literal values 'scheme'/'portfolio' while meaning different things.
# Distinct aliases so mypy catches the mix-up.

ExposureScope = Literal["all", "equity", "debt"]
"""M3 exposures()/concentration(): which instrument classes form the denominator."""

CashflowScope = Literal["scheme", "portfolio"]
"""M3 portfolio_cashflows(): the PLAN.md §9.6 switch-leg flag.

'scheme' includes switch legs; 'portfolio' excludes them as internal transfers.
Both numbers must be labelled or they will not tie out.
"""

AnalysisScope = Literal["portfolio", "scheme", "sector", "issuer", "user"]
"""M4 risk_snapshot()/liquidity(): the entity a computation is scoped to.

Always paired with a `scope_id`.
"""

UniverseScope = Literal["watch", "market"]
"""M5 market-facing methods: the user's watch universe, or the full parsed universe.

Defaults to 'watch' — MODULE_5.md §3.1 rule 2.
"""

# --- enumerations ----------------------------------------------------------


class Confidence(str, Enum):
    """Attached to every derived figure. Never hidden — `PLAN.md` §9.11."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNRESOLVED = "unresolved"


class ValidationStatus(str, Enum):
    OK = "ok"
    WARN = "warn"
    QUARANTINED = "quarantined"


class ViewState(str, Enum):
    """MODULE_6.md §3.3. `state_reason` is required for every non-OK state."""

    OK = "ok"
    EMPTY = "empty"
    SUPPRESSED = "suppressed"
    ERROR = "error"


class InstrumentClass(str, Enum):
    EQUITY = "equity"
    DEBT = "debt"
    CASH = "cash"
    DERIVATIVE = "derivative"
    MFUNIT = "mfunit"
    OTHER = "other"


class Plan(str, Enum):
    """Direct vs Regular. Never conflated — the ~1%/year silent error."""

    DIRECT = "direct"
    REGULAR = "regular"


class Option(str, Enum):
    GROWTH = "growth"
    IDCW_PAYOUT = "idcw_payout"
    IDCW_REINVEST = "idcw_reinvest"


class WeightBasis(str, Enum):
    """MODULE_0.md §9.3 materialises both; M6 exposes the choice to the user."""

    DISCLOSED = "disclosed"
    DRIFT_ADJUSTED = "drift_adj"


class McapBucket(str, Enum):
    LARGE = "large"
    MID = "mid"
    SMALL = "small"
    UNCLASSIFIED = "unclassified"


class Regime(str, Enum):
    """MODULE_2.md §3.2. NO_TENURE_DATA will be common early."""

    SINGLE_REGIME = "SINGLE_REGIME"
    SPLIT_REGIME = "SPLIT_REGIME"
    NO_TENURE_DATA = "NO_TENURE_DATA"


class ClassificationBasis(str, Enum):
    """MODULE_3.md §12.1. A required argument with no default, everywhere.

    `CLAUDE.md` invariant 6: all classification is point-in-time. Nothing in the
    data forces a choice between vintages, so the API forces it.
    """

    AS_OF_HOLDING = "as_of_holding"
    CURRENT = "current"
