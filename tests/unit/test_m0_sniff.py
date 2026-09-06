"""`sniff()` must claim its own files and no one else's. MODULE_0.md §15.2.

§15.2's third test is the one with teeth: *"catches over-eager `sniff()`
implementations, which cause the wrong parser to silently produce plausible
garbage."* That failure mode is specific to this project's shape. Two AMCs
publish the same SEBI-mandated facts in different layouts, so a file read by
the wrong parser does not crash — it produces a portfolio. V1-08 measured what
that looks like: ICICI read through HDFC's rules gives 2.887x the true holdings
and still normalises to 100%.

**The cross-product is driven by `REGISTRY`, not by a hand-written list.** A
parser added without a fixture fails `test_every_registered_parser_has_a_fixture`
rather than quietly shrinking the matrix — which is the only way a
cross-product test stays honest as parsers are added.

**A departure from §15.2 worth naming:** it specifies fixtures under
`tests/fixtures/holdings/{amc}/`; ours live in `tests/fixtures/m0/` alongside
the other M0 fixtures, and moving them now would churn two working test modules
for a directory name. The `sniff` contract is unaffected — it reads the
filename and the magic bytes, neither of which is the fixture's own path.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
from src.m0_data.parse.base import HoldingsParser, HoldingsParseResult, RawFile
from src.m0_data.parse.holdings.registry import MIN_CONFIDENCE, REGISTRY

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "m0"

#: `amc_id` -> (fixture on disk, the name the AMC actually publishes it under).
#:
#: The published name matters and the fixture's own basename does not: `sniff`
#: routes on what the file is called in the wild, so testing it against
#: `hdfc_holdings_sample.xlsx` would test a filename no AMC has ever used.
PUBLISHED = {
    "hdfc": ("hdfc_holdings_sample.xlsx",
             "Monthly HDFC Flexi Cap Fund - 31 July 2026.xlsx"),
    "icici": ("icici_holdings_sample.xlsx",
              "ICICI Prudential Multi-Asset Fund.xlsx"),
}

PARSERS = {p.amc_id: p for p in REGISTRY}
CROSS_PRODUCT = [
    (a, b) for a, b in itertools.permutations(sorted(PUBLISHED), 2)
]


def _raw(amc_id: str) -> RawFile:
    on_disk, published = PUBLISHED[amc_id]
    return RawFile("x", f"S5:{amc_id}", published, (FIXTURES / on_disk).read_bytes())


def _claims_foreign(parser: HoldingsParser) -> list[tuple[str, float]]:
    """Every fixture this parser claims that is not its own, with its score."""
    return [
        (amc_id, parser.sniff(_raw(amc_id)))
        for amc_id in sorted(PUBLISHED)
        if amc_id != parser.amc_id and parser.sniff(_raw(amc_id)) >= MIN_CONFIDENCE
    ]


def test_every_registered_parser_has_a_fixture() -> None:
    """§15.2: "every AMC parser gets at least one real archived file".

    Without this the cross-product silently shrinks whenever a parser is added
    without a fixture, and a test that covers less than it claims is worse than
    one that claims less.
    """
    missing = sorted(set(PARSERS) - set(PUBLISHED))
    assert not missing, f"registered parsers with no fixture: {missing}"
    for amc_id, (on_disk, _) in PUBLISHED.items():
        assert (FIXTURES / on_disk).exists(), f"{amc_id}: {on_disk} is missing"


@pytest.mark.parametrize("amc_id", sorted(PUBLISHED))
def test_a_parser_sniffs_its_own_file(amc_id: str) -> None:
    """§15.2. Below the floor, `route()` raises `NoParserMatched` on a good file."""
    score = PARSERS[amc_id].sniff(_raw(amc_id))
    assert score >= MIN_CONFIDENCE, f"{amc_id} scored {score} on its own disclosure"


@pytest.mark.parametrize(("amc_id", "other"), CROSS_PRODUCT)
def test_a_parser_does_not_claim_another_amcs_file(amc_id: str, other: str) -> None:
    """§15.2's third test. The one that catches an over-eager `sniff`.

    A parser that claims a foreign file does not fail loudly — it parses it,
    and the result is a portfolio with the wrong shape that still sums to 100%.
    """
    score = PARSERS[amc_id].sniff(_raw(other))
    assert score < MIN_CONFIDENCE, (
        f"{amc_id} claims {other}'s disclosure at {score}; the wrong parser "
        f"would win the route and produce plausible garbage"
    )


def test_the_cross_product_catches_an_unconditionally_greedy_sniff() -> None:
    """The test above has teeth, demonstrated rather than assumed.

    A `sniff` that returns 1.0 for everything is the exact failure §15.2
    describes, and it passes `test_a_parser_sniffs_its_own_file` — only the
    cross-product catches it. Asserting that here means the guard cannot be
    weakened without this failing.
    """

    class GreedyParser:
        parser_id = "holdings.greedy"
        version = "0"
        amc_id = "hdfc"  # claims to be one of ours, so its "own" file passes

        def sniff(self, f: RawFile) -> float:
            return 1.0

        def parse(
            self, f: RawFile
        ) -> HoldingsParseResult:  # pragma: no cover - never called
            raise NotImplementedError

    greedy = GreedyParser()
    assert greedy.sniff(_raw("hdfc")) >= MIN_CONFIDENCE  # own file: passes
    assert _claims_foreign(greedy), "the cross-product failed to catch a greedy sniff"

    # And every real parser is clean by the same predicate.
    for parser in REGISTRY:
        assert not _claims_foreign(parser), (
            f"{parser.parser_id} claims {_claims_foreign(parser)}"
        )
