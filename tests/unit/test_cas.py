"""CAS ingestion. MODULE_1.md §5.

Every test runs against a synthetic statement. A real CAS carries a PAN, folio
numbers and a postal address — Zone B data that cannot be committed — so
`tests/fixtures/cas/traps.txt` reproduces the CAMS layout with invented values,
one trap from §5.4 per labelled line.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.types import SchemeId, UserId
from src.m1_ledger.cas import (
    CasContext,
    StagedTxn,
    assign_sequences,
    import_cas,
    map_txn_type,
    parse_cas,
    to_decimal,
)
from src.m1_ledger.cas.importer import (
    SWITCH_AMOUNT_TOL,
    _signed_amount,
    switch_consideration,
    switch_proceeds,
)
from src.m1_ledger.lots import build_book
from src.m1_ledger.txn import UnmappedTransactionType, drop_reversed

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "cas" / "traps.txt"
USER = UserId("USER-01")
HDFC_ISIN = "INF179K01WM2"
HDBA_ISIN = "INF179K01XA5"
ICICI_ISIN = "INF109K01BL4"
FOLIO_1 = "12345678/90"


def load_cas_fixture(path: Path = FIXTURE) -> list[str]:
    """Strip the fixture's own commentary, the way `load_transactions` does.

    The `>>>` prefix is not in the parser's noise allowlist, deliberately: a
    parser taught to ignore an unfamiliar line is a parser that loses
    transactions, which is the failure mode this module is built around.
    """
    return [
        ln
        for ln in path.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith(">>>")
    ]


@pytest.fixture
def lines() -> list[str]:
    return load_cas_fixture()


@pytest.fixture
def ctx() -> CasContext:
    return CasContext()


@pytest.fixture
def staged(lines: list[str], ctx: CasContext) -> list[StagedTxn]:
    return parse_cas(lines, ctx)


def _resolve(s: StagedTxn) -> SchemeId | None:
    """Stand-in for M0's `resolve_scheme`. Always on ISIN, never on name."""
    known = {
        HDFC_ISIN: "AMFI:HDFC-FLEXICAP-DIR-G",
        HDBA_ISIN: "AMFI:HDFC-BALADV-DIR-G",
        ICICI_ISIN: "AMFI:ICICI-BLUECHIP-REG-IDCW",
    }
    found = known.get(s.scheme_raw_isin)
    return SchemeId(found) if found else None


# --- §5.4 traps, one test each ---------------------------------------------


def test_the_summary_section_is_never_parsed(staged: list[StagedTxn]) -> None:
    """§5.4's first trap, and the one that silently doubles a portfolio.

    The summary repeats every holding in transaction shape. The fixture's
    summary contains a verbatim copy of the 10-Jun switch-out; if the state
    machine parsed it, that switch would be booked twice and 30 units would
    leave a folio that only ever had one such sale.
    """
    lines = load_cas_fixture()
    summary_at = next(i for i, ln in enumerate(lines, 1) if ln == "Portfolio Summary")

    switch_outs = [s for s in staged if "Switch Out" in s.desc_raw]
    assert len(switch_outs) == 1
    assert switch_outs[0].row_number < summary_at, "matched the summary copy"
    assert all(s.row_number < summary_at for s in staged)


def test_closing_balance_lines_are_not_transactions(
    staged: list[StagedTxn], ctx: CasContext
) -> None:
    """§5.4: they look like transactions and are consumed before TXN_RE."""
    assert not any("Closing" in s.desc_raw for s in staged)
    assert ctx.closing_balance(FOLIO_1, HDFC_ISIN) == Decimal("159.090417")
    assert ctx.closing_balance(FOLIO_1, HDBA_ISIN) == Decimal("109.144552")


def test_the_opening_balance_is_captured(
    ctx: CasContext, staged: list[StagedTxn]
) -> None:
    """§5.3 defines OPENING_RE and never uses it. DECISIONS V0-15.

    The opening balance is the statement saying, in as many words, that units
    existed before this period. Without it a short ledger and a parser bug look
    identical; with it, §11.2 can say `MISSING_EARLY_CAS` and mean it.
    """
    assert ctx.opening_balance(FOLIO_1, HDFC_ISIN) == Decimal("120.500000")
    assert ctx.opening_balance(FOLIO_1, HDBA_ISIN) == Decimal("0.000000")


def test_indian_digit_grouping_is_read_by_stripping_commas() -> None:
    """§5.4 prefers naive comma-stripping to locale parsing, and is right.

    `locale.atof` needs a system locale that may not be installed, and the
    en_IN grouping is 2-2-3 rather than 3-3-3, so a locale that IS installed
    but wrong reads 1,23,456.78 as 123.45678 or fails outright.
    """
    assert to_decimal("1,23,456.78") == Decimal("123456.78")
    assert to_decimal("1,755.672000") == Decimal("1755.672000")


def test_a_parenthesised_number_is_negative(staged: list[StagedTxn]) -> None:
    """§5.4. Reading `(30.000000)` as +30 turns a sale into a purchase."""
    assert to_decimal("(500.00)") == Decimal("-500.00")
    switch_out = next(s for s in staged if "Switch Out" in s.desc_raw)
    assert switch_out.units == Decimal("-30.000000")


def test_a_zero_unit_idcw_payout_is_read_not_dropped(staged: list[StagedTxn]) -> None:
    """§5.4 says these are legitimate; §5.3's regex cannot match one.

    A payout prints an amount and an unchanged balance, with the units and NAV
    columns blank — four columns, not six. Under §5.3's positional `TXN_RE` it
    fails to match and is skipped without a word, so the distribution never
    reaches the ledger and XIRR is short a cashflow.
    """
    payout = next(s for s in staged if s.txn_date == date(2024, 8, 12))
    assert payout.amount == Decimal("1250.00")
    assert payout.units is None and payout.nav is None
    assert payout.balance == Decimal("500.000000")
    assert map_txn_type(payout.desc_raw, payout.units) == "IDCW_PAYOUT"


def test_a_charge_line_attaches_to_the_transaction_above_it(
    staged: list[StagedTxn],
) -> None:
    """§5.4: stamp duty and exit load are stored separately, not as rows."""
    sip = next(s for s in staged if s.txn_date == date(2024, 4, 1))
    assert sip.charges == {"stamp_duty": Decimal("0.50")}
    switch_out = next(s for s in staged if "Switch Out" in s.desc_raw)
    assert switch_out.charges == {"exit_load": Decimal("564.87")}


def test_a_scheme_block_survives_a_page_break(staged: list[StagedTxn]) -> None:
    """§5.4: headers repeat mid-scheme and context carries across them.

    The 10-Jun switch-out sits after a repeated page header and a repeated
    scheme line. It must still be attributed to the folio and ISIN that opened
    the block, not orphaned.
    """
    switch_out = next(s for s in staged if "Switch Out" in s.desc_raw)
    assert switch_out.folio == FOLIO_1
    assert switch_out.scheme_raw_isin == HDFC_ISIN


def test_the_folio_check_segment_is_not_lost(staged: list[StagedTxn]) -> None:
    """§5.3's `([\\w/\\-]+)` stops at the first space. DECISIONS V0-15.

    CAMS prints `12345678 / 90`. Keeping only `12345678` merges every folio
    that shares a base number — and FIFO is folio-scoped (`PLAN.md` §9.7), so
    merged folios silently consume each other's lots.
    """
    assert {s.folio for s in staged} == {FOLIO_1, "87654321/11"}


# --- the silent-drop guarantee ---------------------------------------------


def test_no_line_inside_a_scheme_block_is_silently_dropped(ctx: CasContext) -> None:
    """The whole reason this parser departs from §5.3. DECISIONS V0-15.

    §5.3 skips any unmatched line inside a scheme block. §5.5 is careful to
    RAISE on an unmapped description — but a row the regex never matched never
    reaches the mapper, so the loud failure the spec designed cannot fire for
    the quiet failure that actually loses data.

    The fixture is clean, so `unparsed` is empty. The test that matters is the
    next one: a line the parser does not understand has to show up.
    """
    parse_cas(load_cas_fixture(), ctx)
    assert ctx.unparsed == []


def test_an_unrecognised_line_is_reported_rather_than_skipped() -> None:
    """Insert a transaction-shaped line the column reader cannot size."""
    lines = load_cas_fixture()
    at = next(i for i, ln in enumerate(lines) if ln.startswith("15-May-2024"))
    lines.insert(at, "18-May-2024   Purchase of units at an unstated price")

    ctx = CasContext()
    parse_cas(lines, ctx)
    assert [text for _, text in ctx.unparsed] == [
        "18-May-2024   Purchase of units at an unstated price"
    ]

    # Drop the deliberately-unmappable row: it sets `partial` for its own
    # reasons, and would let this assertion pass with the unparsed count
    # ignored entirely. Mutation testing found exactly that.
    clean = [ln for ln in lines if not ln.startswith("05-Sep-2024")]
    report = import_cas(USER, clean, _resolve)
    assert report.unmatched == 0, "the only remaining problem is the unread line"
    assert report.unparsed_lines == 1
    assert report.status == "partial", "an unread line must not import as ok"


def test_an_unmapped_description_is_quarantined_never_guessed() -> None:
    """§5.5, the one rule that is never relaxed. `CLAUDE.md` invariant 5.

    A row booked as PURCHASE because nothing else matched is a wrong number
    that reconciles — the only kind this system cannot detect.
    """
    with pytest.raises(UnmappedTransactionType):
        map_txn_type("Consolidation Adjustment Entry", Decimal(0))

    report = import_cas(USER, load_cas_fixture(), _resolve)
    assert report.unmatched == 1
    assert [reason for _, reason in report.quarantined] == ["UNMAPPED_TYPE"]
    assert report.status == "partial"
    assert not any(t.txn_date == date(2024, 9, 5) for t in report.txns)


# --- §5.5 type mapping ------------------------------------------------------


def test_a_rejected_sip_is_a_reversal_not_an_instalment() -> None:
    """§5.5's ordering books this as a purchase. DECISIONS V0-15.

    Its list places `purchase` second-to-last and `reversal` immediately above
    it — but `Rejected - SIP Purchase` matches the SIP pattern first, three
    rules earlier, and a bounced instalment enters the ledger as a real one.
    The units cancel out at reconciliation; the cost basis and the XIRR
    cashflow do not.
    """
    assert map_txn_type("Rejected - SIP Purchase", Decimal("-5.69")) == "REVERSAL"
    assert map_txn_type("Systematic Investment Purchase", Decimal("5.69")) == "SIP"


@pytest.mark.parametrize(
    ("desc", "units", "expected"),
    [
        ("Switch Out - STP to Fund B", Decimal("-10"), "STP_OUT"),
        ("Systematic Transfer In", Decimal("10"), "STP_IN"),
        ("SWP Payment", Decimal("-10"), "SWP"),
        ("Dividend Reinvestment", Decimal("2"), "IDCW_REINVEST"),
        ("Dividend Payout", None, "IDCW_PAYOUT"),
        ("Redemption", Decimal("-10"), "REDEMPTION"),
        ("Purchase", Decimal("10"), "PURCHASE"),
    ],
)
def test_the_first_matching_pattern_wins(
    desc: str, units: Decimal | None, expected: str
) -> None:
    """§5.5: order is load-bearing, and several patterns are substrings.

    `Switch Out - STP` contains `switch out`; `Dividend Reinvestment` contains
    `dividend`. Each of these would map to the wrong type under any other
    ordering, so the table's order is behaviour and is tested as such.
    """
    assert map_txn_type(desc, units) == expected


def test_a_merger_is_read_from_the_sign_of_its_units() -> None:
    """§5.5 names `_disambiguate` and never defines it.

    A merger prints identically in the dying scheme and the surviving one.
    Only the units column says which side you are reading.
    """
    assert map_txn_type("Merger of Scheme XYZ", Decimal("-100")) == "MERGER_OUT"
    assert map_txn_type("Merger of Scheme XYZ", Decimal("100")) == "MERGER_IN"


def test_a_zero_unit_row_is_not_treated_as_outbound() -> None:
    """A distribution has no units. Defaulting it negative books a sale."""
    assert map_txn_type("Merger of Scheme XYZ", Decimal(0)) == "MERGER_IN"
    assert map_txn_type("Merger of Scheme XYZ", None) == "MERGER_IN"


# --- §5.6 idempotence: the V0 gate condition -------------------------------


def test_reimporting_the_same_statement_produces_zero_new_rows() -> None:
    """`PLAN.md` §7 V0. §5.6 exists for this and this alone.

    Every new CAS re-covers periods already imported, so a hash that is not
    stable turns routine use into steady duplication.
    """
    lines = load_cas_fixture()
    first = import_cas(USER, lines, _resolve)
    assert first.inserted == 6

    again = import_cas(
        USER, lines, _resolve, known_txn_ids={t.txn_id for t in first.txns}
    )
    assert again.inserted == 0
    assert again.duplicate == 6
    assert again.txns == []


def test_txn_seq_does_not_depend_on_where_the_statement_starts() -> None:
    """DECISIONS V0-15 — §5.3 and §5.6 cannot both be right.

    §5.3 counts `seq` from zero at each scheme block, so a transaction's
    sequence is its position within whatever period the statement covers. §5.6
    then hashes that number to make re-import idempotent.

    Here the second statement starts in May instead of April, so the 10-Jun
    switch-out is the block's 3rd row in one and its 1st in the other. Under
    §5.3 the two hash differently and `INSERT OR IGNORE` inserts it twice — the
    exact failure §5.6's own "you WILL re-import overlapping statements"
    warning describes.

    Sequencing per (folio, scheme, DATE) removes the dependence: a CAS period
    is bounded by whole days, so a day is either wholly in or wholly out.
    """
    full = load_cas_fixture()
    start = next(i for i, ln in enumerate(full) if ln.startswith("15-May-2024"))
    header = [ln for ln in full[:start] if not ln.startswith(("01-Apr", "20-Apr"))]
    later = header + full[start:]

    a = import_cas(USER, full, _resolve)
    b = import_cas(USER, later, _resolve)

    shared = {t.txn_date for t in a.txns} & {t.txn_date for t in b.txns}
    assert date(2024, 6, 10) in shared, "the overlap must contain the switch"

    ids_a = {t.txn_date: t.txn_id for t in a.txns if t.txn_date in shared}
    ids_b = {t.txn_date: t.txn_id for t in b.txns if t.txn_date in shared}
    assert ids_a == ids_b

    # And the operative consequence: importing the later statement on top of
    # the earlier one adds only what is genuinely new.
    incremental = import_cas(
        USER, later, _resolve, known_txn_ids={t.txn_id for t in a.txns}
    )
    assert incremental.inserted == 0
    assert incremental.duplicate == len(b.txns)


def test_same_day_rows_still_get_distinct_sequences() -> None:
    """The fix must not collapse the ordering `txn_seq` exists to provide.

    MODULE_1.md §4.3: date alone cannot order a purchase and a redemption
    booked on the same day, and the lot engine needs that order.
    """
    same_day = [
        StagedTxn(FOLIO_1, "S", HDFC_ISIN, date(2024, 6, 10), d, None, None, None,
                  None, i, "")
        for i, d in enumerate(["Purchase", "Redemption", "Purchase"])
    ]
    assert assign_sequences(same_day) == [0, 1, 2]

    other_days = [
        StagedTxn(FOLIO_1, "S", HDFC_ISIN, date(2024, 6, d), "Purchase", None, None,
                  None, None, i, "")
        for i, d in enumerate((10, 11, 12))
    ]
    assert assign_sequences(other_days) == [0, 0, 0]


# --- §5.8 and §5.9 linking --------------------------------------------------


def test_switch_legs_are_paired_on_the_figure_they_share() -> None:
    """§5.8's Rs 1.00 tolerance, against the amounts the ledger stores.

    Charges sit between the legs, so neither holds the same number:

        56,486.79 gross out
          - 564.87 exit load   -> 55,921.92 switched
          -   2.80 stamp duty  -> 55,919.12 invested

    §5.8 compares the stored 56,486.79 against the stored 55,919.12 — Rs 567.67
    apart. Both deductions are percentages of the switch, so the gap scales:
    widening the tolerance does not fix the pairing, it only moves the size at
    which it starts failing. This project's own V0.1 fixture already breaches
    it, at Rs 1.36 on a Rs 22,712 switch.

    Reconstructing the middle figure removes the terms rather than budgeting
    for them, and the two sides then tie exactly.
    """
    report = import_cas(USER, load_cas_fixture(), _resolve)
    legs = [t for t in report.txns if t.txn_type in {"SWITCH_IN", "SWITCH_OUT"}]
    assert len(legs) == 2
    assert legs[0].switch_group_id is not None
    assert legs[0].switch_group_id == legs[1].switch_group_id
    assert not any(f == "UNLINKED_SWITCH" for _, f in report.flags)

    out_leg = next(t for t in legs if t.txn_type == "SWITCH_OUT")
    in_leg = next(t for t in legs if t.txn_type == "SWITCH_IN")
    assert out_leg.amount is not None and in_leg.amount is not None

    spec_gap = abs(abs(out_leg.amount) - abs(in_leg.amount))
    assert spec_gap > SWITCH_AMOUNT_TOL, "the spec's comparison would miss this"
    assert switch_proceeds(out_leg) == switch_consideration(in_leg)


def test_the_switch_pairing_gap_scales_with_the_amount() -> None:
    """Why widening §5.8's tolerance is not the fix. DECISIONS V0-15.

    Stamp duty is 0.005% and exit load 1%, so the distance between the stored
    legs is proportional to the switch. A tolerance chosen to accommodate one
    switch is breached by a larger one.
    """
    report = import_cas(USER, load_cas_fixture(), _resolve)
    out_leg = next(t for t in report.txns if t.txn_type == "SWITCH_OUT")
    in_leg = next(t for t in report.txns if t.txn_type == "SWITCH_IN")
    assert out_leg.amount is not None and in_leg.amount is not None

    gap = abs(abs(out_leg.amount) - abs(in_leg.amount))
    as_fraction = gap / abs(out_leg.amount)
    assert as_fraction > Decimal("0.005"), "a ratio, not a fixed overrun"

    # The V0.1 golden ledger's switch is a fifth the size and still breaches:
    # 22,711.85 out against 22,710.4873 in, Rs 1.36 apart on a Rs 1.00 window.
    golden_gap = Decimal("22711.8500") - Decimal("22710.4873")
    assert golden_gap > SWITCH_AMOUNT_TOL


def test_an_unpaired_switch_leg_is_a_warning_not_an_error() -> None:
    """§5.8 is right about this: the IN leg may be outside the statement."""
    lines = [
        ln
        for ln in load_cas_fixture()
        # Drop the IN leg, and the unmapped row too — it would set `partial`
        # for its own reasons and mask what this test is asserting.
        if not ln.startswith(("10-Jun-2024   Switch In", "05-Sep-2024"))
    ]
    report = import_cas(USER, lines, _resolve)
    assert [f for _, f in report.flags] == ["UNLINKED_SWITCH"]
    assert report.status == "ok", "an unlinked leg does not block the import"


def test_a_reversal_points_at_what_it_reverses(staged: list[StagedTxn]) -> None:
    """§5.9, without the float cast.

    §5.9's SQL compares `CAST(units AS REAL)`, which is the SZ-13 trap written
    into the spec. Here the comparison is `Decimal` and exact.

    The link has to be an explicit reference rather than inferred at query
    time: repeated identical SIP amounts make amount-matching ambiguous, which
    is why `drop_reversed` excludes both sides by reference.
    """
    report = import_cas(USER, load_cas_fixture(), _resolve)
    reversal = next(t for t in report.txns if t.txn_type == "REVERSAL")
    original = next(t for t in report.txns if t.txn_type == "SIP")
    assert reversal.reverses_txn_ref == original.txn_ref

    surviving = drop_reversed(report.txns)
    assert reversal not in surviving and original not in surviving
    assert len(surviving) == len(report.txns) - 2, "both sides go, neither is netted"


def test_a_reversal_does_not_link_across_two_unresolved_schemes() -> None:
    """§5.9 joins on `scheme_id IS ?`. Every unresolved row carries NULL.

    `NULL IS NULL` is true in SQLite, so under §5.9 a reversal in one
    unresolved scheme matches a transaction in a different unresolved scheme
    whenever the folio, window and unit count line up — and units cancelling is
    exactly what makes two rows candidates in the first place.

    The consequence is silent: both rows are then excluded by `drop_reversed`,
    so a real purchase disappears from one scheme and a real reversal is
    marked handled in another. Falling back to the normalised raw name keeps
    unresolved schemes apart.
    """
    lines = [
        "Folio No: 12345678 / 90",
        "AAA0001-Alpha Fund - Growth - ISIN: INF000A01AA1",
        "01-Apr-2024   Purchase            1,000.00    10.000000   100.00   10.000000",
        "BBB0002-Beta Fund - Growth - ISIN: INF000B01BB2",
        "10-Apr-2024   Rejected - Purchase 1,000.00   (10.000000)  100.00    0.000000",
    ]
    report = import_cas(USER, lines, resolve_scheme=lambda _s: None)
    assert len(report.txns) == 2
    reversal = next(t for t in report.txns if t.txn_type == "REVERSAL")
    assert reversal.reverses_txn_ref is None, "linked into the wrong scheme"
    assert ("ORPHAN_REVERSAL") in [f for _, f in report.flags]
    assert len(drop_reversed(report.txns)) == 1, "only the reversal itself goes"


def test_the_cash_direction_convention_is_the_transaction_type() -> None:
    """`_signed_amount` in isolation, on rows that carry no charge.

    Every purchase since July 2020 attracts stamp duty, so in the fixture the
    netting step runs on all of them and would mask a broken sign convention
    by rebuilding the sign itself. Tested here where nothing can cover for it.
    """
    assert _signed_amount("SIP", Decimal("10000")) == Decimal("-10000")
    assert _signed_amount("SWITCH_IN", Decimal("10000")) == Decimal("-10000")
    assert _signed_amount("REDEMPTION", Decimal("10000")) == Decimal("10000")
    assert _signed_amount("SWITCH_OUT", Decimal("10000")) == Decimal("10000")
    assert _signed_amount("IDCW_PAYOUT", Decimal("1250")) == Decimal("1250")
    assert _signed_amount("SIP", None) is None


def test_netting_stamp_duty_does_not_overwrite_the_direction() -> None:
    """The netting step must reduce the magnitude, never decide the sign."""
    report = import_cas(USER, load_cas_fixture(), _resolve)
    for t in report.txns:
        if t.stamp_duty == 0 or t.amount is None:
            continue
        assert (t.amount < 0) == (t.txn_type in {"SIP", "PURCHASE", "SWITCH_IN"})


# --- amount reconstruction --------------------------------------------------


def test_stamp_duty_is_removed_from_a_gross_purchase_amount() -> None:
    """Which registrars print gross and which net is settled arithmetically.

    Units were allotted at the NAV for whatever money reached the scheme, so
    `units x nav` IS the net figure. Guessing instead of checking misstates the
    cost basis of every purchase by its stamp duty.
    """
    report = import_cas(USER, load_cas_fixture(), _resolve)
    sip = next(t for t in report.txns if t.txn_type == "SIP")
    assert sip.amount == Decimal("-9999.50"), "10,000.00 printed, 0.50 duty"
    assert sip.stamp_duty == Decimal("0.50")
    assert sip.units is not None and sip.nav is not None
    assert (sip.units * sip.nav).quantize(Decimal("0.01")) == -sip.amount


def test_cash_direction_comes_from_the_transaction_type() -> None:
    """CAS prints amounts unsigned; the ledger is a cashbook and needs a sign."""
    report = import_cas(USER, load_cas_fixture(), _resolve)
    by_type = {t.txn_type: t for t in report.txns}
    assert by_type["SIP"].amount is not None and by_type["SIP"].amount < 0
    assert by_type["SWITCH_IN"].amount is not None and by_type["SWITCH_IN"].amount < 0
    out = by_type["SWITCH_OUT"].amount
    assert out is not None and out > 0
    payout = by_type["IDCW_PAYOUT"].amount
    assert payout is not None and payout > 0


def test_every_parsed_number_is_a_decimal(staged: list[StagedTxn]) -> None:
    """`PLAN.md` §8.2 rule 1. A float here would contaminate every derived figure."""
    for s in staged:
        for value in (s.amount, s.units, s.nav, s.balance, *s.charges.values()):
            assert value is None or isinstance(value, Decimal)


def test_the_reported_balance_survives_into_the_ledger() -> None:
    """Reconciliation has nothing to check against without it (§11.1)."""
    report = import_cas(USER, load_cas_fixture(), _resolve)
    switch_out = next(t for t in report.txns if t.txn_type == "SWITCH_OUT")
    assert switch_out.units_balance_rep == Decimal("159.090417")


def test_an_unresolvable_scheme_is_flagged_not_dropped() -> None:
    """MODULE_1.md §4.3: ingest and quarantine, never discard.

    `scheme_raw_*` is kept verbatim so re-resolution later needs no re-import
    — which matters because the PDF password is never stored.
    """
    report = import_cas(USER, load_cas_fixture(), resolve_scheme=lambda _s: None)
    assert len(report.txns) == 6
    assert all(t.scheme_id is None for t in report.txns)
    assert all(t.scheme_raw_name for t in report.txns)
    assert len([f for _, f in report.flags if f == "UNRESOLVED_SCHEME"]) == 6


# --- end to end: a parsed statement is a ledger -----------------------------


def test_a_parsed_statement_reconciles_against_its_own_closing_balances() -> None:
    """The whole pipeline, on the only evidence a CAS carries about itself.

    A statement states both the transactions and the unit balance they should
    produce. Replaying the former and checking it against the latter is the
    strongest self-consistency check available before any external NAV series
    exists — and it exercises the parser, the type mapping, the sequence
    assignment, reversal linking and the FIFO engine as one thing.

    The unmapped row is dropped first: it is quarantined by design, so the
    ledger is genuinely short one row and would not be expected to tie.
    """
    lines = [ln for ln in load_cas_fixture() if not ln.startswith("05-Sep-2024")]
    report = import_cas(USER, lines, _resolve)
    assert report.status == "ok"

    book = build_book(report.txns)
    for folio, isin in [(FOLIO_1, HDFC_ISIN), (FOLIO_1, HDBA_ISIN)]:
        scheme_id = SchemeId(str(_resolve_isin(isin)))
        printed = report.ctx.closing_balance(folio, isin)
        opening = report.ctx.opening_balance(folio, isin) or Decimal(0)
        assert printed is not None

        computed = sum(
            (
                lot.units_remaining
                for lot in book.all_lots()
                if lot.folio == folio and lot.scheme_id == scheme_id
            ),
            Decimal(0),
        )
        # The opening balance is units this statement does not account for.
        # Adding it back is not a fudge: it is precisely the quantity §11.2
        # calls MISSING_EARLY_CAS, and the statement prints it so the two can
        # be told apart.
        assert computed + opening == printed, f"{folio}/{isin}"


def test_an_ignored_opening_balance_is_what_missing_early_cas_looks_like() -> None:
    """The 120.5 units in the fixture predate the statement period.

    Without the opening balance the ledger is short by exactly that, and the
    shortfall is indistinguishable from a parser that lost rows. This is
    DECISIONS V0-13 arriving from the other direction: V0-13 said a missing
    period presents as an unexplained balance, and here is the statement
    printing the explanation.
    """
    lines = [ln for ln in load_cas_fixture() if not ln.startswith("05-Sep-2024")]
    report = import_cas(USER, lines, _resolve)
    book = build_book(report.txns)

    scheme_id = SchemeId("AMFI:HDFC-FLEXICAP-DIR-G")
    computed = sum(
        (
            lot.units_remaining
            for lot in book.all_lots()
            if lot.folio == FOLIO_1 and lot.scheme_id == scheme_id
        ),
        Decimal(0),
    )
    printed = report.ctx.closing_balance(FOLIO_1, HDFC_ISIN)
    assert printed is not None
    assert printed - computed == Decimal("120.500000")
    assert report.ctx.opening_balance(FOLIO_1, HDFC_ISIN) == printed - computed


def _resolve_isin(isin: str) -> SchemeId | None:
    return _resolve(
        StagedTxn(FOLIO_1, "", isin, date(2024, 1, 1), "", None, None, None, None, 0, "")
    )
