# PROGRESS.md — session log

> Updated at the end of every session. Newest entry at the top.
> This file is the resumption anchor: everything needed to pick the work up
> lives here, in `DECISIONS.md`, or in the commit history. Nothing important
> should exist only in a chat transcript.

## Current state

**Slice:** V1.4 — **the look-through works and persists.** Three of five AMC
formats parse and load; `compute_lookthrough` turns them into issuer exposure,
closure holds at delta 0.00, `scripts/show_lookthrough.py` prints it, and
MODULE_3 §4.2/§4.6's tables store it.
**Repo:** local git, `origin` set to
`github.com/harshilsaini26/Mutual-Fund-Portfolio-Analyser` — **not yet pushed**:
the credential helper cannot prompt in this environment, so the first `git push
-u origin main` has to be run from a terminal. Branch `main`, tree clean.
**Gate:** ruff clean · `mypy --strict` clean (121 files) · 603 tests + 3 skipped ·
verifier no drift.

**Zone B is encrypted** (V1-16): `sqlcipher3` installed, `--allow-unencrypted` gone
from the command line, and the suite runs against a real encrypted ledger. Turning
it on exposed a defect — `sqlcipher3` has its own adapter registry, so the Decimal
discipline had silently detached.

**Nippon is in** (V1-15) — the third format, and the test of whether V1-10's rules
generalise. Most did, unchanged. The one that did not: a table printed *below* the
GRAND TOTAL is +0.155% of the portfolio, **inside** the ±2% guard, so the rule had
to become positional. **Kotak is blocked** — its site is behind Radware bot
detection, which this project will not solve; it needs a hand-downloaded file.

**A CAS now goes in one command** (V1-14): `jobs/import_cas.py` joins the parser to
Zone B — read, decrypt, parse, resolve, import, save, rebuild. Verified live against
the real 3.1M-NAV warehouse: all three golden folios reconcile at exactly 0.000000.

**Zone B persists now** (V0.4b): `MODULE_1.md` §4's schema, `rebuild()` reading and
writing the database, and invariant 5 asserted against real tables that get DROPped
and rebuilt — not against two in-memory books.
**Next:** §4.3's `portfolio_concentration` and `fund_overlap` (both computed,
neither stored); then SBI, and Kotak once a file is supplied; then §6.5
member-level ZIP staging
(ICICI ships 146 workbooks in one archive), then V1 build items 5, 7 and 8 —
Bhavcopy prices, `scheme_issuer_weight`, and the look-through engine itself.

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

**Import a CAS** (prompts for the password; `--allow-unencrypted` is required until
a SQLCipher driver is installed — V1-13):

```bash
python -m jobs.import_cas --file statement.pdf --user USER-01 --allow-unencrypted
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
| `src/m1_ledger/` | `txn.py`, `lots.py` (FIFO engine), `returns.py` (XIRR/TWRR/timing), `reconcile.py` (the V0 gate), `db.py` (Zone B connection + schema; refuses to open unencrypted), `persist.py` (`rebuild()` — `txn` in, every derived table out) |
| `src/m1_ledger/cas/` | `parse.py` (state machine, pure), `mapping.py`, `importer.py` (seq, linking, idempotence), `pdf.py` (the only module touching a password — **untested**, needs a real CAS) |
| `src/m0_data/` | `fetch/` (archive, rate limit, robots, conditional GET, AMFI history), `parse/nav/amfi.py` + `parse/mcap/amfi.py`, `parse/holdings/` (shared reader + `hdfc`, `icici`, `nippon`, registry), `normalise/` (numbers, names, units, weights), `resolve/` (isin, synthetic, fuzzy, cascade, queue), `derive/nav_adj.py`, `load.py`, `validate/` (integrity, checks), `schema/apply.py`, `providers/warehouse.py` |
| `migrations/zone_b/` | `001_ledger.sql` — `app_user`, `cas_import`, `txn`, `lot`, `lot_consumption`, `position`, `reconciliation`. `002_lookthrough.sql` — `lookthrough_exposure`, `lookthrough_contribution`, `portfolio_summary`. Separate from Zone A: a different database, not a later version of the warehouse. |
| `migrations/` | `001_provenance.sql`, `002_scheme_nav.sql`, `003_entity.sql` (`issuer` + a **nine**-row synthetic seed, `instrument`, `name_alias`, `resolution_queue`, `issuer_classification`), `004_holdings.sql` (`holding`, `holding_disclosure`). Numbered, forward-only. |
| `jobs/` | `fetch_nav.py` (daily leading edge) · `backfill_nav.py` (history, per OPEN-07) · `build_entity_master.py` (AMFI market-cap seed) · `load_holdings.py` (L0→L3 for one disclosure) · `import_cas.py` (a statement into Zone B, then a full rebuild). The Zone A jobs write a `job_run` row; `import_cas` writes `cas_import`, Zone B's equivalent. |
| `config/` | `txn_types.yaml` — CAS description → type, per §5.5. `sources.yaml` — per-source URLs and scraping limits (S5 carries the browser agent HDFC's CDN requires, contact in `From:`, per V1-05). `amc_manifest.yaml` — disclosure links per AMC; discovery is still manual (V1-03). |
| `scripts/` | `import_nav_xlsx` · `build_v0_fixture` · `build_v0_cas` · `verify_v0_ledger`. Not part of `src/`; the verifier deliberately imports nothing from it. |
| Fixture portfolio | 3 real funds keyed on their **real ISINs** — `INF179K01UT0`, `INF109K01761`, `INF174KA1EZ1` — on NAVs confirmed against AMFI. Reaches the engine as a **CAS statement**, resolved through `MarketDataProvider`. |
| Test fixtures | `v0_ledger/` (real NAVs, golden rows, `cas_statement.txt`, `expected.yaml`) · `cas/traps.txt` (one §5.4 trap per labelled line) |
| Zone A warehouse | SQLite (V0-19), `DECIMAL_TEXT` throughout. Loaded: 53 AMCs (28 with AMFI codes), 19,598 schemes, **3,118,359 NAVs** back to 31-Jan-2018, 5,427 instruments + 5,436 issuers (nine synthetic) with point-in-time market-cap buckets, and **270 holdings** across 3 disclosure revisions — HDFC Flexi Cap (2 revisions) and Nippon Growth Mid Cap. `scheme_idcw` is **empty**: `nav_adj` is built and consumed, but no IDCW source has been ingested, so it equals `nav` everywhere. |
| Zone B ledger | SQLite + the `MODULE_1.md` §4 schema, **unencrypted until a SQLCipher driver is installed** — `connect_ledger` refuses rather than degrading (V1-13). Holds nothing real yet; the golden statement imports into it on demand. |
| Not built | Kotak (bot-blocked) and SBI parsers · §6.5 ZIP member staging · sector taxonomy (V1-03) · `security_price`/`security_adjustment` · `scheme_issuer_weight` · look-through engine (`src/m3_lookthrough/` is empty) · `direct_holding` · tax engine · **all UI** (`src/m6_views/` is Slice Zero's stub) · M2, M4, M5 |

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

V1's gate cannot pass yet, and the reason is not the parsers: three of its four
criteria are properties of the **look-through engine and the UI**, neither of
which is built. What follows separates what has been measured from what has not
been attempted, because a gate reported as "in progress" says nothing.

- [x] **`pct_normalised` sums to exactly 100 per scheme-date.** Exactly, on all
      three formats: HDFC's real 31-Jul-2026 disclosure (83 holdings), Nippon's
      real one (104), and ICICI's fixture (5). `weight_residual` is stored
      beside it so the adjustment is visible rather than silent. Measured on
      three schemes, not on a portfolio.
- [ ] **Look-through total equals portfolio value.** Not attempted —
      `scheme_issuer_weight` is not materialised and `src/m3_lookthrough/` is
      empty. This is V1 build items 7 and 8.
- [~] **`unresolved_pct` < 2% for every held scheme, and is displayed.**
      Measured at **0.38%** on HDFC's real disclosure, **0.0760%** on Nippon's
      and **0.0000%** on ICICI's, against the 2% bar. It is computed, persisted on
      `holding_disclosure` and returned by the loader — but "displayed" needs
      M6, so the criterion is half-met and counted as such.
- [ ] **Every chart renders its as-of date, staleness and coverage.** No charts.
      `src/m6_views/` is Slice Zero's Protocol stub and envelope, no logic.

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

`DECISIONS.md` holds **57 decisions across 58 entries** (SZ-01…SZ-14,
V0-01…V0-26, V1-01…V1-15, OPEN-03, OPEN-07). OPEN-03 has two entries: the
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

**Finish V1 build item 3** — Kotak, SBI and Nippon. Two formats are in hand and
they disagreed on column order, date format, percentage scale and where a
subtotal lives; both disagreements were settled by rules in the shared reader
rather than per-AMC code, so the third format is the test of whether that
generalises or whether V1-10's arithmetic was tuned to a sample of two.

Then **§6.5 member-level ZIP staging** — ICICI publishes 146 per-scheme
workbooks in one 25 MB archive, so until the ZIP is archived as a single
`raw_file` with each member staged under its own name, ICICI loads only via
`--file --scheme` and has no manifest entry.

Then the engine that consumes all of it — **V1 build items 5, 7 and 8**:
Bhavcopy into `security_price` / `security_adjustment`, `scheme_issuer_weight`
materialisation, and look-through aggregation with pairwise overlap and
concentration. Three of V1's four gate criteria depend on those and on M6, and
none of the three has been started.

**Carried, unblocked, not urgent:**

- `pdf.py` stays untested until a real password-protected CAS exists.
- V0's own build item 9 — KPI cards and a position list — was never built. The
  V0 acceptance gate does not require a UI and passes without one, but the
  slice is not complete against `PLAN.md` §7's build list, and V1's gate needs
  M6 regardless.
- The sector taxonomy deferred in V1-03, after I throttled niftyindices by
  probing it outside the rate limiter.
- `/mf/holdings` from Kite Connect as a manual reconciliation witness on the
  ledger's output (V1-09) — worth doing, needs no schema change, and is the
  first outside check on what the ledger computes rather than what it reads.
