"""Fuzzy name matching. MODULE_0.md §8.2 step 3, on the standard library.

§8.2 specifies `token_set_ratio` with thresholds of 92 to auto-accept and 80 to
review. The algorithm is reimplemented here on the standard library and the
thresholds are kept; what calibration did not support is using that score alone.

**`token_set_ratio` scores 100.0 on genuinely different companies**, so no
threshold makes it safe on its own:

    tech mahindra                  ~ mahindra mahindra      100.0
    tata motors passenger vehicles ~ tata motors            100.0

That is the algorithm working as designed: when one name's tokens are a SUBSET
of the other's, the intersection IS the shorter name, so "is contained in" and
"is equal to" are indistinguishable. On a 400-name sample, 18 would have been
auto-accepted onto the wrong issuer at 95 — including Tech Mahindra onto
Mahindra & Mahindra.

The fix is a second condition rather than a higher bar: the token sets must also
be close, by Jaccard overlap. At `jaccard >= 0.7` the same sample produced ZERO
wrong auto-accepts from 90 to 95, so §8.2's 92 stands — the number was never the
problem (V1-02).

Where either condition fails the row goes to the review queue: a guess wearing a
confidence score is worse than an unresolved row a human can fix (V0-21).
"""

from __future__ import annotations

from difflib import SequenceMatcher
from functools import lru_cache

#: §8.2's own thresholds, which calibration supports.
AUTO_ACCEPT = 92
REVIEW_MIN = 80

#: The guard §8.2 lacks. Token-set overlap required *in addition* to the score
#: before a match may be accepted without a human. Zero false positives on a
#: 400-name sample at this value; 18 without it.
JACCARD_MIN = 0.7


def _ratio(a: str, b: str) -> float:
    """difflib similarity on 0..100. Identical strings short-circuit to 100."""
    if a == b:
        return 100.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio() * 100.0


@lru_cache(maxsize=8192)
def _tokens(s: str) -> frozenset[str]:
    return frozenset(s.split())


def token_set_ratio(a: str, b: str) -> float:
    """`fuzz.token_set_ratio`'s algorithm, on `difflib`.

    Both inputs are expected to be `normalise_name` output already — lowercase,
    punctuation stripped, corporate suffixes removed. Passing raw names would
    score `Reliance Industries Ltd` against `Reliance Industries Limited` on
    their suffixes, which is exactly the noise normalisation exists to remove.

    The three comparisons are the shared tokens against each side's full set,
    and the two full sets against each other. Taking the maximum is what makes
    a subset match strongly: when one name is contained in the other, the
    intersection *is* the shorter name and that comparison scores 100.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0

    shared = ta & tb
    if not shared:
        # No token in common. Fall back to a whole-string comparison rather
        # than returning zero: `hdfcbank` and `hdfc bank` share no token but
        # are obviously the same issuer.
        return _ratio(a, b)

    intersection = " ".join(sorted(shared))
    left = " ".join(sorted(shared) + sorted(ta - tb))
    right = " ".join(sorted(shared) + sorted(tb - ta))

    return max(
        _ratio(intersection, left),
        _ratio(intersection, right),
        _ratio(left, right),
    )


def token_jaccard(a: str, b: str) -> float:
    """Overlap of the two token sets: |shared| / |union|.

    This is what separates "the same name written differently" from "a name
    that contains this one". `tech mahindra` against `mahindra mahindra` scores
    a perfect `token_set_ratio` and a Jaccard of 0.50, because half the tokens
    in the union appear on only one side.
    """
    ta, tb = _tokens(a), _tokens(b)
    union = ta | tb
    return len(ta & tb) / len(union) if union else 0.0


def is_auto_acceptable(score: float, jaccard: float) -> bool:
    """Both conditions, or it goes to a human. See the module docstring."""
    return score >= AUTO_ACCEPT and jaccard >= JACCARD_MIN


def best_matches(
    needle: str, candidates: dict[str, str], limit: int = 5
) -> list[tuple[str, str, float]]:
    """Top `limit` candidates as (normalised_name, issuer_id, score), best first.

    `candidates` maps a normalised issuer name to its `issuer_id`. Returned
    even when every score is poor: §8.5's review queue stores the top five so a
    human sees what was considered, and an empty candidate list tells them
    nothing about whether the matcher looked.

    Ties break on `issuer_id` so the ordering is deterministic — `CLAUDE.md`
    invariant 10 requires a rebuild to reproduce byte-identical output, and an
    arbitrary tie order would break that on any dict reordering.
    """
    scored = [
        (name, issuer_id, token_set_ratio(needle, name))
        for name, issuer_id in candidates.items()
    ]
    scored.sort(key=lambda row: (-row[2], row[1]))
    return scored[:limit]
