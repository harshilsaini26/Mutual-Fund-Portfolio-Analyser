"""§4.3's three metrics: duplication, overlap in rupees, and a signed-pool Gini.

Written before the implementation, per `CLAUDE.md`'s working agreement.

All three are the same kind of number — a claim about the *whole* portfolio
rather than about one fund — and all three have a way of being confidently wrong.
Duplication can count an issuer's entire exposure as redundant instead of only
the part beyond its largest provider. Overlap in rupees can be a percentage
multiplied by a total rather than a sum of per-issuer minimums. And a Gini
coefficient computed over a pool containing a short position is not a small error
— it is a number with no meaning, printed beside numbers that have one.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.types import IssuerId, SchemeId, UserId
from src.m1_ledger.db import apply_ledger_schema, connect_ledger
from src.m3_lookthrough.concentration import (
    concentration,
    gini_coefficient,
    lorenz_points,
)
from src.m3_lookthrough.duplication import portfolio_duplication
from src.m3_lookthrough.engine import Contribution, Exposure, IssuerWeight
from src.m3_lookthrough.overlap import Overlap, pairwise_overlap
from src.m3_lookthrough.persist_metrics import (
    METRIC_TABLES,
    SCOPES,
    drop_metrics,
    load_duplication,
    load_overlap,
    save_concentration,
    save_duplication,
    save_overlap,
)

AS_OF = date(2026, 7, 31)


def c(scheme: str, issuer: str, inr: str) -> Contribution:
    return Contribution(SchemeId(scheme), IssuerId(issuer), Decimal(inr))


def w(issuer: str, weight: str, klass: str = "equity") -> IssuerWeight:
    return IssuerWeight(IssuerId(issuer), Decimal(weight), klass)


def e(issuer: str, inr: str, klass: str = "equity", synth: bool = False) -> Exposure:
    return Exposure(
        issuer_id=IssuerId(issuer),
        exposure_inr=Decimal(inr),
        pct_of_portfolio=Decimal(0),
        instrument_class=klass,
        is_synthetic=synth,
        fund_count=1,
    )


# --- §9.4 portfolio duplication ----------------------------------------------


def test_an_issuer_reached_through_one_fund_is_not_duplicated() -> None:
    """One route to an issuer is not redundancy, however large it is."""
    got = portfolio_duplication([c("S1", "ACME", "80000")], Decimal("80000"))
    assert got.duplicated_inr == Decimal(0)
    assert got.duplicated_pct == Decimal(0)
    assert got.issuers_multi_fund == 0


def test_the_redundant_portion_is_everything_beyond_the_largest_fund() -> None:
    """§9.4: "the exposure you would still have if you kept only the largest".

    The tempting wrong answer is the issuer's whole exposure — 80,000 here, which
    would report the user as twice as duplicated as they are. The right answer is
    what the *second and later* providers add: 80,000 - 60,000.
    """
    contributions = [c("S1", "ACME", "60000"), c("S2", "ACME", "20000")]
    got = portfolio_duplication(contributions, Decimal("200000"))
    assert got.duplicated_inr == Decimal("20000")
    assert got.duplicated_pct == Decimal("10")
    assert got.issuers_multi_fund == 1
    assert got.max_funds_per_issuer == 2


def test_three_funds_holding_one_issuer_leave_only_the_largest() -> None:
    contributions = [
        c("S1", "ACME", "50000"),
        c("S2", "ACME", "30000"),
        c("S3", "ACME", "20000"),
    ]
    got = portfolio_duplication(contributions, Decimal("100000"))
    assert got.duplicated_inr == Decimal("50000")
    assert got.max_funds_per_issuer == 3


def test_synthetics_are_never_counted_as_duplication() -> None:
    """Every fund holds cash. Counting `__CASH__` would put a floor under the
    figure for every portfolio ever built, which is exactly the low end where
    the number should be reassuring."""
    contributions = [
        c("S1", "__CASH__", "40000"),
        c("S2", "__CASH__", "30000"),
        c("S1", "__UNRESOLVED__", "10000"),
        c("S2", "__UNRESOLVED__", "10000"),
    ]
    got = portfolio_duplication(contributions, Decimal("90000"))
    assert got.duplicated_inr == Decimal(0)
    assert got.issuers_multi_fund == 0


def test_a_directly_held_issuer_is_not_a_second_fund() -> None:
    """§9.4 excludes `__DIRECT__`: owning a share yourself and owning it through
    a fund is not one fund duplicating another."""
    contributions = [c("S1", "ACME", "60000"), c("__DIRECT__", "ACME", "20000")]
    got = portfolio_duplication(contributions, Decimal("80000"))
    assert got.duplicated_inr == Decimal(0)
    assert got.issuers_multi_fund == 0


def test_two_lots_of_the_same_fund_are_one_fund() -> None:
    """`max_funds_per_issuer` counts distinct schemes, not contribution rows."""
    contributions = [c("S1", "ACME", "40000"), c("S1", "ACME", "20000")]
    got = portfolio_duplication(contributions, Decimal("60000"))
    assert got.max_funds_per_issuer == 1
    assert got.duplicated_inr == Decimal(0)


def test_duplicated_pct_is_zero_rather_than_undefined_on_an_empty_portfolio() -> None:
    assert portfolio_duplication([], Decimal(0)).duplicated_pct == Decimal(0)


# --- §4.3 overlap_value_inr --------------------------------------------------


def test_overlap_in_rupees_is_the_amount_both_funds_hold() -> None:
    """Σ min(w_a·V_a, w_b·V_b) per issuer.

    S1 holds 60,000 of ACME, S2 holds 20,000 — so 20,000 of the portfolio is
    ACME bought twice. The wrong answer, `overlap_pct` x some total, would
    depend on which total and would not be a rupee figure about anything.
    """
    got = pairwise_overlap(
        SchemeId("S1"), SchemeId("S2"), AS_OF, AS_OF,
        [w("ACME", "60"), w("BETA", "40")],
        [w("ACME", "40"), w("GAMMA", "60")],
        value_a=Decimal("100000"),
        value_b=Decimal("50000"),
    )
    assert got.overlap_value_inr == Decimal("20000")


def test_overlap_in_rupees_is_none_when_a_fund_has_no_value() -> None:
    """Unknown is not zero. A scheme with no NAV on the date has no rupee
    figure, and reporting 0 would read as "nothing is duplicated"."""
    got = pairwise_overlap(
        SchemeId("S1"), SchemeId("S2"), AS_OF, AS_OF,
        [w("ACME", "60")], [w("ACME", "40")],
        value_a=Decimal("100000"),
        value_b=None,
    )
    assert got.overlap_value_inr is None


def test_overlap_in_rupees_ignores_synthetics_like_every_other_overlap_figure() -> None:
    got = pairwise_overlap(
        SchemeId("S1"), SchemeId("S2"), AS_OF, AS_OF,
        [w("__CASH__", "100", "cash")], [w("__CASH__", "100", "cash")],
        value_a=Decimal("100000"),
        value_b=Decimal("100000"),
    )
    assert got.overlap_value_inr == Decimal(0)


def test_the_rupee_figure_is_unaffected_by_which_scheme_is_passed_first() -> None:
    """`pairwise_overlap` reorders its arguments so each pair is stored once.
    The rupee figure must survive that swap."""
    forward = pairwise_overlap(
        SchemeId("AAA"), SchemeId("ZZZ"), AS_OF, AS_OF,
        [w("ACME", "60")], [w("ACME", "40")],
        value_a=Decimal("100000"), value_b=Decimal("50000"),
    )
    reverse = pairwise_overlap(
        SchemeId("ZZZ"), SchemeId("AAA"), AS_OF, AS_OF,
        [w("ACME", "40")], [w("ACME", "60")],
        value_a=Decimal("50000"), value_b=Decimal("100000"),
    )
    assert forward.overlap_value_inr == reverse.overlap_value_inr == Decimal("20000")


def test_overlap_without_values_still_returns_every_other_column() -> None:
    """The rupee figure is additive. Callers that do not have position values —
    `--equal`, and every unit test written before this slice — keep working."""
    got = pairwise_overlap(
        SchemeId("S1"), SchemeId("S2"), AS_OF, AS_OF,
        [w("ACME", "60"), w("BETA", "40")],
        [w("ACME", "40"), w("GAMMA", "60")],
    )
    assert got.overlap_value_inr is None
    assert got.overlap_pct == Decimal("40")
    assert got.common_issuers == 1


# --- §8.3 Gini on a signed pool ----------------------------------------------


def test_gini_is_undefined_when_any_weight_is_negative() -> None:
    """V1-20 deferred this here. A fund's short leg makes an issuer's net
    exposure negative, and the Lorenz construction Gini summarises assumes a
    non-negative pool — the formula still returns a number, and it means
    nothing. None says so; 0.42 does not."""
    assert gini_coefficient([Decimal("0.6"), Decimal("-0.1")]) is None


def test_gini_still_works_on_the_ordinary_pool() -> None:
    """Regression: the common case must not change."""
    perfectly_even = gini_coefficient([Decimal("0.25")] * 4)
    assert perfectly_even is not None
    assert perfectly_even == Decimal(0)
    concentrated = gini_coefficient([Decimal("0.97"), Decimal("0.01")] )
    assert concentrated is not None
    assert concentrated > Decimal("0.4")


def test_gini_of_an_empty_or_worthless_pool_is_zero_not_none() -> None:
    """Distinguish "no inequality to measure" from "the question is invalid"."""
    assert gini_coefficient([]) == Decimal(0)
    assert gini_coefficient([Decimal(0), Decimal(0)]) == Decimal(0)


def test_concentration_reports_no_gini_for_a_portfolio_holding_a_net_short() -> None:
    """The other metrics survive: HHI, effective-N and the topN figures are all
    well defined on a signed pool. Only Gini is not, so only Gini goes NULL."""
    got = concentration([e("ACME", "100000"), e("SHORTED", "-5000")], "all")
    assert got.gini is None
    assert got.hhi is not None
    assert got.effective_n is not None
    assert got.issuer_count == 2


def test_concentration_still_reports_gini_for_a_long_only_portfolio() -> None:
    got = concentration([e("ACME", "60000"), e("BETA", "40000")], "all")
    assert got.gini is not None


# --- §8.3 the Lorenz curve ---------------------------------------------------
#
# This lives in M3 rather than in M6's builder, and not for tidiness: MODULE_6
# §2.1 forbids the view layer from deriving a number, and its WRONG example is
# literally `pct = holding.value / total * 100`. A Lorenz point is a cumulative
# share of a cumulative share — two divisions on provider-sourced values — so
# computing it in a builder would be the exact thing §19.3's static check exists
# to catch.


def test_the_lorenz_curve_starts_at_the_origin_and_ends_at_one() -> None:
    """Both endpoints are definitional. A curve that does not reach (1, 1) is
    not a Lorenz curve and the Gini beside it would not match it."""
    points = lorenz_points([e("ACME", "60000"), e("BETA", "40000")])
    assert points[0] == (Decimal(0), Decimal(0))
    assert points[-1] == (Decimal(1), Decimal(1))


def test_a_perfectly_even_portfolio_is_the_diagonal() -> None:
    """Four equal issuers: 25% of the companies hold 25% of the money."""
    equal = [e(f"I{i}", "25000") for i in range(4)]
    points = lorenz_points(equal)
    assert points == [
        (Decimal(0), Decimal(0)),
        (Decimal("0.25"), Decimal("0.25")),
        (Decimal("0.5"), Decimal("0.5")),
        (Decimal("0.75"), Decimal("0.75")),
        (Decimal(1), Decimal(1)),
    ]


def test_the_curve_is_built_smallest_first() -> None:
    """Lorenz reads left-to-right from the poorest share. Starting from the
    largest would draw the curve above the diagonal — a mirror image that looks
    like a plausible chart and inverts the meaning."""
    points = lorenz_points([e("BIG", "90000"), e("SMALL", "10000")])
    assert points[1] == (Decimal("0.5"), Decimal("0.1"))


def test_synthetics_are_excluded_like_every_other_concentration_figure() -> None:
    """§8.2's denominator trap. __CASH__ is not a company and counting it would
    flatten the curve toward the diagonal."""
    with_cash = [
        e("ACME", "60000"),
        e("BETA", "40000"),
        e("__CASH__", "100000", "cash", synth=True),
    ]
    assert lorenz_points(with_cash) == lorenz_points(
        [e("ACME", "60000"), e("BETA", "40000")]
    )


def test_a_signed_pool_has_no_lorenz_curve() -> None:
    """Same reason Gini goes None (V1-21): with a negative weight the cumulative
    share is not monotonic and the curve crosses its own diagonal. An empty list
    says so; a drawn curve would not."""
    assert lorenz_points([e("ACME", "100000"), e("SHORTED", "-5000")]) == []


def test_an_empty_pool_has_no_curve_rather_than_a_degenerate_one() -> None:
    assert lorenz_points([]) == []


# --- §4.3 storage: the rebuild rule -----------------------------------------
#
# Every test below was written because a mutant survived. The delete-then-insert
# in `persist_metrics` was copied from V1-18 and V1-20's lesson and then not
# tested, which is how the lesson gets unlearned: the code is right, nothing
# holds it right, and the next edit is free to undo it silently.


@pytest.fixture
def ledger(tmp_path: Path) -> sqlite3.Connection:
    db = connect_ledger(str(tmp_path / "personal.db"), key="test-key-not-a-secret")
    apply_ledger_schema(db)
    return db


USER = UserId("USER-01")
AS_OF = date(2026, 9, 4)
JULY = date(2026, 7, 31)


def pair(a: str, b: str, pct: str) -> Overlap:
    return Overlap(
        scheme_a=SchemeId(a), scheme_b=SchemeId(b),
        overlap_pct=Decimal(pct), overlap_equity_pct=Decimal(pct),
        common_issuers=1, union_issuers=2, jaccard=Decimal("0.5"),
        as_of_a=JULY, as_of_b=JULY, as_of_gap_days=0, aligned=True,
        overlap_value_inr=Decimal("1000"),
    )


def test_a_rerun_replaces_the_concentration_scopes(ledger: sqlite3.Connection) -> None:
    """Idempotent, like every other loader here — not an IntegrityError and not
    a second set of rows."""
    metrics = [concentration([e("ACME", "60000"), e("BETA", "40000")], s) for s in SCOPES]
    save_concentration(ledger, USER, AS_OF, metrics)
    save_concentration(ledger, USER, AS_OF, metrics)
    count = ledger.execute("SELECT count(*) FROM portfolio_concentration").fetchone()
    assert count[0] == len(SCOPES)


def test_a_sold_fund_takes_its_overlap_rows_with_it(ledger: sqlite3.Connection) -> None:
    """The V1-20 defect, in a new table. `INSERT OR REPLACE` updates the pairs
    the new run still has and leaves the pairs it dropped, so a fund sold last
    month keeps showing up in the overlap heatmap against funds that no longer
    exist beside it."""
    save_concentration(ledger, USER, AS_OF, [])
    save_overlap(
        ledger, USER, AS_OF,
        [pair("S1", "S2", "40"), pair("S1", "S3", "30"), pair("S2", "S3", "20")],
    )
    save_overlap(ledger, USER, AS_OF, [pair("S1", "S2", "40")])
    remaining = load_overlap(ledger, USER, AS_OF)
    assert [(str(o.scheme_a), str(o.scheme_b)) for o in remaining] == [("S1", "S2")]


def test_a_rerun_replaces_the_duplication_row(ledger: sqlite3.Connection) -> None:
    first = portfolio_duplication(
        [c("S1", "ACME", "60000"), c("S2", "ACME", "20000")], Decimal("200000")
    )
    save_duplication(ledger, USER, AS_OF, first)
    save_duplication(
        ledger, USER, AS_OF, portfolio_duplication([], Decimal("200000"))
    )
    stored = load_duplication(ledger, USER, AS_OF)
    assert stored is not None
    assert stored.duplicated_inr == Decimal(0)
    assert ledger.execute(
        "SELECT count(*) FROM portfolio_duplication"
    ).fetchone()[0] == 1


def test_largest_issuer_pct_is_stored_as_the_top1_share(
    ledger: sqlite3.Connection,
) -> None:
    """§4.3 declares `top1_pct` and `largest_issuer_pct` as separate columns and
    they hold the same quantity. Nothing reads the second back — the provider
    maps it from `top1_pct` — so without this assertion the column is
    write-only and free to hold anything at all."""
    metric = concentration([e("ACME", "60000"), e("BETA", "40000")], "all")
    save_concentration(ledger, USER, AS_OF, [metric])
    row = ledger.execute(
        "SELECT top1_pct, largest_issuer_pct, largest_issuer_id"
        " FROM portfolio_concentration WHERE scope = 'all'"
    ).fetchone()
    assert row[0] == row[1] == metric.top1_pct
    assert row[2] == "ACME"


def test_drop_metrics_drops_the_tables_rather_than_emptying_them(
    ledger: sqlite3.Connection,
) -> None:
    """`CLAUDE.md` invariant 10: the claim is that these are reconstructible.
    Deleting rows leaves the schema standing and proves half of it."""
    save_concentration(
        ledger, USER, AS_OF,
        [concentration([e("ACME", "60000")], s) for s in SCOPES],
    )
    drop_metrics(ledger)
    surviving = {
        r[0]
        for r in ledger.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert not surviving & set(METRIC_TABLES)


def test_the_metric_tables_come_back_on_a_rebuild(ledger: sqlite3.Connection) -> None:
    """The other half of invariant 10 — dropped is only safe if re-appliable."""
    drop_metrics(ledger)
    apply_ledger_schema(ledger, force=True)
    save_duplication(
        ledger, USER, AS_OF, portfolio_duplication([], Decimal("100000"))
    )
    assert load_duplication(ledger, USER, AS_OF) is not None
