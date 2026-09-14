"""Storing §4.3's portfolio-level metrics: concentration, overlap, duplication.

Separate from `persist.py` because it stores a different kind of thing. That
module writes the look-through itself — every issuer, every route to it, and the
summary. These three are *summaries of the summary*: they say nothing new about
what the user owns, only about its shape.

Same write rule as §4.2's tables, and for the same reason: everything here is
derived, so `CLAUDE.md` invariant 2's append-never-update does not apply and
invariant 10's replace-on-rebuild does. Delete the key, then insert — never
`INSERT OR REPLACE` alone, which updates what the new run still has and leaves
behind whatever it dropped. That has now been the defect twice (V1-18's exposure
tables, V1-20's issuer weights), in both cases invisible until a total came out
wrong.

**Nothing is aggregated in SQL.** Every figure here arrives already computed by
`concentration()`, `pairwise_overlap()` and `portfolio_duplication()`, all of
which aggregate in Python `Decimal` — invariant 1. This module only writes.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

from src.common.types import ExposureScope, IssuerId, SchemeId, UserId
from src.m3_lookthrough.concentration import Concentration
from src.m3_lookthrough.duplication import Duplication
from src.m3_lookthrough.overlap import Overlap
from src.m3_lookthrough.persist import DISCLOSED

#: §8.1 computes each independently, and the answers differ: a portfolio can
#: look diversified overall while its equity sleeve is not.
SCOPES: tuple[ExposureScope, ...] = ("all", "equity", "debt")

METRIC_TABLES = (
    "portfolio_concentration",
    "fund_overlap",
    "portfolio_duplication",
)


def _clear(
    conn: sqlite3.Connection,
    table: str,
    user_id: UserId,
    as_of: date,
    weight_basis: str,
) -> None:
    conn.execute(
        f"DELETE FROM {table} WHERE user_id = ? AND as_of = ? AND weight_basis = ?",
        (str(user_id), as_of.isoformat(), weight_basis),
    )


def save_concentration(
    conn: sqlite3.Connection,
    user_id: UserId,
    as_of: date,
    metrics: list[Concentration],
    weight_basis: str = DISCLOSED,
    computed_at: str | None = None,
) -> int:
    """§4.3. One row per scope. Returns rows written.

    `largest_issuer_pct` is `top1_pct`: the largest issuer's share of the scoped
    pool is what `top(1)` already computes. It is stored under both names
    because §4.3 declares both columns, and a reader who finds one NULL beside
    the other populated would reasonably conclude they mean different things.
    """
    stamp = computed_at or datetime.now(UTC).isoformat()
    _clear(conn, "portfolio_concentration", user_id, as_of, weight_basis)
    for metric in metrics:
        conn.execute(
            "INSERT INTO portfolio_concentration ("
            " user_id, as_of, weight_basis, scope, issuer_count, hhi, effective_n,"
            " top1_pct, top5_pct, top10_pct, top20_pct, gini,"
            " largest_issuer_id, largest_issuer_pct, computed_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(user_id), as_of.isoformat(), weight_basis, metric.scope,
                metric.issuer_count, metric.hhi, metric.effective_n,
                metric.top1_pct, metric.top5_pct, metric.top10_pct,
                metric.top20_pct, metric.gini,
                str(metric.largest_issuer_id) if metric.largest_issuer_id else None,
                metric.top1_pct,
                stamp,
            ),
        )
    conn.commit()
    return len(metrics)


def save_overlap(
    conn: sqlite3.Connection,
    user_id: UserId,
    as_of: date,
    pairs: list[Overlap],
    weight_basis: str = DISCLOSED,
) -> int:
    """§4.3. One row per unordered scheme pair. Returns rows written.

    `pairwise_overlap` has already put the lexicographically smaller scheme in
    `scheme_a`, so the primary key stores each pair once however the caller
    ordered its arguments.
    """
    _clear(conn, "fund_overlap", user_id, as_of, weight_basis)
    for pair in pairs:
        conn.execute(
            "INSERT INTO fund_overlap ("
            " user_id, as_of, weight_basis, scheme_a, scheme_b, overlap_pct,"
            " overlap_equity_pct, common_issuers, union_issuers, jaccard,"
            " overlap_value_inr, as_of_a, as_of_b, as_of_gap_days, aligned"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(user_id), as_of.isoformat(), weight_basis,
                str(pair.scheme_a), str(pair.scheme_b), pair.overlap_pct,
                pair.overlap_equity_pct, pair.common_issuers, pair.union_issuers,
                pair.jaccard, pair.overlap_value_inr,
                pair.as_of_a.isoformat(), pair.as_of_b.isoformat(),
                pair.as_of_gap_days, 1 if pair.aligned else 0,
            ),
        )
    conn.commit()
    return len(pairs)


def save_duplication(
    conn: sqlite3.Connection,
    user_id: UserId,
    as_of: date,
    metric: Duplication,
    weight_basis: str = DISCLOSED,
    computed_at: str | None = None,
) -> None:
    """§4.3. One row per user-date-basis."""
    stamp = computed_at or datetime.now(UTC).isoformat()
    _clear(conn, "portfolio_duplication", user_id, as_of, weight_basis)
    conn.execute(
        "INSERT INTO portfolio_duplication ("
        " user_id, as_of, weight_basis, duplicated_pct, duplicated_inr,"
        " issuers_multi_fund, max_funds_per_issuer, computed_at"
        ") VALUES (?,?,?,?,?,?,?,?)",
        (
            str(user_id), as_of.isoformat(), weight_basis,
            metric.duplicated_pct, metric.duplicated_inr,
            metric.issuers_multi_fund, metric.max_funds_per_issuer, stamp,
        ),
    )
    conn.commit()


def load_concentration(
    conn: sqlite3.Connection,
    user_id: UserId,
    as_of: date,
    scope: ExposureScope = "equity",
    weight_basis: str = DISCLOSED,
) -> Concentration | None:
    """One scope back. `None` when it was never computed for this date."""
    row = conn.execute(
        "SELECT scope, issuer_count, hhi, effective_n, top1_pct, top5_pct,"
        " top10_pct, top20_pct, gini, largest_issuer_id"
        " FROM portfolio_concentration"
        " WHERE user_id = ? AND as_of = ? AND weight_basis = ? AND scope = ?",
        (str(user_id), as_of.isoformat(), weight_basis, scope),
    ).fetchone()
    if row is None:
        return None
    return Concentration(
        scope=row[0],
        issuer_count=row[1],
        hhi=row[2],
        effective_n=row[3],
        top1_pct=row[4],
        top5_pct=row[5],
        top10_pct=row[6],
        top20_pct=row[7],
        gini=row[8],
        largest_issuer_id=IssuerId(row[9]) if row[9] else None,
    )


def load_overlap(
    conn: sqlite3.Connection,
    user_id: UserId,
    as_of: date,
    weight_basis: str = DISCLOSED,
) -> list[Overlap]:
    """Every stored pair, most overlapping first.

    **Sorted in Python.** `ORDER BY overlap_pct DESC` on a `DECIMAL_TEXT` column
    sorts it as text, so `"9.5"` outranks `"40.2"` because `'9' > '4'` — the
    same trap `load_exposures` documents, and it is worse here because a
    plausible-looking ordering is the entire output of an overlap table.
    """
    rows = conn.execute(
        "SELECT scheme_a, scheme_b, overlap_pct, overlap_equity_pct,"
        " common_issuers, union_issuers, jaccard, overlap_value_inr,"
        " as_of_a, as_of_b, as_of_gap_days, aligned FROM fund_overlap"
        " WHERE user_id = ? AND as_of = ? AND weight_basis = ?",
        (str(user_id), as_of.isoformat(), weight_basis),
    ).fetchall()
    pairs = [
        Overlap(
            scheme_a=SchemeId(r[0]),
            scheme_b=SchemeId(r[1]),
            overlap_pct=r[2],
            overlap_equity_pct=r[3],
            common_issuers=r[4],
            union_issuers=r[5],
            jaccard=r[6],
            as_of_a=date.fromisoformat(r[8]),
            as_of_b=date.fromisoformat(r[9]),
            as_of_gap_days=r[10],
            aligned=bool(r[11]),
            overlap_value_inr=r[7],
        )
        for r in rows
    ]
    pairs.sort(key=lambda o: (-o.overlap_pct, str(o.scheme_a), str(o.scheme_b)))
    return pairs


def load_duplication(
    conn: sqlite3.Connection,
    user_id: UserId,
    as_of: date,
    weight_basis: str = DISCLOSED,
) -> Duplication | None:
    row = conn.execute(
        "SELECT duplicated_pct, duplicated_inr, issuers_multi_fund,"
        " max_funds_per_issuer FROM portfolio_duplication"
        " WHERE user_id = ? AND as_of = ? AND weight_basis = ?",
        (str(user_id), as_of.isoformat(), weight_basis),
    ).fetchone()
    if row is None:
        return None
    return Duplication(
        duplicated_pct=row[0],
        duplicated_inr=row[1],
        issuers_multi_fund=row[2],
        max_funds_per_issuer=row[3],
    )


def drop_metrics(conn: sqlite3.Connection) -> None:
    """Drop all three. `CLAUDE.md` invariant 10, same as `drop_lookthrough`.

    A DROP rather than a DELETE: the claim is that these are reconstructible,
    and deleting rows leaves the schema standing and proves half of it.
    """
    for table in METRIC_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    # DROP is DDL, so this commit is a NO-OP under `sqlite3`'s legacy
    # isolation: a transaction opens before DML only, and the DROP above
    # already left `in_transaction` False. Measured, and it is why no test
    # holds this line where one holds every other commit in `src/`.
    #
    # Kept rather than deleted: it stops being a no-op the moment this
    # connection is opened with `isolation_level=None` or 3.12's
    # `autocommit`, and a reader who found a bare DROP with no commit
    # beside three functions that do commit would reasonably add one back.
    conn.commit()


__all__ = [
    "METRIC_TABLES",
    "SCOPES",
    "drop_metrics",
    "load_concentration",
    "load_duplication",
    "load_overlap",
    "save_concentration",
    "save_duplication",
    "save_overlap",
]
