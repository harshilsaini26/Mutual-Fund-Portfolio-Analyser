"""What a builder is given. MODULE_6.md §5.2.

§5.2's reference builder takes its providers as separate constructor arguments,
which works when you write one builder by hand and stops working when something
generic has to construct any of them: the caller needs to know which builder
wants which providers, and that knowledge ends up as a branch in the router.

One object instead. Every builder takes `Deps` and reaches for what it needs.
Adding M2's `SchemeCharacteristics` later is a field here and a line in whichever
builders use it — not a new constructor signature and another branch.

`ledger` is exposed alongside the providers for exactly one purpose: §14.1's
version probes, which read `computed_at` and `rebuilt_at` to decide whether a
cached payload is stale. That is metadata about *when* a table was written, not
data from it, and going through a provider for it would mean adding a method to
the frozen `LookThroughProvider` contract to serve M6's cache — §1.3's boundary
protects the numbers, and a timestamp is not one of them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from src.m1_ledger.providers.position import SqlitePositionProvider
from src.m3_lookthrough.providers.sqlite import SqliteLookThroughProvider


@dataclass(frozen=True)
class Deps:
    """The providers a view may read, and nothing else.

    M2, M4 and M5 are absent because they do not exist. When they land they
    arrive as fields here, and the views that cannot be built until then stay
    unregistered rather than stubbed — `registry.py` says why.
    """

    lookthrough: SqliteLookThroughProvider
    positions: SqlitePositionProvider
    ledger: sqlite3.Connection | None = None

    @classmethod
    def over(
        cls, ledger: sqlite3.Connection, warehouse: sqlite3.Connection
    ) -> Deps:
        """The usual wiring: Zone B for the user's data, Zone A for names."""
        return cls(
            lookthrough=SqliteLookThroughProvider(ledger, warehouse),
            positions=SqlitePositionProvider(ledger),
            ledger=ledger,
        )


__all__ = ["Deps"]
