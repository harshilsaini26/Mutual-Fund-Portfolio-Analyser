"""Staged rows -> typed transactions. MODULE_1.md §5.6 - §5.9.

This is where a parsed statement becomes ledger facts: sequence assignment,
type mapping, scheme resolution, switch and reversal linking, and the
idempotence guarantee that makes re-importing an overlapping period safe.

The idempotence guarantee is the reason this module exists rather than being
folded into the parser, and §5.6's version of it does not hold. See
`assign_sequences` and DECISIONS V0-15.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal

from src.common.decimals import MONEY_Q
from src.common.types import SchemeId, UserId
from src.m1_ledger.cas.mapping import map_txn_type
from src.m1_ledger.cas.parse import CasContext, StagedTxn, parse_cas
from src.m1_ledger.txn import CLOSING_TYPES, OPENING_TYPES, Txn, UnmappedTransactionType

#: §5.8's tolerance for pairing the two legs of a switch. Kept at the spec's
#: Rs 1.00, but applied to the figure the two legs actually share rather than
#: to the amounts as stored — see `link_switch_groups`.
SWITCH_AMOUNT_TOL = Decimal("1.00")

#: §5.9. A reversal is booked within days of the transaction it cancels.
REVERSAL_WINDOW_DAYS = 30
REVERSAL_UNITS_TOL = Decimal("0.000001")

#: How far the printed amount may sit from `units x nav` before we stop
#: believing we understood the row. Half a paisa per unit of rounding slack,
#: floored at one paisa — CAS prints money to 2dp and NAV to 4dp, so the
#: product never ties exactly.
AMOUNT_CHECK_FLOOR = Decimal("0.01")


@dataclass(frozen=True)
class ImportReport:
    """MODULE_1.md §5.7.

    `status` is `ok` only when nothing needs a human. §5.5 makes
    `rows_unmatched > 0` blocking; `unparsed_lines > 0` is blocking for the
    same reason and is not in the spec — a line the regexes never recognised is
    a transaction that silently is not in the ledger.
    """

    inserted: int
    duplicate: int
    unmatched: int
    unparsed_lines: int
    status: str
    txns: list[Txn] = field(default_factory=list)
    quarantined: list[tuple[StagedTxn, str]] = field(default_factory=list)
    flags: list[tuple[str, str]] = field(default_factory=list)
    ctx: CasContext = field(default_factory=CasContext)


def assign_sequences(staged: list[StagedTxn]) -> list[int]:
    """`txn_seq` per (folio, scheme, DATE) — not per scheme block. V0-15.

    MODULE_1.md §5.3 increments `seq` from zero at each scheme block, so a
    transaction's sequence is its ordinal position within whatever period the
    statement happens to cover. §5.6 then feeds that number into the hash that
    is supposed to make re-import idempotent.

    Those two cannot both be true. A CAS covering Jan-2024 to Jan-2026 gives a
    January-2025 redemption seq 13; a later CAS covering Jan-2025 to Jun-2026
    gives the same redemption seq 1. Different seq, different `txn_id`,
    `INSERT OR IGNORE` inserts it again — and §5.6's own warning that "you WILL
    re-import overlapping statements constantly" describes exactly the
    condition under which its scheme breaks.

    `txn_seq` has to come from something intrinsic to the transaction. Its job
    (MODULE_1.md §4.3) is only to order same-day rows for the lot engine, so
    the ordinal within a (folio, scheme, date) group is enough — and it is
    stable across statements, because a CAS period is bounded by whole days: a
    given day is either entirely inside the statement or entirely outside it.
    """
    counters: dict[tuple[str, str, str], int] = defaultdict(int)
    out: list[int] = []
    for s in staged:
        key = (s.folio, s.scheme_raw_isin, s.txn_date.isoformat())
        out.append(counters[key])
        counters[key] += 1
    return out


def _txn_ref(s: StagedTxn, seq: int) -> str:
    """A readable handle. Deterministic, so a rebuild reproduces it."""
    return f"{s.scheme_raw_isin[-6:]}-{s.txn_date:%Y%m%d}-{seq}"


def _signed_amount(txn_type: str, printed: Decimal | None) -> Decimal | None:
    """Apply the ledger's cash-direction convention to a printed amount.

    CAS prints amounts unsigned — it is a statement of activity, not a cashbook
    — so the direction comes from the transaction type, which is the only
    place it is unambiguous. Negative is cash leaving the investor.
    """
    if printed is None:
        return None
    magnitude = abs(printed)
    if txn_type in OPENING_TYPES:
        return -magnitude
    if txn_type in CLOSING_TYPES:
        return magnitude
    return magnitude  # IDCW_PAYOUT: cash in.


def _net_of_stamp_duty(
    txn_type: str, amount: Decimal | None, units: Decimal | None,
    nav: Decimal | None, stamp_duty: Decimal,
) -> tuple[Decimal | None, bool]:
    """Decide whether the printed amount is gross or net of stamp duty.

    Registrars differ, and guessing wrong misstates cost basis by the duty on
    every purchase. The statement settles it arithmetically: units were
    allotted at the NAV for whatever money actually reached the scheme, so
    `units x nav` is the net figure by construction. If the printed amount sits
    a stamp duty away from that product, it was gross.

    Returns the net amount and whether the arithmetic was checkable at all.
    """
    if amount is None or units is None or nav is None or stamp_duty == 0:
        return amount, False
    product = (abs(units) * nav).quantize(MONEY_Q)
    gross_gap = abs(abs(amount) - stamp_duty - product)
    net_gap = abs(abs(amount) - product)
    if gross_gap < net_gap:
        # Preserve the direction the caller already established. Rebuilding the
        # sign here would silently override `_signed_amount` for every row that
        # carries a stamp duty, which is every purchase since July 2020 — and
        # the two would then disagree only on rows that carry none.
        direction = -1 if amount < 0 else 1
        return (direction * (abs(amount) - stamp_duty)).quantize(MONEY_Q), True
    return amount, True


def import_cas(
    user_id: UserId,
    lines: list[str],
    resolve_scheme: Callable[[StagedTxn], SchemeId | None] | None = None,
    known_txn_ids: set[str] | None = None,
) -> ImportReport:
    """MODULE_1.md §5.7. Staged rows in, ledger facts out — still no database.

    `known_txn_ids` is what the caller has already stored. `jobs/import_cas.py`
    passes the real set straight out of Zone B, so `inserted` and `duplicate`
    describe what `INSERT OR IGNORE` will actually do rather than approximating
    it. Passing nothing treats every row as new, which is what the parser tests
    want.

    This function stays free of the database on purpose: it is the piece that
    has to be exercised against synthetic statements, because a real CAS is Zone
    B and cannot be committed. Persistence is `persist.save_txns`, one call
    away, and the job is where the two meet.
    """
    ctx = CasContext()
    staged = parse_cas(lines, ctx)
    seqs = assign_sequences(staged)
    seen = set(known_txn_ids or set())

    txns: list[Txn] = []
    quarantined: list[tuple[StagedTxn, str]] = []
    flags: list[tuple[str, str]] = []
    inserted = duplicate = unmatched = 0

    for s, seq in zip(staged, seqs, strict=True):
        try:
            txn_type = map_txn_type(s.desc_raw, s.units)
        except UnmappedTransactionType:
            unmatched += 1
            quarantined.append((s, "UNMAPPED_TYPE"))
            continue

        ref = _txn_ref(s, seq)
        stamp_duty = s.charges.get("stamp_duty", Decimal(0))
        amount = _signed_amount(txn_type, s.amount)
        amount, checked = _net_of_stamp_duty(
            txn_type, amount, s.units, s.nav, stamp_duty
        )
        # Only for rows where the amount IS the purchase or sale
        # consideration. A REVERSAL's amount is a refund of the original
        # debit, stamp duty included, so it is a rupee away from
        # `units x nav` by design rather than by error.
        if (
            not checked
            and txn_type in OPENING_TYPES | CLOSING_TYPES
            and s.units
            and s.nav
            and s.amount
        ):
            product = (abs(s.units) * s.nav).quantize(MONEY_Q)
            if abs(abs(s.amount) - product) > AMOUNT_CHECK_FLOOR:
                flags.append((ref, "AMOUNT_NAV_UNITS_DISAGREE"))

        scheme_id = resolve_scheme(s) if resolve_scheme else None
        if scheme_id is None:
            flags.append((ref, "UNRESOLVED_SCHEME"))

        t = Txn(
            txn_ref=ref,
            user_id=user_id,
            folio=s.folio,
            scheme_id=scheme_id,
            scheme_raw_name=s.scheme_raw_name,
            txn_date=s.txn_date,
            txn_seq=seq,
            txn_type=txn_type,
            units=s.units,
            nav=s.nav,
            amount=amount,
            stamp_duty=stamp_duty,
            stt=s.charges.get("stt", Decimal(0)),
            exit_load=s.charges.get("exit_load", Decimal(0)),
            switch_group_id=None,
            reverses_txn_ref=None,
            units_balance_rep=s.balance,
        )
        if t.txn_id in seen:
            duplicate += 1
            continue
        seen.add(t.txn_id)
        inserted += 1
        txns.append(t)

    txns, switch_flags = link_switch_groups(txns)
    txns, reversal_flags = link_reversals(txns)
    flags.extend(switch_flags + reversal_flags)

    txns.sort(key=lambda t: (t.txn_date, t.txn_seq, t.txn_ref))
    blocking = unmatched > 0 or len(ctx.unparsed) > 0
    return ImportReport(
        inserted=inserted,
        duplicate=duplicate,
        unmatched=unmatched,
        unparsed_lines=len(ctx.unparsed),
        status="partial" if blocking else "ok",
        txns=txns,
        quarantined=quarantined,
        flags=flags,
        ctx=ctx,
    )


def link_switch_groups(txns: list[Txn]) -> tuple[list[Txn], list[tuple[str, str]]]:
    """Pair the two legs of a switch. MODULE_1.md §5.8, with its tolerance fixed.

    CAS prints the OUT and IN legs in different scheme blocks, so they can only
    be paired by (folio, date, amount).

    §5.8 pairs the STORED amounts within Rs 1.00, and the two legs do not hold
    the same number. Charges sit between them: exit load and STT come off the
    proceeds, then stamp duty comes off what is reinvested. Only the middle
    figure is shared.

        out.amount - exit_load - stt  ==  in.amount + stamp_duty

    Both sides of that are Rs 22,711.6229 on this project's own V0.1 fixture,
    exactly; the figures §5.8 compares are Rs 22,711.85 and Rs 22,710.4873,
    Rs 1.36 apart against a Rs 1.00 tolerance. The gap is a percentage of the
    switch, not a fixed overrun — so widening the tolerance does not fix it, it
    just moves the size at which linking starts failing.

    Reconstructing the shared figure removes the terms instead of budgeting for
    them. Rs 1.00 of slack is kept for genuine rounding. DECISIONS V0-15.

    An unpaired OUT leg is flagged `UNLINKED_SWITCH` and left alone: §5.8 is
    right that this is a warning, because the IN leg may sit in a folio outside
    this statement, and the lot engine treats each leg independently regardless.
    """
    by_ref = {t.txn_ref: t for t in txns}
    outs = [t for t in txns if t.txn_type in {"SWITCH_OUT", "STP_OUT"}]
    ins = [t for t in txns if t.txn_type in {"SWITCH_IN", "STP_IN"}]
    taken: set[str] = set()
    flags: list[tuple[str, str]] = []
    group = 0

    for o in outs:
        proceeds = switch_proceeds(o)
        match = None
        for i in ins:
            consideration = switch_consideration(i)
            if i.txn_ref in taken or i.folio != o.folio or i.txn_date != o.txn_date:
                continue
            if proceeds is None or consideration is None:
                continue
            if abs(proceeds - consideration) <= SWITCH_AMOUNT_TOL:
                match = i
                break
        if match is None:
            flags.append((o.txn_ref, "UNLINKED_SWITCH"))
            continue
        group += 1
        gid = f"SW{group:03d}"
        taken.add(match.txn_ref)
        by_ref[o.txn_ref] = replace(o, switch_group_id=gid)
        by_ref[match.txn_ref] = replace(match, switch_group_id=gid)

    return [by_ref[t.txn_ref] for t in txns], flags


def _scheme_key(t: Txn) -> str:
    """What identifies the scheme for linking, resolved or not.

    §5.9 joins on `scheme_id IS ?`, which pairs every UNRESOLVED row with
    every other UNRESOLVED row because they all carry NULL. Falling back to
    the raw name keeps two unresolved schemes apart.
    """
    return str(t.scheme_id) if t.scheme_id else t.scheme_raw_name.strip().lower()


def switch_proceeds(t: Txn) -> Decimal | None:
    """What actually left the OUT leg, after the charges deducted from it.

    Exit load and STT are levied on a redemption or switch-out and reduce the
    money that carries over to the other side.
    """
    return None if t.amount is None else abs(t.amount) - t.exit_load - t.stt


def switch_consideration(t: Txn) -> Decimal | None:
    """What arrived at the IN leg, before the charge levied on it.

    Stamp duty is charged on the money being invested, so the stored amount is
    already net of it. Adding it back recovers the figure the OUT leg sent.
    """
    return None if t.amount is None else abs(t.amount) + t.stamp_duty


def link_reversals(txns: list[Txn]) -> tuple[list[Txn], list[tuple[str, str]]]:
    """Point each reversal at what it cancels. MODULE_1.md §5.9.

    The link has to be explicit rather than inferred at query time: repeated
    identical SIP amounts make amount-matching ambiguous, which is precisely
    why `drop_reversed` takes a reference rather than netting by value.

    §5.9's SQL casts the units column to REAL to compare it. That is the
    SZ-13 trap written into the spec — a `DECIMAL` column round-tripped through
    a float — and it is avoidable here because the comparison happens in
    Python, where `Decimal` arithmetic is exact.
    """
    by_ref = {t.txn_ref: t for t in txns}
    flags: list[tuple[str, str]] = []
    claimed: set[str] = set()

    for r in [t for t in txns if t.txn_type == "REVERSAL"]:
        if r.units is None:
            flags.append((r.txn_ref, "ORPHAN_REVERSAL"))
            continue
        candidates = [
            t
            for t in txns
            if t.txn_ref not in claimed
            and t.txn_type != "REVERSAL"
            and t.folio == r.folio
            and _scheme_key(t) == _scheme_key(r)
            and t.units is not None
            and 0 <= (r.txn_date - t.txn_date).days <= REVERSAL_WINDOW_DAYS
            and abs(t.units + r.units) < REVERSAL_UNITS_TOL
        ]
        if not candidates:
            flags.append((r.txn_ref, "ORPHAN_REVERSAL"))
            continue
        original = max(candidates, key=lambda t: (t.txn_date, t.txn_seq, t.txn_ref))
        claimed.add(original.txn_ref)
        by_ref[r.txn_ref] = replace(r, reverses_txn_ref=original.txn_ref)

    return [by_ref[t.txn_ref] for t in txns], flags
