"""Weights, validation gates and the holdings loader. MODULE_0.md §7.3, §10, §4.6.

These are the steps between "we read the file" and "the look-through is
trustworthy". Each exists to stop a specific plausible-looking wrong number
reaching a chart.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.decimals import connect
from src.m0_data.derive.scheme_family import disclosure_scheme_for
from src.m0_data.load import load_holdings, next_revision, retire_siblings
from src.m0_data.normalise.family import family_key
from src.m0_data.normalise.weights import NormalisationError, normalise_weights
from src.m0_data.validate.checks import (
    HoldingRow,
    as_json,
    not_evaluated,
    promote_or_quarantine,
    validate_disclosure,
)

from tests.conftest import migrated

AS_OF = date(2026, 7, 31)
TODAY = date(2026, 9, 5)


def _rows(*specs: tuple[str | None, str, str, str]) -> list[HoldingRow]:
    return [
        HoldingRow(isin, klass, Decimal(mv), Decimal(pct), "AMFI:X")
        for isin, klass, mv, pct in specs
    ]


def _check(
    rows: list[HoldingRow], code: str, aum: Decimal | None = None, as_of: date = AS_OF
) -> object:
    return next(c for c in validate_disclosure(rows, as_of, TODAY, aum) if c.code == code)


# --- §7.3 weight normalisation -----------------------------------------------


def test_weights_sum_to_exactly_one_hundred() -> None:
    """`PLAN.md` §7 V1: exactly 100 per scheme-date, not approximately.

    Disclosed `% to NAV` sums to 98-102% and the error differs per scheme, so
    aggregating on it means look-through exposure does not equal portfolio
    value — and two funds' exposures are not comparable to each other either.
    """
    result = normalise_weights(
        [Decimal("60"), Decimal("30"), Decimal("10")],
        [Decimal("59.9"), Decimal("30.1"), Decimal("9.8")],
    )
    assert sum(result.weights) == Decimal(100)
    assert result.basis == "market_value"


def test_market_value_is_preferred_over_the_reported_percentage() -> None:
    """§7.3: recomputing from market value beats renormalising a rounded figure.

    The AMC rounds its percentage to two decimals; the market value is exact.
    HDFC reports ICICI Bank at 9.21%; recomputed it is 9.2091%.
    """
    result = normalise_weights(
        [Decimal("1"), Decimal("2")], [Decimal("40"), Decimal("60")]
    )
    assert result.basis == "market_value"
    # A third, at the stored precision — not the reported 40/60 split.
    assert result.weights == [Decimal("33.333333"), Decimal("66.666667")]
    assert sum(result.weights) == Decimal(100)


def test_percentages_are_used_when_there_are_no_market_values() -> None:
    result = normalise_weights([None, None], [Decimal("49"), Decimal("49")])
    assert result.basis == "pct_to_nav"
    assert sum(result.weights) == Decimal(100)


def test_the_residual_records_what_was_adjusted_away() -> None:
    """§7.3: `weight_residual` goes on the header so the adjustment is visible.

    Silently renormalising a 98.5% disclosure to 100% hides that 1.5% of the
    fund was never disclosed.
    """
    result = normalise_weights(
        [Decimal("50"), Decimal("48.5")], [Decimal("50"), Decimal("48.5")]
    )
    assert result.residual == Decimal("1.5")


def test_a_negative_position_keeps_a_negative_weight() -> None:
    """A short leg is real exposure. Forcing it positive overstates the net."""
    result = normalise_weights(
        [Decimal("100"), Decimal("-1")], [Decimal("101"), Decimal("-1")]
    )
    assert result.weights[1] < 0
    assert sum(result.weights) == Decimal(100)


def test_nothing_to_normalise_raises() -> None:
    """Returning zeros would produce a portfolio of nothing that sums to zero."""
    with pytest.raises(NormalisationError):
        normalise_weights([None, None], [None, None])
    with pytest.raises(NormalisationError):
        normalise_weights([], [])


# --- §10 validation gates ----------------------------------------------------


def test_v1_quarantines_a_disclosure_whose_weights_do_not_add_up() -> None:
    """§10.1 V1: reported % outside 95-105 means rows are missing or doubled."""
    assert _check(_rows((None, "equity", "100", "99.5")), "V1").passed  # type: ignore[attr-defined]
    bad = _check(_rows((None, "equity", "100", "60")), "V1")
    assert not bad.passed and bad.severity == "quarantine"  # type: ignore[attr-defined]


def test_v2_catches_a_units_error_by_two_orders_of_magnitude() -> None:
    """§10.1 V2, and it is THE check on §7.2's 100x path.

    Reading `Rs. in Lacs` as absolute understates the portfolio by 100,000.
    Nothing else notices: the weights still sum to 100, and only the absolute
    values are wrong — consistently, so they look internally coherent.
    """
    aum = Decimal("1107360000000")
    scaled = _rows((None, "equity", "1107364118000", "100"))
    assert _check(scaled, "V2", aum).passed  # type: ignore[attr-defined]

    unscaled = _rows((None, "equity", "11073641.18", "100"))
    v2 = _check(unscaled, "V2", aum)
    assert not v2.passed and v2.severity == "quarantine"  # type: ignore[attr-defined]


def test_v3_warns_rather_than_quarantines() -> None:
    """§10.1 V3: unresolved share warns, and blocks the look-through.

    The holdings are still true — we just cannot say whose they are. Showing an
    exposure chart anyway would be a lie of composition rather than of fact,
    and quarantining would throw away figures that are correct.
    """
    rows = [
        HoldingRow(None, "equity", Decimal("95"), Decimal("95"), "AMFI:X"),
        HoldingRow(None, "equity", Decimal("5"), Decimal("5"), "__UNRESOLVED__"),
    ]
    v3 = _check(rows, "V3")
    assert not v3.passed and v3.severity == "warn"  # type: ignore[attr-defined]
    assert (v3.observed or "").startswith("5.")  # type: ignore[attr-defined]


def test_v8_allows_a_negative_weight_only_on_a_derivative() -> None:
    """§10.1 V8, and this fired for real on HDFC's disclosure.

    The short leg of Eternal Limited resolved by ISIN to a real issuer, so it
    was classed `equity` and V8 failed — correctly. The fix was to carry the
    `OPTIONS` section heading down onto the row, because the instrument name
    cannot distinguish the short from the long position twelve rows above it.
    """
    assert not _check(_rows((None, "equity", "-88", "-0.01")), "V8").passed  # type: ignore[attr-defined]
    assert _check(_rows((None, "derivative", "-88", "-0.01")), "V8").passed  # type: ignore[attr-defined]


def test_v10_flags_an_isin_that_fails_its_check_digit() -> None:
    v10 = _check(_rows(("INE002A01019", "equity", "100", "100")), "V10")
    assert not v10.passed  # type: ignore[attr-defined]
    assert "INE002A01019" in (v10.observed or "")  # type: ignore[attr-defined]


def test_v11_quarantines_a_date_that_is_not_a_period_end() -> None:
    """§10.1 V11. A wrong as-of files the portfolio against a month it does not
    describe, and every drift figure computed from it looks plausible."""
    rows = _rows((None, "equity", "100", "100"))
    assert _check(rows, "V11").passed  # type: ignore[attr-defined]

    mid = _check(rows, "V11", as_of=date(2026, 7, 12))
    assert not mid.passed and mid.severity == "quarantine"  # type: ignore[attr-defined]
    assert not _check(rows, "V11", as_of=date(2027, 1, 31)).passed  # type: ignore[attr-defined]


def test_a_fortnight_end_is_valid_for_debt() -> None:
    """Debt schemes disclose fortnightly, so the 15th is a real period end."""
    rows = _rows((None, "debt", "100", "100"))
    assert _check(rows, "V11", as_of=date(2026, 7, 15)).passed  # type: ignore[attr-defined]


def test_promotion_distinguishes_quarantine_from_warn() -> None:
    """§10.2. A warn still loads; a quarantine does not."""
    assert (
        promote_or_quarantine(
            validate_disclosure(_rows((None, "equity", "100", "100")), AS_OF, TODAY)
        )
        == "ok"
    )
    assert (
        promote_or_quarantine(
            validate_disclosure(_rows(("BAD", "equity", "100", "100")), AS_OF, TODAY)
        )
        == "warn"
    )
    assert (
        promote_or_quarantine(
            validate_disclosure(_rows((None, "equity", "100", "10")), AS_OF, TODAY)
        )
        == "quarantined"
    )


def test_every_result_is_persisted_including_the_passes() -> None:
    """§10.2's closing instruction, and it is easy to skip.

    When a number looks wrong six months later, knowing which checks PASSED
    narrows the search as much as knowing which failed.
    """
    payload = json.loads(
        as_json(validate_disclosure(_rows((None, "equity", "100", "100")), AS_OF, TODAY))
    )
    assert any(entry["passed"] is True for entry in payload)
    assert {"V1", "V2", "V3", "V7", "V8", "V10", "V11"} <= {e["code"] for e in payload}


def test_the_gate_names_the_checks_it_cannot_run() -> None:
    """A gate that quietly checks less than it claims is worse than one that
    claims less. V4-V6 and V9 need history or a cross-scheme view."""
    skipped = not_evaluated()
    assert {"V4", "V5", "V6", "V9", "V12", "V13", "V14"} == set(skipped)
    assert all(reason for reason in skipped.values())


# --- §4.6 the loader ---------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "w.db"
    migrated(db)
    connection = connect(str(db))
    connection.execute(
        "INSERT INTO issuer (issuer_id, canonical_name) VALUES (?, ?)",
        ("AMFI:X", "X Ltd"),
    )
    connection.execute(
        "INSERT INTO scheme (scheme_id, scheme_name, plan, option) VALUES (?,?,?,?)",
        ("INF179K01UT0", "HDFC Flexi Cap", "direct", "growth"),
    )
    for file_id in ("file-a", "file-b"):
        connection.execute(
            "INSERT INTO raw_file (file_id, source_id, fetched_at, byte_size,"
            " storage_path) VALUES (?,?,?,?,?)",
            (file_id, "S5:hdfc", "2026-09-05", 1, "/x"),
        )
    connection.commit()
    yield connection
    connection.close()


def _payload() -> tuple[list[dict[str, object]], dict[str, object]]:
    rows: list[dict[str, object]] = [
        {
            "isin": "INE002A01018",
            "issuer_id": "AMFI:X",
            "instrument_raw_name": "X Ltd",
            "quantity": Decimal("10"),
            "market_value": Decimal("100"),
            "pct_to_nav": Decimal("100"),
            "pct_normalised": Decimal("100"),
            "instrument_class": "equity",
            "resolution_method": "isin",
            "resolution_conf": Decimal("1.0"),
        }
    ]
    header: dict[str, object] = {
        "unresolved_mv_pct": Decimal("0"),
        "total_mv": Decimal("100"),
        "validation_status": "ok",
    }
    return rows, header


def test_reloading_identical_bytes_does_not_create_a_revision(
    conn: sqlite3.Connection,
) -> None:
    """A revision means the AMC published something different, not that cron fired.

    `source_file_id` is the sha256 of the bytes, so an unchanged file is
    recognised and skipped — the same content-addressed idempotence the archive
    and every other loader has. Without it a nightly re-run stacks revisions
    until the number tells you only how many times the job ran.
    """
    rows, header = _payload()
    first = load_holdings(conn, "INF179K01UT0", AS_OF, rows, header, "file-a")
    assert first["revision"] == 1
    conn.commit()

    again = load_holdings(conn, "INF179K01UT0", AS_OF, rows, header, "file-a")
    conn.commit()
    assert again["revision"] == 1
    assert again.get("skipped") == 1
    assert conn.execute("SELECT COUNT(*) FROM holding").fetchone()[0] == 1


def test_a_restated_disclosure_becomes_a_new_revision(
    conn: sqlite3.Connection,
) -> None:
    """`CLAUDE.md` invariant 2: never UPDATE a fact row.

    The earlier version is still what we reported at the time, so it keeps its
    rows and loses `is_current`. AMCs do restate.
    """
    rows, header = _payload()
    load_holdings(conn, "INF179K01UT0", AS_OF, rows, header, "file-a")
    load_holdings(conn, "INF179K01UT0", AS_OF, rows, header, "file-b")
    conn.commit()

    assert conn.execute(
        "SELECT revision, is_current FROM holding_disclosure ORDER BY revision"
    ).fetchall() == [(1, 0), (2, 1)]
    assert (
        conn.execute("SELECT COUNT(*) FROM holding WHERE is_current=1").fetchone()[0] == 1
    )
    assert conn.execute("SELECT COUNT(*) FROM holding").fetchone()[0] == 2


def test_next_revision_returns_none_for_an_unchanged_file(
    conn: sqlite3.Connection,
) -> None:
    rows, header = _payload()
    load_holdings(conn, "INF179K01UT0", AS_OF, rows, header, "file-a")
    conn.commit()
    assert next_revision(conn, "INF179K01UT0", AS_OF, "file-a") is None
    assert next_revision(conn, "INF179K01UT0", AS_OF, "file-b") == 2


def test_a_sibling_share_class_is_served_the_scheme_s_disclosure(
    conn: sqlite3.Connection,
) -> None:
    """V1-37. The portfolio belongs to the scheme; the ISIN is a share class.

    Before this, `holding.scheme_id` was whichever ISIN a file happened to be
    loaded against and every sibling resolved to `__NO_DISCLOSURE__` — and the
    siblings being refused were mostly REGULAR plans, which is what
    distributors sell.
    """
    rows, header = _payload()
    load_holdings(conn, "INF179K01UT0", AS_OF, rows, header, "file-a")
    # Both share classes, as AMFI publishes them: one name carrying the plan
    # and option, one carrying only the option.
    for scheme_id, name, plan in (
        ("INF179K01UT0", "HDFC Flexi Cap Fund - Growth Option - Direct Plan", "direct"),
        ("INF179K01608", "HDFC Flexi Cap Fund - Growth Plan", "regular"),
    ):
        conn.execute(
            "INSERT OR REPLACE INTO scheme (scheme_id, scheme_name, plan, option,"
            " amc_id, status, scheme_family) VALUES (?,?,?,'growth','hdfc',"
            " 'active', ?)",
            (scheme_id, name, plan, family_key(name)),
        )
    conn.commit()
    # The key is derived, not asserted into place — both names must reduce to it.
    assert family_key("HDFC Flexi Cap Fund - Growth Plan") == "hdfc flexi cap fund"

    # The loaded ISIN answers for itself, unchanged and without a family lookup.
    assert disclosure_scheme_for(conn, "INF179K01UT0") == "INF179K01UT0"
    # Its Regular sibling is served the same disclosure.
    assert disclosure_scheme_for(conn, "INF179K01608") == "INF179K01UT0"


def test_a_quarantined_share_class_is_served_a_sibling_that_passed(
    conn: sqlite3.Connection,
) -> None:
    """V1-66 made a quarantined disclosure unusable, but this lookup still
    counted it: a share class whose own disclosure was quarantined answered for
    itself, and got no portfolio at all while its sibling's passed. They hold
    the same portfolio, so the sibling's is the right one to serve."""
    rows, header = _payload()
    # The quarantined one is the NEWER, so the sibling ordering alone would
    # pick it: both filters are under test, not just the first.
    load_holdings(conn, "INF179K01UT0", AS_OF + timedelta(days=31), rows, header,
                  "file-a")
    load_holdings(conn, "INF179K01608", AS_OF, rows, header, "file-b")
    for scheme_id, name, plan in (
        ("INF179K01UT0", "HDFC Flexi Cap Fund - Growth Option - Direct Plan", "direct"),
        ("INF179K01608", "HDFC Flexi Cap Fund - Growth Plan", "regular"),
    ):
        conn.execute(
            "INSERT OR REPLACE INTO scheme (scheme_id, scheme_name, plan, option,"
            " amc_id, status, scheme_family) VALUES (?,?,?,'growth','hdfc',"
            " 'active', ?)",
            (scheme_id, name, plan, family_key(name)),
        )
    conn.execute(
        "UPDATE holding_disclosure SET validation_status = 'quarantined'"
        " WHERE scheme_id = 'INF179K01UT0'"
    )
    conn.commit()

    assert disclosure_scheme_for(conn, "INF179K01UT0") == "INF179K01608"
    assert disclosure_scheme_for(conn, "INF179K01608") == "INF179K01608"


def test_a_share_class_is_served_its_familys_newest_disclosure(
    conn: sqlite3.Connection,
) -> None:
    """A disclosure filed under a different share class of the fund than last
    month's -- which merged families cause (Kotak Banking and PSU Debt: FO3 in
    July, KH7 in August) -- must not leave the old share class answering for
    itself with July's portfolio forever. Newest wins; its own on a tie."""
    rows, header = _payload()
    load_holdings(conn, "INF179K01UT0", AS_OF, rows, header, "file-a")
    load_holdings(conn, "INF179K01608", AS_OF + timedelta(days=31), rows, header,
                  "file-b")
    for scheme_id, name, plan in (
        ("INF179K01UT0", "HDFC Flexi Cap Fund - Growth Option - Direct Plan", "direct"),
        ("INF179K01608", "HDFC Flexi Cap Fund - Growth Plan", "regular"),
    ):
        conn.execute(
            "INSERT OR REPLACE INTO scheme (scheme_id, scheme_name, plan, option,"
            " amc_id, status, scheme_family) VALUES (?,?,?,'growth','hdfc',"
            " 'active', ?)",
            (scheme_id, name, plan, family_key(name)),
        )
    conn.commit()
    assert disclosure_scheme_for(conn, "INF179K01UT0") == "INF179K01608"

    load_holdings(conn, "INF179K01UT0", AS_OF + timedelta(days=31), rows, header,
                  "file-a")
    assert disclosure_scheme_for(conn, "INF179K01UT0") == "INF179K01UT0"
    assert disclosure_scheme_for(conn, "INF179K01608") == "INF179K01608"


def test_a_sheet_refiled_under_a_sibling_retires_the_old_filing(
    conn: sqlite3.Connection,
) -> None:
    """When AMFI's scheme master merged Kotak Banking and PSU Debt's plans,
    `canonical_scheme` moved the fund's sheet from the Regular ISIN to the
    Direct one, and the Regular filing of the same file and date stayed current
    beside it: one fund, one date, two portfolios, which a rebuild from the
    archive never produces. Only that file's restatement retires it."""
    direct, regular = "INF179K01UT0", "INF179K01608"
    name = "HDFC Flexi Cap Fund - Growth Plan"
    for scheme_id, plan in ((direct, "direct"), (regular, "regular")):
        conn.execute(
            "INSERT OR REPLACE INTO scheme (scheme_id, scheme_name, plan, option,"
            " amc_id, status, scheme_family) VALUES (?,?,?,'growth','hdfc',"
            " 'active', ?)",
            (scheme_id, name, plan, family_key(name)),
        )
    rows, header = _payload()
    later = AS_OF + timedelta(days=31)
    load_holdings(conn, regular, AS_OF, rows, header, "file-a")  # the old pick
    load_holdings(conn, regular, later, rows, header, "file-b")  # not restated
    load_holdings(conn, direct, AS_OF, rows, header, "file-a")  # the new pick

    assert retire_siblings(conn, direct, None, "hdfc", AS_OF, "file-a") == []
    assert retire_siblings(
        conn, direct, family_key(name), "hdfc", AS_OF, "file-a"
    ) == [regular]
    current = "SELECT DISTINCT scheme_id, as_of_date FROM {} WHERE is_current = 1"
    for table in ("holding_disclosure", "holding"):
        assert sorted(conn.execute(current.format(table))) == sorted([
            (direct, AS_OF), (regular, later)
        ])

    # Another file's filing of that date is not this file's to retire.
    load_holdings(conn, regular, AS_OF, rows, header, "file-c")
    assert retire_siblings(
        conn, direct, family_key(name), "hdfc", AS_OF, "file-a"
    ) == []


def test_a_scheme_with_no_family_behaves_exactly_as_before(
    conn: sqlite3.Connection,
) -> None:
    """NULL `scheme_family` is the safe state and the default.

    It means this scheme does not fan out — either its family could not be
    shown coherent, or the derive step has not run. Both must degrade to the
    warehouse's pre-V1-37 behaviour rather than to a guess, so that skipping
    the derivation loses coverage and never invents it.
    """
    conn.execute("UPDATE scheme SET scheme_family=NULL WHERE scheme_id='INF179K01608'")
    conn.commit()
    assert disclosure_scheme_for(conn, "INF179K01608") == "INF179K01608"


def test_an_improved_resolver_produces_a_new_revision_not_a_skip(
    conn: sqlite3.Connection,
) -> None:
    """V1-29. `issuer_id` is DERIVED, so identical bytes read by a better
    cascade are a different disclosure as far as the warehouse is concerned.

    Skipping on the bytes alone made MODULE_0.md §3's promise false — "a bug in
    any one of them is fixed by re-running from the layer to its left". You
    could re-run all you liked: the job reported the improved figure and wrote
    nothing, and the stale issuer stayed current. Observed exactly that on
    ICICI Multi-Asset, which reported 15.98% unresolved over stored rows that
    were still the 23.25% ones.
    """
    rows, header = _payload()
    load_holdings(conn, "INF179K01UT0", AS_OF, rows, header, "file-a", "1")
    conn.commit()

    # Same bytes, same cascade -> still a skip. This is the property that keeps
    # a nightly cron from stacking revisions, and it must survive the change.
    assert next_revision(conn, "INF179K01UT0", AS_OF, "file-a", "1") is None

    # Same bytes, better cascade -> a new revision.
    assert next_revision(conn, "INF179K01UT0", AS_OF, "file-a", "2") == 2

    again = load_holdings(conn, "INF179K01UT0", AS_OF, rows, header, "file-a", "2")
    conn.commit()
    assert again["revision"] == 2
    assert again.get("skipped") is None

    # Invariant 2 holds: revision 1 keeps its rows and loses is_current.
    assert conn.execute(
        "SELECT revision, is_current, resolver_version FROM holding_disclosure"
        " ORDER BY revision"
    ).fetchall() == [(1, 0, "1"), (2, 1, "2")]


def test_a_check_that_could_not_run_is_not_a_pass() -> None:
    """V1-48. V2 is the units check, and with no AUM on record it records
    `passed = None` -- did not run -- rather than `True`.

    It used to say `True`, so `validation_notes` for every disclosure in the
    warehouse claimed the 100x guard had succeeded. `scheme_aum` is not built,
    so it has never run on any of them.
    """
    from datetime import date
    from decimal import Decimal

    from src.m0_data.validate.checks import (
        HoldingRow,
        promote_or_quarantine,
        validate_disclosure,
    )

    rows = [HoldingRow("INE001A01036", "equity", Decimal("100"), Decimal("100"), "I1")]
    checks = validate_disclosure(rows, date(2026, 7, 31), date(2026, 8, 5), None)

    v2 = next(c for c in checks if c.code == "V2")
    assert v2.passed is None
    # An unrun check neither promotes nor fails the disclosure. `not None` is
    # True, so a naive `not r.passed` here would have failed every one of them.
    assert promote_or_quarantine(checks) == "ok"
