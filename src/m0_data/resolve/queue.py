"""The review queue. MODULE_0.md §8.5.

**Prioritised by materiality, never by first-seen.** The top twenty rows
usually account for most unresolved value, so ordering by `total_mv_inr` is
what makes a few hundred decisions in month one tractable and single digits per
month thereafter. Ordering by arrival would put a Rs 3,000 position ahead of a
Rs 3 crore one and the queue would never be worth opening.

An entry is keyed on the normalised name, so the same unrecognised name across
twenty schemes is **one** decision rather than twenty — and resolving it writes
a `name_alias` row, which is why the queue shrinks monotonically.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.common.types import IssuerId


@dataclass(frozen=True)
class QueueEntry:
    queue_id: str
    raw_name: str
    raw_name_norm: str
    occurrence_count: int
    total_mv_inr: Decimal
    schemes_affected: int
    best_guess_issuer: str | None
    best_score: Decimal | None
    status: str


def queue_id_for(raw_name_norm: str) -> str:
    """Deterministic, so the same name always lands on the same row."""
    return hashlib.sha256(raw_name_norm.encode("utf-8")).hexdigest()[:32]


def enqueue(
    conn: sqlite3.Connection,
    raw_name: str,
    raw_name_norm: str,
    seen_on: date,
    raw_isin: str | None = None,
    market_value: Decimal | None = None,
    scheme_id: str | None = None,
    candidates: tuple[tuple[str, str, float], ...] = (),
) -> str:
    """Record one unresolved row, accumulating onto any existing entry.

    Materiality accumulates: the same name seen in five schemes carries the sum
    of five market values, which is what makes the priority ordering mean
    "resolving this recovers the most exposure".

    `candidates` is stored as JSON for the review UI. §8.5 wants the top five
    visible so a human can see what was considered — an empty list would leave
    them unable to tell a near-miss from a name nothing resembled.
    """
    qid = queue_id_for(raw_name_norm)
    best_name, best_issuer, best_score = (
        candidates[0] if candidates else ("", None, 0.0)
    )
    payload = json.dumps(
        [{"name": n, "issuer_id": i, "score": round(s, 2)} for n, i, s in candidates]
    )
    value = market_value or Decimal(0)

    conn.execute(
        """
        INSERT INTO resolution_queue (
            queue_id, raw_name, raw_name_norm, raw_isin, first_seen, last_seen,
            occurrence_count, total_mv_inr, schemes_affected,
            best_guess_issuer, best_score, candidates_json, status
        ) VALUES (?,?,?,?,?,?, 1, ?, 1, ?, ?, ?, 'pending')
        ON CONFLICT(queue_id) DO UPDATE SET
            last_seen        = excluded.last_seen,
            occurrence_count = resolution_queue.occurrence_count + 1,
            total_mv_inr     = resolution_queue.total_mv_inr + excluded.total_mv_inr,
            candidates_json  = excluded.candidates_json,
            best_guess_issuer= excluded.best_guess_issuer,
            best_score       = excluded.best_score
        """,
        (qid, raw_name, raw_name_norm, raw_isin, seen_on, seen_on, value,
         best_issuer, Decimal(str(round(best_score / 100, 3))), payload),
    )
    _ = best_name, scheme_id  # scheme fan-out is counted when holdings land
    return qid


def pending(conn: sqlite3.Connection, limit: int = 20) -> list[QueueEntry]:
    """The rows worth a human's time, most valuable first. §8.5."""
    rows = conn.execute(
        "SELECT queue_id, raw_name, raw_name_norm, occurrence_count, total_mv_inr,"
        " schemes_affected, best_guess_issuer, best_score, status"
        " FROM resolution_queue WHERE status = 'pending'"
        " ORDER BY total_mv_inr DESC, queue_id LIMIT ?",
        (limit,),
    ).fetchall()
    return [QueueEntry(*r) for r in rows]


def accept(
    conn: sqlite3.Connection,
    queue_id: str,
    issuer_id: IssuerId,
    created_by: str = "human",
) -> None:
    """Resolve one entry and write the alias that stops it recurring.

    The `name_alias` row is the point. Without it the same name returns to the
    queue on the next disclosure and the reviewer answers the same question
    every month — §8.5's "the queue shrinks monotonically" depends entirely on
    this write happening in the same transaction as the resolution.
    """
    row = conn.execute(
        "SELECT raw_name, raw_name_norm, raw_isin FROM resolution_queue"
        " WHERE queue_id = ?",
        (queue_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"no queue entry {queue_id}")
    raw_name, norm, raw_isin = row

    conn.execute(
        "INSERT INTO name_alias (alias_norm, alias_raw, issuer_id, isin, match_method,"
        " confidence, created_by) VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(alias_norm) DO UPDATE SET issuer_id = excluded.issuer_id,"
        " match_method = excluded.match_method, created_by = excluded.created_by",
        (norm, raw_name, str(issuer_id), raw_isin, "manual", Decimal("1.0"), created_by),
    )
    conn.execute(
        "UPDATE resolution_queue SET status='resolved', resolved_to=?,"
        " resolved_at=CURRENT_TIMESTAMP WHERE queue_id=?",
        (str(issuer_id), queue_id),
    )
