"""Validation gates for a parsed disclosure. MODULE_0.md §10.

Run after every L2 load. A failure at `quarantine` severity **stops the batch
being promoted** rather than warning about it, because §10's whole argument is
that a disclosure which is subtly wrong is worse than one that is absent: the
absent one shows as missing coverage, the wrong one shows as a confident number.

§10.2's closing instruction is easy to skip and worth keeping: **persist every
result, passes included.** When a number looks wrong six months later, knowing
which checks *passed* narrows the search as much as knowing which failed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.m0_data.resolve.isin import is_valid_isin

#: §10.1's thresholds, verbatim.
WEIGHT_SUM_MIN = Decimal(95)
WEIGHT_SUM_MAX = Decimal(105)
AUM_TOLERANCE_PCT = Decimal(3)
UNRESOLVED_MAX_PCT = Decimal(2)

QUARANTINE = "quarantine"
WARN = "warn"
INFO = "info"


@dataclass(frozen=True)
class CheckResult:
    """§10.2."""

    code: str
    passed: bool
    severity: str
    message: str
    observed: str | None = None


@dataclass(frozen=True)
class HoldingRow:
    """The minimum a check needs. Deliberately not the full staged row."""

    isin: str | None
    instrument_class: str
    market_value: Decimal
    pct_to_nav: Decimal | None
    issuer_id: str


def validate_disclosure(
    rows: list[HoldingRow],
    as_of: date,
    today: date,
    aum_reported: Decimal | None = None,
) -> list[CheckResult]:
    """§10.1's checks that are computable from a single disclosure.

    V4 (row count vs prior period), V5 (turnover), V6 (NAV continuity), V9
    (coverage by day 22), V12-V14 all need either history or a cross-scheme
    view. They are not silently skipped — `not_evaluated` names them, because a
    gate that quietly checks less than it claims is worse than one that claims
    less.
    """
    results: list[CheckResult] = []
    total_mv = sum((r.market_value for r in rows), Decimal(0))

    # V1 — weight sum. Outside 95-105 means rows are missing or duplicated.
    reported = sum((r.pct_to_nav or Decimal(0) for r in rows), Decimal(0))
    results.append(
        CheckResult(
            "V1", WEIGHT_SUM_MIN <= reported <= WEIGHT_SUM_MAX, QUARANTINE,
            "sum of reported % to NAV is within 95-105", f"{reported}",
        )
    )

    # V2 — value reconciliation. THE units check. §7.2's 100x error fails this
    # by two orders of magnitude, which is why it quarantines rather than warns.
    if aum_reported and aum_reported > 0:
        drift = abs(total_mv - aum_reported) / aum_reported * 100
        results.append(
            CheckResult(
                "V2", drift <= AUM_TOLERANCE_PCT, QUARANTINE,
                f"total market value within {AUM_TOLERANCE_PCT}% of scheme AUM",
                f"{drift:.4f}%",
            )
        )
    else:
        results.append(
            CheckResult("V2", True, INFO, "no AUM on record to reconcile against")
        )

    # V3 — unresolved share. Warns rather than quarantines, and BLOCKS the
    # look-through for this scheme: the holdings are still true, we just cannot
    # say whose they are, so showing an exposure chart would be a lie of
    # composition rather than of fact.
    unresolved = sum(
        (abs(r.market_value) for r in rows if r.issuer_id == "__UNRESOLVED__"),
        Decimal(0),
    )
    pct_unresolved = (unresolved / abs(total_mv) * 100) if total_mv else Decimal(0)
    results.append(
        CheckResult(
            "V3", pct_unresolved < UNRESOLVED_MAX_PCT, WARN,
            f"unresolved market value below {UNRESOLVED_MAX_PCT}%",
            f"{pct_unresolved:.4f}%",
        )
    )

    # V7 — duplicate ISIN. A warning, not an error: legitimate for multi-series
    # debt, where one issuer's several tranches share an ISIN prefix but not the
    # ISIN itself. A true duplicate usually means a section was parsed twice.
    seen: dict[str, int] = {}
    for r in rows:
        if r.isin:
            seen[r.isin] = seen.get(r.isin, 0) + 1
    duplicates = sorted(k for k, n in seen.items() if n > 1)
    results.append(
        CheckResult(
            "V7", not duplicates, WARN, "no ISIN appears twice",
            ", ".join(duplicates) or None,
        )
    )

    # V8 — negative weight, allowed only for a derivative. A short equity leg
    # disclosed as equity is either a parse error or a genuine position the
    # classifier mislabelled; either way it must be looked at.
    bad_negatives = [
        r for r in rows if r.market_value < 0 and r.instrument_class != "derivative"
    ]
    results.append(
        CheckResult(
            "V8", not bad_negatives, WARN,
            "negative market value only on derivatives",
            ", ".join(sorted({r.instrument_class for r in bad_negatives})) or None,
        )
    )

    # V10 — ISIN check digit, per row.
    malformed = sorted({r.isin for r in rows if r.isin and not is_valid_isin(r.isin)})
    results.append(
        CheckResult(
            "V10", not malformed, WARN, "every ISIN passes its check digit",
            ", ".join(malformed) or None,
        )
    )

    # V11 — date sanity. A future as-of date, or one that is not a period end,
    # files the portfolio against a month it does not describe.
    plausible = as_of <= today and _is_period_end(as_of)
    results.append(
        CheckResult(
            "V11", plausible, QUARANTINE,
            "as-of date is a real, past month or fortnight end", as_of.isoformat(),
        )
    )
    return results


def not_evaluated() -> dict[str, str]:
    """§10.1 checks this function cannot perform, and why. Never silent."""
    return {
        "V4": "row count vs prior period — needs a prior disclosure",
        "V5": "turnover — needs a prior disclosure",
        "V6": "NAV continuity — belongs to the NAV loader, not a disclosure",
        "V9": "coverage by day 22 — a cross-scheme calendar check",
        "V12": "price continuity — needs security_price, not built",
        "V13": "orphan file — a filesystem sweep, not a disclosure check",
        "V14": "scheme master drift — a cross-load check",
    }


def promote_or_quarantine(results: list[CheckResult]) -> str:
    """§10.2. `ok` only when nothing failed at all."""
    if any(r.severity == QUARANTINE and not r.passed for r in results):
        return "quarantined"
    return WARN if any(not r.passed for r in results) else "ok"


def as_json(results: list[CheckResult]) -> str:
    """§10.2: persist every result, passes included."""
    return json.dumps(
        [
            {
                "code": r.code, "passed": r.passed, "severity": r.severity,
                "message": r.message, "observed": r.observed,
            }
            for r in results
        ]
        + [{"code": k, "passed": None, "severity": INFO, "message": v}
           for k, v in sorted(not_evaluated().items())]
    )


def _is_period_end(day: date) -> bool:
    """A month end, or a fortnight end (the 15th) for debt disclosures."""
    import calendar

    return day.day == 15 or day.day == calendar.monthrange(day.year, day.month)[1]
