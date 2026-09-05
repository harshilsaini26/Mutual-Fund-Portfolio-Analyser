# PROGRESS.md — session log

> Updated at the end of every session. Newest entry at the top.
> This file is the resumption anchor: everything needed to pick the work up
> lives here, in `DECISIONS.md`, or in the commit history. Nothing important
> should exist only in a chat transcript.

## Current state

**Slice:** V0.1 — ledger on fixtures (`BUILD_ORDER.md` R2). Substantially complete.
**Repo:** local git, 11 commits, no remote, branch `main`. Tree clean.
**Gate:** ruff clean · `mypy --strict` clean (51 files) · 284 tests · verifier no drift.

**Run everything:**

```bash
python -m pytest -q
python -m ruff check src/ tests/ scripts/ && python -m mypy
python -m scripts.verify_v0_ledger --check     # exits 1 on golden-file drift
```

**Regenerate fixtures** after changing the generator, in this order:

```bash
python -m scripts.import_nav_xlsx <xlsx>... --scheme-id <id>... -o tests/fixtures/v0_ledger/nav_series.yaml
python -m scripts.build_v0_fixture
python -m scripts.verify_v0_ledger
```

## What exists

| Area | State |
|---|---|
| Slice Zero contracts | 10 Protocols, 61 frozen dataclasses, 8 `Fake*` providers. Frozen — changes need an ADR. |
| `src/common/` | `decimals.py` (Decimal/SQLite discipline), `fixtures.py` (Decimal-safe YAML), `types.py`, `contracts/` |
| `src/m1_ledger/` | `txn.py`, `lots.py` (FIFO engine), `returns.py` (XIRR/TWRR/timing), `reconcile.py` (the V0 gate) |
| Fixture portfolio | 3 real funds on real AMFI NAV: HDFC Flexi Cap **Direct**, ICICI Multi Asset **Regular**, Kotak Pioneer **Direct** |
| Not built | M0 ingestion, CAS parser, tax engine, M2–M6 |

## V0 acceptance gate (`PLAN.md` §7) — honest status

- [x] **Every folio reconciles**, ≤0.001 units. All three at exactly 0.000000.
- [x] **Value reconciles within 0.5%**, and now means something — V0-12 resolved,
      `nav_cross_check()` is a gate condition, so a wrong NAV series fails.
- [x] **All 5 property invariants pass.** Two now at exact equality, not tolerance.
- [~] **XIRR matches an independent calculation to 4 dp.** Verified three ways
      (closed-form cases, NPV≈0 on real data, Excel's 365-day convention). The
      spreadsheet comparison itself is a human step.
- [~] **Re-import produces zero new rows.** `txn_id` is a deterministic hash and
      is tested stable across reloads; the end-to-end claim needs the CAS parser.

## Open decisions

See `DECISIONS.md` (30 entries). Still open:

- **V0-01** — `scheme_ter` needs both base and total TER. Needs a DDL choice.
  Blocks any fee output.
- **V0-08** — XIRR discounts on 365, TWRR annualises on 365.25, and
  `timing_effect` subtracts one from the other. 0.068% apart; it matters because
  it is a difference of two near-equal numbers.
- **OPEN-07** — NAV backfill depth. Before M0 step 4. Original text lost.
- **OPEN-03** — `index_constituent` scheduling. Before V1 per R4. Text lost.

**Deferred by agreement:** ABSL Large & Mid has no NAV series; HDFC's
Direct-plan TER is unsourced (V0-05 nulled it). Neither blocks progress.

## Practices that have earned their place

- **Mutation-test every new module.** It has found a real gap in all four it has
  touched: the LTCG boundary, exit load in cashflows, folio scoping in
  reconciliation, and two masked fixes in V0-14. Each survived a fully green suite.
- **Vary the fixture, not just the assertions.** One NAV scale hid V0-10; one
  folio per scheme hid a netting bug; one exit-load rate hid a hardcoded
  constant. Each was found by adding a fund, not by review.
- **Compute the derived numbers and ask whether they can be true.** V0-09 — a
  Growth option "paying" IDCW — passed every test while modelling something
  structurally impossible.
- **Keep the independent verifier independent.** `scripts/verify_v0_ledger.py`
  imports nothing from `src.m1_ledger`. When rules change, both implement the new
  rules separately; that is what makes their agreement evidence.

---

## Session log

### Sessions 1–6 (2026-09-04 → 2026-09-05) — condensed

Full detail is in `DECISIONS.md` and the commit messages; this is the shape of
how the work got here.

**S1 · Slice Zero contracts.** Read `PLAN.md` / `BUILD_ORDER.md` and the
interface sections. Found **47 result types named in the specs but defined
nowhere**. Built 10 Protocols (not R1's seven) plus 61 dataclasses.
SZ-01…SZ-12; the substantive ones were the `lookthrough()` name collision
(SZ-01), the missing `max_pairwise_overlap` (SZ-02), and six methods M4's limit
engine calls but never declares (SZ-09).

**S2 · Fakes and Decimal discipline.** Eight `Fake*` providers on YAML fixtures.
**SQLite gives a `DECIMAL` column NUMERIC affinity, not TEXT**, silently
converting decimal strings to REAL — Zone B must declare `DECIMAL_TEXT`
(SZ-13). PyYAML floats fixture data by default (SZ-14).

**S3 · git init and the FIFO lot engine.** Local repo, `.gitignore`,
`.gitattributes`. Engine written test-first. Mutation testing found the **LTCG
boundary untested** — `>` versus `>=` at 365 days, worth 20% against 12.5% on
the entire gain.

**S4 · Real NAV data.** HDFC and ICICI workbooks supplied. **The HDFC file was
the Direct plan against a Regular scheme record — 10.13% apart** (V0-05), the
exact error `PLAN.md` calls canonical. Repriced the fixture onto real NAVs.
Built the returns engine; found V0-07 (`IDCW_REINVEST` booked as an unmatched
outflow) and V0-09 (a Growth option cannot pay IDCW).

**S5 · Third fund and reconciliation.** Kotak Pioneer added a third NAV scale
and a second exit-load rate, surfacing V0-10 (scale-dependent cost drift) and a
hardcoded exit-load constant. Reconciliation built; **found that §11.1's value
check is algebraically the unit delta restated and cannot detect a wrong NAV
series** — the failure it exists to catch (V0-12).

**S6 · V0-06, V0-10 and V0-12 resolved together** (V0-14). Lots carry
`cost_remaining`; the proceeds residual settles deterministically; the NAV
cross-check gates. Invariants 2 and 4 tightened to exact equality. Both
surviving mutations were fixes *masked by an accident of the fixture* rather
than genuinely exercised.

**Next:** V0.2 — the CAS parser replaces the hand-made CSV, which also makes the
re-import gate condition real. `MODULE_1.md` §5.
