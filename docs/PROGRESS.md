# PROGRESS.md — session log

> Updated at the end of every session. Newest entry at the top.
> This file is the resumption anchor: everything needed to pick the work up
> lives here, in `DECISIONS.md`, or in the commit history. Nothing important
> should exist only in a chat transcript.

## Current state

**Slice:** V1.10 — V1 is complete and the whole codebase has been reviewed.
**Repo:** local git, `origin` set to
`github.com/harshilsaini26/Mutual-Fund-Portfolio-Analyser`. **43 commits
unpushed, by decision** — the first push is being held until the project is
finished. Branch `main`, tree clean.
**Gate:** ruff clean · `mypy --strict` clean (163 files) · 867 tests + 3 skipped ·
verifier no drift · mutation **M1 26/26, M3 35/35**, 0 skipped.

### What it does

Point it at a CAS statement and a few AMC disclosure files and it tells you what
you actually own beneath your funds. `python -m jobs.serve` opens six views on
`127.0.0.1`, every one of them carrying its as-of date, staleness and coverage in
a footer that cannot be switched off.

**Two of five AMC formats have parsed a real file** — HDFC and Nippon. ICICI's
parser has only ever seen a 12-row hand-built fixture, and fails V1-08's
reconciliation guard at **+93.8%** on the first real ICICI disclosure it was
shown (V1-25). Closure holds at delta **0.00** from the disclosure through to the
pixels — the same rupees the engine asserts are the rupees the Sankey draws.

### The V1 acceptance gate, four of four

| Criterion | |
|---|---|
| `pct_normalised` sums to exactly 100 per scheme-date | ✅ V1.2 |
| Look-through total equals portfolio value | ✅ delta **0.00** |
| `unresolved_pct` < 2% per held scheme, **and displayed** | ✅ 0.2%, in every footer |
| Every chart renders its as-of date, staleness, and coverage | ✅ V1.9 |

`PLAN.md` §7's success criterion — *"it tells the user something they didn't
know"* — is on a screen: **13.32% overlap, 17 shared companies of 156, and 6.66%
of the portfolio held through more than one fund.**

**Read that as the mechanism working, not as a fact about this portfolio.** The
pair is HDFC Flexi Cap × Nippon India Growth Mid Cap, and **Nippon is not held** —
it is loaded because it was the third parser's test subject (V1-15). The rupee
figures come from `--equal`, which values every *disclosed* scheme at ₹10 L to
show the shape without a ledger, and the report labels that run `ILLUSTRATIVE —
NOT your ledger`. An earlier version of this file called it "the two reference funds",
which is the error the product itself refuses to make (V1-25).

### What is not proven

**That it is usable.** Nobody has used it. The tests assert the honesty
properties and a browser confirms it draws; whether a 40-node Sankey is legible
on a laptop, and whether the caveat strip is read or scrolled past, only use will
show.

`pdf.py` stays untested until a real password-protected CAS lands in
`tests/fixtures/local/`. And no independent source has ever checked what the
ledger *computes* — only what it reads (V1-09 proposes Kite's `/mf/holdings` as
the first outside witness).

### Carried, and honest about it

- **Two of the three reference schemes have no disclosure**, so the look-through sees
  into one of three positions and `__NO_DISCLOSURE__` carries the rest.
  `INF109K01761` ICICI Multi Asset fails the parser; `INF174KA1EZ1` Kotak Pioneer
  is bot-blocked and needs a hand-downloaded file. Only HDFC Flexi Cap is visible
  (V1-25).
- **`holding.market_value` is `NOT NULL`**, so a row the file did not price is
  stored at zero and counted into `validation_notes` as `UNPRICED` rather than
  stored as NULL. Removing the silence was possible without a migration;
  removing the ambiguity is not (V1-24).
- **`weight_basis` is always `disclosed`.** Drift-adjusted weights need
  `security_price`, which is V1 build item 5 and unbuilt, so the column exists
  and one of its two values never appears.
- **Five of `MODULE_6.md` §8.1's eleven portfolio views are absent, not
  stubbed** — treemap, sector tilt, mcap allocation, redundancy, marginal
  contribution — because each needs M2 or M5. The startup check enforces that a
  defined view has a builder, so an unbuildable one cannot ship as a broken
  screen (V1-22).
- **Five KPI tiles read `—`** because M1's returns engine and M2's fee data have
  not run. That is the design working, not a gap being hidden.
- **Drill-down is unbuilt.** Every view has its own URL and is bookmarkable
  (§12.1), but §12.2's targets all need M2 or M5, and a link into a screen that
  does not exist is worse than no link (V1-23).
- **Kotak is blocked** behind Radware bot detection, which this project will not
  solve; it needs a hand-downloaded file. **SBI is not held**, so it is industry
  coverage rather than portfolio coverage and belongs with V3's flow analytics —
  not, as an earlier version of this file had it, ahead of Kotak (V1-25).
- **The sector taxonomy** deferred in V1-03 still blocks `tilts()` and
  `sector_exposure()`, which raise and name it.
- **One unresolved disagreement**: mfapi says 2111.846 for 2026-03-12, AMFI says
  2111.779. One mismatch in 2,117 dates, and nothing establishes which is right.
- **`overlap_value_inr` equalling `duplicated_inr`** is an identity only at two
  funds. It is a real cross-check today and it expires the moment a third fund
  loads (V1-21).

### Two rules this project learned the hard way

Both are in `CLAUDE.md` now, and both cost a defect to find:

1. **Invariant 1's second clause covers any SQL arithmetic on a `DECIMAL_TEXT`
   column**, not only `SUM`/`AVG`/`TOTAL`. A bare `+` inside an `ON CONFLICT`
   coerces through a float just the same, and a grep for the aggregate names
   walks straight past it (V1-24).
2. **A mutation harness must not run in the background alongside other work on
   the same working tree.** It is the one tool here that deliberately makes the
   source wrong, and its in-flight edits are indistinguishable from corruption
   (V1-24).

### Next: nothing is forced

V1 is done, so what follows is a choice rather than a dependency. In rough order
of what the product would notice:

- **Use it.** See "What is not proven" above.
- **More funds — in this order: ICICI, then Kotak, then SBI.** The first two are
  *held*; SBI is not, so it is industry coverage for M5's flow analytics and
  `PLAN.md` §7 puts that in V3 (V1-25).
  - **ICICI first.** The real file is already on disk and the defect is in code
    that exists: V1-10's arithmetic demotion keeps four section rows as holdings
    on a multi-asset sheet, which is the +93.8%. V1-10 asked whether its rules
    were tuned to a sample of two; on a fourth file the answer is yes. Needs its
    own slice — a behaviour change to the shared reader plus a golden fixture
    rebuilt from a real file.
  - **Kotak second.** Held, and only a download blocks it. `--file --scheme`
    already works; the site is behind Radware bot detection this project will not
    solve (V1-03).
  - **SBI last**, and §6.5's member-level ZIP staging with it — that would let
    ICICI load from its 146-workbook archive by manifest rather than by hand, but
    it is convenience, not coverage.
- **V1 build item 5** — Bhavcopy into `security_price` / `security_adjustment`,
  which is what would make `weight_basis = 'drift_adj'` mean anything — and §7's
  `direct_holding`.
- **V2** (`PLAN.md` §7): M2's fund x-ray, the three-window returns, and M1's tax
  engine — which is also what fills the five tiles currently reading `—`.

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

**Load a disclosure** (`--amc` reads `config/amc_manifest.yaml`; `--file` needs
`--scheme` because a disclosure names its scheme in prose and prose is not a key):

```bash
MF_CONTACT_EMAIL=you@example.com python -m jobs.load_holdings --amc hdfc
python -m jobs.load_holdings --amc icici --file <extracted-member>.xlsx --scheme <ISIN>
```

**Import a CAS** (prompts for the statement password, then for the Zone B ledger
key; `--allow-unencrypted` was removed in V1-16 when `sqlcipher3` landed):

```bash
python -m jobs.import_cas --file statement.pdf --user USER-01
```

**Open the thing** (prompts for the ledger key; loopback only, no auth):

```bash
python -m jobs.serve
#   http://127.0.0.1:8765/            the three landing questions
#   http://127.0.0.1:8765/api/views   the same envelopes as JSON
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
| `src/m1_ledger/` | `txn.py`, `lots.py` (FIFO engine, §112A grandfathering), `returns.py` (XIRR/TWRR/timing), `reconcile.py` (the V0 gate), `db.py` (Zone B connection + schema; refuses to open unencrypted), `persist.py` (`rebuild()` — `txn` in, every derived table out), `providers/position.py` (what M6 reads instead of the `position` table) |
| `src/m3_lookthrough/` | `engine.py` (`compute_lookthrough`, closure asserted before returning), `weights.py` (`scheme_issuer_weight`), `concentration.py` (HHI, effective-N, top-N, Gini, Lorenz), `overlap.py` (pairwise, issuer-level), `duplication.py` (§9.4), `persist.py` + `persist_metrics.py` (§4.2/§4.3/§4.6 tables), `providers/sqlite.py` (the `LookThroughProvider` M4/M5/M6 read through — the one object holding a Zone A and a Zone B connection). Pure functions; only the two `persist*` modules and the provider touch a database. |
| `src/m1_ledger/cas/` | `parse.py` (state machine, pure), `mapping.py`, `importer.py` (seq, linking, idempotence), `pdf.py` (the only module touching a password — **untested**, needs a real CAS) |
| `src/m6_views/` | `envelope.py` + `builder.py` (Slice Zero, frozen), `registry.py` (catalogue + the startup consistency check), `caveats.py` (the single source of caveat text), `states.py`, `format.py` (Indian numbers), `colors.py`, `aggregate.py`, `hashing.py`, `serialise.py`, `compose.py`, `render.py`, six `builders/portfolio/`, `export/csv.py`, `api/` (FastAPI + the Jinja page router), `templates/` and `static/` (a vendored, pinned d3 — no CDN, no npm) |
| `src/m0_data/` | `fetch/` (archive, rate limit, robots, conditional GET, AMFI history), `parse/nav/amfi.py` + `parse/mcap/amfi.py`, `parse/holdings/` (shared reader + `hdfc`, `icici`, `nippon`, registry), `normalise/` (numbers, names, units, weights), `resolve/` (isin, synthetic, fuzzy, cascade, queue), `derive/nav_adj.py`, `load.py`, `validate/` (integrity, checks), `schema/apply.py`, `providers/warehouse.py` |
| `migrations/zone_b/` | `001_ledger.sql` — `app_user`, `cas_import`, `txn`, `lot`, `lot_consumption`, `position`, `reconciliation`. `002_lookthrough.sql` — `lookthrough_exposure`, `lookthrough_contribution`, `portfolio_summary`. `003_metrics.sql` — §4.3's `portfolio_concentration`, `fund_overlap`, `portfolio_duplication`. `004_views.sql` — `color_assignment`. Separate from Zone A: a different database, not a later version of the warehouse. |
| `migrations/` | `001_provenance.sql`, `002_scheme_nav.sql`, `003_entity.sql` (`issuer` + a **nine**-row synthetic seed, `instrument`, `name_alias`, `resolution_queue`, `issuer_classification`), `004_holdings.sql` (`holding`, `holding_disclosure`), `005_lookthrough.sql` (`scheme_issuer_weight`), `006_views.sql` (`view_definition`, seeded from code). Numbered, forward-only. |
| `jobs/` | `fetch_nav.py` (daily leading edge) · `backfill_nav.py` (bulk history, per AMC) · `backfill_scheme_nav.py` (per-scheme history via mfapi, V1-19) · `build_entity_master.py` (AMFI market-cap seed) · `load_holdings.py` (L0→L3 for one disclosure) · `import_cas.py` (a statement into Zone B, then a full rebuild) · `serve.py` (the views, on 127.0.0.1, prompting for the ledger key). The Zone A jobs write a `job_run` row; `import_cas` writes `cas_import`, Zone B's equivalent. |
| `config/` | `txn_types.yaml` — CAS description → type, per §5.5. `sources.yaml` — per-source URLs and scraping limits (S5 carries the browser agent HDFC's CDN requires, contact in `From:`, per V1-05). `amc_manifest.yaml` — disclosure links per AMC; discovery is still manual (V1-03). |
| `scripts/` | `import_nav_xlsx` · `build_v0_fixture` · `build_v0_cas` · `verify_v0_ledger` · `show_lookthrough` · `thin_warehouse`. Not part of `src/`; the verifier deliberately imports nothing from it. |
| Fixture portfolio | 3 real funds keyed on their **real ISINs** — `INF179K01UT0`, `INF109K01761`, `INF174KA1EZ1` — on NAVs confirmed against AMFI. Reaches the engine as a **CAS statement**, resolved through `MarketDataProvider`. |
| Test fixtures | `v0_ledger/` (real NAVs, golden rows, `cas_statement.txt`, `expected.yaml`) · `cas/traps.txt` (one §5.4 trap per labelled line) |
| Zone A warehouse | SQLite (V0-19), `DECIMAL_TEXT` throughout. **Two files on disk, and `MF_WAREHOUSE` picks one.** `canonical.db` (599 MB) holds 53 AMCs, 19,598 schemes and **3,118,359 NAVs** back to 31-Jan-2018. `thin.db` (13.7 MB) is the same warehouse with NAV history kept only for held schemes — 13,452 rows — built by `scripts/thin_warehouse.py` after V1-19 measured that 99.81% of the history served schemes nobody holds. The thin copy answers every look-through question identically and **nothing was deleted**: the fat one sits beside it. Both carry 5,427 instruments + 5,436 issuers (nine synthetic) with point-in-time market-cap buckets, and the loaded disclosures. `scheme_idcw` is **empty**: `nav_adj` is built and consumed, but no IDCW source has been ingested, so it equals `nav` everywhere. |
| Zone B ledger | SQLCipher + the `MODULE_1.md` §4 schema. **Encrypted at rest** since V1-16; `connect_ledger` refuses to open it unkeyed rather than degrading. Holds nothing real yet; the golden statement imports into it on demand. |
| Not built | Kotak (bot-blocked) and SBI parsers · §6.5 ZIP member staging · sector taxonomy (V1-03) · `security_price`/`security_adjustment` · drift-adjusted weights (§3.2, needs prices) · `direct_holding` (§7) · fund-of-funds recursion (§6) · tax engine · M6's payload cache, saved views, annotations, display preferences, PNG export and drill-down targets · five of §8.1's eleven portfolio views (each needs M2 or M5) · M2, M4, M5 |

## V0 acceptance gate (`PLAN.md` §7) — honest status

Since V0.3 every box below is checked **against a ledger parsed from a statement**,
not against a hand-made CSV.

- [x] **Every folio reconciles**, ≤0.001 units. All three at exactly 0.000000.
- [x] **Value reconciles within 0.5%**, and now means something — V0-12 resolved,
      `nav_cross_check()` is a gate condition, so a wrong NAV series fails.
- [x] **All 5 property invariants pass**, on the CSV-derived book and on the
      CAS-derived one. Two are at exact equality, not tolerance — including the
      P&L closure, which ties exactly on charges read from the statement.
      **Invariant 5 is now asserted against the database** (V1-13): import,
      rebuild, `DROP TABLE` every derived table, rebuild again, content
      identical. It had been an in-memory comparison, which proved the engine
      deterministic and nothing about storage.
- [x] **Re-import produces zero new rows.** Real since V0.2. Proven on two
      statements with different start dates: the shared transactions hash
      identically and the overlap inserts nothing. §5.3's per-block `txn_seq`
      made this impossible (V0-15); it is now per (folio, scheme, date).
- [~] **XIRR matches an independent calculation to 4 dp.** Verified three ways
      — closed-form cases, NPV≈0 on real data, Excel's 365-day convention — and
      now also on cashflows built from the parsed ledger. **The spreadsheet
      comparison itself is a human step, and is the only gate item still open.**

## V1 acceptance gate (`PLAN.md` §7) — honest status

**Four of four met**, as of V1.9. What follows separates what was measured from
what was not, because a gate reported as "passing" says nothing about which half
of it was checked and which was assumed.

- [x] **`pct_normalised` sums to exactly 100 per scheme-date.** Exactly, on all
      three formats: HDFC's real 31-Jul-2026 disclosure (83 holdings), Nippon's
      real one (104), and ICICI's fixture (5). `weight_residual` is stored
      beside it so the adjustment is visible rather than silent. Measured on
      three schemes, not on a portfolio.
- [x] **Look-through total equals portfolio value.** `compute_lookthrough`
      asserts §2.2's closure before returning, so a caller cannot receive a
      result that does not add up. Measured on two real disclosures at delta
      **0.00**, and on the golden portfolio through the CAS ledger with every
      folio reconciling at **0.000000**. V1 build items 7 and 8, done.
- [x] **`unresolved_pct` < 2% for every held scheme, and is displayed.**
      Measured at **0.38%** on HDFC's real disclosure, **0.0760%** on Nippon's
      and **0.0000%** on ICICI's, against the 2% bar. Computed, persisted on
      `holding_disclosure` and `portfolio_summary`, and **printed** by
      `scripts/show_lookthrough.py` alongside coverage and the caveats.
      Counted as met on the substance — the number reaches the user — while
      noting the display is a terminal report and not M6.
- [x] **Every chart renders its as-of date, staleness and coverage.** Six views
      render in a browser, and the provenance footer is part of `view_container`
      rather than part of each chart — §16.3's rule that no chart renders outside
      the wrapper is what makes that structural. Asserted on the **rendered
      HTML**, not on a payload: one test parses the page and requires every
      `[data-chart]` element to be a descendant of a `section.view`, another
      requires the footer to carry an as-of date, a holdings date, coverage and
      the unresolved share. A non-ok panel renders the footer too, with an em
      dash for what it cannot answer (V1-24).

      What this does **not** establish is that any of it is legible or useful.
      Nobody has used the thing.

**Coverage against V1 build item 3** — "top 5 AMCs" — is **3 of 5**: HDFC,
ICICI Prudential and Nippon India. The three disagreed on nearly everything
that matters — column order, date format, percentage scale, where a subtotal
lives, how many schemes share a file, and whether anything is printed below the
total. The third one settled V1-10's open question: the shared rules mostly
generalise, and the exception (a table below the grand total, +0.155%) was
**inside the reconciliation guard's tolerance**, which is the kind of gap only a
third real file could have exposed.

**Kotak is blocked, not pending.** Its disclosure page sits behind Radware bot
detection; solving it is out of bounds and probing for a file URL is what V1-03
was written about. It needs a file downloaded by hand. SBI is untried.

## Open decisions

`DECISIONS.md` holds **66 decisions across 67 entries** (SZ-01…SZ-14,
V0-01…V0-26, V1-01…V1-24, OPEN-03, OPEN-07). OPEN-03 has two entries: the
conditional decision and the settlement that supersedes it — append-only, so the
superseded text stays readable beside what replaced it.

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

### S23 · V1.10 — a whole-project review, and 14 fixes (2026-09-12)

Ten angles over 17,500 lines of `src/`. Fourteen findings, nine correctness, all fixed.
Details in V1-24; four things worth carrying.

**Invariant 1 was broken twice in one file, and neither was caught by the obvious grep.**
`resolve/queue.py` ordered its review queue with `ORDER BY total_mv_inr DESC` on a
`DECIMAL_TEXT` column — measured as `['9000', '5000', '2500000', '25000']`, so `LIMIT 20`
returned the twenty least significant unresolved names to a human whose attention is the
scarce resource. And its `ON CONFLICT` accumulated with a bare `+` in SQL, which a search
for `SUM`/`AVG`/`TOTAL` does not find: 0.1 accumulated eleven times stored
1.0999999999999999, and a large value lost its paise outright.

**Grandfathering was implemented, tested, and never called.** `build_book` omitted the
parameter and took its `None` default, so §112A relief never applied to any lot — while
the same lots were stamped `confidence="low"`, which made the symptom look like a data gap.
`backfill_nav` clamps `--from` to 31-Jan-2018 specifically to fetch that NAV and
`persist.py` already wrote the column. Only the argument was missing, and nothing failed.

**The mutation harness found four survivors in my own fixes** — two of them in
grandfathering, where dropping `max(actual, ...)` would let the relief be taken when it
*hurt* the taxpayer, and dropping `min(fmv, sale)` would manufacture a capital loss. M1 is
now 26/26 and M3 35/35, 0 skipped.

**And a process note.** I ran the M3 harness in the background and kept editing the same
tree. Its in-flight mutations looked exactly like corruption — two "stranded" edits, a red
baseline, two failing tests — and I spent a detour restoring files that did not need it.
Nothing was wrong; the harness restores in a `finally` and finished 35/35. **A mutation
harness must not run in the background alongside other work on the same working tree.** It
is the one tool here that deliberately makes the source wrong.

### S22 · V1.9 — the screen, and V1 closes (2026-09-12)

The last criterion. Six views render in a browser with their as-of date, staleness and
coverage beneath every one of them, and **V1's acceptance gate passes four of four**.

**Jinja and a vendored d3, not React** (V1-23) — overriding `PLAN.md` §9.10 and
`MODULE_6.md` Appendix A. Worth being precise about why, because the locked decision was
not wrong: its argument was against **Streamlit**, whose weakness §9.10 names exactly
("awkward for Sankey/RRG"). The Sankey is `d3-sankey` under any frontend — §16.2 says so
itself — so the flagship screen was never going to be React components. What the override
avoids is a second place `ViewEnvelope` is written down, a JS toolchain in a repo served to
one person, and a CDN call from a page showing that person's finances. **Given up:**
client-side interactivity beyond a link and a `<details>`. V1 asks for none, and the
envelope contract is unchanged, so the door stays open.

**§16.3's rule is enforced harder than the spec asks.** The spec lints React sources for
charts outside `ViewContainer`; here a test parses the rendered HTML and asserts every
`[data-chart]` is a descendant of a `section.view`. A source lint checks the code looks
right; this checks the page *is* right, and survives a refactor that moves the templates.

**Three defects the screen found that no unit test had.** Jinja was autoescaping the script
block, so the page shipped `&lt;script src="…d3…"&gt;` and d3 never loaded — and the test
that should have caught it matched the *substring* inside the escaped text. The holdings
table used the compact rupee form, rendering `₹1.00 L` where the user's own statement says
`1,00,000.00`, on the one screen whose job is to be checkable against that statement
(§9.1's never-mix rule, now split into `fmt_tile` and `fmt_cell`). And a two-fund overlap
matrix is one cell, which an SVG with a viewBox and no width stretched to 600px.

Rendered against the real warehouse: the Sankey draws 161 exposures with the tail folded
into `144 smaller holdings`, and every synthetic — unresolved, TREPS, margin, receivables,
government securities — pinned outside `top_n` in italic with dashed links, exactly as
Appendix A requires.

**What it does not prove: that it is usable.** Nobody has used it. The tests assert the
honesty properties and a browser confirms it draws. Whether a 40-node Sankey is legible on
a laptop, and whether the caveat strip is read or scrolled past, only use will show.

### S21 · V1.8 — M6's backend, and a spec that fails its own test (2026-09-12)

Six views build, serialise, export and serve. Everything except the browser, and
deliberately so: this is the half that carries the honesty commitments, and it is the half
that can be tested without one. Details in V1-22; four things worth carrying forward.

**§9.1's `group_indian` and §19.4's test table disagree on the same input.** Running the
spec's own function on the spec's own test case gives `12,34,56,78,901.00` where the table
expects `1,23,45,67,89,01.00`. The code is right — Indian grouping is
last-three-then-pairs, so a grouped number always ends in a three-digit block — and the
tests follow the code. That property is asserted directly rather than row by row, because
it is the one the table violates. Second spec defect this project has found by
implementing rather than reading, after §5.2's contradictory tolerances (V1-20).

**Five of §8.1's eleven portfolio views are absent, not stubbed.** The treemap, sector
tilt, mcap allocation, redundancy and marginal contribution all need M2 or M5. §5.3's
startup check asserts the registry and the catalogue match exactly in both directions, so
an unbuildable view is an honest gap the check enforces rather than a screen that ships
broken. Same reasoning one level down: the provider's unbuilt methods raise and name the
missing module instead of returning `[]`, which reads as "measured, and there is nothing".

**The async-route reasoning was wrong and the first API test caught it.** The claim was
that `async def` routes run on the event loop's single thread so sqlite3's same-thread
check would be satisfied. It is not — the loop runs in whatever thread the server started
it in. It surfaced as a `ProgrammingError` swallowed into a well-formed `error` envelope,
which is precisely how a wrong assumption hides when the error path works. `connect` and
`connect_ledger` now take an explicit `check_same_thread`, defaulting to `True`; the API is
the only caller that turns it off and it puts the guarantee back with a lock.

**§19.3's static check did its job before it was written.** `concentration_curve` needs a
Lorenz curve, and each point is a cumulative share of a cumulative share — two divisions on
provider-sourced values, which §2.1 forbids in a view. `lorenz_points` went into M3
instead, test-first, with V1-21's signed-pool rule. The check that forbids division in
`builders/` is what kept it out.

Verified against the real warehouse through a live uvicorn, not just `TestClient`:
**closure delta 0.00 on the payload a chart would draw**, every synthetic pinned and
visible, overlap 13.316447% and duplication 6.658224% unchanged through the whole stack,
and five KPI tiles honestly `None` because M1's returns engine has not run.

**The V1 gate did not move, and this slice must not be reported as closing it.** Every
assertion here is on a Python object or a JSON body. "Every chart renders its as-of date,
staleness, and coverage" needs a chart.

### S20 · V1.6 the review fixes, V1.7 the M3 boundary (2026-09-12)

Two slices. The first fixed all 13 findings from a review of `10a0102..HEAD`; the second
finished M3 so M6 has something legitimate to read.

**V1.6 — the 13 findings** (V1-20). Five were correctness, four reproduced before being
reported. The one worth remembering is not a code defect at all: **§5.2's two tolerances
contradict each other.** Closure is ±₹1 absolute and the weight check is ±0.01 percentage
points, so above ₹10,000 of position value the weight guard accepts data the closure guard
then refuses. Measured — 99.995% on a ₹1 crore position raised `ClosureViolation` for a
discrepancy the line above had explicitly permitted. Fixed by making the engine accumulate
the rupee drift it actually tolerated and pass it to `assert_closure`, so closure is exact
to ₹1 *beyond* the slack already granted and unchanged for correct data.

Also in V1.6: an issuer held **only** short crashed the weight materialisation (a
`defaultdict(Decimal)` that a negative weight could never beat); a restatement left behind
the issuers it dropped, so a scheme's weights summed to 120; `show_lookthrough` never
re-materialised at all, so every report after a restatement used the withdrawn revision
silently; unknown staleness scored `high`; and §5.5's "on or before" bound was
unimplemented, so a look-through could use holdings from after its own date and store a
**negative** staleness. `overlap.py` — 106 tested lines nothing called — was wired into the
report, and immediately paid: HDFC Flexi Cap × Nippon Growth Mid Cap **overlap 13.32%, 17
shared issuers of 156**.

**And the mutation harness was lying.** Hoisting a helper removed the line one mutant
targeted; the harness reported `15/15 killed` while that mutant had merely been *skipped*.
A score that counts an un-run mutant as passing is worse than no score. Skips are now
separated and named, everywhere.

**V1.7 — M3's boundary** (V1-21). `LookThroughProvider` implemented over the stored
tables, plus §4.3's three metrics persisted (`portfolio_concentration`, `fund_overlap`,
`portfolio_duplication`), plus §9.4's duplication computation, plus `overlap_value_inr`,
plus the signed-pool Gini decision V1-20 had deferred to exactly this slice. Details in
V1-21; the two things worth carrying forward:

- **Two `Exposure` dataclasses meet in one place, by design.** The engine's and the frozen
  contract's differ, and R3 freezes the contract's because M4 depends on its shape. The
  adapter reads the stored columns directly rather than back-filling from
  `load_exposures` — a back-fill would have to invent `holdings_as_of`, which is stored
  precisely because it cannot be re-derived.
- **Duplication and `overlap_value_inr` agree to the paisa** — ₹133,164.47 — from functions
  sharing no code and reading different tables. At *n* = 2 they are the same sum
  rearranged, so the agreement is an identity rather than luck, and it expires when a third
  fund loads.

Mutation: 35 mutants, **5 survived on the first pass**, all in the new persistence module
and all genuine test gaps — the delete-then-insert rule copied from V1-18 and V1-20's
lesson and then never tested, a write-only `largest_issuer_pct` column, and `drop_metrics`
untouched by any test. Tests added; **35/35 killed, 0 skipped**. That is the gate doing the
job it exists for: the code was right and nothing held it right, which is how a lesson gets
unlearned by the next edit.

### S19 · M3 lands, Zone B is encrypted, and the warehouse loses 98% (2026-09-06)

The session that made the product's actual claim work. Four slices, and each one turned up
a storage-layer default that is wrong in a way nothing downstream can see — which is now
three of the same family after SZ-13.

**`sqlcipher3` installed, Zone B encrypted** (V1-16), `--allow-unencrypted` gone from the
command line, and the suite runs against a real encrypted ledger. Asserted on the bytes:
the file does not carry SQLite's plaintext magic, the stdlib driver cannot open it, a wrong
key fails the page HMAC. Turning it on **exposed a defect** — `sqlcipher3` is a separate
driver with its **own** adapter registry, so registering against stdlib `sqlite3` did
nothing and the entire Decimal discipline silently detached. Writes raised `InterfaceError`,
which is the lucky half; reads would have returned `str` for every money column.

**The look-through works** (V1-17). `scheme_issuer_weight` collapses instruments to issuers;
`compute_lookthrough` asserts closure before returning, so a caller cannot receive a result
that does not add up. §2.2's three failure modes each have a defence and a test:
`assert_weights_sum_to_100` wired into the engine, `__UNRESOLVED__` carried through and
caveated above 2%, and `__NO_DISCLOSURE__` taking the full value of a fund we cannot see
into — which is the real portfolio's shape, three funds held and one parsed.

**The exposure tables persist** (V1-18), and found the sibling of the aggregation trap:
`ORDER BY` on a `DECIMAL_TEXT` column sorts as **text**, so `"5000"` precedes `"25000"`
and a top-20 ordered in SQL is not the top 20. §4.2's own `ix_lte_size` index is declared on
exactly that. The test asserts both that the Python sort is right *and* that the SQL sort is
wrong, so the fixture cannot drift into a shape where the two agree and the bug hides.

**The warehouse was 44x too big** (V1-19). 3,118,359 NAV rows served 5,924 — 0.19%. The
cause was a decision that was never implementable: OPEN-07 asked for full history on held
schemes, and AMFI's export is keyed on the **AMC**, so three funds meant three fund houses.
mfapi (S6) is per-scheme. 598.6 MB → 13.7 MB, verified twice — byte-identical look-through,
and the full CAS pipeline still reconciling every folio at 0.000000. Nothing deleted.

**Mutation testing, 31 mutants across three modules.** M3 engine 16/16 after one genuine gap
— deleting `assert_closure` survived, because every fixture closes by construction and the
net never fires; a spy test now asserts the call happens. Persistence 15/15 after two gaps,
both **columns written and never read back**, which is a persistence layer's specific blind
spot: `fund_inr` stored as zero, and `weight_in_fund` divided by the portfolio rather than
the fund — the two denominators coincide for a single-fund portfolio, which is the fixture
one reaches for first.

**Two mutation survivors were my own broken mutants** and are recorded as such rather than
counted as passes. A mutation score is worthless if a failed mutant reads as a passing test.

**Also corrected, honestly:** the first draft of the SQL-coercion test asserted that
`0.1 × 10` sums wrong. It does not — SQLite uses compensated summation and returns exactly
1.0. What is always true is that the aggregate is a *float*, so type and scale are lost; the
value follows only past float64, demonstrated at 22 significant digits. And HDFC's stored
weights turned out to predate V1-06, carrying 34 significant digits where `normalise_weights`
always quantises to six — superseded code the content-addressed loader had been skipping.
Re-loaded from the archive.

**The remote is set** to `github.com/harshilsaini26/Mutual-Fund-Portfolio-Analyser` and
**nothing is pushed**: the credential helper cannot prompt in this environment. Verified
safe to publish first — the user's email appears only as the git author identity, zero
occurrences in file content, and no `data/`, `.db` or `.pdf` is tracked.

### S18 · V0.4c the CAS wiring, and V1.2e the third parser (2026-09-06)

**The ledger runs end to end from a file** (V1-14). `jobs/import_cas.py` joins the two
halves that had been finished and disconnected: read → decrypt → parse → resolve → import →
save → rebuild, Zone A read-only for resolution and NAVs, Zone B written.
`import_cas.known_txn_ids` stops being a stand-in — its docstring said persistence *"does
not exist yet"*, so the duplicate count was a claim about a database rather than an
observation of one. The job passes the real set now.

Two bugs the wiring tests found. The provider addresses columns by name and needs
`sqlite3.Row`, which failed deep inside resolution rather than near the call. And the audit
row described the *insert* rather than the *statement*: `report.txns` holds only new rows,
so a re-import recorded a statement spanning **zero folios and no dates** — false about the
file, and useless for the one question `cas_import` exists to answer.

Verified live against the real warehouse, not the test one: 14 inserted, 0 unmatched, 0
unparsed; re-run inserts 0 and reports 14 duplicates; **all three folios reconcile at
exactly 0.000000**.

**Nippon is the third format** (V1-15), and it was the test V1-10 named: general rules, or
rules tuned to a sample of two? Mostly general. The percentage-scale detection, the
month-first date, header-driven mapping, the subtotal demotion and the total-row patterns
all worked unchanged, and `nippon.py` is a config plus a `sniff`.

Two things did not. Nippon publishes **108 schemes as 108 sheets of one workbook** — the
third packaging model in three AMCs — so `parse_holdings` gained an optional `sheet`;
without it the merge reads **+222,869.8%** and is refused.

And the one that mattered: a stock-future table printed **below the GRAND TOTAL** with the
portfolio's own column shape, plus a derivatives annexure whose `Margin maintained` column
lands under `Market/Fair Value`. Read as holdings they add Rs 78.5 crore to a Rs 5,075 crore
portfolio — **+0.155%, inside the ±2% reconciliation tolerance**. V1-08's guard cannot see
it, and tightening the threshold would start refusing files that are merely rounded. So the
rule became positional: nothing below the file's own total is a holding.

**The ordering cost a regression on the way.** Applying that rule before the summary branch
swallowed HDFC's industry summary and NAV history — which sit below *its* grand total — and
with them `stated_navs`, the three-way NAV agreement from V1.2a. The existing tests caught
it; a test now asserts the ordering rather than trusting it.

**Kotak could not be fetched.** Its disclosure page is behind Radware bot detection, which
this project will not solve, and probing for a file URL is what V1-03 was written about. No
retries, no URL guessing — it needs a hand-downloaded file, and that is recorded rather than
worked around.

Measured on Nippon's real file: reconciles at **−0.0000000315%**, 104 securities from 185
rows, 21 rows correctly excluded, warnings **20 → 1**, **0.0760% unresolved** against the 2%
gate, `validation_status=ok`, 98.953707% equity + 1.046293% cash summing to exactly
**100.000000%**.

Also found, and worth carrying: **`SELECT sum(col)` over a `DECIMAL_TEXT` column returns a
float.** SZ-13's family in aggregate form — the stored values stay exact, but SQLite coerces.
That lands squarely on M3, whose whole claim is that exposures add up.

### S17 · docs/, the sniff cross-product, and V0.4b Zone B persistence (2026-09-06)

**The corpus folder is `docs/`.** `PLAN.md` §8.1 and every cross-reference always said so —
`CLAUDE.md` alone pointed at six `docs/…` paths that resolved to nothing, including the two
lines of its working agreement telling a session where to record what it did. The rename was
the fix; not one reference needed editing (V1-11). Only two files mentioned the old name and
both were prose.

**`sniff()` cross-product** per `MODULE_0.md` §15.2, driven by `REGISTRY` rather than a
hand-written list, so a parser added without a fixture fails loudly instead of quietly
shrinking the matrix. Its third case is the one with teeth: a parser claiming a foreign file
does not crash, it produces a portfolio with the wrong shape that still sums to 100%. A
greedy stub returning 1.0 unconditionally is asserted to be caught, so the guard cannot be
weakened without a failure.

**V0-19 got a revisit trigger** (V1-12). It had closed with "revisit when a query is actually
slow", which is a sentiment: nobody was measuring and "slow" was undefined. The trigger is
M5 flow computation over ~5 minutes, or `holding` over ~50M rows — it holds 166 today.
DuckDB reads SQLite directly, so the move needs no export or dual-write window, which is
what makes deferring correct rather than lazy.

**V0.4b — the ledger is in a database** (V1-13). `MODULE_1.md` §4's schema in
`migrations/zone_b/`, kept separate from Zone A because it is a different database rather
than a later version of the warehouse. `rebuild()` reads `txn` and writes every derived
table, and `txn` is its only input — anything that cannot be reconstructed from it does not
belong in a derived table.

**Invariant 5 now means what it says.** It had been two in-memory `fingerprint()` calls,
which prove the engine deterministic and nothing about storage — no table dropped, no
Decimal round-tripped, no evidence the derived rows are droppable. It is now import →
rebuild → **`DROP TABLE`** every derived table → rebuild, with two comparisons answering
different questions: a content hash that excludes the rebuild timestamps, and a whole-row
comparison with the timestamp pinned so a column the hash misses cannot drift.

**SQLCipher is not installed and the code refuses to pretend.** No binding is importable,
and V0-19 chose SQLite precisely to avoid a native dependency. So the wiring is present with
the driver optional — and an unencrypted Zone B database is **refused, not silently opened**.
§6.3 puts a PAN and folio numbers under the strictest handling in the project; a quiet
plaintext fallback is the exact failure mode this project keeps finding.

**Mutation testing killed 9 of 11 and found five real gaps**, each a claim the code made and
the tests did not check. The sharpest: `load_txns` without its `ORDER BY` still passed,
because SQLite returns rowid order and for a single import that *is* insertion order — the
rebuild was deterministic **by accident**, and would have stopped being so the first time a
statement covering an earlier period was imported. Also: the FY boundary was asserted
nowhere; the decimal-safety check had never had anything to find; and weakening the
fingerprint survived the drop-and-rebuild test because **both sides used the same weakened
hash** — the check was checking itself. Both remaining survivors are equivalent mutants and
are recorded as such rather than papered over with a contorted test.

### S16 · V1.2d — the ICICI parser, and Kite Connect declined (2026-09-06)

Two formats now parse. Almost nothing ICICI needed turned out to be
configuration — `icici.py` is three lines and a `sniff`, and both real problems
were solved in the shared reader where the next AMC inherits them (V1-10).

**A subtotal is a row that equals the sum of the rows beneath it.** ICICI prints
the section total *on* the section row where HDFC prints a bare heading above
the numbers, so the two AMCs need opposite answers to `classify_row`'s question
— and V1-08 had already measured that vocabulary cannot separate them, since a
hand-written label list still leaves ICICI 18.8% too large. Arithmetic can, at
any depth in any wording. Only rows with no ISIN and no quantity are candidates,
so HDFC's Rs 343194.12 of TREPS cash is considered and kept, because nothing
beneath it sums to that. A demoted subtotal still names its rows' section,
because V1-07 established the heading is the only thing distinguishing a short
leg from the long position written under an identical name.

**The percentage scale is read, never assumed.** HDFC writes `9.21`, ICICI
writes `0.0596550260489`, and both columns are headed `% to Nav`. The file's own
total row settles it — `99.99999999999996` against `0.9999999999896085` — and
the scale is applied in the loader, not the parser, exactly as
`market_value_unit` already works. A column totalling neither ~1 nor ~100
raises rather than being guessed at.

Both rules living in the shared reader meant HDFC's config could suddenly read
ICICI's sheet, which **invalidated an existing test's premise**. It was replaced
rather than dropped: the guard is now proven against a purpose-built sheet whose
section row claims 900 while the holdings under it come to 1000, so demotion
correctly declines to fire and it reads 1900 against its own stated 1000.

Also found: `jobs/load_holdings.py --file` could never have worked — it supplied
no `scheme_id` and `_one` reads one unconditionally, so any local file raised
`KeyError`. Only the `--amc` path had ever been exercised. It takes `--scheme`
now and refuses without it rather than guessing a scheme from a filename.

Measured: ICICI reconciles at **1.3E-14%**, 5 holdings, **0.0000% unresolved**,
`validation_status=ok`, weights exactly 100, revision unchanged on re-run. HDFC
still **+0.000000%** with its TREPS, G-Sec, cash and short leg classified as
before. The fixture load was then **deleted from the warehouse** — it is a
5-row trimmed file and leaving it under `INF109K015K4` would misrepresent a real
fund's portfolio.

**Kite Connect evaluated and declined** (V1-09). Zerodha's API was assessed as a
data source. It carries **no fund constituents at all**, which is the only thing
blocking V1.2; its MF order history is **7 days**, so it cannot rebuild a
ledger; its equity instruments dump has **no ISIN column**, which is this
project's join key throughout; and its access token **expires at 6 AM daily**
by regulatory requirement, against every other source here being public,
keyless and cron-safe. Two uses kept on the shelf and recorded: `/mf/holdings`
as a manual, human-present reconciliation witness on the ledger's *output*
(its `tradingsymbol` **is** the ISIN, so it joins with no mapping layer), and
historical candles revisited at V4 against NSE Bhavcopy, which is free and
carries ISIN natively.

### S15 · V1.2c — the reconciliation guard (2026-09-05)

Fetched ICICI Prudential's disclosure to add the second parser. It is not
shaped like HDFC's: one 25 MB ZIP of 146 workbooks rather than a file per
scheme, name and ISIN columns swapped, **`% to Nav` as a fraction rather than a
percentage**, subtotals printed ON the section rows and nested four deep, plus
covered calls, stock futures and **interest-rate swaps at notional value**.

Read through HDFC's rules it gives **2.887x** the true portfolio. A hand-written
list of structural section names gets it to 1.188x — still wrong, and every
extension of that list is a guess about the next AMC's vocabulary.

So the guard is arithmetic instead (V1-08). **Every disclosure states what it
adds up to** — `Grand Total` on HDFC's sheet, `Total Net Assets` on ICICI's —
and the parser now reconciles against it, raising beyond ±2%. HDFC reconciles
at **+0.000000%**; ICICI through the HDFC parser reads **+188.7%** and is
refused. A portfolio that counted its subtotals still normalises to 100%, so
the publisher's own arithmetic is the only independent witness there is.

It would have caught V1-04's TREPS regression too — that moved the total by
3.1%, and it was found by hand at the time.

### S14 · V1.2b — the disclosure pipeline (2026-09-05)

Fetch, parse, resolve, normalise, validate and load, end to end on HDFC Flexi
Cap's real 31-Jul-2026 disclosure. 83 holdings, `validation_status=ok`, no
failed checks, and **the fund dissolved into issuers**: 94.19% equity, 3.18%
cash, 2.17% ReIT units, 0.46% debt, -0.001% derivative, summing to exactly
100.000%. Top exposure ICICI Bank at 9.21% / Rs 10,198 cr.

Three defects, all found by verification rather than by reading (V1-05..07):

- **§2.3's honest User-Agent is unsatisfiable** on HDFC's CDN and
  niftyindices — both 403 an honest agent *and* a browser string with the
  contact appended. The contact moved to the RFC 7231 `From:` header, which
  keeps §2.3's intent. Per-source; AMFI still gets an honest agent.
- **The weight drift correction silently did nothing** when the largest weight
  already used the decimal context's 34 digits. Weights now quantise to the
  stored 6dp *before* the drift is settled.
- **A revision meant "the job ran"**, not "the AMC restated". Keyed on the
  file's sha256 now, so a re-run skips.

And V8 fired for real: HDFC's short leg of Eternal Limited is written
identically to its long position twelve rows above, so only the `OPTIONS`
section heading distinguishes them. Section context now flows onto rows.

### S13 · V1.2a — the holdings parser (2026-09-05)

HDFC Flexi Cap Fund's **real** disclosure for 31-Jul-2026 parses end to end and
resolves through the V1.1 cascade at **`unresolved_mv_pct = 0.38%`**, against
the V1 gate's 2%. 83 securities, zero unclassified rows, zero warnings.

Three independent confirmations on one number: the disclosure's own notes say
`Direct Plan - Growth Option 2267.177`, AMFI's NAV for `INF179K01UT0` on that
date is 2267.177, and mfapi's mirror agrees. The parsed market values also sum
to the file's own Grand Total with a delta of **exactly zero**, and to the AUM
in `scheme_master.yaml` to screenshot rounding.

§6.4's row classifier broke the portfolio two ways (V1-04), both invisible
downstream because the weights still sum to 100: the trailing
industry-summary block counts every sector twice, and dropping §6.4's
`and mv is None` guard made the fund's entire Rs 3,432 crore cash position
disappear. Also §8.4 misses `Net Current Assets`, and a `@` footnote marker
was being treated as a malformed number.

### S12 · V1.1 — entity master and resolution (2026-09-05)

5,427 real issuers and instruments seeded from AMFI's market-cap list, with
point-in-time `amfi_mcap` buckets. The resolution cascade runs ISIN → synthetic
rule → provisional → alias → fuzzy → queue. Measured on 289 realistic
disclosure rows: **1.73% unresolved**, against the V1 gate's 2%.

Implementing §8 against the real universe found three defects (V1-02), two of
which misattribute holdings silently:

- **§8.2 runs the synthetic rules before ISIN**, and §8.4's `future`
  derivative pattern captures **seven real listed companies** — the whole Future
  Group. A fund holding Future Retail, ISIN and all, would have that equity
  bucketed as a derivative. A known ISIN now wins.
- **`token_set_ratio` scores a subset as a perfect match**: `tech mahindra`
  against `mahindra mahindra` is 100.0, by construction. 18 of 400 sampled
  names would auto-accept onto the wrong issuer. Fixed with a Jaccard guard
  rather than a higher threshold — §8.2's 92 is kept, and wrong accepts go to
  zero.
- **Two workbook columns are formulas**, including the one `RANK` operates on.
  Reading column E alone ranked on BSE only. Recomputed, and cross-checked
  against AMFI's own categorisation column: **5,427 rows, zero disagreements**.

Also: `__NO_DISCLOSURE__` was missing from §4.3's mandatory seed (V1-01), and
NSE sector data is deferred because I throttled the host by probing it without
the rate limiter (V1-03).

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

See **Current state** at the top of this file — it is the live list. In short: **M6**, the
only unmet V1 gate criterion, then SBI and Kotak, then §6.5 ZIP staging, then Bhavcopy
prices.

**Carried, unblocked, not urgent:**

- `pdf.py` stays untested until a real password-protected CAS exists in
  `tests/fixtures/local/`.
- The sector taxonomy deferred in V1-03, after niftyindices was throttled by a probe made
  outside the rate limiter. M5 needs it, and so do `tilts()` and `sector_exposure()` — both
  currently raise and name it.
- `/mf/holdings` from Kite Connect as a manual reconciliation witness on the ledger's
  output (V1-09) — worth doing, needs no schema change, and is the first outside check on
  what the ledger *computes* rather than what it reads.
- Which source is right on 2026-03-12 — mfapi says 2111.846, AMFI says 2111.779. One
  mismatch in 2,117 dates, unresolved.
- The thin-warehouse copy still carries `raw_file` rows for archived NAV files whose rows
  it no longer holds. Harmless; pruning provenance is worse than over-reporting it.
