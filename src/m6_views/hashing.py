"""`upstream_hash`. MODULE_6.md §14.1.

The hash is what a cache would key on, and **the hash is the truth**: no TTLs, no
manual invalidation. A daily NAV changes `portfolio_summary.computed_at`, which
changes the m3 probe, which changes every look-through view's hash. Nothing else
recomputes.

**There is no cache table yet**, deliberately — `MODULE_6.md`'s own build
sequence puts caching at step 12, after the views, and a cache with nothing in it
is configurability nobody asked for. The hash is computed anyway because
`upstream_hash` is a required field on the frozen `ViewEnvelope`, and because a
field that exists but is empty is the kind of thing that gets wired up wrong
later. It is correct now; the table that would use it can land whenever it earns
its place.

A builder declares what it reads through `required_sources()` rather than having
it inferred. §5.1: *"declared rather than inferred, so a cache key cannot
silently go stale when a builder starts reading a new source."* Inference would
have to trace calls through a provider protocol, which is exactly the kind of
cleverness that fails quietly.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from typing import Any

from src.m6_views.builder import Scope

#: §14.1's probe set. Only `m3` and `m1` exist — M2, M4 and M5 are unbuilt, and a
#: probe returning a constant for them would produce a hash that never changes,
#: which is worse than no hash because it looks live.
ProbeFn = Callable[[sqlite3.Connection, Scope], str]


def m3_probe(ledger: sqlite3.Connection, scope: Scope) -> str:
    """When was this user's look-through last recomputed?

    `computed_at` is stamped by `save_lookthrough` on every write, so it moves
    whenever the exposures do — a new disclosure, a new CAS, a re-run. Missing
    rows hash as `"none"`, which is a real state (nothing stored) and must not
    collide with a stored-but-empty one.
    """
    row = ledger.execute(
        "SELECT computed_at FROM portfolio_summary"
        " WHERE user_id = ? AND as_of = ?",
        (str(scope.user_id), scope.as_of.isoformat()),
    ).fetchone()
    return str(row[0]) if row and row[0] else "none"


def m1_probe(ledger: sqlite3.Connection, scope: Scope) -> str:
    """When was this user's position table last rebuilt?

    `max(rebuilt_at)` over the user's rows: a rebuild restamps them all, so the
    maximum moves on any change. Read as TEXT — `rebuilt_at` is an ISO string,
    not a `DECIMAL_TEXT` column, so `max()` here is a lexicographic maximum over
    ISO timestamps, which is chronological. Invariant 1's ban is on aggregating
    *decimals* in SQL; this aggregates a sortable string.
    """
    row = ledger.execute(
        "SELECT max(rebuilt_at) FROM position WHERE user_id = ?",
        (str(scope.user_id),),
    ).fetchone()
    return str(row[0]) if row and row[0] else "none"


VERSION_PROBES: dict[str, ProbeFn] = {"m1": m1_probe, "m3": m3_probe}


def upstream_hash(
    view_id: str,
    scope: Scope,
    params: dict[str, Any],
    required_sources: list[str],
    ledger: sqlite3.Connection | None = None,
) -> str:
    """§14.1. sha256 over the view, the scope, the params and every source.

    `sort_keys=True` on the params is what makes this deterministic: two callers
    passing the same options in a different order must produce the same hash, or
    the cache misses for no reason and the "hash is the truth" rule stops being
    true.

    An unknown module in `required_sources` contributes `"unbuilt:<module>"`
    rather than raising. That is the honest value for a source that does not
    exist yet, and it changes the moment a probe is registered for it — so a
    view that starts reading M5 next year gets a new hash automatically.
    """
    parts = [
        view_id,
        scope.scope_type,
        scope.scope_id or "",
        scope.as_of.isoformat(),
        json.dumps(params, sort_keys=True, default=str),
    ]
    for source in required_sources:
        module = source.split(".")[0]
        probe = VERSION_PROBES.get(module)
        if probe is None or ledger is None:
            parts.append(f"unbuilt:{module}")
        else:
            parts.append(f"{module}:{probe(ledger, scope)}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


__all__ = ["VERSION_PROBES", "m1_probe", "m3_probe", "upstream_hash"]
