"""Groww scheme pages. MODULE_0.md §6.1, DECISIONS V1-43.

**The coverage tier.** Every parser before this one reads an AMC's own
statutory workbook, which is why the warehouse covers five fund houses and not
fifty-two. This one reads an aggregator's scheme page, so it reaches any fund
Groww lists — and it is the answer to "what if I hold a fund from an AMC with
no parser", which no amount of work on the Excel readers would ever give.

It is not the source of record and must never outrank one. See `HAS NO ISIN`
below for what that costs, measured.

## The page is server-rendered, so there is no browser here

The whole portfolio sits in the `__NEXT_DATA__` payload of the HTML that a
plain GET returns: `props.pageProps.mfServerSideData.holdings`. No headless
browser, no internal API. `/v1/api/*` is `Disallow` in Groww's robots.txt and
is deliberately not used; `/mutual-funds/<slug>` is allowed and the page sets
`meta_robots: index`.

## It is a whole portfolio, not a top-ten teaser

Verified on HDFC Flexi Cap, 2026-08-31:

    86 rows, `corpus_per` summing to exactly 100.0000%
    sum(market_value) = 113606.4660 == the page's own `aum` field, exactly

That equality is what makes §6's reconciliation gate work here at all. Every
Excel reader leans on the AMC printing what its portfolio adds up to; this page
prints the same thing under a different name, so `stated_total` has a real
witness and a misparse still refuses to load.

## HAS NO ISIN, and that is the whole trade

The rows carry `company_name`, not an identifier. Four of our resolution rules
key on ISIN STRUCTURE -- V1-29's issuer segment, V1-30's sovereign prefix,
V1-41's `INF` fund rule, V1-42's commodity-by-absence -- and all four are
inert here. Measured through the real cascade against the real entity master,
same fund, same month:

    the AMC's own file      0.00% unresolved
    this page               8.96% unresolved, 7 rows of 86

The seven say it better than the percentage does. `Zomato Ltd` is `Eternal
Limited` in the master and has been since the rename; `Kalpataru Power
Transmission` is now `Kalpataru Projects International`. No name matcher fixes
a rename, ever, and the ISIN never moved. The others are ambiguity the cascade
refuses on purpose -- three `Kotak Mahindra` issuers, `Apollo Hospitals
Enterprise` against the master's `...Enterprises Ltd.` -- which is V1-02
departure 2 doing its job rather than failing.

So the rows are staged with `isin_raw=None`. Not a placeholder, not a guess
from the name: the page did not say, and §6.3 rule 1 means the parser does not
invent what the source withheld.

## One month only

The page carries the latest disclosure and no archive. There is no backfill
from here and a month not fetched is a month lost, which is the argument for
the AMC tier keeping its place rather than being retired.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from src.m0_data.parse.base import (
    HoldingsParseResult,
    ParseFailed,
    ParseWarning,
    RawFile,
    StagedHolding,
)

PARSER_ID = "holdings.groww"
VERSION = "2"

#: A constant, not a lookup: IST has no DST and never has had any, so anything
#: cleverer would be pretending the offset could move.
IST = timezone(timedelta(hours=5, minutes=30))

#: The payload the page ships its server-side props in. Matched rather than
#: parsed as HTML because the whole file is one `<script>` away from the data
#: and pulling in a DOM parser to reach it would be the only reason to have one.
NEXT_DATA = re.compile(r'<script[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

#: Groww reports crores. It says so nowhere on the page -- there is no header
#: cell to read a unit from, the way `unit_from_header` reads one off every
#: workbook -- so this is the single place where a unit is asserted rather than
#: observed, and `_check_unit` below is what stops that assertion being silent.
MARKET_VALUE_UNIT = "crore"

#: `nature_name` -> the section heading an AMC workbook would have written.
#: V1-07: the section is the only reliable signal that a row is a derivative,
#: and this page keeps that signal in two columns rather than a heading.
NATURE_SECTION = {
    "EQUITY": "EQUITY & EQUITY RELATED",
    "DEBT": "DEBT INSTRUMENTS",
    "CASH": "CASH & CASH EQUIVALENT",
    "REALEST": "REITS & INVITS",
}

#: `instrument_name` values that name a derivative. The section built from
#: `nature_name` alone would call a stock future EQUITY, which is precisely the
#: misclassification V1-07 exists to prevent -- Groww files `Futures` under
#: `nature_name: EQUITY`, just as HDFC writes its short leg under the same
#: name as the long position twelve rows above.
#:
#: `deriv` is in the list because `Index Derivatives` is how Groww labels an
#: index position and the first draft, which matched only the four instrument
#: words, classified one as EQUITY. Found on the second fund this parser was
#: pointed at, which is the argument for pointing it at a second fund.
DERIVATIVE_INSTRUMENTS = re.compile(r"\b(deriv|future|option|swap|forward)", re.I)

#: `instrument_name` values that are money market rather than debt. Groww files
#: `CBLO` under `nature_name: DEBT`, and on Axis Small Cap that was Rs 2,337 Cr
#: -- the single largest unresolved row in the fund -- because `debt` is
#: deliberately absent from §8.4's `CLASS_FALLBACK`: a bond HAS an issuer and
#: bucketing one as cash would hide real credit exposure. CBLO does not have
#: one. It is TREPS by its former name, and the cascade already knows the word
#: once the row reaches it classed as cash.
CASH_INSTRUMENTS = re.compile(
    r"\b(cblo|treps|repo|reverse\s*repo|net\s+(payable|receivable)|"
    r"cash|margin|deposit)",
    re.I,
)


class GrowwHoldingsParser:
    """Satisfies `HoldingsParser`."""

    parser_id = PARSER_ID
    version = VERSION
    #: Not an AMC. Every other parser is named for the house that published the
    #: file; this one is named for the aggregator that republished it, and
    #: `source_tier` on the disclosure is what carries that distinction
    #: downstream.
    amc_id = "groww"

    def sniff(self, f: RawFile) -> float:
        """Cheap checks only (§6.1): magic bytes and two marker strings."""
        if f.content[:2] == b"PK" or f.content[:4] == b"\xd0\xcf\x11\xe0":
            return 0.0  # a workbook, and an AMC parser's business

        # The WHOLE document, not a prefix. Next.js emits `__NEXT_DATA__` as
        # the last element of `<body>`: on the page this was written against it
        # begins at byte 250,231 of 491,655, so a 200 KB window -- which looked
        # like the frugal choice -- scored this parser 0.00 on the very file it
        # was written for. `bytes.find` over half a megabyte is microseconds,
        # and "cheap" in §6.1 means no parsing, not no scanning.
        #
        # Both markers are literals Next.js and Groww emit with fixed casing,
        # so they are matched case-sensitively rather than lowercasing a copy
        # of the document to find them.
        if b"__NEXT_DATA__" not in f.content:
            return 0.0
        score = 0.6
        if b"mfServerSideData" in f.content:
            score += 0.3
        if "groww" in f.filename.lower() or b"groww.in" in f.content[:100_000]:
            score += 0.1
        return min(score, 1.0)

    def parse(self, f: RawFile, sheet: str | None = None) -> HoldingsParseResult:
        """§6.3 rule 3: raise rather than return a partial portfolio.

        `sheet` is meaningless for a page that describes exactly one scheme,
        and is accepted only to satisfy the protocol. Passing one is a caller
        confusing this with Nippon's 108-sheet workbook, so it is refused
        rather than ignored.
        """
        if sheet is not None:
            raise ParseFailed(
                f"{f.filename}: a Groww page carries one scheme; got sheet={sheet!r}"
            )

        data = _payload(f)
        rows = data.get("holdings")
        if not isinstance(rows, list) or not rows:
            raise ParseFailed(f"{f.filename}: no holdings in the page payload")

        result = HoldingsParseResult()
        result.scheme_raw_name = _text(data.get("scheme_name"))
        result.as_of_date = _as_of(rows, f)
        result.headers_seen = ("company_name", "market_value", "corpus_per")

        # The page states its own total under a different name than a workbook
        # does, and it is the same witness: a parse that disagrees with it has
        # misread the payload. §6's reconciliation gate reads this.
        result.stated_total = _decimal(data.get("aum"))
        # `corpus_per` is written as 9.19 for 9.19%, so the scale is 1 -- the
        # same reading HDFC's workbook gets and the opposite of ICICI's.
        result.pct_scale = Decimal(1)

        nav = _decimal(data.get("nav"))
        if nav is not None:
            # An independent witness that the slug maps to the scheme we think
            # it does, which is the one thing a URL cannot tell us.
            result.stated_navs["Groww"] = nav

        for index, raw in enumerate(rows, start=1):
            if not isinstance(raw, dict):
                raise ParseFailed(f"{f.filename}: holding {index} is not an object")
            result.rows.append(_stage(raw, index, f))

        _check_unit(result, f)
        return result


def _payload(f: RawFile) -> dict[str, Any]:
    """`mfServerSideData`, or raise saying which step of the walk failed."""
    text = f.content.decode("utf-8", errors="replace")
    match = NEXT_DATA.search(text)
    if match is None:
        raise ParseFailed(f"{f.filename}: no __NEXT_DATA__ script in the page")
    try:
        blob = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ParseFailed(f"{f.filename}: __NEXT_DATA__ is not JSON: {exc}") from exc

    node: Any = blob
    for key in ("props", "pageProps", "mfServerSideData"):
        if not isinstance(node, dict) or key not in node:
            raise ParseFailed(f"{f.filename}: payload has no {key!r}")
        node = node[key]
    if not isinstance(node, dict):
        raise ParseFailed(f"{f.filename}: mfServerSideData is not an object")
    return node


def _stage(raw: dict[str, Any], row_number: int, f: RawFile) -> StagedHolding:
    """One holding, verbatim. §6.3 rule 1 -- nothing is converted here.

    Every row is a `security`. The page publishes a table of holdings and
    nothing else: no section headings, no subtotals, no grand total, none of
    §6.4's furniture. `Net Payables` is a real negative holding and is staged
    as one, the same as a workbook's `Net Current Assets` row.
    """
    name = _text(raw.get("company_name"))
    if not name:
        raise ParseFailed(f"{f.filename}: holding {row_number} has no company_name")

    nature = _text(raw.get("nature_name")) or ""
    instrument = _text(raw.get("instrument_name")) or ""
    section = NATURE_SECTION.get(nature.upper(), nature.upper() or None)
    # `instrument_name` OVERRIDES `nature_name`, in that order and only in that
    # order. The two columns disagree on exactly the rows where the coarse one
    # is wrong -- a future filed under EQUITY, CBLO filed under DEBT -- and the
    # finer one is right both times. Derivative is tested last so that a
    # hypothetical `Repo Future` reads as the derivative it is.
    if CASH_INSTRUMENTS.search(instrument):
        section = "CASH & CASH EQUIVALENT"
    if DERIVATIVE_INSTRUMENTS.search(instrument):
        section = "DERIVATIVES"

    return StagedHolding(
        row_number=row_number,
        row_kind="security",
        instrument_raw_name=name,
        # The page does not carry one. §6.3 rule 1: do not invent it.
        isin_raw=None,
        # Nor a quantity -- Groww publishes value and weight only.
        quantity_raw=None,
        market_value_raw=_decimal(raw.get("market_value")),
        market_value_unit=MARKET_VALUE_UNIT,
        pct_to_nav_raw=_decimal(raw.get("corpus_per")),
        reported_sector=_text(raw.get("sector_name")),
        coupon_or_rating=_text(raw.get("rating")),
        sheet_name="groww",
        section=section,
    )


def _as_of(rows: list[Any], f: RawFile) -> date:
    """`portfolio_date`, which every row repeats.

    Refused when the rows disagree. A page serving two dates at once is one
    describing two portfolios, and picking either would be a guess about which.
    """
    seen = {
        stamp
        for r in rows
        if isinstance(r, dict) and (stamp := _text(r.get("portfolio_date")))
    }
    if not seen:
        raise ParseFailed(f"{f.filename}: no portfolio_date on any holding")
    if len(seen) > 1:
        raise ParseFailed(
            f"{f.filename}: holdings carry {len(seen)} portfolio dates: "
            + ", ".join(sorted(seen))
        )
    stamp = seen.pop()
    try:
        # `2026-08-30T18:30:00.000Z` is midnight of the 31st in IST, which is
        # the date the AMC disclosed and the date every other parser records.
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ParseFailed(f"{f.filename}: portfolio_date {stamp!r}: {exc}") from exc
    return parsed.astimezone(IST).date()


def _check_unit(result: HoldingsParseResult, f: RawFile) -> None:
    """Warn when the rows do not add up to the total the page states.

    `MARKET_VALUE_UNIT` is the one assertion in this parser that no cell on the
    page supports, so it gets its own check: `aum` and `market_value` are
    published in the SAME unit whatever that unit is, and they agreed exactly
    on every page measured. A disagreement means the assumption has moved and
    the load must not proceed quietly -- §7.2 calls this the 100x-error path.
    """
    stated = result.stated_total
    if stated is None or stated == 0:
        result.warnings.append(
            ParseWarning("GROWW_NO_TOTAL", "page states no aum to reconcile against")
        )
        return
    summed = sum(
        (r.market_value_raw for r in result.securities if r.market_value_raw), Decimal(0)
    )
    drift = (summed - stated) / stated * 100
    if abs(drift) > Decimal("0.5"):
        result.warnings.append(
            ParseWarning(
                "GROWW_TOTAL_DRIFT",
                f"holdings sum to {summed} against a stated aum of {stated}"
                f" ({drift:+.4f}%)",
            )
        )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decimal(value: Any) -> Decimal | None:
    """JSON numbers arrive as `float`. Through `str` so the text is what binds.

    `Decimal(9.19386133)` is the binary expansion; `Decimal("9.19386133")` is
    the number the page printed, and CLAUDE.md invariant 1 wants the second.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
