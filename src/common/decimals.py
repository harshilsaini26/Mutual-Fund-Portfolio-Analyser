"""Decimal storage convention.

MODULE_1.md §4.1. Zone B is SQLite + SQLCipher; SQLite has no native decimal
type and `REAL` is forbidden (`PLAN.md` §8.2 rule 1). All money and unit columns
use TEXT affinity holding decimal strings, converted through the adapters here.

Never `float`. Units carry 6 decimals in a CAS; float accumulation over hundreds
of SIP instalments produces reconciliation failures at exactly the 0.001 tolerance
that matters. Floats are permitted only inside the XIRR solver and must be
converted back at the boundary.
"""

from __future__ import annotations

import sqlite3
from decimal import ROUND_HALF_UP, Decimal, getcontext
from typing import Any

# --- context ---------------------------------------------------------------
# 34 significant digits is IEEE 754-2008 decimal128. Wide enough that no
# intermediate in this project rounds, and the arithmetic stays exact.
#
# NOTE: this mutates the *process-global* decimal context on import, exactly as
# MODULE_1.md §4.1 specifies. It is stated here because it is easy to miss: any
# library in the same process that assumes the 28-digit default sees 34 instead.
# Use `decimal.localcontext()` if a routine ever needs the default back.

getcontext().prec = 34
getcontext().rounding = ROUND_HALF_UP

# --- quantisation ----------------------------------------------------------

UNITS_Q = Decimal("0.000001")  # 6 dp — CAS precision
MONEY_Q = Decimal("0.0001")  # 4 dp
NAV_Q = Decimal("0.000001")  # 6 dp
EPS = Decimal("0.0000005")  # half of units precision

#: Reconciliation tolerance from `PLAN.md` §7 V0: |computed - reported| <= 0.001 units.
#: Three orders of magnitude coarser than UNITS_Q, which is the headroom float
#: arithmetic silently eats.
RECON_UNITS_TOLERANCE = Decimal("0.001")


def dec(v: Any) -> Decimal | None:
    """Coerce to Decimal via `str`, preserving None.

    Going through `str` is deliberate: `Decimal(0.1)` captures the binary
    float's full error (0.1000000000000000055511151231257827…), while
    `Decimal(str(0.1))` gives `Decimal('0.1')`. If a float ever reaches this
    boundary, this is the only thing standing between it and the ledger.
    """
    return None if v is None else Decimal(str(v))


def quantise_units(v: Decimal) -> Decimal:
    """Round to CAS unit precision, ROUND_HALF_UP."""
    return v.quantize(UNITS_Q)


def quantise_money(v: Decimal) -> Decimal:
    """Round to rupee money precision, ROUND_HALF_UP."""
    return v.quantize(MONEY_Q)


def quantise_nav(v: Decimal) -> Decimal:
    """Round to NAV precision, ROUND_HALF_UP."""
    return v.quantize(NAV_Q)


# --- SQLite adapters -------------------------------------------------------
#
# The declared column type decides affinity, and NUMERIC affinity rewrites a
# decimal string as a REAL, silently, on write:
#
#     CREATE TABLE t (v DECIMAL)   -- NUMERIC affinity
#     INSERT  '4821.442100'        -> typeof() = 'real', value 4821.4421
#
# Exactness and trailing zeros both gone, which `PLAN.md` §8.2 rule 1 forbids.
# Only the text rule gives TEXT affinity, so `DECIMAL`, `NUMERIC` and
# `DECIMAL(18,6)` are unsafe; `TEXT`, `VARCHAR(n)` and `DECIMAL_TEXT` are safe.
#
# `DECIMAL_TEXT` rather than the spec's bare `TEXT`: it contains "TEXT" so it
# takes TEXT affinity, and being a distinct name the converter cannot hijack
# ordinary text columns the way registering against `TEXT` would.

DECIMAL_SQLITE_TYPE = "DECIMAL_TEXT"
"""Declare Zone B money, unit, NAV and weight columns as this.

Not `DECIMAL` and not `NUMERIC`: both take NUMERIC affinity and silently store
a REAL. See the note above.
"""

#: Type names that SQLite would give NUMERIC or REAL affinity. Declaring a
#: money column as any of these loses precision on write.
UNSAFE_DECIMAL_TYPES = frozenset(
    {"DECIMAL", "NUMERIC", "REAL", "FLOAT", "DOUBLE", "DOUBLE PRECISION"}
)


def _adapt_decimal(d: Decimal) -> str:
    return str(d)


def _convert_decimal(b: bytes) -> Decimal:
    return Decimal(b.decode("utf-8"))


def register_decimal_sqlite(module: Any = sqlite3) -> None:
    """Register both directions of the Decimal <-> TEXT mapping.

    §4.1 registers only the adapter (write side). Without a converter the read
    side hands back `str` and every call site has to remember to re-wrap, which
    is the boundary a stray `float()` enters through.

    The converter fires only for `DECIMAL_TEXT` columns on connections opened
    with `detect_types=PARSE_DECLTYPES` — `connect()` below and
    `m1_ledger.db.connect_ledger` both handle it.

    **`module` exists because these registries are per-module, not global.**
    `sqlcipher3` is a separate driver with its own tables, so registering
    against stdlib `sqlite3` does nothing for an encrypted Zone B connection:
    turning encryption on detached the whole Decimal discipline. Writes raised
    `InterfaceError`, which is how this was found; reads would have returned
    `str` for every money column, silently.
    """
    module.register_adapter(Decimal, _adapt_decimal)
    module.register_converter(DECIMAL_SQLITE_TYPE, _convert_decimal)


def connect(path: str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection with Decimal handling already wired up.

    `detect_types=PARSE_DECLTYPES` is what makes the registered converter fire.
    Opening a connection any other way returns `str` for every money column, so
    prefer this over `sqlite3.connect` for anything touching a Decimal column.

    `check_same_thread=False` is an explicit opt-out, not a default. sqlite3's
    same-thread check is a real safety property — a connection shared across
    threads without external serialisation corrupts its own cursor state — and
    the only caller that needs it off is M6's HTTP API, which serves requests on
    an event loop that is not the thread that opened the database. It takes a
    lock around every use; see `src/m6_views/api/app.py`.
    """
    register_decimal_sqlite()
    return sqlite3.connect(
        path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=check_same_thread,
    )


# Registered on import so that a bare `sqlite3.connect(...)` still writes
# Decimals correctly, matching the spec's module-level registration.
register_decimal_sqlite()
