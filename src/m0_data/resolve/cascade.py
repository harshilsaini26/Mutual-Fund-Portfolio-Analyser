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

4. **An ISIN's issuer segment is tried before it is called unknown.** §8.2 has
   no such step and neither did this cascade, which made §8.1's own premise
   false for anything but equity: the exposure unit is the *issuer*, but an
   issuer's bonds and certificates of deposit carry different ISINs from its
   shares, and only the share ISIN is in the entity master. On ICICI
   Multi-Asset that left HDFC Bank resolved once and unresolved five times —
   a certificate of deposit, an AT1 bond and three more CDs, 1,939 Cr of one
   company's paper sitting in `__UNRESOLVED__` while its equity resolved
   cleanly. See `ISSUER_SEGMENT` and V1-29.

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

#: What this cascade currently is. Bumped whenever a change would resolve an
#: already-loaded row differently, and compared by `next_revision` so that the
#: next load re-resolves rather than reporting `skipped=1` over stale issuers.
#:
#: `"1"` is every cascade before the ISIN issuer segment existed; rows written
#: then carry `'0'` from the migration default, which is not equal to this and
#: is therefore what makes the first run after an upgrade actually rewrite.
RESOLVER_VERSION = "2"

#: How many leading characters of an Indian ISIN identify the ISSUER rather
#: than the security. `IN` is the country, the next five are the entity NSDL
#: allocated the code to, and characters 8-9 are the security type — so
#: `INE040A` is HDFC Bank whether what follows is `01034` (equity), `08419`
#: (an AT1 bond) or `16JC3` (a certificate of deposit).
#:
#: Measured against the whole entity master before this was relied on: 5,427
#: instruments produce 5,425 distinct segments, and the two that collide are
#: share-class pairs of one company that the master had already split into two
#: issuers. A colliding segment is therefore never resolved — see
#: `load_isin_prefix_index`.
ISSUER_SEGMENT = 7


@dataclass(frozen=True)
class Resolution:
    """Where a disclosed row landed, and how it got there.

    `method` and `confidence` map straight onto `Holding.resolution_method` and
    `Holding.resolution_conf`, which are frozen contract fields — the cascade
    was designed against them rather than the other way round.
    """

    issuer_id: IssuerId
    method: str  # rule|isin|isin_prefix|alias|fuzzy|provisional|unresolved
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
    prefix_index: dict[str, str] | None = None,
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
    known_isin = bool(raw_isin) and is_valid_isin(raw_isin)
    isin = str(raw_isin).strip().upper() if known_isin else ""
    if known_isin:
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

    # 1b. ISIN ISSUER SEGMENT. The exact ISIN is unknown, but its issuer
    #     segment may not be: a company's bonds and certificates of deposit are
    #     different securities of the SAME legal entity, and §8.1 says the
    #     exposure unit is that entity. Without this the by-issuer promise held
    #     only for equity.
    #
    #     Placed AFTER the synthetic rules rather than beside step 0. An exact
    #     ISIN is direct evidence and earned its precedence with a measurement
    #     (departure 1); a segment match is INFERRED from how ISINs are
    #     allocated, so it does not get to override a rule that exists to keep
    #     derivatives and cash out of the issuer space.
    if known_isin:
        prefixes = (
            prefix_index if prefix_index is not None
            else load_isin_prefix_index(conn)
        )
        found = prefixes.get(isin[:ISSUER_SEGMENT])
        if found:
            # Not 1.0. The segment identifies the issuer by construction, but
            # this row's own ISIN was never seen, so the instrument behind it
            # is still unverified — which is a real difference from step 0 and
            # should be visible in `resolution_conf`.
            return Resolution(IssuerId(found), "isin_prefix", Decimal("0.9"))

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


def load_isin_prefix_index(conn: sqlite3.Connection) -> dict[str, str]:
    """ISIN issuer segment -> issuer_id, for step 1b.

    **A segment that maps to more than one issuer is left out entirely**, so an
    ambiguous row falls through to `provisional` and is queued rather than
    being attached to whichever issuer happened to sort first. Two such
    segments exist in the current master, both share-class pairs of a single
    company — precisely the case where guessing would be silently wrong and
    nothing downstream could tell.

    Loaded once per file for the same reason `load_issuer_index` is: a
    disclosure has hundreds of rows and the candidate set does not change
    between them.
    """
    seen: dict[str, set[str]] = {}
    for isin, issuer_id in conn.execute("SELECT isin, issuer_id FROM instrument"):
        if not isin or len(str(isin)) != 12:
            continue
        seen.setdefault(str(isin)[:ISSUER_SEGMENT], set()).add(str(issuer_id))
    return {
        segment: next(iter(owners))
        for segment, owners in seen.items()
        if len(owners) == 1
    }


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
