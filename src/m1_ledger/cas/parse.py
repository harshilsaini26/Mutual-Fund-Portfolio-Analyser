"""CAS text -> staged rows. MODULE_1.md §5.3.

A line-oriented state machine, not table extraction (§5.2): CAS tables are
unruled, columns drift across pages, and folio/scheme context lives outside any
table, so table extractors lose the context that makes a row meaningful.

Deliberately PURE — `list[str]` in, staged rows out. Decryption and text
extraction live in `pdf.py`, the only place touching a password or a file. That
split is what makes the parser testable: a real CAS is Zone B personal data and
can never be committed, so every test runs against a synthetic statement.

Two departures from §5.3, both recorded in V0-15:

1.  **Nothing is silently dropped.** §5.3 skips any line inside a scheme block
    that `TXN_RE` does not match, so a regex that fails on a real transaction
    loses it without trace. Every unconsumed line is collected in
    `CasContext.unparsed`, and the import refuses to report `ok` while any
    remain.
2.  **Trailing numeric columns are tokenised from the right.** §5.3's `TXN_RE`
    mandates exactly six columns, so a zero-unit `IDCW_PAYOUT` — legitimate per
    §5.4 — prints no units, fails the match, and falls into the silent skip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum, auto

from src.common.decimals import dec

#: Bumped on ANY change to what this parser produces, so a fixed parser can be
#: replayed over every statement the old one touched. `cas_import` records it
#: per import, which is what makes "which parser version produced this row?"
#: answerable three months from now.
PARSER_VERSION = "1"


class CasParseError(ValueError):
    """The statement is not shaped like a CAS at all."""


class CasState(Enum):
    """MODULE_1.md §5.3."""

    SEEKING_FOLIO = auto()
    IN_FOLIO = auto()
    IN_SCHEME = auto()
    IN_SUMMARY = auto()  # must be skipped entirely: it repeats every holding


# --- line patterns ---------------------------------------------------------
# `re.I` throughout: registrars are inconsistent about case, and MF Central
# re-cases CAMS wording when it merges the two.

#: CAMS prints a folio as `12345678 / 90` — a base number and a check
#: segment, spaces and all. §5.3's `([\w/\-]+)` stops at the first space and
#: keeps only the base, so two folios sharing a base collide into one. The
#: segments are captured and the whitespace normalised out instead.
FOLIO_RE = re.compile(r"^Folio\s+No[:.\s]+([\w\-]+(?:\s*/\s*[\w\-]+)*)", re.I)
#: An ISIN is twelve characters: a two-letter country code, nine
#: alphanumerics and a numeric check digit. Indian mutual fund units all
#: begin `INF`, so nine characters follow it.
#:
#: MODULE_1.md §5.3 writes this as `INF\w{9}\d`, which demands THIRTEEN. It
#: cannot match any ISIN that has ever existed, so `SCHEME_RE` never fires,
#: the state machine never enters IN_SCHEME, and the parser returns zero
#: transactions from a perfectly good statement. DECISIONS V0-15.
SCHEME_RE = re.compile(
    # U+2013 EN DASH, written as an escape because it is
    # indistinguishable from a hyphen on screen. CAMS separates the scheme
    # name from the ISIN with one; KFintech uses a hyphen; MF Central, either.
    r"^(.+?)\s*[-\u2013]\s*ISIN[:\s]*(INF[A-Z0-9]{8}[0-9])\b",
    re.I,
)
OPENING_RE = re.compile(r"Opening\s+Unit\s+Balance[:\s]*([\d,\.]+)", re.I)
CLOSING_RE = re.compile(r"Closing\s+Unit\s+Balance[:\s]*([\d,\.]+)", re.I)
SUMMARY_RE = re.compile(
    r"^(Portfolio\s+Summary|Summary\s+of|Total\s+Value|Grand\s+Total)", re.I
)
#: A folio block ends the summary; so does a new statement page header.
DATE_HEAD_RE = re.compile(r"^(\d{2}-[A-Za-z]{3}-\d{4})\s+(.*\S)\s*$")
#: One printed money/units/NAV column. Handles Indian digit grouping
#: (`1,23,456.78` — naive comma stripping is correct, §5.4 warns against
#: locale parsing) and parenthesised negatives (`(500.00)` -> -500.00).
NUM_TOKEN_RE = re.compile(r"^\(?-?[\d,]*\d(?:\.\d+)?\)?$")

#: Statement furniture: page headers, column headings, rules. These are the
#: only lines allowed to vanish without being recorded, and they are an
#: explicit list rather than a catch-all for exactly that reason — the
#: moment "skip anything that looks like noise" is a rule, a transaction
#: that looks unusual is noise.
NOISE_RE = re.compile(
    r"^(Page\s+\d+\s+of\s+\d+"
    r"|CONSOLIDATED\s+ACCOUNT\s+STATEMENT"
    r"|Date\s+Transaction\s+"
    r"|Registrar\s*[:\s]"
    r"|[-=_*\s]+)$",
    re.I,
)

#: Column counts we know how to read, right-anchored. See the module docstring.
#: 4 = amount, units, nav, balance — the ordinary row.
#: 2 = amount, balance — a zero-unit IDCW payout, which prints no units or NAV.
_KNOWN_WIDTHS = (4, 2)


@dataclass(frozen=True)
class StagedTxn:
    """One parsed line, before type mapping or scheme resolution.

    Everything is kept verbatim as `*_raw` alongside the parsed value.
    MODULE_1.md §4.3 requires the raw scheme text to survive so an unresolvable
    scheme can be re-resolved later without re-importing the PDF — and the same
    reasoning applies to every field: when a number looks wrong six months on,
    the question is always what the statement actually printed.
    """

    folio: str
    scheme_raw_name: str
    scheme_raw_isin: str
    txn_date: date
    desc_raw: str
    amount: Decimal | None
    units: Decimal | None
    nav: Decimal | None
    balance: Decimal | None
    row_number: int
    line_raw: str
    #: Charges printed on their own line beneath this one (§5.4): stamp duty,
    #: STT, exit load, TDS. Keyed by the `field` in config/txn_types.yaml.
    charges: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class BalanceMarker:
    """An opening or closing unit balance printed by the statement itself.

    The closing balance is what reconciliation checks the ledger against
    (§11.1); without it there is only internal self-consistency.

    The OPENING balance is captured too, which §5.3 does not do — it defines
    `OPENING_RE` and never uses it. It is direct evidence for §11.2's
    `MISSING_EARLY_CAS`: a non-zero opening balance says units existed before
    this statement's period (V0-15).
    """

    folio: str
    scheme_raw_isin: str
    kind: str  # "opening" | "closing"
    units: Decimal
    row_number: int


@dataclass
class CasContext:
    """Everything the parse learned that is not a transaction."""

    balances: list[BalanceMarker] = field(default_factory=list)
    #: Lines inside a scheme block that nothing consumed. Never empty-and-fine:
    #: see the module docstring.
    unparsed: list[tuple[int, str]] = field(default_factory=list)

    def record_balance(
        self, folio: str, isin: str, kind: str, units: Decimal, row_number: int
    ) -> None:
        self.balances.append(BalanceMarker(folio, isin, kind, units, row_number))

    def closing_balance(self, folio: str, isin: str) -> Decimal | None:
        """The last closing balance printed for this folio-scheme."""
        found = [
            b
            for b in self.balances
            if b.folio == folio and b.scheme_raw_isin == isin and b.kind == "closing"
        ]
        return found[-1].units if found else None

    def opening_balance(self, folio: str, isin: str) -> Decimal | None:
        found = [
            b
            for b in self.balances
            if b.folio == folio and b.scheme_raw_isin == isin and b.kind == "opening"
        ]
        return found[0].units if found else None


def to_decimal(raw: str) -> Decimal | None:
    """Parse one printed column. §5.4's two number traps, together.

    Indian digit grouping (`1,23,456.78`) is handled by stripping commas, which
    §5.4 prefers over `locale.atof` — that needs a system locale which may not
    be installed and misreads the grouping silently when it is not.

    Parenthesised negatives are CAMS's convention for units leaving a folio.
    Reading `(20.000)` as +20 turns a redemption into a purchase.
    """
    token = raw.strip()
    if not token or token in {"-", "--"}:
        return None
    negative = token.startswith("(") and token.endswith(")")
    token = token.strip("()").replace(",", "").strip()
    if not token:
        return None
    try:
        value = dec(token)
    except (InvalidOperation, ArithmeticError) as exc:  # pragma: no cover - defensive
        raise CasParseError(f"unparseable number {raw!r}") from exc
    if value is None:
        return None
    if not value.is_finite():
        # `Decimal` parses "Infinity" and "NaN" without complaint, and a folio's
        # units and rupees run through here. NaN is the dangerous one: every
        # comparison against it is False, so a redemption for NaN units would
        # pass `units > held` and corrupt the book silently. M0's `to_decimal`
        # carries the same guard — the duplication is invariant 3's, not an
        # oversight.
        raise CasParseError(f"not a finite number: {raw!r}")
    return -value if negative else value


def parse_date(raw: str) -> date:
    """`04-Nov-2024`. The only date format CAS uses, in every registrar's output."""
    return datetime.strptime(raw.strip(), "%d-%b-%Y").date()


def _split_trailing_numbers(rest: str) -> tuple[str, list[Decimal | None]]:
    """Peel numeric columns off the right-hand edge of a transaction line.

    The description is free text and can contain digits and parentheses; the
    numeric columns cannot. So the reliable anchor is the right edge, not a
    greedy `(.+?)` in the middle: stop at the first non-numeric token from the
    right, and whatever remains to its left is the description.
    """
    tokens = rest.split()
    numbers: list[Decimal | None] = []
    while tokens and NUM_TOKEN_RE.match(tokens[-1]):
        numbers.insert(0, to_decimal(tokens.pop()))
    return " ".join(tokens).strip(), numbers


def parse_cas(lines: list[str], ctx: CasContext | None = None) -> list[StagedTxn]:
    """Run the state machine. MODULE_1.md §5.3.

    Returns staged rows in printed order. Type mapping, scheme resolution and
    sequence assignment all happen later, in `importer.py` — this function's
    only job is to say faithfully what the statement printed.
    """
    ctx = ctx if ctx is not None else CasContext()
    state = CasState.SEEKING_FOLIO
    folio: str | None = None
    scheme_name: str | None = None
    scheme_isin: str | None = None
    out: list[StagedTxn] = []
    charge_patterns = _charge_patterns()

    for lineno, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue

        # Order matters throughout. The summary section repeats every holding
        # (§5.4); parsing it double-counts the entire portfolio, so the check
        # comes before anything that could match inside it.
        if SUMMARY_RE.match(line):
            state = CasState.IN_SUMMARY
            continue

        m = FOLIO_RE.match(line)
        if m:
            folio = re.sub(r"\s+", "", m.group(1)).rstrip(".")
            state = CasState.IN_FOLIO
            scheme_name = scheme_isin = None
            continue

        if state is CasState.IN_SUMMARY:
            continue

        m = SCHEME_RE.match(line)
        if m:
            if folio is None:
                raise CasParseError(f"line {lineno}: scheme block before any folio")
            scheme_name, scheme_isin = m.group(1).strip(), m.group(2).upper()
            state = CasState.IN_SCHEME
            continue

        if state is not CasState.IN_SCHEME:
            continue
        assert folio is not None and scheme_isin is not None

        # Balance lines look like transactions and must be consumed first
        # (§5.4). A closing balance parsed as a transaction invents units that
        # were never bought.
        m = CLOSING_RE.search(line)
        if m:
            units = to_decimal(m.group(1))
            if units is not None:
                ctx.record_balance(folio, scheme_isin, "closing", units, lineno)
            continue

        m = OPENING_RE.search(line)
        if m:
            units = to_decimal(m.group(1))
            if units is not None:
                ctx.record_balance(folio, scheme_isin, "opening", units, lineno)
            continue

        # A charge line belongs to the transaction above it (§5.4), so it is
        # attached rather than staged. Checked before the transaction match
        # because "*** Stamp Duty *** 0.50" has no leading date and would
        # otherwise land in `unparsed`.
        charge = _match_charge(line, charge_patterns)
        if charge is not None:
            name, value = charge
            if not out:
                ctx.unparsed.append((lineno, line))
                continue
            out[-1].charges[name] = out[-1].charges.get(name, Decimal(0)) + value
            continue

        if NOISE_RE.match(line):
            continue

        m = DATE_HEAD_RE.match(line)
        if m:
            desc, numbers = _split_trailing_numbers(m.group(2))
            if len(numbers) in _KNOWN_WIDTHS and desc:
                amount, units, nav, balance = _assign_columns(numbers)
                out.append(
                    StagedTxn(
                        folio=folio,
                        scheme_raw_name=scheme_name or "",
                        scheme_raw_isin=scheme_isin,
                        txn_date=parse_date(m.group(1)),
                        desc_raw=desc,
                        amount=amount,
                        units=units,
                        nav=nav,
                        balance=balance,
                        row_number=lineno,
                        line_raw=line,
                    )
                )
                continue

        # Reached only when nothing above consumed the line. §5.3 would drop it
        # here without a word. Keeping it is the whole point: a transaction the
        # regexes did not recognise is a missing transaction, and a ledger
        # missing a row reconciles wrong for a reason nothing reports.
        ctx.unparsed.append((lineno, line))

    return out


def _assign_columns(
    numbers: list[Decimal | None],
) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
    """Map right-anchored columns onto (amount, units, nav, balance).

    Four columns is the ordinary row. Two is a zero-unit distribution: CAS
    prints the amount and the unchanged unit balance, with the units and NAV
    columns blank, and `layout=True` collapses the blanks away. §5.4 says these
    rows are legitimate and must not be filtered out, so they have to be
    readable in the first place.
    """
    if len(numbers) == 4:
        return numbers[0], numbers[1], numbers[2], numbers[3]
    return numbers[0], None, None, numbers[1]


def _charge_patterns() -> list[tuple[re.Pattern[str], str]]:
    from src.m1_ledger.cas.mapping import charge_patterns

    return charge_patterns()


def _match_charge(
    line: str, patterns: list[tuple[re.Pattern[str], str]]
) -> tuple[str, Decimal] | None:
    for pattern, name in patterns:
        if pattern.search(line):
            _, numbers = _split_trailing_numbers(line)
            if len(numbers) == 1 and numbers[0] is not None:
                return name, numbers[0]
            # A charge line whose amount we cannot read is worse than useless:
            # it would silently zero a real cost. Let it fall through to
            # `unparsed` so the import reports it.
            return None
    return None
