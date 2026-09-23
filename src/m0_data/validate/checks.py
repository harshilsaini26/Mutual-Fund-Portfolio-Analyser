"""Validation gates for a parsed disclosure. MODULE_0.md §10.

Run after every L2 load. A failure at `quarantine` severity STOPS the batch
being promoted rather than warning: a disclosure that is subtly wrong is worse
than one that is absent, because the absent one shows as missing coverage and
the wrong one shows as a confident number.

§10.2: **persist every result, passes included.** When a number looks wrong six
months later, knowing which checks passed narrows the search as much as knowing
which failed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType

from src.m0_data.resolve.isin import is_valid_isin

#: §10.1's thresholds, verbatim.
WEIGHT_SUM_MIN = Decimal(95)
WEIGHT_SUM_MAX = Decimal(105)
AUM_TOLERANCE_PCT = Decimal(3)

#: The same check against a QUARTERLY AVERAGE rather than a month-end balance.
#:
#: §10 gives V2 one tolerance and assumes a point-in-time figure; the source
#: that exists holds AMFI's quarterly average (V1-49), which sits -10.4% (HDFC
#: Flexi Cap) and -4.6% (PPFAS) from their August portfolios. Real movement,
#: outside 3%, so the spec's number would quarantine two correct disclosures.
#:
#: 25% is chosen against what V2 is FOR: a 100x error is 9,900% off, so 25%
#: catches it with three orders of magnitude to spare while leaving room for a
#: quarter of drift. A units check, not a valuation check.
AUM_AVERAGE_TOLERANCE_PCT = Decimal(25)
UNRESOLVED_MAX_PCT = Decimal(2)

#: How far off a QUARTERLY AVERAGE must be before V2 may quarantine (V1-67).
#:
#: An average cannot tell an error from three things that are not one: growth
#: since the quarter, a fund that existed for only part of it (Kotak Nifty Alpha
#: Low Volatility 30 launched eight days before it closed: 7.4x), and a witness
#: covering one plan of a fund AMFI lists per plan (Kotak Banking and PSU Debt:
#: 2.5x; summed across its plans, -1.6%). All 17 quarantines of 2026-09-23 were
#: one of these, within 7.4x. What an average CAN prove is a units error, and
#: every scale confusion in an Indian disclosure -- rupees, thousands, lakhs,
#: crores -- is 100x or more. So a factor of 10 quarantines; beyond the 25%
#: tolerance but inside it, V2 fails as a warning, recorded but not blocking.
UNITS_ERROR_RATIO = Decimal(10)


#: Which tolerance V2 applies to which kind of AUM, as a table rather than a
#: comparison against one literal. Written as `AVERAGE if basis ==
#: "quarterly_average" else STRICT`, any other string — a typo, a basis added
#: later and not wired here — fell through to the strict 3% silently: a
#: portfolio 14% from its AUM passes as `quarterly_average` and QUARANTINES as
#: `quaterly_average` (V1-51).
#:
#: `migrations/012_scheme_aum.sql` declares the same vocabulary on the column,
#: and `tests/unit/test_m0_aum.py` asserts the fetch layer's `BASIS` is a key
#: here — which keeps the two from drifting without an import between layers
#: that do not otherwise depend on each other.
TOLERANCE_BY_BASIS: Mapping[str, Decimal] = MappingProxyType({
    "point_in_time": AUM_TOLERANCE_PCT,
    "quarterly_average": AUM_AVERAGE_TOLERANCE_PCT,
})


class UnknownAumBasis(ValueError):
    """§10's V2 was handed a basis it has no tolerance for.

    `CLAUDE.md` invariant 5: raise, do not clamp. Defaulting to either
    tolerance would be a plausible wrong answer -- the strict one quarantines
    correct data, the loose one stops checking -- and both are worse than a
    crash that names the basis.
    """


def age_days(witness_as_of: date, disclosure_as_of: date) -> int:
    """How stale the AUM witness was when the disclosure was published.

    Here rather than on `AumWitness`, because this is the only caller and the
    method on that dataclass had none -- `src/m0_data/load.py` defined
    `age_days` and only its own test used it, while this function recomputed
    the same subtraction inline.
    """
    return (disclosure_as_of - witness_as_of).days


def tolerance_for(basis: str) -> Decimal:
    try:
        return TOLERANCE_BY_BASIS[basis]
    except KeyError:
        known = ", ".join(sorted(TOLERANCE_BY_BASIS))
        raise UnknownAumBasis(
            f"no V2 tolerance for aum_basis {basis!r}; known: {known}"
        ) from None

QUARANTINE = "quarantine"
WARN = "warn"
INFO = "info"


@dataclass(frozen=True)
class CheckResult:
    """§10.2."""

    code: str
    #: `None` means the check COULD NOT RUN, which is neither a pass nor a
    #: failure. `as_json` already used that spelling for `not_evaluated()`;
    #: V2 needed it too and was recording `True` instead -- see below.
    passed: bool | None
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
    aum_basis: str = "point_in_time",
    aum_as_of: date | None = None,
) -> list[CheckResult]:
    """§10.1's checks that are computable from a single disclosure.

    V4, V5, V6, V9 and V12-V14 need history or a cross-scheme view. They are not
    silently skipped — `not_evaluated` names them, because a gate that quietly
    checks less than it claims is worse than one that claims less.
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
        tolerance = tolerance_for(aum_basis)
        # The witness's own date is in the message, not just its kind. A figure
        # can be the right KIND of number and two quarters out of date, and
        # `validation_notes` is the only place a later reader can find out.
        witness = aum_basis
        if aum_as_of is not None:
            witness += f" as of {aum_as_of}, {age_days(aum_as_of, as_of)}d before"
        # `tolerance_for` has already refused an unknown basis, so comparing
        # the string here cannot fall through the way V1-51's did.
        ratio = total_mv / aum_reported
        conclusive = aum_basis != "quarterly_average" or not (
            1 / UNITS_ERROR_RATIO < ratio < UNITS_ERROR_RATIO
        )
        results.append(
            CheckResult(
                "V2", drift <= tolerance, QUARANTINE if conclusive else WARN,
                f"total market value within {tolerance}% of scheme AUM"
                f" ({witness})",
                f"{drift:.4f}%",
            )
        )
    else:
        # NOT a pass. With no AUM on record there is nothing to reconcile
        # against, so V2 did not run -- but it recorded `passed: True`, so
        # `validation_notes` for all 205 current disclosures claimed the units
        # check succeeded when it had never run. `None` is the spelling
        # `as_json` already uses for a check that was not evaluated, and
        # `promote_or_quarantine` reads `is False` so an unrun check neither
        # promotes nor fails a disclosure.
        results.append(
            CheckResult("V2", None, INFO, "no AUM on record to reconcile against")
        )

    # V3 — unresolved share. Warns rather than quarantines, so it does NOT
    # block the look-through: the holdings are still true, and the part we
    # cannot attribute is carried as `__UNRESOLVED__` and reported as its own
    # figure. Only a quarantine blocks (`weights.latest_disclosure`, V1-66).
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
    """§10.2. `ok` only when nothing failed at all.

    `is False`, not `not r.passed`: a check that could not run carries
    `passed = None`, and `not None` is True -- which would have turned every
    unrunnable check into a failure the moment one existed.
    """
    if any(r.severity == QUARANTINE and r.passed is False for r in results):
        return "quarantined"
    return WARN if any(r.passed is False for r in results) else "ok"


def as_json(results: list[CheckResult], unpriced: list[str] | None = None) -> str:
    """§10.2: persist every result, passes included.

    `unpriced` names the rows the file listed but did not price.
    `holding.market_value` is NOT NULL, so such a row stores as zero with a zero
    weight — it contributes nothing to any look-through while every quality
    figure is computed against a total that already excludes it, so no number
    moves. Invariant 4 is "never silently drop rows": the row survives, its
    exposure does not, and this is the record that says so.
    """
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
        + (
            [{
                "code": "UNPRICED",
                "passed": False,
                "severity": WARN,
                "message": (
                    f"{len(unpriced)} row(s) carry no market value and are "
                    f"stored at zero, so they contribute no exposure: "
                    f"{', '.join(unpriced[:5])}"
                    + (" ..." if len(unpriced) > 5 else "")
                ),
                "observed": str(len(unpriced)),
            }]
            if unpriced
            else []
        )
    )


def _is_period_end(day: date) -> bool:
    """A month end, or a fortnight end (the 15th) for debt disclosures."""
    import calendar

    return day.day == 15 or day.day == calendar.monthrange(day.year, day.month)[1]
