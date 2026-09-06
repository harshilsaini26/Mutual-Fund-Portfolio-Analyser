"""Parser registry and router. MODULE_0.md §6.2.

    scores = [(p, p.sniff(f)) for p in REGISTRY]
    best, score = max(scores, key=...)
    if score < 0.5: raise NoParserMatched

§6.2's argument for routing on `sniff` rather than a hardcoded AMC->parser
table: AMCs rename files, merge entities, and occasionally publish one month in
a different format. A table goes stale silently; a sniff simply scores lower,
and a score below the floor raises with every candidate's score attached so the
failure says what it considered.
"""

from __future__ import annotations

from src.m0_data.parse.base import HoldingsParser, ParseFailed, RawFile
from src.m0_data.parse.holdings.hdfc import HdfcHoldingsParser
from src.m0_data.parse.holdings.icici import IciciHoldingsParser
from src.m0_data.parse.holdings.nippon import NipponHoldingsParser

#: Every registered parser. Adding an AMC is one entry.
REGISTRY: tuple[HoldingsParser, ...] = (
    HdfcHoldingsParser(),
    IciciHoldingsParser(),
    NipponHoldingsParser(),
)

#: §6.2. Below this, no parser is confident enough to be trusted with the file.
MIN_CONFIDENCE = 0.5


class NoParserMatched(ParseFailed):
    """§6.2. Carries every candidate's score, so the failure is diagnosable."""


def route(f: RawFile) -> HoldingsParser:
    """The best-scoring parser, or raise naming what was tried."""
    scores = [(p, p.sniff(f)) for p in REGISTRY]
    if not scores:
        raise NoParserMatched(f"{f.filename}: no parsers registered")
    best, score = max(scores, key=lambda pair: pair[1])
    if score < MIN_CONFIDENCE:
        detail = ", ".join(f"{p.parser_id}={s:.2f}" for p, s in scores)
        raise NoParserMatched(
            f"{f.filename}: no parser above {MIN_CONFIDENCE} ({detail})"
        )
    return best
