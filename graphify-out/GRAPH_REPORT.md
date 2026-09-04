# Graph Report - Project Context  (2026-08-21)

## Corpus Check
- 8 files · ~55,543 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 457 nodes · 625 edges · 32 communities (28 shown, 4 thin omitted)
- Extraction: 89% EXTRACTED · 10% INFERRED · 1% AMBIGUOUS · INFERRED: 65 edges (avg confidence: 0.85)
- Token cost: 527,012 input · 87,562 output

## Community Hubs (Navigation)
- Flow Engine & View Rendering
- CAS Ingest & FIFO Lot Ledger
- Sector Intelligence Dashboards
- Module Contracts & L0-L3 Layers
- Entity Resolution & Issuer Master
- Portfolio Risk & Attribution
- Look-Through Aggregation
- Corporate Actions & Fund Metadata
- Concentration, Liquidity & Brinson
- Overlap, Correlation & Factors
- Stress Testing & Factor Exposure
- Reconciliation & Peer Ranking
- Manager Careers & Turnover
- Disclosure Coverage Validation
- Position Context Handoff
- Return Windows & Manager Transitions
- Core Platform Invariants
- Parser Routing & Schema Drift
- XIRR, TWRR & Benchmarks
- Regime-Aware X-Ray Windows
- Style Snapshots & Mcap Basis
- Raw Archive & Replay
- Source Config & Scraping Hygiene
- Fetcher Protocol & Job Scheduling
- Market Value Units Trap
- Regime Segmentation Gate
- TRI Index Levels
- Rolling Returns
- Direct Holdings Upsert
- Colour & Accessibility Encoding
- Null Em-Dash Rendering
- Three-Window Returns View

## God Nodes (most connected - your core abstractions)
1. `compute_lookthrough()` - 14 edges
2. `issuer Table` - 11 edges
3. `MF Flow Engine` - 11 edges
4. `MarketDataProvider` - 10 edges
5. `resolve (Entity Resolution Cascade)` - 10 edges
6. `FundDataProvider` - 10 edges
7. `LookThroughProvider` - 10 edges
8. `import_cas` - 9 edges
9. `PositionContext` - 9 edges
10. `MarketDataProvider` - 9 edges

## Surprising Connections (you probably didn't know these)
- `NAV_MISMATCH Alarm` --semantically_similar_to--> `TRI-Only Benchmark Enforcement`  [INFERRED] [semantically similar]
  MODULE_1.md → MODULE_2.md
- `Grandfathering (31-Jan-2018 FMV)` --semantically_similar_to--> `Point-in-Time mcap_basis Rule`  [INFERRED] [semantically similar]
  MODULE_1.md → MODULE_2.md
- `TWRR via Adjusted NAV Ratio` --semantically_similar_to--> `segment_position`  [INFERRED] [semantically similar]
  MODULE_1.md → MODULE_2.md
- `Closure Invariant` --semantically_similar_to--> `Unexplained Residual`  [INFERRED] [semantically similar]
  MODULE_3.md → MODULE_4.md
- `blended_score (Sort Key Only)` --semantically_similar_to--> `returns_factor_regression()`  [INFERRED] [semantically similar]
  MODULE_3.md → MODULE_4.md

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **L0-L3 Regeneration Chain** — module_0_layer_architecture, module_0_l0_raw_archive, module_0_l1_staged_layer, module_0_l2_canonical_layer, module_0_l3_derived_layer [EXTRACTED 1.00]
- **Fetch-Parse-Resolve-Validate-Materialise Pipeline** — module_0_sourcefetcher, module_0_parser_protocol, module_0_resolution_cascade, module_0_validation_gates, module_0_materialise_weights, module_0_compute_coverage [EXTRACTED 1.00]
- **100x Units-Error Defence Chain** — module_0_units_trap, module_0_staged_holding, module_0_to_inr, module_0_unit_multiplier, module_0_validation_gates [INFERRED 0.95]
- **M1 Deterministic Rebuild Pipeline** — module_1_rebuild, module_1_build_lot_book, module_1_apply_transaction, module_1_build_positions, module_1_reconcile, module_1_emit_position_contexts [EXTRACTED 1.00]
- **M1 to M2 PositionContext Handoff** — module_1_position_context, module_1_context_hash, module_1_position_context_table, module_2_load_contexts, module_2_build_position_xray, module_2_should_rebuild_xray [EXTRACTED 1.00]
- **M2 Manager Dossier Pipeline** — module_2_manager_performance, module_2_manager_career, module_2_manager_transition, module_2_style_shift, module_2_can_merge_manager_identity, module_2_amc_manager_stability [EXTRACTED 1.00]
- **Closure Preservation Pattern** — module_3_closure_invariant, module_3_assert_closure, module_3_unresolved_bucket, module_3_no_disclosure_bucket, module_3_compute_lookthrough, module_3_weight_basis_disclosed [EXTRACTED 1.00]
- **Three-Signal Redundancy Assessment** — module_3_redundancy, module_3_pairwise_overlap, module_2_schemecharacteristics, module_3_style_basis_guard, module_3_blended_score [EXTRACTED 1.00]
- **Attribution Honesty Chain** — module_4_interim_trading_problem, module_4_ledger_sourced_active_return, module_4_brinson_fachler, module_4_carino_link, module_4_unexplained_residual, module_4_attribution_run [EXTRACTED 1.00]
- **MF Flow Derivation Pipeline** — module_5_eligible_schemes, module_5_flow_engine, module_5_mf_flow_issuer, module_5_mf_flow_sector, module_5_conviction_scoring, module_5_coverage_pct [EXTRACTED 1.00]
- **Point-in-Time Sector Index Correctness** — module_5_sector_index_construction, module_5_point_in_time_membership, module_5_corporate_action_adjustment, module_0_index_constituent, module_5_universe_id [EXTRACTED 1.00]
- **Structural Honesty Chain** — module_6_view_envelope, module_6_caveat_assembly, module_6_render_states, module_6_view_container, module_6_export_provenance, module_6_language_lint [EXTRACTED 1.00]

## Communities (32 total, 4 thin omitted)

### Community 0 - "Flow Engine & View Rendering"
Cohesion: 0.05
Nodes (48): Breadth (median vs cap-weighted return), Conviction Scoring, Corporate-Action Adjustment, coverage_pct (stored, never extrapolated), Honest Differentiation Assessment, Weight Dispersion & Divided Consensus, Both-Disclosure Eligibility Filter, Equity-Only Conviction Filter (+40 more)

### Community 1 - "CAS Ingest & FIFO Lot Ledger"
Cohesion: 0.08
Nodes (37): MarketDataProvider, apply_merger, apply_segregation, apply_transaction, assert_m0_ready (pre-import gate), assert_tax_rules_verified, build_lot_book, build_positions (+29 more)

### Community 2 - "Sector Intelligence Dashboards"
Cohesion: 0.06
Nodes (37): ClassificationBasis, Deterministic Result Ordering, LookThroughProvider, Risk & Attribution (M4), Company Snapshot (price-based), contribution_1m Decomposition Aid, Corporate Announcements, Headline-and-Link-Only Rule (+29 more)

### Community 3 - "Module Contracts & L0-L3 Layers"
Cohesion: 0.07
Nodes (34): M0 Data & Ingestion Module, M1 Ledger Module, One-Way Module Dependency Chain, PLAN.md, Plan Mode for Schema and Boundary Changes, Protocol-Only Module Boundaries, Never Invent Tax Rates, Vertical Slice Build Order (+26 more)

### Community 4 - "Entity Resolution & Issuer Master"
Cohesion: 0.08
Nodes (33): Never Silently Drop Rows, Point-in-Time Classification, amc Table, benchmark_index Table, Failure Catalogue and Runbook, holding Table, index_constituent Table, instrument Table (+25 more)

### Community 5 - "Portfolio Risk & Attribution"
Cohesion: 0.08
Nodes (27): risk_free_rate Table, S13 RBI T-Bill Yields, SchemeCharacteristics (M2), aggregate_quality(), Single mcap_basis Per Aggregation, portfolio_returns(), portfolio_summary Table, portfolio_tilt Table (+19 more)

### Community 6 - "Look-Through Aggregation"
Cohesion: 0.11
Nodes (23): PositionContext (M1), add_direct_holdings(), assert_closure(), Closure Invariant, compute_lookthrough(), Direct Equity Unification, expand_fof(), Fund-of-Funds Recursion (+15 more)

### Community 7 - "Corporate Actions & Fund Metadata"
Cohesion: 0.11
Nodes (23): Descriptive Language Only, M2 Fund Analytics Module, M3 Look-Through Module, M4 Risk Module, M5 Market Module, M6 Views Module, Test-First for Ledger and Look-Through, cumulative_factor (+15 more)

### Community 8 - "Concentration, Liquidity & Brinson"
Cohesion: 0.10
Nodes (21): concentration(), Descriptive Framing for Concentration Output, filter_scope(), fund_marginal_contribution Table, marginal_contribution(), portfolio_concentration Table, Synthetic Issuers, adv_20d() (+13 more)

### Community 9 - "Overlap, Correlation & Factors"
Cohesion: 0.10
Nodes (22): As-Of Alignment Policy, blended_score (Sort Key Only), Contribution (result type), fund_overlap Table, fund_redundancy Table, Issuer as the Exposure Unit, pairwise_overlap(), portfolio_duplication Table (+14 more)

### Community 10 - "Stress Testing & Factor Exposure"
Cohesion: 0.14
Nodes (15): Exposure (result type), Built-In Stress Scenarios, factor_exposure_holdings Table, factor_shock(), historical_replay(), holdings_factor_exposure(), Point-in-Time Fundamentals Requirement, security_fundamental Table (blocked) (+7 more)

### Community 11 - "Reconciliation & Peer Ranking"
Cohesion: 0.22
Nodes (11): diagnose (diagnostic ladder), reconcile (units + value), reconciliation Table, assemble_caveats, build_peer_group, coverage_pct Disclosure Requirement, DisclosureQuality, peer_group Table (+3 more)

### Community 12 - "Manager Careers & Turnover"
Cohesion: 0.22
Nodes (11): can_merge_manager_identity, Co-Management Attribution Ambiguity, compute_turnover, FundDataProvider, manager_career, manager_career Table, manager_performance, manager_scheme_performance Table (+3 more)

### Community 13 - "Disclosure Coverage Validation"
Cohesion: 0.22
Nodes (10): CheckResult, compute_coverage, coverage_stat Table, holding_disclosure Table, promote_or_quarantine, S3 AMFI AUM Disclosure, scheme_aum Table, unresolved_mv_pct Quality Metric (+2 more)

### Community 14 - "Position Context Handoff"
Cohesion: 0.25
Nodes (9): context_hash, Decimal Storage Convention, emit_position_contexts, M1 Portfolio Ledger, position_context Table, Zone B (personal.db, SQLCipher), M2 Fund X-Ray, position_xray Table (+1 more)

### Community 15 - "Return Windows & Manager Transitions"
Cohesion: 0.22
Nodes (9): amc_manager_stability Table, capture (up/down capture ratios), compute_return_window, manager_transition, manager_transition Table, max_drawdown, scheme_return_window Table, style_shift (style_shift_score) (+1 more)

### Community 16 - "Core Platform Invariants"
Cohesion: 0.25
Nodes (8): Never UPDATE a Fact Row, Decimal for All Money, DECISIONS.md, MF Look-Through Platform, PROGRESS.md, Every Derived Table Is Regenerable, L0-L3 Layer Architecture, to_decimal

### Community 17 - "Parser Routing & Schema Drift"
Cohesion: 0.32
Nodes (8): Raise, Don't Clamp, detect_drift (Schema Drift Detection), Parser Golden Fixtures, Parser Protocol, ParseResult, ParseWarning, route (Parser Router), sniff()-Based Routing

### Community 18 - "XIRR, TWRR & Benchmarks"
Cohesion: 0.36
Nodes (8): build_cashflows, compute_returns, Timing Effect (XIRR minus TWRR), Transaction Taxonomy, TWRR via Adjusted NAV Ratio, xirr (Newton-Raphson + bisection), benchmark_equivalent, TRI-Only Benchmark Enforcement

### Community 19 - "Regime-Aware X-Ray Windows"
Cohesion: 0.32
Nodes (8): LotSummary, PositionContext, build_position_xray, classify_regime, load_contexts, Regime (SINGLE/SPLIT/NO_TENURE_DATA), Three-Window Model (W_fund/W_manager/W_user), user_returns

### Community 20 - "Style Snapshots & Mcap Basis"
Cohesion: 0.32
Nodes (8): DataQuality, drift_magnitude, fee_drag, Point-in-Time mcap_basis Rule, SchemeCharacteristics, scheme_style_snapshot Table, Shared mcap_basis Requirement for M3, style_snapshot

### Community 21 - "Raw Archive & Replay"
Cohesion: 0.33
Nodes (7): archive(), Restic Backup Policy, L0 Immutable Raw Archive, parser_version, Quarantine Handling (No Auto-Retry), raw_file Table, Replay Procedure

### Community 22 - "Source Config & Scraping Hygiene"
Cohesion: 0.33
Nodes (7): As-Of Date Detection Order, DomainRateLimiter, S5 AMC Portfolio Disclosure, Scraping Hygiene Rules, should_poll_portfolios (Publication Window), source_config_snapshot Table, config/sources.yaml

### Community 23 - "Fetcher Protocol & Job Scheduling"
Cohesion: 0.29
Nodes (7): discover_disclosure_links, FetchCandidate, FetchResult, Job Contract, job_run Table, SourceFetcher Protocol, systemd Timers with Persistent=true

### Community 24 - "Market Value Units Trap"
Cohesion: 0.40
Nodes (5): classify_row, staged_holding Table, to_inr, UNIT_MULTIPLIER, Market Value Units Trap (100x Error Path)

### Community 25 - "Regime Segmentation Gate"
Cohesion: 0.50
Nodes (4): apply_gate (V0 reconciliation gate), confidence_from_obs (confidence tiers), position_regime_segment Table, segment_position

### Community 26 - "TRI Index Levels"
Cohesion: 0.67
Nodes (3): Benchmarks Must Be TRI, index_level Table, S12 Index Levels (TRI)

### Community 27 - "Rolling Returns"
Cohesion: 0.67
Nodes (3): rolling_returns, scheme_rolling_return Table, scheme_rolling_summary Table

## Ambiguous Edges - Review These
- `L0-L3 Layer Architecture` → `Scraping Hygiene Rules`  [AMBIGUOUS]
  MODULE_0.md · relation: conceptually_related_to
- `As-Of Date Detection Order` → `Validation Gates V1-V14`  [AMBIGUOUS]
  MODULE_0.md · relation: conceptually_related_to
- `portfolio_tilt Table` → `policy_benchmark`  [AMBIGUOUS]
  MODULE_4.md · relation: conceptually_related_to
- `flows_ready Coverage Gate` → `Four Render States (ok|empty|suppressed|error)`  [AMBIGUOUS]
  MODULE_6.md · relation: conceptually_related_to

## Knowledge Gaps
- **77 isolated node(s):** `DECISIONS.md`, `PROGRESS.md`, `One-Way Module Dependency Chain`, `L3 Derived Materialised Layer`, `SchemeRef` (+72 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **4 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `L0-L3 Layer Architecture` and `Scraping Hygiene Rules`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `As-Of Date Detection Order` and `Validation Gates V1-V14`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `portfolio_tilt Table` and `policy_benchmark`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `flows_ready Coverage Gate` and `Four Render States (ok|empty|suppressed|error)`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `LookThroughProvider` connect `Sector Intelligence Dashboards` to `Concentration, Liquidity & Brinson`, `Overlap, Correlation & Factors`, `Stress Testing & Factor Exposure`, `Look-Through Aggregation`?**
  _High betweenness centrality (0.372) - this node is a cross-community bridge._
- **Why does `lookthrough_sankey` connect `Sector Intelligence Dashboards` to `Style Snapshots & Mcap Basis`?**
  _High betweenness centrality (0.364) - this node is a cross-community bridge._
- **Why does `SchemeCharacteristics` connect `Style Snapshots & Mcap Basis` to `Sector Intelligence Dashboards`?**
  _High betweenness centrality (0.342) - this node is a cross-community bridge._