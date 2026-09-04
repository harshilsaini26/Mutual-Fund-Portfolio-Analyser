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
- If a spec is ambiguous or looks wrong, say so before coding around it.
