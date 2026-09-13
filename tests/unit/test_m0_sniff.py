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
from src.m0_data.parse.holdings.registry import (
    MIN_CONFIDENCE,
    REGISTRY,
    NoParserMatched,
    by_parser_id,
    route,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "m0"

#: `amc_id` -> (fixture on disk, the name the AMC actually publishes it under).
#:
#: The published name matters and the fixture's own basename does not: `sniff`
#: routes on what the file is called in the wild, so testing it against
#: `hdfc_holdings_sample.xlsx` would test a filename no AMC has ever used.
PUBLISHED = {
    "hdfc": (
        "hdfc_holdings_sample.xlsx",
        "Monthly HDFC Flexi Cap Fund - 31 July 2026.xlsx",
    ),
    "icici": ("icici_holdings_sample.xlsx", "ICICI Prudential Multi-Asset Fund.xlsx"),
    "nippon": ("nippon_holdings_sample.xlsx", "NIMF-MONTHLY-PORTFOLIO-31-July-26.xls"),
    # Kotak's published name says what the document is, not who published it,
    # which is the case §6.1 said would one day earn a content check. It has
    # not yet — the cross-product below is what proves that.
    "kotak": ("kotak_pioneer_2026-07-31.xlsx", "ConsolidatedSEBIPortfolioJuly2026.xlsx"),
    # PPFAS is the easy case: its filename says both who published it and
    # which scheme it is.
    "ppfas": (
        "ppfas_flexi_cap_2026-07-31.xlsx",
        "PPFCF_PPFAS_Monthly_Portfolio_Report_July_31_2026.xlsx",
    ),
    # Not an AMC and not a workbook: a page, from the coverage tier (V1-43).
    # It is in the cross-product for exactly the reason the others are — five
    # workbook parsers that each open with a magic-byte check must all score it
    # 0.00, and it must not claim any of theirs. The published name is the
    # slug, because a page is not downloaded under a name of the AMC's choosing.
    "groww": (
        "groww_hdfc_flexi_cap_2026-08-31.html",
        "hdfc-equity-fund-direct-growth.html",
    ),
}

PARSERS = {p.amc_id: p for p in REGISTRY}
CROSS_PRODUCT = [(a, b) for a, b in itertools.permutations(sorted(PUBLISHED), 2)]


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
            self, f: RawFile, sheet: str | None = None
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


# --- re-reading a file the archive already holds ------------------------------


def test_a_parser_can_be_found_by_the_id_raw_file_recorded() -> None:
    """`route` cannot help with an archived file and never could.

    The archive is content-addressed, so a stored disclosure is named for its
    sha256 — and `sniff` reads the filename and the magic bytes by design
    (§6.1), which on `0ab4fd30...xlsx` scores 0.10 from every candidate.
    MODULE_0.md §3 promises "a bug in any one of them is fixed by re-running
    from the layer to its left"; for a disclosure that raised
    `NoParserMatched` instead.
    """
    for parser in REGISTRY:
        assert by_parser_id(parser.parser_id) is parser


def test_an_unregistered_parser_id_fails_with_what_is_available() -> None:
    """A recorded parser can outlive its module — an AMC's format is dropped,
    or a parser is renamed. The failure has to say what it looked for and what
    it had, because the alternative is a stack trace about None.
    """
    with pytest.raises(NoParserMatched) as excinfo:
        by_parser_id("holdings.nosuchamc")
    message = str(excinfo.value)
    assert "holdings.nosuchamc" in message
    assert "holdings.hdfc" in message


def test_an_archived_file_cannot_be_routed_by_its_name() -> None:
    """The defect itself, pinned so the fix cannot quietly regress.

    This asserts `sniff` behaves as §6.1 says it does — routing on the name —
    rather than asserting the bug is gone. The fix is not to make `sniff`
    cleverer; opening the workbook would cost a parse per candidate. It is to
    stop asking `sniff` a question the warehouse had already answered.
    """
    on_disk, _ = PUBLISHED["hdfc"]
    archived = RawFile(
        "x",
        "S5:hdfc",
        # what the archive actually calls it
        "0ab4fd3000675be362444fd0547afa64b67e61c220b5ff36064dc05e0a36f24a.xlsx",
        (FIXTURES / on_disk).read_bytes(),
    )
    for parser in REGISTRY:
        assert parser.sniff(archived) < MIN_CONFIDENCE
    with pytest.raises(NoParserMatched):
        route(archived)
