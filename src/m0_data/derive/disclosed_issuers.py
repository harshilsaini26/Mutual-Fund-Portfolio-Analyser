"""Issuers the disclosures name but the market-cap list does not. V1-41.

§8.1's exposure unit is the issuer, and the only issuer source was AMFI's
market-cap list — LISTED INDIAN EQUITY. Commercial paper, unlisted subsidiaries
and foreign listings had nowhere to resolve to: 1,394 rows and Rs 198,510 Cr in
`__UNRESOLVED__` across 183 disclosures, which is a hole in the analysis rather
than a coverage gap, since `__UNRESOLVED__` is excluded from overlap and
concentration entirely.

**The disclosures already name these issuers.** No new source, no new fetch.

**The key is the ISIN, the name is a vote.** Grouping is by issuer segment
(V1-29's key), so identity comes from the identifier and the disclosed name only
labels something already identified — a wrong name is cosmetic where a wrong
grouping would not be. The most informative name across the files wins: 281 of
297 segments yield a usable one, and the 16 that do not are `CP` and `CD` all
the way down (Rs 2,275 Cr), which stay unresolved because an issuer called `CP`
is worse than no issuer.

**State development loans too, since V1-71.** A state government ISIN is `IN`
+ a two-digit government code + `20`, and the code names the borrower whatever
the row calls itself -- ICICI writes `7.5% State Government Securities`, Nippon
only the coupon. The issuer is that state's government, named for it
("Government of Maharashtra"), from `config/state_isin_codes.yaml`, where every
code carries its evidence. A code without evidence stays unresolved, and a
disclosure naming a different state for a code refuses the code: one of the two
is wrong, and neither is trusted. Central government paper (code 00) is
`__GSEC__`'s (V1-30). Mutual fund units resolve to `__MFUNIT__` first (V1-41).

Not silent, which is V1-02 departure 3's requirement: the `issuer_id` carries a
`DISC:` prefix, `is_listed` is 0, and `source_file_id` points at the disclosure
that supplied the name.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict

from src.m0_data.config import state_isin_codes

#: A disclosed name that identifies nothing. Every one of these was observed in
#: the warehouse, not imagined: an AMC writing `CP` in the name column is
#: describing the instrument, not the borrower.
USELESS_NAME = re.compile(
    r"^(cp|cd|ncd|tbill|t-bill|treps|bond|bonds|debenture|debentures|"
    r"commercial paper|certificate of deposit|state government securities|"
    r"government securities|[\d.\s%]+)$",
    re.I,
)

#: A state government's ISIN: `IN`, the government's two digits, then `20`.
STATE_ISIN = re.compile(r"^IN(\d{2})20")

#: A coupon printed ahead of the borrower: `7.45% Bharti Telecom Limited`.
COUPON_PREFIX = re.compile(r"^\s*\d+(\.\d+)?\s*%\s*")

#: Maturities, footnote markers and the brackets AMCs hang them in.
PARENTHETICAL = re.compile(r"\([^)]*\)")
MARKERS = re.compile(r"[*#$^~]+")


def issuer_segment(isin: str) -> str | None:
    """The entity an ISIN belongs to, or None where we decline to say.

    Indian corporate and fund ISINs give the issuer in characters 1-7. A
    foreign ISIN has no issuer field in ISO 6166, so the first eight characters
    stand in — enough to separate Alphabet from Amazon and no more is claimed.
    """
    code = isin.strip().upper()
    if len(code) != 12:
        return None
    if code.startswith("INF"):
        # Units of a scheme. `__MFUNIT__` owns these (V1-41's companion rule),
        # and saying so here rather than relying on the cascade having run
        # first means the order of the two can never matter.
        return None
    if code.startswith("INE"):
        return code[:7]
    if code.startswith("IN") and code[2].isdigit():
        return None            # government — V1-30's decision, not this one's
    return code[:8]


def clean_name(name: str) -> str:
    """The borrower, with the instrument's details taken off it."""
    text = COUPON_PREFIX.sub("", name)
    text = PARENTHETICAL.sub(" ", text)
    text = MARKERS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" -,.")
    return text


def is_informative(name: str) -> bool:
    """Does this name identify a borrower, or just an instrument type?"""
    cleaned = clean_name(name)
    if not cleaned or USELESS_NAME.match(cleaned):
        return False
    return len([w for w in re.split(r"[^A-Za-z]+", cleaned) if len(w) > 2]) >= 2


def _best_name(names: list[str]) -> str | None:
    """The most informative disclosed name in a segment.

    Most alphabetic words first, then longest, then alphabetical — the last two
    only to break ties, because `CLAUDE.md` invariant 10 wants a rebuild to
    produce byte-identical output and `canonical_name` is output.
    """
    usable = sorted({clean_name(n) for n in names if is_informative(n)})
    if not usable:
        return None
    return max(
        usable,
        key=lambda n: (len([w for w in re.split(r"[^A-Za-z]+", n) if len(w) > 2]),
                       len(n), n),
    )


def state_code(isin: str) -> str | None:
    """The issuing state's two-digit code, or None: not a state's ISIN."""
    found = STATE_ISIN.match(isin.strip().upper())
    return found.group(1) if found and found.group(1) != "00" else None


def derive_disclosed_issuers(
    conn: sqlite3.Connection, states: dict[str, str] | None = None
) -> dict[str, int]:
    """Create an issuer per named segment or evidenced state, and an
    instrument per ISIN. `states` defaults to `config/state_isin_codes.yaml`."""
    summary = _derive_states(conn, state_isin_codes() if states is None else states)
    rows = conn.execute(
        "SELECT h.isin, h.instrument_raw_name, h.instrument_class,"
        "       h.source_file_id"
        "  FROM holding h"
        " WHERE h.is_current = 1 AND h.issuer_id = '__UNRESOLVED__'"
        "   AND h.isin IS NOT NULL AND length(h.isin) = 12"
    ).fetchall()

    segments: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for isin, name, klass, file_id in rows:
        segment = issuer_segment(str(isin))
        if segment:
            segments[segment].append(
                (str(isin).upper(), str(name), str(klass), str(file_id))
            )

    created = instruments = unnamed = 0
    for segment in sorted(segments):
        group = segments[segment]
        name = _best_name([n for _i, n, _k, _f in group])
        if name is None:
            unnamed += 1
            continue
        issuer_id = f"DISC:{segment}"
        source = sorted(f for _i, _n, _k, f in group)[0]
        country = "IN" if segment.startswith("IN") else segment[:2]
        conn.execute(
            "INSERT OR IGNORE INTO issuer (issuer_id, canonical_name, country,"
            " is_listed, is_synthetic, source_file_id) VALUES (?,?,?,0,0,?)",
            (issuer_id, name, country, source),
        )
        created += 1
        for isin, _n, klass, _f in sorted(set((i, n, k, f) for i, n, k, f in group)):
            conn.execute(
                "INSERT OR IGNORE INTO instrument (isin, issuer_id,"
                " instrument_type, source_file_id) VALUES (?,?,?,?)",
                (isin, issuer_id, klass or "unknown", source),
            )
            instruments += 1

    return {
        "segments": len(segments),
        "issuers": created,
        "instruments": instruments,
        "unnamed_segments": unnamed,
        **summary,
    }


def _derive_states(conn: sqlite3.Connection, states: dict[str, str]) -> dict[str, int]:
    """An issuer per evidenced state, and its loans as instruments.

    Every current row with a state ISIN is read, resolved or not, so the
    evidence is re-checked on every load and not only the first.
    """
    by_code: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for isin, name, file_id in conn.execute(
        "SELECT isin, instrument_raw_name, source_file_id FROM holding"
        " WHERE is_current = 1 AND isin GLOB 'IN[0-9][0-9]20*'"
    ):
        code = state_code(str(isin))
        if code:
            by_code[code].append((str(isin).upper(), str(name), str(file_id)))

    created = conflicts = 0
    for code in sorted(by_code):
        state = states.get(code)
        if state is None:
            continue
        named = {
            other for other in states.values()
            for _i, name, _f in by_code[code] if other.lower() in name.lower()
        }
        if named - {state}:
            conflicts += 1
            continue
        issuer_id = f"STATE:IN{code}"
        source = sorted(f for _i, _n, f in by_code[code])[0]
        conn.execute(
            "INSERT OR IGNORE INTO issuer (issuer_id, canonical_name, country,"
            " is_listed, is_synthetic, source_file_id) VALUES (?,?,'IN',0,0,?)",
            (issuer_id, f"Government of {state}", source),
        )
        created += 1
        for isin in sorted({i for i, _n, _f in by_code[code]}):
            conn.execute(
                "INSERT OR IGNORE INTO instrument (isin, issuer_id,"
                " instrument_type, source_file_id) VALUES (?,?,'sdl',?)",
                (isin, issuer_id, source),
            )
    return {"state_issuers": created, "state_conflicts": conflicts}


__all__ = [
    "clean_name",
    "derive_disclosed_issuers",
    "is_informative",
    "issuer_segment",
    "state_code",
]
