"""The FIFO lot engine.

MODULE_1.md §7. FIFO is **statutory** for Indian mutual fund units — not a
design choice — so there is no strategy parameter here and there never will be.
What the product can offer is FIFO-aware *planning* ("these are the units that
will be sold, and here is their tax character"), which is descriptive and
belongs in a view, not here.

V0.1 scope: everything that needs no NAV. Lots, cost basis, FIFO consumption,
holding periods and realised gains are all computable from transactions alone.
Market value, unrealised P&L and TWRR need the NAV series and are not here yet.

Grandfathering (§7.5) needs the 31-Jan-2018 NAV from M0 and is stubbed with an
explicit marker rather than silently skipped — see `effective_cost`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from src.common.decimals import EPS, MONEY_Q, NAV_Q
from src.common.types import SchemeId, UserId
from src.m1_ledger.txn import (
    CLOSING_TYPES,
    OPENING_TYPES,
    ORIGIN_MAP,
    Txn,
    drop_reversed,
)

#: Equity units acquired before this date get the §112A grandfathering benefit.
GRANDFATHER_DATE = date(2018, 1, 31)

#: Long-term threshold for equity-oriented schemes, in days. Strictly greater
#: than 365 is long-term; exactly 365 is short-term.
LTCG_DAYS_EQUITY = 365


class InsufficientUnits(RuntimeError):
    """MODULE_1.md §7.4. NEVER clamp.

    A redemption for more units than the book holds means the transaction
    history has a gap — almost always a missing CAS period. Clamping hides the
    gap and produces a plausible-looking, wrong ledger. The correct response is
    to surface which folio and scheme, and ask for the missing statement.
    """

    def __init__(self, txn: Txn, shortfall: Decimal) -> None:
        self.txn = txn
        self.shortfall = shortfall
        super().__init__(
            f"{txn.txn_type} of {txn.units} units in {txn.scheme_id} "
            f"(folio {txn.folio}, {txn.txn_date}) exceeds units held by "
            f"{shortfall}. This usually means a missing CAS period."
        )


class InvalidOpeningTransaction(ValueError):
    """An opening transaction with no positive unit count."""


@dataclass
class Lot:
    """One acquisition. MODULE_1.md §4.4.

    `acquisition_date` is the TAX CLOCK and `book_date` is the actual credit
    date. They diverge on mergers and segregations, and collapsing them is the
    single most common source of wrong tax output — a merger-carried lot keeps
    its original acquisition date while its book date moves to the merger.
    """

    lot_id: str
    golden_ref: str  # stable handle for the golden fixture; not persisted
    user_id: UserId
    folio: str
    scheme_id: SchemeId
    open_txn_id: str
    open_txn_ref: str
    acquisition_date: date
    book_date: date
    units_original: Decimal
    units_remaining: Decimal
    cost_per_unit: Decimal
    cost_total: Decimal
    origin: str
    grandfathered_nav: Decimal | None = None
    is_closed: bool = False


@dataclass(frozen=True)
class Consumption:
    """One lot's contribution to one closing transaction. MODULE_1.md §4.4.

    `gain_type` is a property of the LOT, not of the sale: a single redemption
    spanning the long-term boundary produces both LTCG and STCG rows. An engine
    that classifies per-transaction gets this wrong.

    `tax_class_at_sale` is persisted rather than recomputed. Once a gain is
    realised its tax character is a historical fact — this is the difference
    between a tax report you can defend and one that silently changes when the
    rules table is updated.
    """

    lot_id: str
    lot_golden_ref: str
    close_txn_id: str
    close_txn_ref: str
    acquisition_date: date
    units_consumed: Decimal
    sale_nav: Decimal | None
    proceeds_gross: Decimal
    proceeds_net: Decimal
    cost_allocated: Decimal
    cost_basis_method: str  # actual|grandfathered
    holding_days: int
    gain_type: str  # STCG|LTCG
    gain_amount: Decimal
    tax_class_at_sale: str
    sequence_in_txn: int
    confidence: str = "high"


def gain_type_for(holding_days: int, tax_class: str) -> str:
    """Long-term only when strictly beyond the threshold.

    Exactly 365 days is short-term; 366 is long-term. Off-by-one here moves
    real money, so the boundary is stated once and tested directly.
    """
    if tax_class == "equity":
        return "LTCG" if holding_days > LTCG_DAYS_EQUITY else "STCG"
    # Non-equity thresholds are versioned in config/tax_rules.yaml and are not
    # resolved in V0.1. CLAUDE.md invariant 8: never invent a tax rate — and
    # that extends to the holding-period threshold that selects one.
    raise NotImplementedError(
        f"holding-period threshold for tax_class={tax_class!r} is not resolved "
        "in V0.1; it belongs in config/tax_rules.yaml, human-verified"
    )


def effective_cost(
    lot: Lot, units: Decimal, sale_nav: Decimal | None, tax_class: str
) -> tuple[Decimal, str]:
    """Cost allocated to a consumption. MODULE_1.md §7.5.

    Grandfathering applies to equity units acquired before 31-Jan-2018:

        effective_cost_per_unit = max(actual, min(FMV_31Jan2018, sale_price))

    It needs the 31-Jan-2018 NAV from M0. When the lot predates the cutoff and
    that NAV is absent, the caller marks the consumption `confidence=low`
    rather than computing a wrong number.
    """
    actual = (lot.cost_per_unit * units).quantize(MONEY_Q)

    if (
        lot.acquisition_date >= GRANDFATHER_DATE
        or tax_class != "equity"
        or lot.grandfathered_nav is None
        or sale_nav is None
    ):
        return actual, "actual"

    gf = min(lot.grandfathered_nav, sale_nav)
    grandfathered = (max(lot.cost_per_unit, gf) * units).quantize(MONEY_Q)
    return (
        (grandfathered, "grandfathered") if grandfathered > actual else (actual, "actual")
    )


@dataclass
class LotBook:
    """Open lots and realised consumptions for one user.

    Scoped by (folio, scheme_id): FIFO runs per book, never across schemes and
    never across folios. `PLAN.md` §9.7 aggregates folios for display, but lots
    and tax stay folio-scoped.
    """

    lots: list[Lot] = field(default_factory=list)
    consumptions: list[Consumption] = field(default_factory=list)

    # --- FIFO ---------------------------------------------------------------

    def open_fifo(self, folio: str, scheme_id: SchemeId) -> list[Lot]:
        """MODULE_1.md §7.3: acquisition_date, then book_date, then lot_id.

        Merger-carried lots hold an old acquisition date with a recent book
        date and must still be consumed in true acquisition order. `lot_id` is
        the tie-breaker that makes the ordering deterministic — without it two
        lots sharing both dates could be consumed in arbitrary order and the
        rebuild would not be reproducible.
        """
        return sorted(
            (
                lot
                for lot in self.lots
                if lot.folio == folio
                and lot.scheme_id == scheme_id
                and not lot.is_closed
                and lot.units_remaining > EPS
            ),
            key=lambda lot: (lot.acquisition_date, lot.book_date, lot.lot_id),
        )

    # --- queries ------------------------------------------------------------

    def all_lots(self) -> list[Lot]:
        return list(self.lots)

    def all_consumptions(self) -> list[Consumption]:
        return list(self.consumptions)

    def consumptions_for(self, txn_ref: str) -> list[Consumption]:
        return [
            c
            for c in sorted(self.consumptions, key=lambda x: x.sequence_in_txn)
            if c.close_txn_ref == txn_ref
        ]

    def closing_txn_refs(self) -> list[str]:
        return sorted({c.close_txn_ref for c in self.consumptions})

    def total_units_remaining(self) -> Decimal:
        return sum((lot.units_remaining for lot in self.lots), Decimal(0))

    def units_remaining(self, scheme_id: str) -> Decimal:
        return sum(
            (lot.units_remaining for lot in self.lots if lot.scheme_id == scheme_id),
            Decimal(0),
        )

    @staticmethod
    def signed_txn_units(txns: list[Txn]) -> Decimal:
        """Signed units across every transaction that reaches the engine.

        The reversed pair is excluded first — PLAN.md §8.3 invariant 1 holds
        over what the engine actually saw, not over the raw CSV.
        """
        total = Decimal(0)
        for t in drop_reversed(txns):
            if t.units is None:
                continue
            if t.txn_type in OPENING_TYPES:
                total += abs(t.units)
            elif t.txn_type in CLOSING_TYPES:
                total -= abs(t.units)
        return total

    def fingerprint(self) -> str:
        """Stable hash of the derived state.

        PLAN.md §8.3 invariant 5 and CLAUDE.md invariant 10: a full rebuild must
        reproduce byte-identical derived tables, which is what makes every
        derived table droppable.
        """
        h = hashlib.sha256()
        for lot in sorted(self.lots, key=lambda x: x.lot_id):
            h.update(
                f"{lot.lot_id}|{lot.acquisition_date}|{lot.book_date}|"
                f"{lot.units_original}|{lot.units_remaining}|{lot.cost_total}|"
                f"{lot.cost_per_unit}|{lot.origin}|{lot.is_closed}\n".encode()
            )
        for c in sorted(
            self.consumptions, key=lambda x: (x.close_txn_id, x.sequence_in_txn)
        ):
            h.update(
                f"{c.lot_id}|{c.close_txn_id}|{c.units_consumed}|{c.cost_allocated}|"
                f"{c.proceeds_net}|{c.holding_days}|{c.gain_type}|{c.gain_amount}|"
                f"{c.cost_basis_method}|{c.tax_class_at_sale}\n".encode()
            )
        return h.hexdigest()


def _make_lot_id(t: Txn) -> str:
    """Derived from the transaction id, so a rebuild reproduces it exactly."""
    return hashlib.sha256(f"lot|{t.txn_id}".encode()).hexdigest()[:32]


def apply_transaction(
    t: Txn,
    book: LotBook,
    tax_class: str,
    grandfathered_nav: Decimal | None = None,
) -> list[Consumption]:
    """Apply one transaction to the book. MODULE_1.md §7.2.

    `tax_class` is passed in rather than looked up: it is point-in-time from M0
    and must be resolved at the *transaction* date, not at report time.
    """
    # ---------- OPENING ----------
    if t.txn_type in OPENING_TYPES:
        if t.units is None or t.units <= 0:
            raise InvalidOpeningTransaction(f"{t.txn_ref}: units={t.units}")
        if t.scheme_id is None:
            raise InvalidOpeningTransaction(f"{t.txn_ref}: unresolved scheme")

        base = abs(t.amount) if t.amount is not None else abs(t.units * (t.nav or 0))
        # Stamp duty is part of what the units cost. Omitting it understates
        # cost and so overstates the gain.
        cost_total = (base + t.stamp_duty).quantize(MONEY_Q)

        book.lots.append(
            Lot(
                lot_id=_make_lot_id(t),
                golden_ref="",
                user_id=t.user_id,
                folio=t.folio,
                scheme_id=t.scheme_id,
                open_txn_id=t.txn_id,
                open_txn_ref=t.txn_ref,
                acquisition_date=t.txn_date,
                book_date=t.txn_date,
                units_original=t.units,
                units_remaining=t.units,
                cost_per_unit=(cost_total / t.units).quantize(NAV_Q),
                cost_total=cost_total,
                origin=ORIGIN_MAP[t.txn_type],
                grandfathered_nav=grandfathered_nav,
            )
        )
        return []

    # ---------- non-unit income ----------
    # IDCW_PAYOUT is slab-taxable income with no unit effect and no lot.
    # IDCW_REINVEST is handled above: it is BOTH income and an opening.
    if t.txn_type == "IDCW_PAYOUT":
        return []

    # ---------- CLOSING ----------
    if t.txn_type not in CLOSING_TYPES:
        raise InvalidOpeningTransaction(f"{t.txn_ref}: unhandled {t.txn_type}")
    if t.units is None or t.scheme_id is None:
        raise InvalidOpeningTransaction(f"{t.txn_ref}: closing needs units and scheme")

    to_close = abs(t.units)
    gross = abs(t.amount or Decimal(0))
    # What the investor actually receives, after the AMC's exit load and STT.
    net_total = gross - t.exit_load - t.stt
    net_per_unit = (net_total / to_close).quantize(NAV_Q)
    gross_per_unit = (gross / to_close).quantize(NAV_Q)

    out: list[Consumption] = []
    seq = 0
    for lot in book.open_fifo(t.folio, t.scheme_id):
        if to_close <= EPS:
            break
        take = min(lot.units_remaining, to_close)
        days = (t.txn_date - lot.acquisition_date).days
        cost, method = effective_cost(lot, take, t.nav, tax_class)
        proceeds_net = (take * net_per_unit).quantize(MONEY_Q)

        confidence = "high"
        if lot.acquisition_date < GRANDFATHER_DATE and lot.grandfathered_nav is None:
            # §7.5: flag rather than compute a wrong number.
            confidence = "low"

        out.append(
            Consumption(
                lot_id=lot.lot_id,
                lot_golden_ref=lot.golden_ref,
                close_txn_id=t.txn_id,
                close_txn_ref=t.txn_ref,
                acquisition_date=lot.acquisition_date,
                units_consumed=take,
                sale_nav=t.nav,
                proceeds_gross=(take * gross_per_unit).quantize(MONEY_Q),
                proceeds_net=proceeds_net,
                cost_allocated=cost,
                cost_basis_method=method,
                holding_days=days,
                gain_type=gain_type_for(days, tax_class),
                gain_amount=(proceeds_net - cost).quantize(MONEY_Q),
                tax_class_at_sale=tax_class,
                sequence_in_txn=seq,
                confidence=confidence,
            )
        )
        lot.units_remaining -= take
        lot.is_closed = lot.units_remaining <= EPS
        to_close -= take
        seq += 1

    if to_close > EPS:
        raise InsufficientUnits(t, shortfall=to_close)

    book.consumptions.extend(out)
    return out


def build_book(
    txns: list[Txn],
    tax_classes: dict[str, str] | None = None,
    default_tax_class: str = "equity",
) -> LotBook:
    """Replay every transaction in order and return the derived book.

    Reversals and the transactions they reverse are dropped first, before the
    engine sees anything — MODULE_1.md §3.2.
    """
    book = LotBook()
    live = drop_reversed(txns)

    for t in sorted(live, key=lambda x: (x.txn_date, x.txn_seq, x.txn_ref)):
        tax_class = (tax_classes or {}).get(str(t.scheme_id), default_tax_class)
        apply_transaction(t, book, tax_class=tax_class)

    _assign_golden_refs(book)
    return book


def _assign_golden_refs(book: LotBook) -> None:
    """Label lots L1..Ln in acquisition order, for the golden fixture only.

    Keeps `expected.yaml` readable without persisting a second identifier or
    letting test ergonomics leak into the production `lot_id`.
    """
    ordered = sorted(book.lots, key=lambda x: (x.acquisition_date, x.book_date, x.lot_id))
    labels = {lot.lot_id: f"L{i}" for i, lot in enumerate(ordered, start=1)}
    for lot in book.lots:
        lot.golden_ref = labels[lot.lot_id]
    book.consumptions[:] = [
        Consumption(**{**c.__dict__, "lot_golden_ref": labels[c.lot_id]})
        for c in book.consumptions
    ]
