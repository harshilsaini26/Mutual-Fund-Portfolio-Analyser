"""Which index is a fund measured against? MODULE_0.md §2.1 source S12.

**Recognition, not extraction.** The obvious way to get an index out of
`Kotak Nifty Alpha 50 Index Fund Growth` is to strip the house name, the share
class and the word "Fund" and keep the rest -- and it breaks on the first name
that does not fit the pattern, silently, producing a plausible index nobody
publishes. This goes the other way: NSE says which 259 indices exist, and a
fund is matched to the longest one whose name appears inside its own.

**Longest first is load-bearing.** `NIFTY50` is a substring of `NIFTY500`, so a
shortest-first or arbitrary-order search files every Nifty 500 fund under Nifty
50 -- two different markets, and the error is invisible in every downstream
number.

**Why this does not false-positive.** Measured against the 3,788 active equity
funds in this warehouse, which are not index funds and must not acquire a
benchmark this way: four matched, and all four are ETFs that spell out
"Exchange Traded Fund" rather than "ETF" -- correct matches, not false ones.
Every key in NSE's catalogue begins with `NIFTY` except five `BHARATBOND*`
entries, and that prefix is what keeps `Nifty Bank` from claiming every fund
with "Bank" in its name.

**What this deliberately cannot do.** An ACTIVE fund's name does not contain
its benchmark -- `Parag Parikh Flexi Cap Fund` is measured against NIFTY 500
and says so nowhere in its name. Those come from the S5 disclosure workbooks,
which print a labelled `Benchmark - ...` row; this module resolves the text
once something else has found it. A fund that matches nothing here keeps
`benchmark_id IS NULL`, which is the honest answer and not a gap to fill with
the nearest guess (invariant 5).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.m0_data.normalise.index_id import index_key, is_composite


@dataclass(frozen=True)
class Benchmark:
    """One resolved index, and how the match was made."""

    index_id: str
    index_name: str
    #: `name` when the index was found inside a scheme's own name, `declared`
    #: when the text came from a disclosure's benchmark row. A reader deciding
    #: how much to trust an alpha wants to know which.
    basis: str


class Ambiguous(ValueError):
    """The text names more than one index. S12: refuse rather than pick.

    Raised by `load._registered_index` when a level series' name keys to two
    registered indices. NSE's catalogue has no such pair today; the raise is
    what keeps a future one from silently filing a series under the wrong
    index.
    """


class BenchmarkMatcher:
    """Built once, asked 19,598 times.

    The key list is sorted at construction because rebuilding it per scheme is
    the difference between a second and several minutes over the warehouse.
    """

    def __init__(self, entries: list[tuple[str, str, str | None]]) -> None:
        """`entries` is `(index_id, index_name, trading_name)`."""
        self._by_key: dict[str, tuple[str, str]] = {}
        for index_id, name, trading in entries:
            for spelling in (name, trading):
                if not spelling:
                    continue
                key = index_key(spelling)
                # `setdefault`: the long name is registered first and wins, so
                # a trading name that collides with another index's long name
                # cannot steal it.
                if key:
                    self._by_key.setdefault(key, (index_id, name))
        self._keys = sorted(self._by_key, key=len, reverse=True)

    @classmethod
    def from_warehouse(cls, conn: sqlite3.Connection) -> BenchmarkMatcher:
        rows = conn.execute(
            "SELECT index_id, index_name, trading_name FROM benchmark_index"
            " WHERE is_total_return = 1"
        ).fetchall()
        return cls([(str(r[0]), str(r[1]), r[2]) for r in rows])

    def __len__(self) -> int:
        return len(self._keys)

    def match(self, text: str, *, basis: str = "name") -> Benchmark | None:
        """The longest catalogue index named inside `text`, or None.

        Returns None for a composite benchmark rather than its first leg. A
        fund measured against `85% Nifty 500 + 15% MSCI ACWI IT` has an alpha
        that Nifty 500 alone does not describe, and the wrong one would look
        entirely reasonable on screen.
        """
        if is_composite(text):
            return None
        subject = index_key(text)
        if not subject:
            return None
        for key in self._keys:
            if key in subject:
                index_id, name = self._by_key[key]
                return Benchmark(index_id=index_id, index_name=name, basis=basis)
        return None

    def exact(self, text: str, *, basis: str = "declared") -> Benchmark | None:
        """The catalogue index this text IS, by key equality, or None.

        For text that names an index outright -- a disclosure's benchmark
        row -- containment is the wrong test. `match` looks for an index
        inside a fund's name, and inside `Nifty Liquid Index A-I` it finds
        `Nifty Liquid Index`: a different index, and the plausible kind of
        wrong. Equality is identity; containment is recognition.
        """
        if is_composite(text):
            return None
        hit = self._by_key.get(index_key(text))
        if hit is None:
            return None
        return Benchmark(index_id=hit[0], index_name=hit[1], basis=basis)


def resolve_scheme_benchmarks(
    conn: sqlite3.Connection, matcher: BenchmarkMatcher | None = None
) -> dict[str, int]:
    """Fill `scheme.benchmark_id` from each scheme's own name, where it can.

    Only ever writes a scheme whose `benchmark_id` is NULL, so a benchmark that
    came from a disclosure -- a fact the AMC stated -- is never overwritten by
    one inferred from a name. Same precedence `load_navs_where_absent` uses
    between a publisher and a mirror.

    Returns counts, because "how many did this actually fill" is the only
    question worth asking of it.
    """
    matcher = matcher or BenchmarkMatcher.from_warehouse(conn)
    rows = conn.execute(
        "SELECT scheme_id, scheme_name FROM scheme WHERE benchmark_id IS NULL"
    ).fetchall()

    filled = 0
    for scheme_id, scheme_name in rows:
        found = matcher.match(str(scheme_name))
        if found is None:
            continue
        conn.execute(
            "UPDATE scheme SET benchmark_id = ?"
            " WHERE scheme_id = ? AND benchmark_id IS NULL",
            (found.index_id, scheme_id),
        )
        filled += 1
    return {"considered": len(rows), "filled": filled}


def resolve_declared(
    texts: list[str], matcher: BenchmarkMatcher
) -> tuple[Benchmark | None, str]:
    """What a disclosure's stated benchmarks resolve to, and why if nothing.

    Returns `(benchmark, reason)`. The reasons are the tally a run reports:
    `declared` for a resolved index, and otherwise the specific refusal.

    Strict on purpose. Every distinct text on the sheet must resolve to the
    same one index: a sheet stating an NSE index beside a CRISIL one has not
    said which is its primary, and using the one we happen to recognise is
    choosing for the AMC. No archived sheet states two today, so this costs
    nothing now and stops a guess later (invariant 5).
    """
    distinct = list(dict.fromkeys(texts))
    if not distinct:
        return None, "none stated"
    if any(is_composite(t) for t in distinct):
        return None, "composite"
    resolved = [matcher.exact(t) for t in distinct]
    if any(b is None for b in resolved):
        # CRISIL and BSE indices, a gold or silver price: real benchmarks,
        # just not ones NSE publishes a total-return series for.
        return None, "not an NSE index"
    ids = {b.index_id for b in resolved if b is not None}
    if len(ids) > 1:
        return None, "states two indices"
    return resolved[0], "declared"


def apply_declared(
    conn: sqlite3.Connection, plan: dict[tuple[str, str], Benchmark]
) -> dict[str, int]:
    """Write each family's declared benchmark onto every share class in it.

    `plan` maps `(amc_id, scheme_family)` to its benchmark. The whole family,
    because a disclosure describes a scheme and every share class of it holds
    the identical portfolio (V1-37) -- so Direct, Regular and each IDCW
    variant are measured against the same index.

    Overwrites a name-inferred benchmark. That is the precedence
    `resolve_scheme_benchmarks` was written for: a benchmark the AMC stated is
    a fact, one read out of a fund's name is an inference, and the inference
    only ever fills a NULL. Running this after `--resolve` or before it gives
    the same result.
    """
    counts = {"families": 0, "schemes": 0, "was_null": 0, "changed": 0, "unchanged": 0}
    for (amc_id, family), bm in plan.items():
        rows = conn.execute(
            "SELECT scheme_id, benchmark_id FROM scheme"
            " WHERE scheme_family = ? AND amc_id = ?",
            (family, amc_id),
        ).fetchall()
        if not rows:
            continue
        counts["families"] += 1
        for scheme_id, current in rows:
            counts["schemes"] += 1
            if current == bm.index_id:
                counts["unchanged"] += 1
                continue
            counts["was_null" if current is None else "changed"] += 1
            conn.execute(
                "UPDATE scheme SET benchmark_id = ? WHERE scheme_id = ?",
                (bm.index_id, scheme_id),
            )
    return counts
