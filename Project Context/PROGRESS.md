# PROGRESS.md — session log

> Claude Code updates this at the end of every session. Newest entry at the top.

## Current state

**Slice:** V0.1 — ledger on fixtures (`BUILD_ORDER.md` R2). Substantially complete.
**Step:** Lot engine, returns engine and reconciliation all built and proven on
three real NAV series. All five `PLAN.md` §8.3 property invariants pass, two of
them now at exact equality rather than a tolerance. V0-06, V0-10 and V0-12 are
resolved (V0-14).
**Blocked on:** nothing to continue. Of the five V0 gate conditions, three are
fully met and two are partly met for reasons outside the ledger — an Excel
cross-check is a human step, and the re-import claim needs the CAS parser.
Deferred by agreement: ABSL NAV series, HDFC Direct-plan TER (V0-05).
Still outstanding: OPEN-07 before M0 step 4; OPEN-03 before V1 (R4);
V0-01 (base vs total TER) before any fee output.

## Acceptance gate for the current slice

Slice Zero has no runtime behaviour, so its gate is static:

- [x] `make lint` clean — ruff on `src/` and `tests/`, `mypy --strict` on 44 files
- [x] All ten Protocols import and resolve
- [x] Every contract dataclass is frozen (61 of 61)
- [x] No contract dataclass has a `float` field (61 of 61)
- [x] `ClassificationBasis` never carries a default on any protocol method
- [x] `RiskInputs`' M3 block is satisfiable by `LookThroughProvider` (R1's amendment)
- [x] Types M6 renders all carry `caveats`
- [x] `ViewEnvelope` provenance and quality fields have no defaults
- [x] No module imports one downstream of itself
- [x] `Fake*` implementations exist and run with no database (8 fakes, 204 tests)
- [x] Every fake satisfies its Protocol's full method set and signatures
- [x] Decimal round-trips through SQLite with no precision loss
- [x] No fixture value can reach a fake as a `float`

## Acceptance gate for V0 (`PLAN.md` §7) — honest status

- [x] **Every folio reconciles**, `|computed − reported| ≤ 0.001` units.
      All three are at exactly 0.000000 against the statements' printed balances.
- [x] **Value reconciles within 0.5%**, and now genuinely means something.
      V0-12 is resolved: `nav_cross_check()` is a gate condition, so a wrong NAV
      series fails with `WRONG_NAV_SERIES` even when units and the spec's value
      ratio are both perfect. The V0-05 mismatch would now be caught.
- [~] **XIRR matches an independent calculation to 4 dp.** Verified three ways:
      closed-form cases, NPV≈0 at the returned rate on real data, and Excel's
      365-day convention. Not yet compared against an actual spreadsheet — the
      gate says Excel specifically, and that is a human step.
- [x] **All 5 property invariants pass.** Unit conservation, cost conservation,
      FIFO ordering, P&L closure, determinism.
- [~] **Re-importing produces zero new rows.** `txn_id` is a deterministic hash
      and is tested stable across reloads, so the mechanism holds. The end-to-end
      claim cannot be made until the CAS parser exists (V0.2).

---

## Session log

### 2026-09-05 — session 6

**Did:** Resolved V0-06, V0-10 and V0-12 together (V0-14), since both allocation
fixes change the numbers the reconciliation gate then checks.
- Lots carry `cost_remaining`; a closing lot hands out its remainder exactly.
- Per-lot proceeds settle their residual onto the last consumption.
- `nav_cross_check` is now a reconciliation gate condition.
- Invariants 2 and 4 tightened from tolerance to exact equality.

**Passed:** ruff clean; `mypy --strict` clean on 51 files; 284/284 tests;
verifier reports no drift.

**Findings worth carrying forward:**
- The V0-10 fix was **incomplete on the first pass**: allocation was corrected
  but `cost_basis_remaining()` still recomputed the drifting form one query
  away. Caught only because P&L closure then failed by a paisa. Fixing a
  formula means finding every place that formula appears.
- **Both surviving mutations were fixes masked by an accident of the fixture**,
  not by a missing assertion: `min()` happened to cap the one overshooting lot
  that closes, and the wrong-series NAV deviation (~5,600%) sat so far from the
  0.5% boundary that a 200x change in tolerance altered nothing. A passing suite
  cannot report this about itself; only mutation can.

**Next:** V0.2 — the CAS parser replaces the hand-made CSV, which also makes the
re-import gate condition real.


### 2026-09-05 — session 5

**Did:**
- Added Kotak Pioneer (third fund, third NAV scale, second exit-load rate).
- Returns engine: XIRR, TWRR, timing effect (`src/m1_ledger/returns.py`).
- Reconciliation and the gate (`src/m1_ledger/reconcile.py`).
- `units_balance_rep` added to `txn` and the generator, so there is an
  independent figure to reconcile against.
- Closed `PLAN.md` §8.3 invariant 4 (P&L closure), the last of the five.

**Passed:** ruff clean; `mypy --strict` clean on 51 files; 276/276 tests;
verifier reports no drift.

**Blocked:** nothing.

**Decisions appended:** V0-07 … V0-13.

**Findings worth carrying forward:**
- **V0-12 is the significant one.** `MODULE_1.md` §11.1's value check multiplies
  both sides by the same NAV, so the NAV cancels and the check reduces to the
  unit delta as a fraction. It cannot detect a wrong NAV series — the exact
  failure it claims to catch, and the one `PLAN.md` §7 V0 depends on it for.
  V0-05 was that failure with real data and would have passed both checks.
  `nav_cross_check()` uses the statement's printed NAV as an independent witness
  and does catch it.
- **Mutation testing has now found a real gap in every module it has touched:**
  the LTCG boundary (session 3), exit load and STT in cashflows (session 4),
  folio scoping in reconciliation (this one). Each survived the whole suite.
  It is worth running on every new module rather than occasionally.
- **A fixture is only as good as its variety.** One NAV scale hid V0-10; one
  folio per scheme hid the reconciliation netting bug; one exit-load rate hid a
  hardcoded constant. Each was found by adding a fund, not by review.

**Next:**
1. Resolve V0-12 (adopt the NAV cross-check as a gate condition) and V0-06 /
   V0-10 together (residual allocation, both sides of the same rounding issue).
2. Then V0.2: the CAS parser replaces the hand-made CSV, which also makes the
   re-import gate condition real.


### 2026-09-05 — session 3

**Did:**
- `git init` in the project folder (local only; no remote, nothing pushed),
  `.gitignore` and `.gitattributes`, and the Slice Zero freeze commit per R1 step 4.
- Transcribed three real funds from supplied screenshots into
  `tests/fixtures/v0_ledger/scheme_master.yaml`.
- V0.1 ledger, test-first: `src/m1_ledger/txn.py` (taxonomy, idempotent txn_id,
  reversal handling) and `src/m1_ledger/lots.py` (FIFO engine, cost basis,
  realised gains).
- Golden fixture: 14 hand-verified synthetic transactions + `expected.yaml`
  computed independently of the engine.

**Passed:** ruff clean; `mypy --strict` clean on 47 files; 229/229 tests.

**Blocked:** market value, XIRR and TWRR — waiting on the NAV series.

**Decisions appended:** V0-01 (OPEN: base vs total TER), V0-02, V0-03, V0-04.

**Findings worth carrying forward:**
- **Mutation testing found a hole the golden fixture could not.** Changing the LTCG
  boundary from `> 365` to `>= 365` left all 19 tests green, because no fixture
  holding period lands on 365 days. At the quoted rates that boundary is 20% vs
  12.5% on the whole gain. Golden fixtures do not catch off-by-ones; boundaries
  need their own tests. Five other mutations (LIFO, dropped stamp duty, ignored
  exit load, clamped InsufficientUnits, netted reversal) were caught.
- **A fund with no benchmark is not the same as a fund with a PRI benchmark.**
  The spec covers the second; the ICICI multi-asset fund is the first. Two states,
  two reasons, both suppressed (V0-02).
- **Real fund pages publish two TERs and the schema has one column** (V0-01). The
  gap is 0.29-0.43pp, which is a quarter to a third of the headline fee.
- Second B023 (closure over a loop variable) caught by ruff in `txn.py`, same
  class as the one in `m5_market/providers/fake.py` last session. Worth watching
  for in any fixture-parsing code.

**Next:**
1. NAV series arrives -> position valuation, XIRR, TWRR, timing effect, and
   PLAN.md §8.3 invariant 4.
2. Reconciliation (units AND value) — MODULE_1.md §11, the V0 gate.
3. Then V0.2: the CAS parser replaces the hand-made CSV.


### 2026-09-04 — session 2

**Did:**
- `src/common/decimals.py` — MODULE_1.md §4.1 constants and adapters, plus
  `quantise_*` helpers, `connect()`, and `RECON_UNITS_TOLERANCE`.
- `src/common/fixtures.py` — `DecimalSafeLoader`, `FixtureStore`, `FixtureError`,
  `fixture_key`.
- Eight `Fake*` providers across `src/m0_data`, `m2_fund`, `m3_lookthrough`,
  `m4_risk`, `m5_market` — R1's seven plus `FakeLookThroughDataProvider`.
- One coherent fixture portfolio in `tests/fixtures/slice_zero/` (3 funds, 2 direct
  holdings, 8 issuers), carrying the cases that break naive code: a quarantined
  disclosure, 4.2% unresolved, a shared issuer held both directly and via a fund,
  Direct/Regular plan pairs, a merged scheme on a 731/1000 ratio, a 1:1 bonus, and
  an issuer that changes mcap bucket between AMFI lists.
- `tests/unit/test_decimals.py` (26) and `tests/unit/test_fakes.py` (48).

**Passed:** ruff clean; `mypy --strict` clean on 44 files; 204/204 tests.

**Blocked:** nothing for this slice.

**Decisions appended:** SZ-13, SZ-14.

**Findings worth carrying forward:**
- **SQLite gives `DECIMAL` NUMERIC affinity, not TEXT.** A Zone B column declared
  `DECIMAL` silently converts `'4821.442100'` to the REAL `4821.4421` on write —
  exactness and trailing zeros gone, no error. MODULE_1.md §4.1 says "TEXT affinity"
  and its DDL is correct, but never warns that the obvious column type defeats it.
  Zone B DDL must use `DECIMAL_TEXT` (or plain `TEXT`); `UNSAFE_DECIMAL_TYPES` is
  exported for a migration lint. This one would have been invisible until
  reconciliation failed months in.
- **PyYAML floats fixture data by default.** Handled by `DecimalSafeLoader`, and six
  committed fixture values are left unquoted so the guarantee is tested against real
  data rather than a synthetic input.
- Two fixture bugs were caught by the tests rather than by review: look-through
  closure was out by Rs 423.53, and `direct_value_inr` was wrong by Rs 254. Both are
  now computed from the fixture inputs rather than typed by hand.

**Next:**
1. Commit and freeze (R1 step 4).
2. Resolve OPEN-07 before M0 step 4.
3. V0.1 — hand-type 20-30 real transactions into a CSV plus a NAV fixture, and prove
   FIFO, XIRR and TWRR against numbers whose correct answers are already known.
   `FakeMarketDataProvider` is ready for this.


### 2026-09-04 — session 1

**Did:**
- Read `PLAN.md`, `BUILD_ORDER.md`, and the interface sections of MODULE_0/2/3/4/5.
- Built Slice Zero contracts: 10 Protocols and 61 result dataclasses across
  `src/common/contracts/`, `src/m0_data/providers/`, `src/m1_ledger/handoff.py`,
  `src/m2_fund/providers/`, `src/m3_lookthrough/providers/`, `src/m4_risk/providers/`,
  `src/m5_market/providers/`, `src/m6_views/`.
- Wrote `tests/unit/test_contracts.py` (130 tests) as the static gate.
- Created `pyproject.toml` (ruff + mypy + pytest config; none existed) and a minimal
  `Makefile` so the `make lint` / `make test` commands `CLAUDE.md` documents are real.
- Created `DECISIONS.md` — it was referenced throughout the corpus but absent.

**Passed:** ruff clean; `mypy --strict` clean on 35 files; 130/130 tests.

**Blocked:** nothing for this slice.

**Decisions appended:** SZ-01 … SZ-12. The substantive ones are SZ-01 (the
`lookthrough()` name collision between M3 §15.1 and M4 §14.4), SZ-02
(`max_pairwise_overlap` missing from `LookThroughProvider`, which R1's amendment requires),
and SZ-09 (six methods M4 §13's limit engine calls but §14.4 never declares).

**Findings worth carrying forward:**
- **47 result types named in the interface sections are defined nowhere in the corpus** —
  not in any MODULE file and not in `PLAN.md`. Roughly 40 were recoverable from DDL or call
  sites; five (`PeerContext`, `FeeDrag`, `IndexMeta`, `PolicyComponent`, `StressShock`) had
  neither and their fields are inferred from adjacent tables. Confirm those before the
  owning modules are built. This corroborates `PLAN.md` §12's own note that M3 and M4 were
  specified least completely.
- R1's 3-day estimate for Slice Zero was based on writing seven Protocol stubs. The
  Protocols transcribe almost verbatim; defining the 47 missing types was the actual work.

**Next:**
1. R1 step 2 — the `Fake*` implementations reading YAML fixtures. Highest value first:
   `FakeMarketDataProvider`, since V0.1 (ledger on fixtures) depends on it.
2. Resolve OPEN-07 before M0 step 4.
3. Then V0.1: hand-type 20-30 real transactions into a CSV plus a NAV fixture, and prove
   FIFO, XIRR and TWRR against numbers whose correct answers are already known.
