# PROGRESS.md — session log

> Updated at the end of every session. Newest entry at the top.
> This file is the resumption anchor: everything needed to pick the work up
> lives here, in `DECISIONS.md`, or in the commit history. Nothing important
> should exist only in a chat transcript.

## Current state

**Slice:** V0 complete. The gate passes on a ledger parsed from a statement,
priced on AMFI's own NAVs, resolved through M0's interface.
**Repo:** local git, 17 commits, no remote, branch `main`. Tree clean.
**Gate:** ruff clean · `mypy --strict` clean (84 files) · 422 tests · verifier no drift.
**Next:** V1.1 — entity master and ISIN resolution (`BUILD_ORDER.md` R4 order).

**Run everything:**

```bash
python -m pytest -q
python -m ruff check src/ tests/ scripts/ jobs/ && python -m mypy
python -m scripts.verify_v0_ledger --check     # exits 1 on golden-file drift
```

**Load real market data** (writes to `/data`, which is gitignored in full):

```bash
MF_CONTACT_EMAIL=you@example.com python -m jobs.fetch_nav
MF_CONTACT_EMAIL=you@example.com python -m jobs.backfill_nav     --amc hdfc icici_prudential kotak_mahindra --from 2024-01-01
```

`backfill_nav` clamps `--from` to 31-Jan-2018 and discovers AMFI's AMC codes on
first run. Both jobs are safe to re-run: the archive is content-addressed and
the loads are upserts.

**Regenerate fixtures** after changing a generator. Order matters — each step
reads the previous step's output:

```bash
python -m scripts.import_nav_xlsx <xlsx>... --scheme-id <id>... -o tests/fixtures/v0_ledger/nav_series.yaml
python -m scripts.build_v0_fixture      # NAVs   -> transactions.csv + expected.yaml
python -m scripts.build_v0_cas          # rows   -> cas_statement.txt
python -m scripts.build_m0_fixture      # AMFI slice -> the fake's market_data.yaml
python -m scripts.verify_v0_ledger      # recomputes expected.yaml longhand
```

## What exists

| Area | State |
|---|---|
| Slice Zero contracts | 10 Protocols, 61 frozen dataclasses, 8 `Fake*` providers. Frozen — changes need an ADR. |
| `src/common/` | `decimals.py` (Decimal/SQLite discipline), `fixtures.py` (Decimal-safe YAML), `types.py`, `contracts/` |
| `src/m1_ledger/` | `txn.py`, `lots.py` (FIFO engine), `returns.py` (XIRR/TWRR/timing), `reconcile.py` (the V0 gate) |
| `src/m1_ledger/cas/` | `parse.py` (state machine, pure), `mapping.py`, `importer.py` (seq, linking, idempotence), `pdf.py` (the only module touching a password — **untested**, needs a real CAS) |
| `src/m0_data/` | `fetch/` (archive, rate limit, robots, conditional GET), `parse/nav/amfi.py`, `normalise/`, `resolve/isin.py`, `derive/nav_adj.py`, `load.py`, `validate/integrity.py`, `schema/apply.py`, `providers/warehouse.py` |
| `migrations/` | `001_provenance.sql` (`raw_file`, `job_run`), `002_scheme_nav.sql` (`amc`, `scheme`, `nav_daily`, `scheme_idcw`). Numbered, forward-only, never edited once applied. |
| `jobs/` | `fetch_nav.py` (daily leading edge) · `backfill_nav.py` (one-time history, per OPEN-07). Both write a `job_run` row whatever happens. |
| `config/` | `txn_types.yaml` — CAS description → type, per §5.5. `sources.yaml` — S1's verified URL and scraping limits. |
| `scripts/` | `import_nav_xlsx` · `build_v0_fixture` · `build_v0_cas` · `verify_v0_ledger`. Not part of `src/`; the verifier deliberately imports nothing from it. |
| Fixture portfolio | 3 real funds keyed on their **real ISINs** — `INF179K01UT0`, `INF109K01761`, `INF174KA1EZ1` — on NAVs confirmed against AMFI. Reaches the engine as a **CAS statement**, resolved through `MarketDataProvider`. |
| Test fixtures | `v0_ledger/` (real NAVs, golden rows, `cas_statement.txt`, `expected.yaml`) · `cas/traps.txt` (one §5.4 trap per labelled line) |
| Zone A warehouse | SQLite (V0-19), `DECIMAL_TEXT` throughout. Loaded: 53 AMCs (28 with AMFI codes), 19,598 schemes, **3.1 M NAVs** back to 31-Jan-2018 for the three reference AMCs. |
| Not built | Holdings, index data, tax engine, M2–M6 |

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

`DECISIONS.md` holds 42 entries (SZ-01…SZ-14, V0-01…V0-26, OPEN-03, OPEN-07).

**Nothing is undecided.** The four that were open closed on 2026-09-05:

| Was | Now |
|---|---|
| **V0-08** — XIRR on 365, TWRR on 365.25 | **V0-17.** Both on 365. `timing_effect` is a difference of two near-equal numbers, so the basis mismatch became the result rather than diluting into it — 1.1–1.2 bp on the fixture's schemes. Excel compatibility fixes XIRR at 365, so TWRR moved. |
| **V0-01** — `scheme_ter` base vs total DDL | **V0-18.** Deferred to V2. It blocks fee output, which is V2; choosing storage for a table nothing reads is speculative. The distinction survives in `scheme_master.yaml` and in the ADR. |
| **OPEN-07** — NAV backfill depth | **Decided.** Full history for held schemes, earliest-transaction-onward for the rest, **31-Jan-2018 required regardless** for equity grandfathering. One-time overnight job. Text was lost; restored by the user. |
| **OPEN-03** — `index_constituent` source and depth | **Decided.** Source a constituent file with history; if unobtainable, build the universe from AMFI's semi-annual market-cap list as `universe_id = 'amfi_universe'`. Before V1, not before V0.5. Text restored by the user. |

Every ADR is now self-contained — a rule added to both `DECISIONS.md` and
`CLAUDE.md` after `OPEN-03` and `OPEN-07` spent weeks as bare IDs cited from two
documents with their text missing, so the citations pointed at nothing.

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

- **Mutation-test the correctness gates.** `CLAUDE.md` scopes this to M1 ledger
  and M3 look-through — **not parsers, not view builders, not fixtures**. It has
  found a real gap in every gate it touched: the LTCG boundary, exit load in
  cashflows, folio scoping in reconciliation, and two masked fixes in V0-14.
  Each survived a fully green suite.

  *Scope correction:* V0-15 and V0-16 ran it against the CAS parser and the
  statement rendering, which the rule excludes. It found eight real defects
  there, so the tests it produced are kept — but the practice stops at the gate
  boundary from here, and the parser is not mutation-tested again.
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

### S11 · The fixture re-key — V0 complete (2026-09-05)

The golden portfolio moved off invented identifiers onto its real ISINs, which
`MODULE_0.md` §4.4 makes the `scheme_id`. **Not one number moved**: all 124
values in `expected.yaml` are identical as a multiset, three of them at
different positions only because the file sorts by `scheme_id`. The independent
verifier agrees.

The `_resolve` stub is gone. The CAS gate resolves through
`FakeMarketDataProvider` — the interface, as R1 intends — and all fourteen
transactions come back `matched_by=isin`, `confidence=high`, none unresolved.

`PLAN.md` §4.2 now holds for identity and prices, and `scheme_master.yaml` says
plainly that it still does not hold for TER, AUM, exit load or inception. That
distinction is more useful than the blanket caveat it replaced.

### S10 · V0.5 — historical NAV backfill (2026-09-05)

OPEN-07 implemented: 27 year-chunks across the three reference AMCs, ~400 MB
archived, **3,118,359 NAV rows** back to 31-Jan-2018. `--from` is clamped to
that date so the grandfathering NAV is inside the fetched range by construction
rather than by a special-case request. HDFC Flexi Cap Direct on 31-Jan-2018 is
699.5850, and `assert_m1_contract` passes for the held scheme.

Two live failures found by running it (V0-24). An unknown AMC code returns
**HTTP 200 with an HTML error page**, so "no rows parsed" would have recorded
zero NAVs and reported success. And `MODULE_0.md` §3.1 puts `source_id` into the
archive path while §4.2's own example `source_id` is `'S5:hdfc'` — a colon is
illegal in a Windows path, so the two rules cannot both be followed literally.

The bigger find was before that (V0-23): the history export carries the **same
eight columns in a different order**, with the scheme name where the daily file
puts an ISIN. Positional parsing would have read nine years of backfill quietly
wrong — the name failing ISIN validation, every row unresolved, no message
saying why. The parser now reads the header and maps columns by name.

**And the fixture is confirmed** (V0-25). All three golden NAV series were
compared against AMFI's own history: **3,638 overlapping values, zero
mismatches.** The supplied workbooks are validated against the authoritative
source, and each fund resolves to exactly one real ISIN.

### S9 · V0.4 — M0 behind the real provider (2026-09-05)

`MarketDataProvider` now serves from a SQLite warehouse loaded from AMFI's own
published file, not from YAML. The Slice Zero interface did not move, which is
R1's entire payoff collected: this was swapping an implementation behind a
frozen contract. `tests/unit/test_m0_provider.py` parametrises one test body
over the real and fake providers so the equivalence is proven, not asserted.

Verifying the source before writing code changed the design (V0-20). The live
file has **eight columns, not §2.2's six, and states plan and option** — so the
V0-05 error class is closed at source, and the file confirms V0-05's own numbers
to the paisa: HDFC Flexi Cap Direct 2271.324 against Regular 2062.377 on
04-Sep-2026.

Four things the spec does not mention, all found in real bytes: `option` is far
wider than the schema's three values; `(name, plan, option)` maps to more than
one scheme for **1,467** names, which is why resolution drops the fuzzy step
(V0-21); the ISIN columns contain the literal string `Redeemed`, which had
collapsed nine IL&FS schemes into one row until §8.3's check-digit validation
went in; and **four ISINs are published against two different funds**, splicing
one NAV history onto another — now detected, counted, and reported as `partial`.

Live run: 53 AMCs, 18,882 schemes, 14,338 NAVs, 14 warnings. Second run returned
HTTP 304 — no new bytes, no second manifest row.

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

Then **V0.5**, historical NAV backfill — **unblocked**: OPEN-07 now specifies
full history for held schemes, earliest-transaction-onward for the rest, and
31-Jan-2018 regardless, as a one-time overnight job.

`pdf.py` stays untested until a real password-protected CAS exists.
