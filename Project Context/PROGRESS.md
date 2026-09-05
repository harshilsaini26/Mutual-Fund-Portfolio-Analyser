# PROGRESS.md — session log

> Claude Code updates this at the end of every session. Newest entry at the top.

## Current state

**Slice:** V0.1 — ledger on fixtures (`BUILD_ORDER.md` R2)
**Step:** The NAV-independent half is done and proven: lots, cost basis, FIFO
consumption, holding periods, realised gains. Slice Zero is complete and frozen.
**Blocked on:** the real NAV series, which the user is supplying. Without it,
market value, unrealised P&L, XIRR and TWRR cannot be computed or verified —
and PLAN.md §8.3 invariant 4 (P&L closure) cannot be asserted.
Also outstanding: OPEN-07 (NAV backfill depth) before M0 step 4; OPEN-03
(`index_constituent`) before V1, per R4; and V0-01 (base vs total TER) before
any fee-drag output.

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

## Acceptance gate for V0 (`PLAN.md` §7)

- [ ] Every folio reconciles: `|computed − reported| ≤ 0.001` units
- [ ] Value reconciles within 0.5%
- [ ] XIRR matches an independent Excel calculation to 4 dp
- [ ] All 5 ledger property invariants pass
- [ ] Re-importing the same CAS produces zero new rows

---

## Session log

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
