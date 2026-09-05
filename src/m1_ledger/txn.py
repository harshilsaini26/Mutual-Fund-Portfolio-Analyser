"""Transactions — the immutable facts the whole ledger is derived from.

MODULE_1.md §3 (taxonomy) and §4.3 (schema). Everything downstream — lots, cost
basis, gains, XIRR — is a function of these rows, so they are append-only and
never edited in place.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.common.decimals import MONEY_Q, UNITS_Q, dec
from src.common.types import SchemeId, UserId

# --- MODULE_1.md §3.3 type sets --------------------------------------------

OPENING_TYPES = frozenset(
    {"PURCHASE", "SIP", "SWITCH_IN", "STP_IN", "IDCW_REINVEST", "MERGER_IN", "UNIT_SPLIT"}
)
CLOSING_TYPES = frozenset({"REDEMPTION", "SWITCH_OUT", "STP_OUT", "SWP", "MERGER_OUT"})
TAXABLE_GAIN_TYPES = frozenset({"REDEMPTION", "SWITCH_OUT", "STP_OUT", "SWP"})
TAXABLE_INCOME_TYPES = frozenset({"IDCW_PAYOUT", "IDCW_REINVEST"})
NON_TRANSFER_TYPES = frozenset({"MERGER_OUT", "MERGER_IN", "SEGREGATION", "UNIT_SPLIT"})
CASH_NEUTRAL_TYPES = frozenset(
    {
        "SWITCH_IN",
        "SWITCH_OUT",
        "STP_IN",
        "STP_OUT",
        "IDCW_REINVEST",
        "MERGER_IN",
        "MERGER_OUT",
        "SEGREGATION",
        "UNIT_SPLIT",
    }
)

#: Which `lot.origin` each opening type produces.
ORIGIN_MAP = {
    "PURCHASE": "purchase",
    "SIP": "sip",
    "SWITCH_IN": "switch_in",
    "STP_IN": "stp_in",
    "IDCW_REINVEST": "idcw_reinvest",
    "MERGER_IN": "merger",
    "UNIT_SPLIT": "split",
}


class UnmappedTransactionType(ValueError):
    """CLAUDE.md invariant 5: raise, never guess.

    An unrecognised CAS description must stop the import, not be silently
    treated as a purchase.
    """


@dataclass(frozen=True)
class Txn:
    """One transaction. MODULE_1.md §4.3.

    `scheme_id` is nullable by design: an unresolvable scheme is ingested and
    quarantined rather than dropped, with `scheme_raw_*` kept verbatim so
    re-resolution is possible later without re-importing the PDF.

    `txn_seq` orders same-day rows. Date alone cannot order a purchase and a
    redemption booked on the same day, and the lot engine needs that order.
    """

    txn_ref: str  # human-readable fixture handle; not persisted
    user_id: UserId
    folio: str
    scheme_id: SchemeId | None
    scheme_raw_name: str
    txn_date: date
    txn_seq: int
    txn_type: str
    units: Decimal | None
    nav: Decimal | None
    amount: Decimal | None
    stamp_duty: Decimal
    stt: Decimal
    exit_load: Decimal
    switch_group_id: str | None
    reverses_txn_ref: str | None

    @property
    def txn_id(self) -> str:
        """Deterministic hash — MODULE_1.md §5.6.

        This is what makes re-import idempotent: `INSERT OR IGNORE` does the
        right thing with no dedup logic, which matters because every new CAS
        re-covers periods already imported.
        """
        scheme_key = self.scheme_id or self.scheme_raw_name.strip().lower()
        payload = "|".join(
            [
                str(self.user_id),
                self.folio,
                str(scheme_key),
                self.txn_date.isoformat(),
                str(self.txn_seq),
                self.txn_type,
                str(self.units.quantize(UNITS_Q)) if self.units is not None else "",
                str(self.amount.quantize(MONEY_Q)) if self.amount is not None else "",
            ]
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def _replace_units_for_test(self, units: Decimal) -> Txn:
        """Test helper for constructing an over-redemption. Not production code."""
        return replace(self, units=units)


def _money(row: dict[str, str], field_name: str) -> Decimal:
    """Optional money column, defaulting to zero.

    Module-level so it never closes over the loop variable (ruff B023).
    """
    value = dec(row.get(field_name) or "0")
    assert value is not None
    return value


def load_transactions(path: Path) -> list[Txn]:
    """Read the hand-made CSV. Lines starting with '>' are comments.

    Ordered by (txn_date, txn_seq) so the lot engine sees them in the order
    they actually occurred, not the order they were typed.
    """
    rows: list[Txn] = []
    with path.open(encoding="utf-8") as fh:
        body = [ln for ln in fh if not ln.lstrip().startswith(">")]

    for raw in csv.DictReader(body):
        txn_type = (raw["txn_type"] or "").strip().upper()
        if txn_type not in OPENING_TYPES | CLOSING_TYPES | TAXABLE_INCOME_TYPES | {
            "REVERSAL",
            "SEGREGATION",
        }:
            raise UnmappedTransactionType(f"{raw['txn_ref']}: {txn_type!r}")

        scheme_raw = (raw.get("scheme_id") or "").strip()
        rows.append(
            Txn(
                txn_ref=raw["txn_ref"].strip(),
                user_id=UserId(raw["user_id"].strip()),
                folio=raw["folio"].strip(),
                scheme_id=SchemeId(scheme_raw) if scheme_raw else None,
                scheme_raw_name=raw["scheme_raw_name"].strip(),
                txn_date=date.fromisoformat(raw["txn_date"].strip()),
                txn_seq=int(raw["txn_seq"] or 0),
                txn_type=txn_type,
                units=dec(raw["units"]) if (raw.get("units") or "").strip() else None,
                nav=dec(raw["nav"]) if (raw.get("nav") or "").strip() else None,
                amount=dec(raw["amount"]) if (raw.get("amount") or "").strip() else None,
                stamp_duty=_money(raw, "stamp_duty"),
                stt=_money(raw, "stt"),
                exit_load=_money(raw, "exit_load"),
                switch_group_id=(raw.get("switch_group_id") or "").strip() or None,
                reverses_txn_ref=(raw.get("reverses_txn_ref") or "").strip() or None,
            )
        )

    rows.sort(key=lambda t: (t.txn_date, t.txn_seq, t.txn_ref))
    return rows


def drop_reversed(txns: list[Txn]) -> list[Txn]:
    """Remove reversals and the transactions they reverse.

    MODULE_1.md §3.2: both sides are excluded, NOT netted. Netting would leave
    a phantom zero-unit lot in the FIFO queue, and inferring the pair by
    amount-matching at query time breaks on repeated identical SIP amounts —
    which is why the link is an explicit reference.
    """
    reversed_refs = {t.reverses_txn_ref for t in txns if t.reverses_txn_ref}
    return [
        t for t in txns if t.txn_type != "REVERSAL" and t.txn_ref not in reversed_refs
    ]
