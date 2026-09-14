"""Reconciliation — the V0 gate.

MODULE_1.md §11. `PLAN.md` §7 V0 does not ship until every folio passes: a
ledger that is 99% right produces analytics that are confidently wrong.

Two checks are specified; this module implements a third. §11.1 says the value
check catches what the unit check cannot — a wrong-plan resolution, where units
match while the NAV series belongs to the other share class. As written it
cannot: it multiplies both sides by the same NAV, so the NAV cancels and the
result is the unit delta restated.

`nav_cross_check` is the fix (V0-12): it compares the NAV each transaction was
priced at, as printed on the statement, against our resolved scheme's NAV on
that date — an independent witness to which scheme the units belong to. It is a
gate condition, so a wrong-plan resolution now fails.

§11.1's own value ratio is still reported as `delta_value_pct`, because a
reader will look for it. It simply does not decide the gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from src.common.types import SchemeId
from src.m1_ledger.lots import LotBook
from src.m1_ledger.txn import Txn, drop_reversed

#: MODULE_1.md §11.1 and PLAN.md §7 V0.
UNIT_TOL = Decimal("0.001")
VALUE_TOL = Decimal("0.005")  # 0.5%

#: A printed NAV further than this from ours means we resolved a different
#: scheme, not that the statement rounded. Direct and Regular plans of the same
#: fund diverge by far more than this within a year of launch.
NAV_MATCH_TOL_PCT = Decimal("0.5")

#: Below this delta, across a long history, the signature is float accumulation
#: rather than a missing transaction — see §11.2.
FLOAT_CONTAMINATION_DELTA = Decimal("0.01")
FLOAT_CONTAMINATION_MIN_TXNS = 100


@dataclass(frozen=True)
class NavCrossCheck:
    """Whether the statement's printed NAVs agree with our resolved scheme's.

    This is the check that actually detects a wrong-plan resolution.
    """

    scheme_id: SchemeId
    checked: int
    matched: int
    mismatched: int
    unavailable: int
    worst_deviation_pct: Decimal
    status: str


@dataclass(frozen=True)
class Reconciliation:
    """One folio-scheme's result. `status` is ok | warn | fail.

    `warn` is not a pass. It means there was nothing to check against, so the
    position is unverified rather than verified-correct.
    """

    scheme_id: SchemeId
    folio: str
    as_of: date
    status: str
    confidence: str
    units_computed: Decimal
    units_reported: Decimal | None = None
    delta_units: Decimal = Decimal(0)
    delta_value_pct: Decimal | None = None
    diagnosis: list[str] = field(default_factory=list)
    #: The V0-12 witness. None when there were no transactions to check.
    nav_check: NavCrossCheck | None = None


@dataclass(frozen=True)
class GateResult:
    """MODULE_1.md §11.3. V0 does not ship while `passed` is False."""

    passed: bool
    failures: list[Reconciliation]
    excluded_scheme_ids: set[SchemeId]


def diagnose(
    delta: Decimal, starts_at_zero: bool | None, txn_count: int
) -> list[str]:
    """Name a probable cause. MODULE_1.md §11.2.

    When reconciliation fails the system must say WHY — the actionable output
    is "import the statement covering before <date>", not "mismatch".

    Hypotheses needing warehouse lookups (IDCW events near the delta, merger
    ratios, round-multiple splits) are not reachable from this signature. The
    ones computable from the ledger alone are here, and "UNKNOWN" is returned
    rather than an empty list so a caller always has something to render.
    """
    hypotheses: list[str] = []

    if delta < 0:
        # Fewer units than the statement reports: something bought them that we
        # have not imported. Blame the start of the history only when it does
        # not demonstrably begin at zero — `starts_at_zero=True` means the first
        # transaction we hold opened the position, so pointing at an earlier
        # statement wastes time on a file that will not fix anything.
        #
        # `None` is the third state, and an unknown is reported rather than
        # suppressed: a missing early CAS is the most common cause of a
        # negative delta.
        if starts_at_zero is not True:
            hypotheses.append("MISSING_EARLY_CAS")
    else:
        # More units than reported: something sold them that we have not seen.
        hypotheses.append("MISSING_REDEMPTION")

    if (
        abs(delta) < FLOAT_CONTAMINATION_DELTA
        and txn_count > FLOAT_CONTAMINATION_MIN_TXNS
    ):
        hypotheses.append("FLOAT_CONTAMINATION")

    return hypotheses or ["UNKNOWN"]


def reconcile(
    scheme_id: SchemeId,
    folio: str,
    computed: Decimal,
    reported: Decimal | None,
    as_of: date,
    navs: dict[date, Decimal],
    txns: list[Txn],
) -> Reconciliation:
    """Both checks, always. MODULE_1.md §11.1.

    NOTE on the value check: it is implemented exactly as specified, and as
    specified it reduces to the unit delta expressed as a fraction, because the
    same NAV multiplies both sides. See the module docstring and V0-12. Use
    `nav_cross_check` for the wrong-plan detection §11.1 attributes to this.
    """
    if reported is None:
        return Reconciliation(
            scheme_id=scheme_id,
            folio=folio,
            as_of=as_of,
            status="warn",
            confidence="medium",
            units_computed=computed,
            diagnosis=["NO_REPORTED_BALANCE"],
        )

    delta_units = computed - reported

    nav = _nav_on_or_before(navs, as_of)
    value_computed = computed * nav if nav is not None else None
    value_reported = reported * nav if nav is not None else None
    delta_value = (
        abs(value_computed - value_reported) / value_reported
        if (value_computed is not None and value_reported and value_reported > 0)
        else Decimal(0)
    )

    # V0-12: the check that actually detects a wrong NAV series. Skipped when
    # there are no transactions to witness against — a caller checking a bare
    # unit count has nothing to cross-check.
    nav_check = nav_cross_check(txns, navs) if txns else None
    nav_ok = nav_check is None or nav_check.status != "fail"
    # `warn` is this module's existing word for "nothing to check against, so
    # the position is unverified rather than verified-correct" — the same
    # reasoning the missing-reported-balance path above already applies.
    nav_unverified = nav_check is not None and nav_check.status == "unverified"

    units_ok = abs(delta_units) <= UNIT_TOL
    value_ok = delta_value <= VALUE_TOL

    if units_ok and value_ok and nav_ok:
        return Reconciliation(
            scheme_id=scheme_id,
            folio=folio,
            as_of=as_of,
            status="warn" if nav_unverified else "ok",
            confidence="medium" if nav_unverified else "high",
            units_computed=computed,
            units_reported=reported,
            delta_units=delta_units,
            delta_value_pct=delta_value * 100,
            diagnosis=["NAV_SERIES_UNVERIFIED"] if nav_unverified else [],
            nav_check=nav_check,
        )

    findings = []
    if not units_ok or not value_ok:
        findings.extend(diagnose(delta_units, _starts_at_zero(txns), len(txns)))
    if not nav_ok:
        assert nav_check is not None
        # The actionable message: the units are fine, the SCHEME is wrong.
        findings.append("WRONG_NAV_SERIES")
        if units_ok:
            findings.append("LIKELY_PLAN_MISMATCH_DIRECT_VS_REGULAR")

    return Reconciliation(
        scheme_id=scheme_id,
        folio=folio,
        as_of=as_of,
        status="fail",
        confidence="low",
        units_computed=computed,
        units_reported=reported,
        delta_units=delta_units,
        delta_value_pct=delta_value * 100,
        diagnosis=findings or ["UNKNOWN"],
        nav_check=nav_check,
    )


def nav_cross_check(txns: list[Txn], navs: dict[date, Decimal]) -> NavCrossCheck:
    """Compare each statement-printed NAV against our resolved scheme's NAV.

    What §11.1 wanted the value check to do. A statement prints the NAV each
    transaction was priced at, so a disagreement with our resolved series means
    we resolved the wrong scheme — most often the wrong plan.

    V0-05 is exactly this: HDFC Direct NAVs against a Regular scheme record,
    ~10% apart, with units unaffected so neither specified check noticed.

    A date the fund did not price counts as `unavailable`: an absence is not a
    disagreement.
    """
    scheme_id = SchemeId(str(txns[0].scheme_id)) if txns else SchemeId("")
    matched = mismatched = unavailable = 0
    worst = Decimal(0)

    for t in drop_reversed(txns):
        if t.nav is None or t.nav <= 0:
            continue
        ours = navs.get(t.txn_date)
        if ours is None or ours <= 0:
            unavailable += 1
            continue
        deviation = abs(t.nav - ours) / ours * 100
        worst = max(worst, deviation)
        if deviation <= NAV_MATCH_TOL_PCT:
            matched += 1
        else:
            mismatched += 1

    checked = matched + mismatched
    return NavCrossCheck(
        scheme_id=scheme_id,
        checked=checked,
        matched=matched,
        mismatched=mismatched,
        unavailable=unavailable,
        worst_deviation_pct=worst,
        # Three states, not two. `status = "fail" if mismatched else "ok"` could
        # not tell 500 checked and 0 mismatched from nothing checked at all, and
        # the second is exactly V0-05's case: with no NAV rows loaded for the
        # scheme we wrongly resolved, the witness reported success at high
        # confidence. An absence of evidence is not evidence of agreement.
        status="fail" if mismatched else ("ok" if checked else "unverified"),
    )


def reconcile_all(
    book: LotBook,
    txns: list[Txn],
    navs: dict[str, dict[date, Decimal]],
    as_of: date,
) -> list[Reconciliation]:
    """Reconcile every (folio, scheme) the ledger touches.

    FIFO is folio-scoped (`PLAN.md` §9.7), so reconciliation is too. Folios
    aggregate for display only; a mismatch in one must not be netted away by a
    surplus in another.
    """
    # `drop_reversed` FIRST. The engine never saw the reversed pair
    # (`build_book` drops them) and `nav_cross_check` drops them internally, so
    # bucketing the raw list was the one place they survived: a reversed
    # redemption's printed running balance could become `units_reported`, and a
    # folio-scheme pair existing only in reversed rows produced computed == 0
    # against a live balance and failed the V0 gate for a position that is not
    # there.
    books: dict[tuple[str, SchemeId], list[Txn]] = {}
    for t in drop_reversed(txns):
        if t.scheme_id is None:
            continue
        books.setdefault((t.folio, t.scheme_id), []).append(t)

    results = []
    for (folio, scheme_id), scoped in sorted(books.items()):
        computed = sum(
            (
                lot.units_remaining
                for lot in book.all_lots()
                if lot.folio == folio and lot.scheme_id == scheme_id
            ),
            Decimal(0),
        )
        results.append(
            reconcile(
                scheme_id=scheme_id,
                folio=folio,
                computed=computed,
                reported=_latest_reported_balance(scoped, as_of),
                as_of=as_of,
                navs=navs.get(str(scheme_id), {}),
                txns=scoped,
            )
        )
    return results


def apply_gate(results: list[Reconciliation]) -> GateResult:
    """MODULE_1.md §11.3.

    A failing position is marked low confidence and EXCLUDED from every
    portfolio aggregate. Including it with a caveat would let a known-wrong
    number into a total that the user reads as authoritative.
    """
    failures = [r for r in results if r.status == "fail"]
    return GateResult(
        passed=not failures,
        failures=failures,
        excluded_scheme_ids={r.scheme_id for r in failures},
    )


def _latest_reported_balance(txns: list[Txn], as_of: date) -> Decimal | None:
    """The statement's printed balance from the last entry on or before `as_of`.

    Ordered by (date, seq) rather than by date alone: a same-day purchase and
    redemption print different running balances, and the later one is the one
    that stands.
    """
    candidates = [
        t for t in txns if t.txn_date <= as_of and t.units_balance_rep is not None
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda t: (t.txn_date, t.txn_seq, t.txn_ref))
    return latest.units_balance_rep


def _starts_at_zero(txns: list[Txn]) -> bool | None:
    """Did this folio's history begin with the earliest transaction we hold?

    The statement is the witness: it prints a running balance against every
    entry, so if the earliest one's balance equals its own units, the position
    opened there. A larger balance means units existed before anything we
    imported — the missing-early-CAS case.

    `None` when the statement printed no balance: unanswerable rather than
    answered either way, and `diagnose` treats an unknown as actionable.
    """
    live = [t for t in drop_reversed(txns) if t.units is not None]
    if not live:
        return None
    first = min(live, key=lambda t: (t.txn_date, t.txn_seq, t.txn_ref))
    if first.units_balance_rep is None:
        return None
    return abs(first.units_balance_rep) == abs(first.units or Decimal(0))


def _nav_on_or_before(navs: dict[date, Decimal], on: date) -> Decimal | None:
    candidates = [d for d in navs if d <= on]
    return navs[max(candidates)] if candidates else None
