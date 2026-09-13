"""Which scheme does this sheet describe? DECISIONS V1-38.

A workbook holding one scheme per sheet is the common packaging — Kotak ships
119, Nippon 108, ICICI 146 — and every sheet names its own fund somewhere above
the table. Matching that name to the AMFI master is what turns one download
into a hundred portfolios instead of one.

**A wrong match is worse than no match**, and not by a little. It files one
fund's holdings against another fund's ISIN, which looks exactly like a
correct load: the portfolio reconciles against its own total, the weights sum
to 100, the charts render. Nothing downstream can tell. So every rule here
refuses rather than guesses, and refusing is the expected outcome for a
meaningful minority of sheets.

### Three gates, and why each exists

**Liveness.** A candidate must have been priced by AMFI on or after the
disclosure date. This is the one that matters most, because the trap is not
similar names — it is *dead* names. AMFI still lists
`ICICI Prudential Multi-Asset Fund- Institutional`, one ISIN, last priced
2020-04-24, beside the live `Multi Asset Allocation Fund`; and the AMC's own
sheet says "Multi-Asset Fund", so the dead scheme is the *better* string match
and the live one is not a string match at all. Latest NAV rather than a window
around the date, because the warehouse backfills full history only where it
needs it and a live fund can hold exactly one NAV row, from today.

**Per line, not per blob.** A header is separate cells — the AMC's name, the
fund's name, an as-on date, then column titles. Scored as one string the column
titles drown the fund: ICICI's sheet fuzzy-matched
`icici prudential psu equity fund` at 93 that way.

**One family, one sheet.** Within a workbook a scheme appears once, so two
sheets claiming the same family means at least one is wrong and **both are
refused**. Measured on the two real multi-sheet workbooks, this catches two
distinct causes: Nippon's `Index` sheet is a contents page listing every fund
it contains, and Kotak's Gold *Fund*-of-fund names the Gold *ETF* it invests
in. Neither is a near-miss a similarity threshold would have caught.

Measured over 227 real sheets: 96 of Kotak's 119 identify and 94 of Nippon's
108, with the rest refusing. The refusals are mostly debt and index schemes
whose sheets name themselves by an internal code alone.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from src.m0_data.resolve.fuzzy import (
    is_auto_acceptable,
    token_jaccard,
    token_set_ratio,
)


@dataclass(frozen=True)
class SchemeMatch:
    """What a sheet was matched to, and how — or why it was refused.

    `method` is one of `contained`, `fuzzy`, `ambiguous`, `unmatched`. The last
    two carry `family=None`; `rivals` names what was in contention so a human
    reading the queue can see the near-miss rather than guess at it.
    """

    family: str | None
    method: str
    rivals: tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        return self.family is not None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def live_families(
    conn: sqlite3.Connection, amc_id: str, as_of: date
) -> dict[str, list[str]]:
    """Family key -> scheme ids, for families this AMC was still pricing.

    The liveness gate. A family whose newest NAV predates the disclosure is not
    what the disclosure is about, however well its name reads.
    """
    rows = conn.execute(
        "SELECT s.scheme_family, s.scheme_id, s.plan, s.option,"
        "       (SELECT max(n.nav_date) FROM nav_daily n"
        "         WHERE n.scheme_id = s.scheme_id) AS last_priced"
        "  FROM scheme s"
        " WHERE s.amc_id = ? AND s.scheme_family IS NOT NULL"
        "   AND s.status = 'active'",
        (amc_id,),
    ).fetchall()

    members: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    alive: set[str] = set()
    cutoff = as_of.isoformat()
    for family, scheme_id, plan, option, last_priced in rows:
        members[str(family)].append((str(scheme_id), str(plan), str(option)))
        if last_priced and str(last_priced) >= cutoff:
            alive.add(str(family))
    return {
        family: [sid for sid, _p, _o in group]
        for family, group in members.items()
        if family in alive
    }


def identify_scheme(candidates: list[str], families: dict[str, list[str]]) -> SchemeMatch:
    """Match a sheet's header lines to one family, or refuse.

    Containment first: a header that literally contains `kotak pioneer fund` is
    not a similarity judgement and does not need one. The longest containment
    wins, so `nippon india growth mid cap fund` beats `nippon india growth
    fund` on a sheet that says the former.

    Fuzzy only when nothing is contained, and only through V1-02's guard — an
    AMC may not spell its own fund the way AMFI does, which is the whole reason
    ICICI needs it. A tie at the top is a refusal either way.
    """
    lines = [_norm(c) for c in candidates]
    lines = [line for line in lines if line]

    hits: list[str] = []
    for line in lines:
        padded = f" {line} "
        hits += [k for k in families if k and f" {k} " in padded]
    if hits:
        longest = max(len(k) for k in hits)
        top = sorted({k for k in hits if len(k) == longest})
        if len(top) > 1:
            return SchemeMatch(None, "ambiguous", tuple(top))
        return SchemeMatch(top[0], "contained")

    scored: list[tuple[float, str]] = []
    for line in lines:
        for family in families:
            score = token_set_ratio(line, family)
            if is_auto_acceptable(score, token_jaccard(line, family)):
                scored.append((score, family))
    if not scored:
        return SchemeMatch(None, "unmatched")
    scored.sort(reverse=True)
    best = scored[0][0]
    winners = sorted({k for score, k in scored if score == best})
    if len(winners) > 1:
        return SchemeMatch(None, "ambiguous", tuple(winners))
    return SchemeMatch(winners[0], "fuzzy")


def refuse_contested(matches: dict[str, SchemeMatch]) -> dict[str, SchemeMatch]:
    """Within one workbook, a family claimed by two sheets is claimed by neither.

    A scheme appears once in its AMC's monthly workbook. Two sheets matching it
    means at least one is wrong, and there is no way to tell which — so both
    refuse. On the two real multi-sheet workbooks this catches a contents page
    that lists every fund, and a fund-of-fund that names the ETF it holds.
    """
    claimed: dict[str, list[str]] = defaultdict(list)
    for sheet, match in matches.items():
        if match.family:
            claimed[match.family].append(sheet)

    contested = {f for f, sheets in claimed.items() if len(sheets) > 1}
    if not contested:
        return matches
    return {
        sheet: (
            SchemeMatch(None, "ambiguous", (match.family,))
            if match.family in contested and match.family
            else match
        )
        for sheet, match in matches.items()
    }


def canonical_scheme(
    conn: sqlite3.Connection, family: str, amc_id: str
) -> str | None:
    """Which ISIN of a family to file the holdings against.

    Any of them serves the whole family after V1-37, so this only has to be
    STABLE — `CLAUDE.md` invariant 10 wants a rebuild to reproduce byte-identical
    output, and `holding.scheme_id` is output. Direct-Growth first because it is
    the class that always exists and the one a reader recognises, then the
    lowest scheme id.
    """
    row = conn.execute(
        "SELECT scheme_id FROM scheme"
        " WHERE amc_id = ? AND scheme_family = ? AND status = 'active'"
        " ORDER BY (plan = 'direct' AND option = 'growth') DESC, scheme_id ASC"
        " LIMIT 1",
        (amc_id, family),
    ).fetchone()
    return str(row[0]) if row else None


__all__ = [
    "SchemeMatch",
    "canonical_scheme",
    "identify_scheme",
    "live_families",
    "refuse_contested",
]
