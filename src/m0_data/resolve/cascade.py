"""The resolution cascade. MODULE_0.md §8.2.

Executed in order, first hit wins: known ISIN, synthetic rule, provisional
ISIN, alias table, fuzzy name, then the review queue. §8.1 is why any of it
exists — the exposure unit is the **issuer**, so every disclosed row has to end
up pointing at one.

Three departures from §8.2, all recorded in DECISIONS V1-02:

1. **A KNOWN ISIN beats a name rule.** §8.2 runs the synthetic rules at step 0,
   before ISIN. Against the real AMFI universe that captures **seven listed
   companies** — `Future Retail Ltd.`, `Future Consumer Limited`,
   `Future Enterprises Limited` and four more — because §8.4's derivative
   pattern matches the bare word `future`. A fund holding Future Retail, with a
   valid ISIN on the row, would have that equity bucketed as `__DERIV__`: the
   exposure disappears from the look-through and the derivative bucket inflates
   by the same amount.

   An ISIN that resolves to a known instrument is harder evidence than a word
   in a name, so it is tried first. This does not reopen the flood §8.4
   prevents — TREPS, cash and receivables rows carry no ISIN, so they still
   reach the rules at step 1.

2. **Fuzzy acceptance needs a second condition.** §8.2 auto-accepts on
   `token_set_ratio >= 92` alone, and that score is 100 for a name that merely
   *contains* another — `tech mahindra` against `mahindra mahindra`. See
   `fuzzy.py`.

3. **A valid ISIN that is unknown does not create a provisional issuer
   silently.** §8.2's `create_provisional` is kept, because a real ISIN is
   strong evidence of a real security and dropping it would lose the holding —
   but the row is also queued, so a human sees that a new issuer appeared
   rather than finding it later.

Nothing here returns None. `CLAUDE.md` invariant 4: an unresolvable row
resolves to `__UNRESOLVED__` and stays visible, never disappears from a join.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from src.common.types import UNRESOLVED, IssuerId
from src.m0_data.normalise.names import normalise_name
from src.m0_data.resolve.fuzzy import (
    REVIEW_MIN,
    best_matches,
    is_auto_acceptable,
    token_jaccard,
)
from src.m0_data.resolve.isin import is_valid_isin
from src.m0_data.resolve.synthetic import match_synthetic


@dataclass(frozen=True)
class Resolution:
    """Where a disclosed row landed, and how it got there.

    `method` and `confidence` map straight onto `Holding.resolution_method` and
    `Holding.resolution_conf`, which are frozen contract fields — the cascade
    was designed against them rather than the other way round.
    """

    issuer_id: IssuerId
    method: str  # rule|isin|alias|fuzzy|provisional|unresolved
    confidence: Decimal
    #: Set when the row should also be shown to a human, even though it
    #: resolved. A provisional issuer is the case that matters.
    needs_review: bool = False
    candidates: tuple[tuple[str, str, float], ...] = ()


def resolve(
    conn: sqlite3.Connection,
    raw_name: str,
    raw_isin: str | None = None,
    instrument_class: str | None = None,
    issuer_index: dict[str, str] | None = None,
) -> Resolution:
    """§8.2, in order. `issuer_index` maps normalised issuer name -> issuer_id.

    The index is passed in rather than queried per row because a disclosure has
    hundreds of rows and the candidate set is the same for all of them; loading
    it once per file rather than once per row is the difference between a
    second and a minute.
    """
    # 0. KNOWN ISIN. Ahead of the name rules, not behind them — see departure 1
    #    in the module docstring. ~95% of equity rows, because SEBI mandates
    #    the column, and it is the only unambiguous identifier on the row.
    known_isin = raw_isin and is_valid_isin(raw_isin)
    if known_isin:
        isin = str(raw_isin).strip().upper()
        row = conn.execute(
            "SELECT issuer_id FROM instrument WHERE isin = ?", (isin,)
        ).fetchone()
        if row:
            return Resolution(IssuerId(row[0]), "isin", Decimal("1.0"))

    # 1. RULE. Synthetic rows never reach the queue (§8.4) — subtotals, cash,
    #    TREPS and derivatives are most of what would otherwise flood it. None
    #    of them carries an ISIN, which is why step 0 does not shadow this.
    synthetic = match_synthetic(raw_name, instrument_class)
    if synthetic:
        return Resolution(synthetic, "rule", Decimal("1.0"))

    # 2. UNKNOWN BUT VALID ISIN. The check digit passed, so this is a real
    #    security in a real company; dropping it would lose a holding we have
    #    strong evidence about. Provisional, and queued rather than accepted.
    if known_isin:
        return Resolution(UNRESOLVED, "provisional", Decimal("0.5"), needs_review=True)

    # 3. ALIAS. Every accepted match writes one, so the queue shrinks
    #    monotonically (§8.5) and the same name is never asked about twice.
    norm = normalise_name(raw_name)
    row = conn.execute(
        "SELECT issuer_id, confidence FROM name_alias WHERE alias_norm = ?", (norm,)
    ).fetchone()
    if row:
        return Resolution(IssuerId(row[0]), "alias", row[1] or Decimal("1.0"))

    # 4. FUZZY, with the guard §8.2 lacks.
    index = issuer_index if issuer_index is not None else load_issuer_index(conn)
    candidates = tuple(best_matches(norm, index))
    if candidates:
        name, issuer_id, score = candidates[0]
        if is_auto_acceptable(score, token_jaccard(norm, name)):
            return Resolution(
                IssuerId(issuer_id), "fuzzy",
                Decimal(str(round(score / 100, 3))), candidates=candidates,
            )

    # 5. QUEUE. Never silently dropped — invariant 4.
    return Resolution(
        UNRESOLVED, "unresolved", Decimal(0), needs_review=True, candidates=candidates
    )


def load_issuer_index(conn: sqlite3.Connection) -> dict[str, str]:
    """Normalised issuer name -> issuer_id, for the fuzzy step.

    Synthetic issuers are excluded. `Cash & Bank Balance` normalises to
    `cash bank balance`, which would otherwise be a fuzzy candidate for any row
    mentioning a bank — and a bank holding matched onto `__CASH__` is a real
    equity exposure that vanishes from the look-through.
    """
    return {
        normalise_name(name): issuer_id
        for issuer_id, name in conn.execute(
            "SELECT issuer_id, canonical_name FROM issuer WHERE is_synthetic = 0"
        )
    }


def review_band(score: float) -> bool:
    """Whether a score is worth showing a human at all. §8.2's `FUZZY_REVIEW_MIN`."""
    return score >= REVIEW_MIN
