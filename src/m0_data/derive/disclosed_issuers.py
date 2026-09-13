"""Issuers the disclosures name but the market-cap list does not. V1-41.

§8.1's exposure unit is the issuer, and the only issuer source was AMFI's
market-cap list — **listed Indian equity**. Everything else a fund holds had
nowhere to resolve to: a company's commercial paper, an unlisted subsidiary, a
foreign listing. Measured across 183 disclosures, that was 1,394 rows and
Rs 198,510 Cr in `__UNRESOLVED__`, which is not a gap in coverage so much as a
hole in the analysis — `__UNRESOLVED__` is synthetic, so it is excluded from
overlap and concentration and the exposure leaves the look-through entirely.

**The disclosures already name these issuers.** 183 files from five fund houses
are in the warehouse, and between them they say what `INE261F` is (National
Bank For Agriculture and Rural Development) and what `US02079K` is (Alphabet).
No new source, no new fetch.

### The key is the ISIN, the name is a vote

Grouping is by **issuer segment** — V1-29's key, characters 1-7 of an Indian
ISIN. That is what makes this safe: the identity comes from the identifier, and
the disclosed name only has to supply a label for something already identified.
A wrong name is a cosmetic error; a wrong grouping would be a real one, and the
grouping is not a judgement.

Naming is where the 183 files pay off. One publisher writes `CP` for a piece of
commercial paper and another names the issuer in full on a different row, so the
most informative name in a segment is taken:

    INE556F  ->  SMALL INDUSTRIES DEVELOPMENT BANK OF INDIA   (4 houses, 83 rows)
    INE261F  ->  National Bank For Agriculture and Rural ...   (4 houses, 81 rows)
    US02079K ->  Alphabet Inc A                                (2 houses, 2 rows)

281 of 297 segments yield a usable name. The 16 that do not are `CP` and `CD`
all the way down — Rs 2,275 Cr — and they stay unresolved, because an issuer
called `CP` is worse than no issuer at all.

### What is deliberately left out

**Government paper.** Segments are `None` for `IN` + digits, so state
development loans and sovereign securities never reach here. V1-30 decided that
and the reasoning has not changed: a state is a real borrower and bucketing it
is a loss of information, but naming it needs a decision this module should not
make on its own.

**Mutual fund units.** An `INF` ISIN resolves to `__MFUNIT__` before the
cascade gets this far (V1-41's companion rule), so a fund held inside a fund
does not become an issuer.

### Visibility, which §8.2 asked for

V1-02 departure 3 refused to create a provisional issuer silently, because *"a
human sees that a new issuer appeared rather than finding it later"*. These are
not silent: the `issuer_id` carries a `DISC:` prefix, `is_listed` is 0, and
`source_file_id` points at the disclosure that supplied the name. `SELECT * FROM
issuer WHERE issuer_id LIKE 'DISC:%'` is the review, and it does not flood the
queue §8.4 exists to protect.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict

#: A disclosed name that identifies nothing. Every one of these was observed in
#: the warehouse, not imagined: an AMC writing `CP` in the name column is
#: describing the instrument, not the borrower.
USELESS_NAME = re.compile(
    r"^(cp|cd|ncd|tbill|t-bill|treps|bond|bonds|debenture|debentures|"
    r"commercial paper|certificate of deposit|state government securities|"
    r"government securities|[\d.\s%]+)$",
    re.I,
)

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


def derive_disclosed_issuers(conn: sqlite3.Connection) -> dict[str, int]:
    """Create an issuer per named segment, and an instrument per ISIN."""
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
    }


__all__ = [
    "clean_name",
    "derive_disclosed_issuers",
    "is_informative",
    "issuer_segment",
]
