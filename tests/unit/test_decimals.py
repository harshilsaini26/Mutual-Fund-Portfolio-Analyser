"""Decimal survives the SQLite boundary without precision loss.

MODULE_1.md §4.1 and `PLAN.md` §8.2 rule 1. Zone B stores money and units as
TEXT because SQLite has no decimal type and `REAL` is forbidden. These tests
prove the adapters hold that line, and — via the float control cases — show what
breaks when they do not.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal, getcontext

import pytest
from src.common.decimals import (
    DECIMAL_SQLITE_TYPE,
    EPS,
    MONEY_Q,
    NAV_Q,
    RECON_UNITS_TOLERANCE,
    UNITS_Q,
    UNSAFE_DECIMAL_TYPES,
    connect,
    dec,
)
from src.common.types import SchemeId

from tests.fakes.m0 import FakeMarketDataProvider

# NAVs and unit counts that no binary float can hold exactly. Every one is a
# realistic figure: 6-decimal units are what a CAS actually prints.
AWKWARD = [
    Decimal("0.1"),
    Decimal("1284.339072"),
    Decimal("142.839104"),
    Decimal("4821.442100"),
    Decimal("31402.118400"),
    Decimal("0.000001"),
    Decimal("-1792442.7266"),
    Decimal("19447000000000.0000"),
    Decimal("1234567890123456789.123456789012345"),
]


def _table(conn: sqlite3.Connection) -> None:
    # Declared DECIMAL_TEXT, not DECIMAL: the name contains "TEXT" so SQLite
    # gives it TEXT affinity, and it is distinct enough to key a converter on
    # without hijacking ordinary text columns.
    conn.execute(f"CREATE TABLE t (k TEXT PRIMARY KEY, v {DECIMAL_SQLITE_TYPE} NOT NULL)")


# --- constants -------------------------------------------------------------


def test_quantisation_constants_match_spec() -> None:
    """§4.1 verbatim. These are load-bearing: 6 dp is CAS unit precision."""
    assert UNITS_Q == Decimal("0.000001")
    assert MONEY_Q == Decimal("0.0001")
    assert NAV_Q == Decimal("0.000001")
    assert EPS == Decimal("0.0000005")
    assert EPS * 2 == UNITS_Q, "EPS is half of units precision"


def test_context_precision_is_34() -> None:
    """Wide enough that no intermediate in this project rounds."""
    assert getcontext().prec == 34
    assert getcontext().rounding == "ROUND_HALF_UP"


def test_recon_tolerance_is_coarser_than_unit_precision() -> None:
    """`PLAN.md` §7 V0 reconciles to 0.001 units, three orders above 1e-6.

    That headroom is exactly what float accumulation eats.
    """
    assert RECON_UNITS_TOLERANCE > UNITS_Q * 100


# --- the round trip --------------------------------------------------------


@pytest.mark.parametrize("value", AWKWARD, ids=str)
def test_decimal_round_trips_through_sqlite(value: Decimal) -> None:
    """Write a Decimal, read it back, get the same Decimal — not a float, not a str."""
    with connect(":memory:") as conn:
        _table(conn)
        conn.execute("INSERT INTO t VALUES (?, ?)", ("k", value))
        (back,) = conn.execute("SELECT v FROM t WHERE k = 'k'").fetchone()

    assert isinstance(back, Decimal), f"got {type(back).__name__}, not Decimal"
    assert back == value
    assert str(back) == str(value), "trailing zeros and scale must survive too"


def test_stored_column_is_text_not_real() -> None:
    """The value on disk is a decimal string, stored with TEXT affinity."""
    value = Decimal("1284.339072")
    with connect(":memory:") as conn:
        _table(conn)
        conn.execute("INSERT INTO t VALUES (?, ?)", ("k", value))
        (typename, raw) = conn.execute(
            "SELECT typeof(v), CAST(v AS TEXT) FROM t WHERE k = 'k'"
        ).fetchone()

    assert typename == "text", f"stored as {typename}, must be text"
    assert raw == "1284.339072"


@pytest.mark.parametrize("decl", sorted(UNSAFE_DECIMAL_TYPES))
def test_unsafe_column_types_silently_destroy_the_value(decl: str) -> None:
    """The trap, pinned.

    A column declared `DECIMAL` takes NUMERIC affinity — not TEXT — and SQLite
    converts the decimal string to a REAL on write. The adapter is correct and
    the value is still ruined, with no error anywhere.

    This is why `DECIMAL_SQLITE_TYPE` exists and why Zone B DDL must never
    declare a money column with any of these names.
    """
    value = Decimal("4821.442100")
    with sqlite3.connect(":memory:") as conn:
        conn.execute(f"CREATE TABLE t (v {decl})")
        conn.execute("INSERT INTO t VALUES (?)", (str(value),))
        (typename, raw) = conn.execute(
            "SELECT typeof(v), CAST(v AS TEXT) FROM t"
        ).fetchone()

    assert typename == "real", f"{decl} unexpectedly kept {typename} affinity"
    assert raw != str(value), f"{decl} preserved the value; the trap may be gone"
    assert raw == "4821.4421", "trailing zeros dropped by the REAL conversion"


def test_safe_column_types_keep_text_affinity() -> None:
    """TEXT, VARCHAR(n) and DECIMAL_TEXT all keep the string intact.

    `TEXT` is what MODULE_1.md §4.1's DDL declares, and it works — it just
    cannot key a converter, so the read side must call `dec()` itself.
    """
    value = Decimal("4821.442100")
    for decl in ("TEXT", "VARCHAR(64)", DECIMAL_SQLITE_TYPE):
        with sqlite3.connect(":memory:") as conn:
            conn.execute(f"CREATE TABLE t (v {decl})")
            conn.execute("INSERT INTO t VALUES (?)", (value,))
            (typename, raw) = conn.execute(
                "SELECT typeof(v), CAST(v AS TEXT) FROM t"
            ).fetchone()
        assert typename == "text", f"{decl} gave {typename} affinity"
        assert dec(raw) == value


def test_float_control_shows_what_the_adapter_prevents() -> None:
    """The failure mode, made explicit.

    Storing the same NAV as REAL loses the exact value. This test documents why
    `REAL` is forbidden rather than merely discouraged — and it is the reason
    `dec()` routes through `str`.
    """
    value = Decimal("1284.339072")
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE t (v REAL NOT NULL)")
        conn.execute("INSERT INTO t VALUES (?)", (float(value),))
        (back,) = conn.execute("SELECT v FROM t").fetchone()

    assert isinstance(back, float)
    # Round-trips numerically here, but the type is wrong and the exactness is
    # gone: reconstructing a Decimal from it captures the binary error.
    assert Decimal(back) != value
    assert dec(back) == value, "dec() goes via str, which repairs it"


def test_sip_accumulation_float_drifts_decimal_does_not() -> None:
    """The §4.1 rationale, demonstrated.

    240 monthly SIP instalments of a 6-decimal unit count. Decimal lands exactly
    on the answer; float misses it, and the gap is the kind that shows up as a
    reconciliation failure months later with no obvious cause.
    """
    instalment = Decimal("4821.442100")
    n = 240

    with connect(":memory:") as conn:
        _table(conn)
        for i in range(n):
            conn.execute("INSERT INTO t VALUES (?, ?)", (f"lot-{i}", instalment))
        rows = conn.execute("SELECT v FROM t").fetchall()

    total = sum((r[0] for r in rows), Decimal(0))
    assert total == instalment * n
    assert total == Decimal("1157146.104000")

    float_total = 0.0
    for _ in range(n):
        float_total += float(instalment)
    assert Decimal(str(float_total)) != total

    # And the drift is inside the 0.001-unit reconciliation tolerance, which is
    # what makes it dangerous: it does not trip the gate, it just makes the
    # number quietly wrong.
    drift = abs(Decimal(str(float_total)) - total)
    assert 0 < drift < RECON_UNITS_TOLERANCE


def test_quantisers_round_half_up() -> None:
    """ROUND_HALF_UP, not banker's rounding: .5 goes away from zero."""
    assert Decimal("1.0000005").quantize(UNITS_Q) == Decimal("1.000001")
    assert Decimal("1.0000015").quantize(UNITS_Q) == Decimal("1.000002")
    assert Decimal("10.00005").quantize(MONEY_Q) == Decimal("10.0001")
    assert Decimal("142.8391045").quantize(NAV_Q) == Decimal("142.839105")


def test_none_survives_dec() -> None:
    assert dec(None) is None
    assert dec("142.839104") == Decimal("142.839104")
    assert dec(Decimal("1.5")) == Decimal("1.5")


# --- end to end: a fake's value reaches SQLite intact ----------------------


def test_fake_nav_round_trips_through_sqlite() -> None:
    """A NAV read from a YAML fixture, stored and read back, unchanged.

    This is the whole chain Slice Zero has to get right: YAML -> Decimal ->
    SQLite TEXT -> Decimal. A float anywhere in it — PyYAML's default resolver
    is the likely culprit — would show up here.
    """
    from datetime import date

    provider = FakeMarketDataProvider()
    point = provider.nav(SchemeId("AXIS-MID-DIR"), date(2026, 7, 31))
    assert isinstance(point.nav, Decimal)

    with connect(":memory:") as conn:
        _table(conn)
        conn.execute("INSERT INTO t VALUES (?, ?)", (point.scheme_id, point.nav))
        (back,) = conn.execute("SELECT v FROM t").fetchone()

    assert back == point.nav == Decimal("142.839104")
    assert str(back) == "142.839104"


# --- SQL aggregation is the hole in the DECIMAL_TEXT discipline -------------
#
# Measured, not assumed. SQLite's `sum` uses compensated summation, so the
# textbook `0.1 * 10` case comes back as exactly 1.0 and proves nothing. What
# is always true is that the aggregate is a *float*: the type and the scale are
# gone, and the value follows once the operands exceed what float64 holds.


def test_sql_aggregation_always_returns_a_float() -> None:
    """CLAUDE.md invariant 1's second clause, and DECISIONS V1-15.

    The whole schema is `DECIMAL_TEXT`, every stored value is exact, and
    `assert_schema_is_decimal_safe` passes — and `SUM` still hands back a float.
    Coercion happens inside the aggregate regardless of the column's declared
    affinity, and the registered converter cannot help because the result of
    `SUM` is not a `DECIMAL_TEXT` column.

    This is the guaranteed half of the failure, and it holds on perfectly
    ordinary values. `sum(v)` here is numerically right and still wrong: it is
    `100.0` where the column holds `100.000000`, so the scale that says "six
    decimal places of weight" is gone, and the next arithmetic on it is float
    arithmetic.
    """
    weights = [Decimal("33.333333"), Decimal("33.333333"), Decimal("33.333334")]
    with connect(":memory:") as conn:
        _table(conn)
        for i, w in enumerate(weights):
            conn.execute("INSERT INTO t VALUES (?, ?)", (str(i), w))
        in_sql = conn.execute("SELECT sum(v) FROM t").fetchone()[0]
        sql_type = conn.execute("SELECT typeof(sum(v)) FROM t").fetchone()[0]
        stored_type = conn.execute("SELECT typeof(v) FROM t LIMIT 1").fetchone()[0]
        rows = [r[0] for r in conn.execute("SELECT v FROM t")]

    # Every stored value survived exactly.
    assert stored_type == "text"
    assert all(isinstance(v, Decimal) for v in rows)

    in_python = sum(rows, Decimal(0))
    assert in_python == Decimal("100.000000")

    # The aggregate did not survive as a Decimal, though it is numerically equal
    # in this case. Type and scale are lost even when the value is not.
    assert sql_type == "real"
    assert isinstance(in_sql, float)
    assert str(in_sql) == "100.0"
    assert str(in_python) == "100.000000"


def test_sql_aggregation_loses_value_once_precision_exceeds_float64() -> None:
    """And the other half: past float64's mantissa the number itself is wrong.

    Not a contrived magnitude. `getcontext().prec` is 34 in this project, and
    V1-06 recorded real weights carrying all 34 significant digits before
    quantisation — that is exactly where a `SUM` stops being merely untyped and
    starts being untrue.
    """
    with connect(":memory:") as conn:
        _table(conn)
        for i, v in enumerate(
            (Decimal("1234567.123456789012345"), Decimal("0.000000000000001"))
        ):
            conn.execute("INSERT INTO t VALUES (?, ?)", (str(i), v))
        in_sql = conn.execute("SELECT sum(v) FROM t").fetchone()[0]
        in_python = sum((r[0] for r in conn.execute("SELECT v FROM t")), Decimal(0))

    assert in_python == Decimal("1234567.123456789012346")
    assert Decimal(str(in_sql)) != in_python

    # And in rupees carried to paise, which is what a portfolio-wide exposure
    # figure is: the paise vanish entirely.
    with connect(":memory:") as conn:
        _table(conn)
        for i, v in enumerate((Decimal("9007199254740993.01"), Decimal("0.01"))):
            conn.execute("INSERT INTO t VALUES (?, ?)", (str(i), v))
        in_sql = conn.execute("SELECT sum(v) FROM t").fetchone()[0]
        in_python = sum((r[0] for r in conn.execute("SELECT v FROM t")), Decimal(0))

    assert in_python == Decimal("9007199254740993.02")
    assert Decimal(str(in_sql)) != in_python


def test_the_same_hole_is_in_avg_and_total_and_any_arithmetic() -> None:
    """`SUM` is not special. Anything that computes numerically coerces.

    Named explicitly because a rule that says "never SUM" invites reaching for
    `TOTAL` instead, which has the same problem and a more reassuring name.
    """
    with connect(":memory:") as conn:
        _table(conn)
        for i in range(3):
            conn.execute("INSERT INTO t VALUES (?, ?)", (str(i), Decimal("0.1")))
        for expression in ("sum(v)", "avg(v)", "total(v)", "max(v) + 0"):
            kind = conn.execute(f"SELECT typeof({expression}) FROM t").fetchone()[0]
            assert kind == "real", f"{expression} returned {kind}, not real"

        # `max` alone is a comparison, not arithmetic, so it returns the stored
        # value — but as `str`, because the converter keys on the column's
        # declared type and an aggregate has none. A different trap, same cause.
        assert conn.execute("SELECT typeof(max(v)) FROM t").fetchone()[0] == "text"
        assert isinstance(conn.execute("SELECT max(v) FROM t").fetchone()[0], str)
