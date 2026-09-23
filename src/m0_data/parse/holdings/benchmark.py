"""The benchmark a disclosure sheet states, verbatim. S12, for active funds.

An index fund's benchmark is in its name; an active fund's is not.
`Parag Parikh Flexi Cap Fund` is measured against NIFTY 500 and says so nowhere
in its name -- but its monthly disclosure says so, and so do Kotak's, Nippon's
and HDFC's. This reads that statement out of a sheet. Resolving it to an index
is `resolve.benchmark`'s job, not this one's (§6.3 rule 1: parsers never
normalise).

Four layouts, measured across every workbook in the archive:

    Kotak   117 of 119 sheets   column A, footer   `Benchmark - X`
                                                   `Benchmark - - X` on some
    Nippon  107 of 108 sheets   column F, footer   `BENCHMARK NAME - X`
    HDFC    riskometer note                         `... Benchmark "NIFTY 500 TRI" ...`
    PPFAS   returns table                           a `Benchmark` column header,
                                                   the value in the cell below

**Anchored to a label, never to the word.** The same workbooks carry "Benchmark
Riskometer", "Standard Deviation( Benchmark )", "Risk O Meter and Benchmark are
as per last data", a fund named "NIPPON INDIA ETF NIFTY 5 YR BENCHMARK G-SEC",
and -- in AMFI's market-cap list -- a company called "Benchmark Computer
Solutions Limited". A search for the word finds all of them. Every pattern
here requires the word to be a label: followed by a separator, quoting a name,
or standing alone as a column header. None of those five match, and anything
that does still has to resolve to an index NSE publishes before it counts.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

#: `Benchmark - X`, `Benchmark - - X`, `BENCHMARK NAME - X`, `Benchmark: X`.
#: The separator is required: it is what tells a label from a company name.
_LABELLED = re.compile(
    r"^\s*benchmark(?:\s+name)?\s*[-:\u2013\u2014]"
    r"\s*(?P<text>.+?)\s*$",
    re.I,
)

#: HDFC's riskometer note: `... Portfolio Benchmark "NIFTY 500 TRI" as on ...`.
_QUOTED = re.compile(r"\bbenchmark\s*[\"\u201c](?P<text>[^\"\u201d]+)[\"\u201d]", re.I)

#: Leading separators a label leaves behind -- Kotak's `Benchmark - - X`.
_SEPARATORS = " -:\u2013\u2014"


def declared_benchmarks(rows: Sequence[Sequence[object]]) -> list[str]:
    """Every benchmark the sheet states, in the order they appear, verbatim.

    `rows` is the sheet as a list of rows of cell values -- what
    `openpyxl`'s `iter_rows(values_only=True)` gives -- so this is a pure
    function a test can call with literals.

    More than one is possible and not resolved here: a sheet can state the
    same benchmark twice, or a primary and something else. Deciding what the
    set means is the resolver's job.
    """
    found: list[str] = []
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            if not isinstance(value, str):
                continue
            text = " ".join(value.split())
            if m := _LABELLED.match(text):
                found.append(m.group("text").lstrip(_SEPARATORS))
            elif m := _QUOTED.search(text):
                found.append(m.group("text").strip())
            elif text.lower() == "benchmark" and _has_header(row, "scheme"):
                # A performance table's column header -- the value is the cell
                # directly below. The row must also head a `Scheme` column:
                # AMFI's market-cap list has a company whose short name is
                # BENCHMARK, and without that condition the next company's
                # code came back as a benchmark.
                below = _cell(rows, r + 1, c)
                if isinstance(below, str) and below.strip():
                    found.append(" ".join(below.split()))
    return [t for t in found if t]


def _has_header(row: Sequence[object], label: str) -> bool:
    return any(isinstance(v, str) and v.strip().lower() == label for v in row)


def _cell(rows: Sequence[Sequence[object]], r: int, c: int) -> object:
    if r >= len(rows) or c >= len(rows[r]):
        return None
    return rows[r][c]
