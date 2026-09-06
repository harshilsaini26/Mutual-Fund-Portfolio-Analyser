# MF Look-Through Platform

Self-hosted analytical workbench for Indian mutual fund investors. Dissolves funds into
the companies actually owned. Read `docs/PLAN.md` before any substantive work.

## Docs — read on demand, do not load all at once

| File | When to read |
|---|---|
| `docs/PLAN.md` | **Always, first.** Aim, scope, vertical slices, coding rules |
| `docs/MODULE_N.md` | Before touching `src/mN_*/` — full schema and internals |
| `docs/DECISIONS.md` | Before changing anything that looks like a design choice |

Module specs are 1,400–1,800 lines each. Read the sections you need, not whole files.

## Build order — vertical slices, never horizontal

Do not build "all of M0" then "all of M1". Build V0 end-to-end, pass its gate, then V1.
Slice definitions and acceptance gates are `PLAN.md` §7. Current slice is in
`docs/PROGRESS.md`.

## Non-negotiable invariants

Violating any of these silently corrupts data. If a task seems to require it, stop and ask.

1. **`Decimal` for all money, units, NAVs, weights. Never `float`.** Floats permitted only
   inside XIRR/covariance/OLS routines, converted back at the boundary. SQLite stores
   Decimals as `TEXT` with adapters — never `REAL`.

   **Never aggregate `DECIMAL_TEXT` in SQL.** SQLite coerces to float on `SUM`/`AVG`/
   `TOTAL` over text-affinity columns. Select rows, aggregate in Python with `Decimal`.
   Applies to every closure, weight and exposure figure. The stored values stay exact —
   it is the aggregate that is silently a float, which is why this survives a schema
   that is entirely `DECIMAL_TEXT` and passes `assert_schema_is_decimal_safe`. Found in
   V1-15; `test_decimals.py` holds the demonstration.

   **Never `ORDER BY` one either.** It sorts as TEXT, so `"5000"` comes before
   `"25000"`. A top-20 list ordered in SQL is not the top 20, and the SQL reads
   as obviously correct. Sort in Python. V1-18.
2. **Never `UPDATE` a fact row.** Append with a new `revision`, flip `is_current`.
3. **Modules talk through Protocol interfaces, never direct SQL across boundaries.**
   Dependency direction is one-way: M0 → M1 → M2 → M3 → M4/M5 → M6.
4. **Never silently drop rows.** Unresolved holdings → `__UNRESOLVED__`. Missing
   disclosures → `__NO_DISCLOSURE__`. Missing coverage → displayed `coverage_pct`.
5. **Raise, don't clamp.** `InsufficientUnits`, `UnmappedTransactionType`, and
   `NoParserMatched` must raise. A plausible wrong answer is worse than a crash.
6. **All classification is point-in-time.** Market-cap and sector lookups take an
   effective date. `ClassificationBasis` is a required argument with no default.
7. **Benchmarks must be TRI.** Suppress the comparison if `is_total_return` is false.
8. **Never invent tax rates.** They live in `config/tax_rules.yaml`, human-verified.
   `assert_tax_rules_verified()` blocks tax output until then.
9. **Descriptive language only.** Never "you should", "consider selling", "too risky".
   CI lints for this.
10. **Every derived table is droppable and regenerable.** Full rebuild must reproduce
    byte-identical output.

## Layout

```
src/common/       Decimal utils, hashing, config, logging
src/m0_data/      fetch, parse, resolve, validate, providers
src/m1_ledger/    cas, lots, returns, tax, reconcile, handoff
src/m2_fund/      style, holdings, performance, manager, peers, position
src/m3_lookthrough/ engine, metrics, tilts, returns, quality
src/m4_risk/      series, metrics, covariance, attribution, factors, stress
src/m5_market/    sectors, flows, conviction, relevance, companies
src/m6_views/     envelope, builders, format, export, api
frontend/         React + Vite
tests/            unit, property, golden, integration
migrations/       numbered, forward-only
```

## Commands

```bash
make test          # full suite
make test-fast     # unit + property only
make lint          # ruff + mypy + descriptive-language lint
make migrate       # apply migrations
python -m jobs.<name> [--as-of YYYY-MM-DD]
```

## Working agreement

- **Use plan mode for anything touching a schema or a module boundary.** Show the plan
  before writing code.
- **Write the test first for anything in `src/m1_ledger/` or `src/m3_lookthrough/`.**
  Those two carry the correctness gates.
- **Update `docs/PROGRESS.md`** at the end of every session: slice, step, what passed,
  what is blocked.
- **Append to `docs/DECISIONS.md`** when resolving an OPEN item or departing from a spec.
  Never edit an existing entry — supersede it.
- **Every ADR is self-contained.** State the decision and the question it answers inside
  the entry. Never reference an entry by ID alone — `OPEN-03` and `OPEN-07` were cited
  that way from two documents while their text was lost, and the citations pointed at
  nothing.
- If a spec is ambiguous or looks wrong, say so before coding around it.

## Working principles

**Think before coding.** State assumptions explicitly. When the specs are
ambiguous, present the interpretations — don't pick one silently. If a simpler
approach exists, say so before implementing the specified one. If confused,
name what's unclear and stop.

**Simplicity first.** Minimum code that satisfies the current gate. No
abstraction for single-use code, no configurability that wasn't asked for, no
error handling for impossible states. If 200 lines could be 50, rewrite it.

**Surgical changes.** Every changed line traces to the current task. Don't
improve adjacent code, don't refactor what isn't broken, match existing style.
Remove only the orphans your own change created. Mention unrelated dead code;
don't delete it.

**Goal-driven.** Before multi-step work, state the plan as steps with
verification: `1. [step] → verify: [check]`. Then loop until each verifies.

### Applied here

- **The V0 verifier is a V0 artifact.** `scripts/verify_v0_ledger.py` stays for
  the ledger. Do not build an independent verifier for M0, M2, or anything else.
  The ledger justified the cost because everything inherits its errors. Parsers
  don't.
- **Mutation testing is for the correctness gates only** — M1 ledger and M3
  look-through. Not for parsers, not for view builders, not for fixtures.
- **Prefer a real file over a better simulation.** When a synthetic fixture and
  a real artifact would answer the same question, get the real artifact.
- **Contracts are frozen.** Changing a Protocol or dataclass in
  `src/common/contracts/` requires an ADR before any code.
- **Do not extend the fixture pipeline.** Four scripts with an order dependency
  is the ceiling. A fifth generator makes the fixtures a system in their own
  right, and a system that produces test data is a system nothing tests.
- **Parsers get** a golden fixture, a `sniff()` cross-product test, and a
  schema-drift check. That is the ceiling, per `MODULE_0.md` §15.2 — parsers
  are not mutation-tested and get no independent verifier.
- **Say what a check does not prove.** If a test's fixture was produced by the
  code path under test, record that in the docstring. The CAS round trip
  re-reads rows `build_v0_fixture.py` computed, so it tests the parser and not
  the arithmetic — writing that down is what stops it being cited later as
  evidence it never was.
