---
paths:
  - "src/m1_ledger/**/*.py"
  - "tests/**/test_*ledger*.py"
  - "tests/**/test_*lot*.py"
---

# Ledger rules (M1)

Full spec: `docs/MODULE_1.md`. This file covers only what must never be violated.

- **FIFO is statutory** for Indian MF units. No LIFO, no specific-identification.
  Tie-break order: `acquisition_date`, `book_date`, `lot_id`.
- **`acquisition_date` ≠ `book_date`.** Mergers and segregations preserve the acquisition
  date while the book date moves. Collapsing them produces wrong tax.
- **A switch is a redemption plus a purchase** — taxable, and the holding-period clock
  resets on the IN leg. Two rows, linked by `switch_group_id`.
- **`IDCW_REINVEST` fires twice**: taxable slab income *and* a new lot.
- **`MERGER_IN` carries forward date and cost** (§47(xviii)). Rescale
  `grandfathered_nav` by the swap ratio — it is a per-unit figure.
- **Reconciliation checks units AND value.** Value catches Direct-vs-Regular plan
  confusion, which unit reconciliation cannot see.
- **`InsufficientUnits` raises.** It means a missing CAS period.
- Never rebuild cashflows outside M1. `returns.build_cashflows` is the one place
  they are built.
