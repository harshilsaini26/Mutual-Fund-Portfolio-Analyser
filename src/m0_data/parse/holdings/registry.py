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
from src.m0_data.parse.holdings.groww import GrowwHoldingsParser
from src.m0_data.parse.holdings.hdfc import HdfcHoldingsParser
from src.m0_data.parse.holdings.icici import IciciHoldingsParser
from src.m0_data.parse.holdings.kotak import KotakHoldingsParser
from src.m0_data.parse.holdings.nippon import NipponHoldingsParser
from src.m0_data.parse.holdings.ppfas import PpfasHoldingsParser

#: Every registered parser. Adding an AMC is one entry.
REGISTRY: tuple[HoldingsParser, ...] = (
    # Not an AMC. The coverage tier -- an aggregator's page rather than a fund
    # house's workbook -- and it scores 0.00 on anything that is a workbook, so
    # it never competes with the five below for a file one of them should read.
    # DECISIONS V1-43.
    GrowwHoldingsParser(),
    HdfcHoldingsParser(),
    IciciHoldingsParser(),
    KotakHoldingsParser(),
    NipponHoldingsParser(),
    PpfasHoldingsParser(),
)

#: §6.2. Below this, no parser is confident enough to be trusted with the file.
MIN_CONFIDENCE = 0.5


class NoParserMatched(ParseFailed):
    """§6.2. Carries every candidate's score, so the failure is diagnosable."""


def by_parser_id(parser_id: str) -> HoldingsParser:
    """The parser named by `raw_file.parser_id`, for re-reading an archived file.

    `route` cannot help there. The archive is content-addressed, so a stored
    file is named for its sha256 and `sniff` — which reads the filename and the
    magic bytes by design (§6.1) — scores 0.10 from every candidate. MODULE_0.md
    §3 says "a bug in any one of them is fixed by re-running from the layer to
    its left, never by editing data", and for a disclosure that was not true:
    re-parsing an archived file raised `NoParserMatched`.

    Nothing had to be discovered to fix it. The warehouse recorded which parser
    read the file at the time it read it, so the question `route` was being
    asked had already been answered and written down.
    """
    for parser in REGISTRY:
        if parser.parser_id == parser_id:
            return parser
    known = ", ".join(sorted(p.parser_id for p in REGISTRY))
    raise NoParserMatched(f"no parser {parser_id!r} in the registry; have {known}")


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
