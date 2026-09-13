"""The coverage tier. MODULE_0.md §6, DECISIONS V1-43.

`tests/fixtures/m0/groww_hdfc_flexi_cap_2026-08-31.html` was built from the
page Groww served for HDFC Flexi Cap Direct Plan Growth on 2026-09-13, 491,655
bytes as fetched. **It is trimmed, and here is exactly how:** the rendered
markup was dropped and the `__NEXT_DATA__` payload kept, minus the keys this
parser never reads (peer comparison, news, fund-manager prose, return series).
All 86 holdings are verbatim, as are `aum`, `nav`, `isin` and `scheme_name`.
Nothing was edited to make a number work — unlike the workbook fixtures, whose
stated totals were adjusted after trimming, this one needed no such edit
because the rows were not trimmed.

**What that fixture cannot prove**, and a test below covers separately: on the
real page the payload begins at byte 250,231, and in the fixture it begins at
byte 344. The first draft of `sniff` looked at a 200 KB prefix and scored 0.00
on the real page while scoring 1.00 on a fixture built from it. A fixture
derived from the artifact under test can agree with a bug, so the offset is
tested against a synthetic page instead.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from src.m0_data.parse.base import ParseFailed, RawFile
from src.m0_data.parse.holdings.groww import (
    MARKET_VALUE_UNIT,
    GrowwHoldingsParser,
)
from src.m0_data.parse.holdings.registry import MIN_CONFIDENCE, route

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "m0"
FIXTURE = FIXTURES / "groww_hdfc_flexi_cap_2026-08-31.html"
SLUGS = Path(__file__).resolve().parents[2] / "config" / "groww_slugs.yaml"


def _raw(name: str = "hdfc-equity-fund-direct-growth.html") -> RawFile:
    return RawFile("x", "S7", name, FIXTURE.read_bytes())


def _page(payload: dict[str, object], pad: int = 0) -> bytes:
    """A synthetic page with the payload `pad` bytes into the document."""
    filler = "<!-- " + ("x" * pad) + " -->" if pad else ""
    return (
        "<!doctype html><html><body>"
        + filler
        + '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"mfServerSideData": payload}}})
        + "</script></body></html>"
    ).encode()


HOLDING = {
    "company_name": "ICICI Bank Ltd",
    "nature_name": "EQUITY",
    "instrument_name": "Equity",
    "market_value": 10444.8209,
    "corpus_per": 9.19386133,
    "sector_name": "Financial",
    "rating": None,
    "portfolio_date": "2026-08-30T18:30:00.000Z",
}


def test_the_page_yields_a_whole_portfolio_not_a_top_ten() -> None:
    """The premise the tier rests on. A truncated list would be useless here.

    §6's reconciliation gate needs the disclosure to state its own total, and
    an aggregator showing the largest ten positions states nothing of the kind.
    """
    result = GrowwHoldingsParser().parse(_raw())

    assert len(result.securities) == 86
    assert result.as_of_date is not None
    assert result.as_of_date.isoformat() == "2026-08-31"
    assert result.scheme_raw_name == "HDFC Flexi Cap Direct Plan Growth"

    pct = sum(
        (r.pct_to_nav_raw for r in result.securities if r.pct_to_nav_raw), Decimal(0)
    )
    assert pct == Decimal("100.00000000"), f"weights sum to {pct}, not 100"


def test_the_rows_reconcile_against_the_total_the_page_states() -> None:
    """`aum` is this page's `Grand Total`, and it is what makes a misparse loud.

    Not asserted as exact equality: the rows are published to four decimal
    places and `aum` to eight, so they agree to 0.00002 of a crore and no
    further. The check that matters is the one the parser itself applies.
    """
    result = GrowwHoldingsParser().parse(_raw())
    assert result.stated_total is not None

    summed = sum(
        (r.market_value_raw for r in result.securities if r.market_value_raw),
        Decimal(0),
    )
    drift = abs(summed - result.stated_total) / result.stated_total * 100
    assert drift < Decimal("0.0001"), f"rows drift {drift}% from the stated total"
    assert not result.warnings


def test_every_row_carries_no_isin_and_that_is_recorded_not_guessed() -> None:
    """The whole cost of the tier, asserted so it cannot be quietly papered over.

    A later change that filled `isin_raw` by looking the name up would make
    this tier indistinguishable from the AMC tier while being materially worse,
    and §6.3 rule 1 forbids a parser resolving anything. If this test is ever
    failing because ISINs appeared, the page changed — and the tier's whole
    trade-off changed with it.
    """
    result = GrowwHoldingsParser().parse(_raw())
    assert all(r.isin_raw is None for r in result.securities)


def test_units_are_carried_as_a_label_not_applied() -> None:
    """§6.3 rule 1 and §7.2's 100x-error path.

    The parser asserts crores because no cell on the page states a unit. What
    it must not do is convert: `market_value_raw` stays as published and
    `normalise/units.py` scales it.
    """
    result = GrowwHoldingsParser().parse(_raw())
    first = result.securities[0]

    assert first.market_value_unit == MARKET_VALUE_UNIT == "crore"
    assert first.market_value_raw == Decimal("10444.8209")
    assert result.pct_scale == Decimal(1)


def test_a_number_is_read_through_its_text_not_its_float() -> None:
    """CLAUDE.md invariant 1. JSON numbers arrive as `float`.

    `Decimal(9.19386133)` is the binary expansion and carries a tail of noise;
    `Decimal("9.19386133")` is the number the page printed.
    """
    result = GrowwHoldingsParser().parse(_raw())
    pct = result.securities[0].pct_to_nav_raw

    assert pct == Decimal("9.19386133")
    # RUF032 is the lint for exactly the mistake this asserts we do not make,
    # so the float literal is the subject of the test rather than an oversight.
    assert pct != Decimal(9.19386133)  # noqa: RUF032


class TestTheSectionIsBuiltFromBothColumns:
    """V1-07: the section is the only reliable signal of what a row is.

    Groww splits that signal across `nature_name` and `instrument_name`, and
    they disagree on exactly the rows where the coarse one is wrong.
    """

    def test_a_future_under_nature_equity_is_still_a_derivative(self) -> None:
        """The fixture carries V1-07's case whole: ONE COMPANY, TWO ROWS.

        HDFC Flexi Cap holds Dixon Technologies as equity and writes a future
        on it, and Groww files BOTH under `nature_name: EQUITY` — so the only
        thing separating them is `instrument_name`. The names barely differ
        (`... (India) Ltd` against `... (India) Limited`), which is the same
        trap V1-07 found on HDFC's own sheet, where the short leg is spelled
        identically to the long position twelve rows above it.

        Classed on `nature_name` alone, both join the equity book and the
        fund reads as holding twice the Dixon exposure it has.
        """
        result = GrowwHoldingsParser().parse(_raw())
        dixon = {
            r.section: r for r in result.securities if "Dixon" in r.instrument_raw_name
        }
        assert set(dixon) == {"EQUITY & EQUITY RELATED", "DERIVATIVES"}, (
            "both Dixon rows landed in the same section"
        )

    def test_an_index_derivative_is_one_too(self) -> None:
        """`Index Derivatives` matched none of `future|option|swap|forward`.

        Found on the second fund this parser was pointed at, not the first.
        """
        page = _page(
            {
                "scheme_name": "A Fund",
                "aum": 100.0,
                "holdings": [
                    {
                        **HOLDING,
                        "instrument_name": "Index Derivatives",
                        "market_value": 100.0,
                        "corpus_per": 100.0,
                    },
                ],
            }
        )
        result = GrowwHoldingsParser().parse(RawFile("x", "S7", "a.html", page))
        assert result.securities[0].section == "DERIVATIVES"

    def test_cblo_under_nature_debt_is_cash(self) -> None:
        """Rs 2,337 Cr of Axis Small Cap, and the fund's largest unresolved row.

        §8.4's `CLASS_FALLBACK` deliberately has no `debt` entry — a bond has
        an issuer and bucketing one as cash would hide credit exposure. CBLO
        has no issuer; it is TREPS under its former name.
        """
        page = _page(
            {
                "scheme_name": "A Fund",
                "aum": 100.0,
                "holdings": [
                    {
                        **HOLDING,
                        "company_name": "Others CBLO",
                        "nature_name": "DEBT",
                        "instrument_name": "CBLO",
                        "market_value": 100.0,
                        "corpus_per": 100.0,
                    },
                ],
            }
        )
        result = GrowwHoldingsParser().parse(RawFile("x", "S7", "a.html", page))
        assert result.securities[0].section == "CASH & CASH EQUIVALENT"

    def test_a_plain_equity_is_left_alone(self) -> None:
        result = GrowwHoldingsParser().parse(_raw())
        assert result.securities[0].section == "EQUITY & EQUITY RELATED"


class TestSniff:
    def test_it_claims_its_own_page(self) -> None:
        assert GrowwHoldingsParser().sniff(_raw()) >= MIN_CONFIDENCE
        assert route(_raw()).parser_id == "holdings.groww"

    def test_it_finds_a_payload_the_fixture_cannot_place_far_enough_away(
        self,
    ) -> None:
        """The near-miss the fixture agrees with, tested against a synthetic page.

        Next.js emits `__NEXT_DATA__` last, so on the real 491 KB page it began
        at byte 250,231. A `sniff` reading a 200 KB prefix scored 0.00 there
        while scoring 1.00 on the 30 KB fixture built from that very page —
        the fixture could not have caught it, because trimming moved the thing
        being looked for.
        """
        page = _page(
            {
                "scheme_name": "A Fund",
                "aum": 100.0,
                "holdings": [{**HOLDING, "market_value": 100.0, "corpus_per": 100.0}],
            },
            pad=400_000,
        )
        assert len(page) > 400_000
        parser = GrowwHoldingsParser()
        assert parser.sniff(RawFile("x", "S7", "a.html", page)) >= MIN_CONFIDENCE
        assert parser.parse(RawFile("x", "S7", "a.html", page)).securities

    @pytest.mark.parametrize(
        "workbook",
        [
            "hdfc_holdings_sample.xlsx",
            "icici_multi_asset_2026-07-31.xlsx",
            "kotak_pioneer_2026-07-31.xlsx",
            "ppfas_flexi_cap_2026-07-31.xlsx",
        ],
    )
    def test_it_claims_no_workbook(self, workbook: str) -> None:
        """A page parser reading a workbook would produce nothing; a workbook
        parser reading a page would too. The magic-byte check is mutual."""
        raw = RawFile("x", "S5", workbook, (FIXTURES / workbook).read_bytes())
        assert GrowwHoldingsParser().sniff(raw) == 0.0


class TestItRefusesRatherThanGuesses:
    def test_a_sheet_argument_is_refused(self) -> None:
        """A page carries one scheme. A caller passing a sheet has confused it
        with Nippon's 108-sheet workbook, and ignoring the argument would load
        the wrong thing silently."""
        with pytest.raises(ParseFailed, match="one scheme"):
            GrowwHoldingsParser().parse(_raw(), sheet="KPF")

    def test_two_portfolio_dates_are_refused(self) -> None:
        """A page serving two dates describes two portfolios. Picking either
        would be a guess about which one the rows belong to."""
        page = _page(
            {
                "scheme_name": "A Fund",
                "aum": 200.0,
                "holdings": [
                    {**HOLDING, "market_value": 100.0, "corpus_per": 50.0},
                    {
                        **HOLDING,
                        "market_value": 100.0,
                        "corpus_per": 50.0,
                        "portfolio_date": "2026-07-30T18:30:00.000Z",
                    },
                ],
            }
        )
        with pytest.raises(ParseFailed, match="portfolio dates"):
            GrowwHoldingsParser().parse(RawFile("x", "S7", "a.html", page))

    def test_a_page_without_the_payload_is_refused(self) -> None:
        raw = RawFile("x", "S7", "a.html", b"<html><body>nothing here</body></html>")
        with pytest.raises(ParseFailed, match="__NEXT_DATA__"):
            GrowwHoldingsParser().parse(raw)

    def test_an_empty_portfolio_is_refused(self) -> None:
        """§6.3 rule 3. Zero holdings loaded as a portfolio would read as a
        fund that owns nothing, which is a very different claim from a fetch
        that failed."""
        page = _page({"scheme_name": "A Fund", "aum": 1.0, "holdings": []})
        with pytest.raises(ParseFailed, match="no holdings"):
            GrowwHoldingsParser().parse(RawFile("x", "S7", "a.html", page))

    def test_rows_that_do_not_reach_the_stated_total_warn(self) -> None:
        """§7.2's 100x path. `crore` is asserted, not read off a header cell,
        so the assertion gets a witness: a page whose rows and whose `aum`
        stopped agreeing is a page whose units may have moved."""
        page = _page(
            {
                "scheme_name": "A Fund",
                "aum": 1000.0,
                "holdings": [{**HOLDING, "market_value": 100.0, "corpus_per": 100.0}],
            }
        )
        result = GrowwHoldingsParser().parse(RawFile("x", "S7", "a.html", page))
        assert [w.code for w in result.warnings] == ["GROWW_TOTAL_DRIFT"]


class TestTheSlugMap:
    """The slug is the one input that can be wrong without anything noticing."""

    def test_every_key_is_an_isin_and_every_entry_has_a_slug(self) -> None:
        slugs = yaml.safe_load(SLUGS.read_text(encoding="utf-8"))["slugs"]
        assert slugs
        for scheme_id, entry in slugs.items():
            assert re.fullmatch(r"INF[A-Z0-9]{9}", scheme_id), scheme_id
            assert entry["slug"] and " " not in entry["slug"]

    def test_the_two_renamed_funds_keep_their_former_names(self) -> None:
        """Not a style check — the reason the file exists.

        `slugify("HDFC Flexi Cap Fund")` does not produce
        `hdfc-equity-fund-direct-growth`, and both slugs below were guessed
        from the current name first and 404'd. Anything that later replaces
        this map with a derivation has to fail here.
        """
        slugs = yaml.safe_load(SLUGS.read_text(encoding="utf-8"))["slugs"]
        assert slugs["INF179K01UT0"]["slug"] == "hdfc-equity-fund-direct-growth"
        assert (
            slugs["INF879O01027"]["slug"]
            == "parag-parikh-long-term-value-fund-direct-growth"
        )
