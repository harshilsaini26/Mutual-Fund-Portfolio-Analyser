# PROGRESS.md — session log

> Updated at the end of every session. Newest entry at the top.
> This file is the resumption anchor: everything needed to pick the work up
> lives here, in `DECISIONS.md`, or in the commit history. Nothing important
> should exist only in a chat transcript.

## Current state

**Slice:** V0.3 complete — the V0 gate runs on a ledger that came through the parser.
**Repo:** local git, 13 commits, no remote, branch `main`. Tree clean.
**Gate:** ruff clean · `mypy --strict` clean (58 files) · 343 tests · verifier no drift.
**Next:** V0.4 — M0 behind the real provider (`BUILD_ORDER.md` R2).

**Run everything:**

```bash
python -m pytest -q
python -m ruff check src/ tests/ scripts/ && python -m mypy
python -m scripts.verify_v0_ledger --check     # exits 1 on golden-file drift
```

**Regenerate fixtures** after changing a generator. Order matters — each step
reads the previous step's output:

```bash
python -m scripts.import_nav_xlsx <xlsx>... --scheme-id <id>... -o tests/fixtures/v0_ledger/nav_series.yaml
python -m scripts.build_v0_fixture      # NAVs   -> transactions.csv + expected.yaml
python -m scripts.build_v0_cas          # rows   -> cas_statement.txt
python -m scripts.verify_v0_ledger      # recomputes expected.yaml longhand
```

## What exists

| Area | State |
|---|---|
| Slice Zero contracts | 10 Protocols, 61 frozen dataclasses, 8 `Fake*` providers. Frozen — changes need an ADR. |
| `src/common/` | `decimals.py` (Decimal/SQLite discipline), `fixtures.py` (Decimal-safe YAML), `types.py`, `contracts/` |
| `src/m1_ledger/` | `txn.py`, `lots.py` (FIFO engine), `returns.py` (XIRR/TWRR/timing), `reconcile.py` (the V0 gate) |
| `src/m1_ledger/cas/` | `parse.py` (state machine, pure), `mapping.py`, `importer.py` (seq, linking, idempotence), `pdf.py` (the only module touching a password — **untested**, needs a real CAS) |
| `config/` | `txn_types.yaml` — CAS description → type. Data, not code, per §5.5, so a new registrar wording needs no release. |
| `scripts/` | `import_nav_xlsx` · `build_v0_fixture` · `build_v0_cas` · `verify_v0_ledger`. Not part of `src/`; the verifier deliberately imports nothing from it. |
| Fixture portfolio | 3 real funds on real AMFI NAV: HDFC Flexi Cap **Direct**, ICICI Multi Asset **Regular**, Kotak Pioneer **Direct**. Reaches the engine as a **CAS statement**, not a CSV. |
| Test fixtures | `v0_ledger/` (real NAVs, golden rows, `cas_statement.txt`, `expected.yaml`) · `cas/traps.txt` (one §5.4 trap per labelled line) |
| Not built | M0 ingestion, tax engine, persistence, M2–M6 |

## V0 acceptance gate (`PLAN.md` §7) — honest status

Since V0.3 every box below is checked **against a ledger parsed from a statement**,
not against a hand-made CSV.

- [x] **Every folio reconciles**, ≤0.001 units. All three at exactly 0.000000.
- [x] **Value reconciles within 0.5%**, and now means something — V0-12 resolved,
      `nav_cross_check()` is a gate condition, so a wrong NAV series fails.
- [x] **All 5 property invariants pass**, on the CSV-derived book and on the
      CAS-derived one. Two are at exact equality, not tolerance — including the
      P&L closure, which ties exactly on charges read from the statement.
- [x] **Re-import produces zero new rows.** Real since V0.2. Proven on two
      statements with different start dates: the shared transactions hash
      identically and the overlap inserts nothing. §5.3's per-block `txn_seq`
      made this impossible (V0-15); it is now per (folio, scheme, date).
- [~] **XIRR matches an independent calculation to 4 dp.** Verified three ways
      — closed-form cases, NPV≈0 on real data, Excel's 365-day convention — and
      now also on cashflows built from the parsed ledger. **The spreadsheet
      comparison itself is a human step, and is the only gate item still open.**

## Open decisions

`DECISIONS.md` holds 30 entries (SZ-01…SZ-14, V0-01…V0-16).

**Genuinely undecided:**

- **V0-01** — `scheme_ter` needs both base and total TER. Needs a DDL choice.
  Blocks any fee output.
- **V0-08** — XIRR discounts on 365, TWRR annualises on 365.25, and
  `timing_effect` subtracts one from the other. 0.068% apart; it matters because
  it is a difference of two near-equal numbers.
- **OPEN-07** — NAV backfill depth. Needed before V0.5. Original text lost.
- **OPEN-03** — `index_constituent` scheduling. Before V1 per R4. Text lost.

**Decided, but resting on inference until real data arrives:**

- **V0-15** — whether a registrar prints a purchase amount gross or net of stamp
  duty is undocumented anywhere in the corpus. The importer settles it per row
  against `units × nav`, and V0-16 asserts both renderings read the same. Replace
  with a real CAS when one exists.

**Data requests — nothing downstream is blocked:**

- **V0-16** — the golden "switch" crosses AMCs, which cannot happen, so §5.8
  correctly refuses to link it. Fixing it needs a second scheme from **one** AMC
  with a real NAV series.
- **Deferred by agreement** — ABSL Large & Mid has no NAV series; HDFC's
  Direct-plan TER is unsourced (V0-05 nulled it).

## Practices that have earned their place

- **Mutation-test every new module.** It has found a real gap in every one it
  has touched, across six rounds: the LTCG boundary, exit load in cashflows,
  folio scoping in reconciliation, two masked fixes in V0-14, four survivors in
  V0-15 (a sign convention silently rebuilt by a later step, dead code, two
  tests passing for the wrong reason), and four in V0-16 of which three were
  real and one genuinely equivalent. Every one survived a fully green suite.
- **Mutate the fixture, not only the code.** New in V0-16. A gate that passes on
  a corrupted *statement* is not a gate — and three of the four survivors there
  were fixture and test defects, not code: a summary section with nothing
  transaction-shaped in it, a closing-balance line nothing consulted, and a
  correct behaviour that was accidental rather than asserted.
- **Vary the fixture, not just the assertions.** One NAV scale hid V0-10; one
  folio per scheme hid a netting bug; one exit-load rate hid a hardcoded
  constant. Each was found by adding a fund, not by review.
- **Compute the derived numbers and ask whether they can be true.** Twice now a
  hand-typed field has encoded something impossible and passed every test:
  V0-09, a Growth option "paying" IDCW; V0-16, a switch between two fund houses.
  Both were caught by making the fixture more realistic, not by reading it.
- **Keep the independent verifier independent.** `scripts/verify_v0_ledger.py`
  imports nothing from `src.m1_ledger`. When rules change, both implement the new
  rules separately; that is what makes their agreement evidence.
- **Say what a check does not prove.** The CAS round trip re-reads rows
  `build_v0_fixture.py` computed, so it tests the parser and not the arithmetic.
  Recording that in the test docstring is what stops it being cited later as
  evidence it never was.

---

## Session log

Newest first. Full detail is in `DECISIONS.md` and the commit messages; this is
the shape of how the work got here.

### S8 · V0.3 — the gate on parsed data (2026-09-05)

The golden portfolio now reaches the engine as a statement
(`scripts/build_v0_cas.py`), not a CSV, and the full `PLAN.md` §7 gate runs on
the far side of the parser: 14 transactions, 3 folios, `status == ok`, every
folio reconciling at exactly 0.000000, NAV cross-check clean, XIRR closing to
zero NPV, re-import inserting nothing, second parse byte-identical.

All five `PLAN.md` §8.3 invariants are asserted on the parsed book too, P&L
closure included — it ties exactly, because both sides draw on the statement's
own figures.

The round trip loses **exactly seven figures** — every charge the fixture
computes from a rate, because no statement prints 4dp. The set is declared with
individual bounds and asserted exact, so a new difference fails rather than
disappearing into a blanket tolerance.

It also exposed a fixture defect the CSV could never show: **the golden "switch"
crosses AMCs**, which cannot happen, so §5.8 correctly refuses to link it
(V0-16). Mutation testing ran against the statement *rendering* as well as the
parser; three of the four survivors were fixture and test defects rather than
code.

### S7 · V0.2 — the CAS parser (2026-09-05)

§5 is the only section of the corpus shipping working code, and implementing it
found **seven defects** (V0-15). Worst first: §5.3's per-block `txn_seq`
contradicts §5.6's idempotence hash, so every overlapping re-import duplicates
the overlap; §5.3 drops unrecognised lines in silence, so a lost transaction
reports nothing; §5.4 mandates zero-unit IDCW rows that §5.3's six-column regex
cannot match; and **§5.3's ISIN pattern demands thirteen characters where an
ISIN has twelve**, so `SCHEME_RE` never fires and the parser returns an empty
list from a good statement.

Parser split three ways — `pdf.py` (password, untestable), `parse.py` (pure,
fully covered), `importer.py`. Tested against a synthetic CAS carrying one §5.4
trap per labelled line, since a real one is Zone B. End to end, the parsed
statement replays through the FIFO engine and ties to its own printed closing
balances exactly.

### S6 · V0-06, V0-10 and V0-12 resolved together (2026-09-05)

V0-14. Lots carry `cost_remaining`; the proceeds residual settles
deterministically; the NAV cross-check gates. Invariants 2 and 4 tightened to
exact equality. Both surviving mutations were fixes *masked by an accident of
the fixture* rather than genuinely exercised.

### S5 · Third fund and reconciliation (2026-09-05)

Kotak Pioneer added a third NAV scale and a second exit-load rate, surfacing
V0-10 (scale-dependent cost drift) and a hardcoded exit-load constant.
Reconciliation built; **found that §11.1's value check is algebraically the unit
delta restated and cannot detect a wrong NAV series** — the failure it exists to
catch (V0-12).

### S4 · Real NAV data (2026-09-05)

HDFC and ICICI workbooks supplied. **The HDFC file was the Direct plan against a
Regular scheme record — 10.13% apart** (V0-05), the exact error `PLAN.md` calls
canonical. Repriced the fixture onto real NAVs. Built the returns engine; found
V0-07 (`IDCW_REINVEST` booked as an unmatched outflow) and V0-09 (a Growth
option cannot pay IDCW).

### S3 · git init and the FIFO lot engine (2026-09-04)

Local repo, `.gitignore`, `.gitattributes`. Engine written test-first. Mutation
testing found the **LTCG boundary untested** — `>` versus `>=` at 365 days,
worth 20% against 12.5% on the entire gain.

### S2 · Fakes and Decimal discipline (2026-09-04)

Eight `Fake*` providers on YAML fixtures. **SQLite gives a `DECIMAL` column
NUMERIC affinity, not TEXT**, silently converting decimal strings to REAL — Zone
B must declare `DECIMAL_TEXT` (SZ-13). PyYAML floats fixture data by default
(SZ-14).

### S1 · Slice Zero contracts (2026-09-04)

Read `PLAN.md` / `BUILD_ORDER.md` and the interface sections. Found **47 result
types named in the specs but defined nowhere**. Built 10 Protocols (not R1's
seven) plus 61 dataclasses. SZ-01…SZ-12; the substantive ones were the
`lookthrough()` name collision (SZ-01), the missing `max_pairwise_overlap`
(SZ-02), and six methods M4's limit engine calls but never declares (SZ-09).

---

## Next

**V0.4** — `BUILD_ORDER.md` R2: *"M0 archive + fetch + NAV + scheme master,
behind the real provider."* The fake is swapped for a real implementation behind
an interface frozen in Slice Zero, which is the whole payoff of R1 and R2. It
also retires the `_resolve` stub the CAS tests currently pass in, and is what
`PLAN.md` §4.2 needs before any number traces to an archived source file.

Then **V0.5**, historical NAV backfill — blocked on OPEN-07 (backfill depth).

`pdf.py` stays untested until a real password-protected CAS exists.
