# BUILD_ORDER.md — recalibrated sequencing

> **Supersedes `PLAN.md` §7 sequencing.** Slice *definitions* and acceptance gates in
> §7 stand unchanged. What changes is the order of work inside and before them, based on
> structural analysis of the spec corpus (457 concepts, 625 relations).

---

## What the analysis found

| Finding | Number | Implication |
|---|---|---|
| Cross-module coupling | 16% of edges | Module boundaries are sound — do not redesign |
| M0 share of all concepts | 121 / 457 = 26% | Largest sink of effort; highest stall risk |
| Highest betweenness nodes | All provider Protocols | Interfaces are the load-bearing structure |
| M1 ledger community purity | 1.00 (37 nodes) | Buildable and provable in isolation |
| M3↔M4 strong coupling | 13 edges | One conceptual territory, two slices |
| M5↔M6 strong coupling | 16 edges | Same |
| `index_constituent` betweenness | 0.164, bridges 4 modules | Unscheduled load-bearing dependency |
| Leaf nodes (degree ≤1) | 113 / 457 = 25% | Long tail of reference detail, not build work |

---

## R1 — Add a contracts-first Slice Zero

**Why.** Every one of the top structural beams is a Protocol: `LookThroughProvider`,
`SchemeCharacteristics`, `FundDataProvider`, `MarketDataProvider`, `MarketDataFeed`,
`RiskInputs`, `MarketIntelligence`. They carry the graph's betweenness because they are
what everything routes through. Defining them late means widening them repeatedly.

**Do this first — before any implementation, ~3 days:**

1. Write all seven Protocol definitions as pure type stubs, no logic.
2. Write the seven `Fake*` implementations, reading YAML fixtures.
3. Write the result dataclasses (`SchemeRef`, `NavPoint`, `PositionContext`,
   `Exposure`, `Contribution`, `DataQuality`, `StyleSnapshot`, `RiskSnapshot`,
   `ViewEnvelope`).
4. Commit. Do not touch them again without an ADR.

**Payoff.** Every module afterwards is independently testable against fakes, in any
order, with no database. This is what makes the rest of the plan parallelisable.

**One amendment while doing it:** define `LookThroughProvider` to satisfy **M4's needs as
well as M6's** — `sector_exposure()`, `exposures()`, `concentration()`. The M3↔M4 edges
are almost entirely M4 consuming M3 outputs. Designing it for M6 alone guarantees a
widening later.

---

## R2 — Invert V0: prove the ledger before building the pipeline

**Why.** M1's ledger cluster is the only region of the graph with purity 1.00 — it has no
structural entanglement with anything else. And M0 is 26% of the corpus sitting directly
in front of it.

`PLAN.md` §7 currently puts M0 steps 1–4 (archive, fetch, NAV parsers, historical
backfill) before the ledger. That is roughly two weeks of plumbing before the first
number a human can check.

**Revised V0 order:**

| Step | Work | Days | Output |
|---|---|---|---|
| V0.0 | Slice Zero contracts + fakes (R1) | 3 | Everything testable |
| V0.1 | **Ledger on a hand-made CSV + `FakeMarketDataProvider`** | 4 | FIFO, XIRR, TWRR proven |
| V0.2 | CAS parser → replaces the hand-made CSV | 3 | Real transactions |
| V0.3 | Reconciliation against your own CAS | 1 | **V0 GATE** |
| V0.4 | M0 archive + fetch + NAV + scheme master, behind the real provider | 5 | Fake swapped for real |
| V0.5 | Historical NAV backfill | 1 | Full return series |

For V0.1, hand-type 20–30 of your own transactions into a CSV and a NAV fixture. You will
know the correct answers, which is exactly what makes the lot engine and XIRR provable.

**Payoff.** A verifiable number in the first week instead of the third. And when the M0
work does start, it is swapping a fake for a real implementation behind a frozen
interface, which is a much better-defined task than "build the ingestion layer."

---

## R3 — Design M3 and M4 together; build them apart

**Why.** Five detected communities are ~50% mixtures of M3 and M4 concepts
("Portfolio Risk & Attribution", "Concentration, Liquidity & Brinson", "Overlap,
Correlation & Factors"). The strong edges are M4 consuming M3: `Exposure →
historical_replay`, `concentration() → evaluate_limits()`, `LookThroughProvider →
RiskInputs`.

**Do not merge the slices** — V1's look-through carries the launch story and V4's risk
work does not. But when you build V1:

- Write `RiskInputs` at the same time as `LookThroughProvider` (already covered by R1).
- Store `portfolio_return_series` from V1 onward, even though nothing reads it until V4.
  It is cheap to write and expensive to backfill.
- Keep the `Exposure` dataclass stable. M4 depends on its shape.

**Same treatment for M5↔M6** (16 strong edges, two mixed communities). Build the flow
engine and its views in one pass rather than M5 then M6 — the conviction map and flow
Sankey are inseparable from the data that feeds them.

---

## R4 — Promote `index_constituent` to a V1 dependency

**Why.** Betweenness 0.164, bridging M0, M2, M3, and M5. `DECISIONS.md` OPEN-03 currently
ranks it as V3/V4 work. The graph says it is load-bearing much earlier:

| Consumer | Needs it for |
|---|---|
| M2 | `active_share_vs_bm` |
| M3 | `benchmark_pct` and `active_tilt_pp` on every sector tilt |
| M5 | Sector index construction, industry weight comparison |
| M4 | Benchmark sector weights for Brinson |

Without it, `sector_tilt` — a V1 launch view — renders absolute exposure with no
comparison, which is a materially weaker screen.

**Action:** resolve OPEN-03 before V1 begins, and add `index_constituent` ingestion to
M0's V1 block (a ~1.5 day job).

---

## R5 — Treat M6's view catalogue as parallel work, not a sequence

**Why.** M6 has the highest leaf ratio in the corpus (41%): 46 concepts, 19 of them
dangling. That is the signature of a *list*, not a structure. The only structural nodes
are `ViewEnvelope`, `assemble_caveats`, and `lookthrough_sankey`.

**Action.** Once `ViewEnvelope` and `assemble_caveats` are frozen (Slice Zero + 1 day),
every individual view is an independent unit of work. Build them in whatever order the
current slice needs. They carry almost no risk of cross-contamination — which is unusual
in this codebase and worth exploiting.

---

## R6 — Implement the connected core; treat leaves as reference

**Why.** 113 of 457 concepts (25%) have degree ≤1. Concentrated in M6 (41%), M3 (30%),
M4 (30%).

Leaf nodes are things like "Null Rendering as Em Dash", "Global Colour Assignment",
"Rolling Returns tables". They are real requirements but structurally isolated —
implementing them early buys nothing, and deferring them breaks nothing.

**Action.** For each slice, implement the concepts that the graph shows as connected to
the slice's acceptance gate. Read the rest of the spec section when you get to it. Do
**not** work through a module spec top to bottom trying to implement every subsection.

---

## Revised slice sequence

```
SLICE ZERO   contracts + fakes                              3 d   ← NEW
   │
V0 ├─ ledger on fixtures ─── CAS parser ─── reconcile       8 d   ← REORDERED
   └─ M0 fetch/NAV/backfill behind real provider            6 d
   │
V1 ├─ entity master + resolution                            4 d
   ├─ holdings parsers (5 AMCs) + normalisation             7 d
   ├─ index_constituent + prices + adjustments              4 d   ← PROMOTED
   ├─ look-through engine + closure                         4 d
   ├─ overlap + concentration + tilts                       3 d
   └─ M6: Sankey, heatmap, treemap, tilt                    5 d
   │
V2   fund x-ray + manager layer (M1 tax, M2 full, M6 fund views)
V3   flows + conviction + market views (M5 and M6 together)
V4   risk + attribution (M4, reusing V1's series and interfaces)
```

**Net effect on V1 delivery:** roughly unchanged in total days, but the first verifiable
number arrives in week one instead of week three, and the highest-risk work (M0 parsers)
happens against a frozen, already-proven interface.

---

## What did not change

- Module decomposition — 16% coupling says the boundaries are right.
- All acceptance gates in `PLAN.md` §7.
- Every invariant in `CLAUDE.md`.
- Every entry in `DECISIONS.md` Parts 1–2.
- The descriptive-not-prescriptive rule, trust zones, and Decimal discipline.

The graph validated the architecture. It changed the order of work, not the design.
