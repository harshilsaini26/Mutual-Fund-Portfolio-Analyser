# Progress

Where the project actually is. Numbers here are measured from the warehouse and
the test suite, not remembered — if one looks stale it is, and it should be
re-measured rather than trusted.

**Last updated:** 2026-09-23 · 1,415 tests passing

> This file was deleted in `0bd425b` when the repository was published, and
> restored on request. It is public now, so it says what the project does and
> does not do — it is not a session log.

---

## Coverage

| | |
|---|---|
| Schemes in the AMFI universe | 19,598 |
| Schemes with a loaded portfolio | **192** |
| **ISINs a look-through can answer for** | **1,050** |
| Holding rows | 10,976 |
| AMC formats with a parser | 5 — HDFC, ICICI, Kotak, Nippon, PPFAS |
| **AMCs that fetch themselves** | **2** — Kotak, ICICI |
| **Schemes reachable without one** | **1,973**, via the coverage tier |

The gap between 192 schemes and 1,050 ISINs is V1-37: a disclosure describes a
*scheme*, and every share class of that scheme — Direct, Regular, Growth, each
IDCW variant — holds the identical portfolio.

**Fund analytics:**

| | |
|---|---|
| Schemes with a benchmark index | **1,819** — 129 indices, from fund names and from disclosures |
| Benchmark indices with a total-return series | 93, 348,174 daily levels from 2011 |
| **Schemes with alpha, beta, tracking error and capture** | **1,220**, 163 of them active funds |
| Risk-free rate | every 91-day T-bill auction, 2011-04-06 to 2026-04-08 (772) |

**All three reference funds are covered:**

| | rows | unresolved |
|---|---|---|
| HDFC Flexi Cap | 83 | **0.00%** |
| Kotak Pioneer | 55 | **0.00%** |
| ICICI Multi Asset | 290 | 4.20% |

PPFAS Flexi Cap, the fifth fund with a disclosure, is also at **0.00%**.

Disclosure quality across all 192: **109 `ok`, 83 `warn`, 0 quarantined.** Most
warnings are V8 (a negative value on something not classified as a derivative —
the covered-call defect below). No parse is being stored that disagrees with
the file it came from.

Unresolved across the whole warehouse is **2.30%** of value, measured over each
scheme's newest disclosure.

## How to add a fund

**Two tiers, and which one you get depends on the fund house.**

### The coverage tier — any fund, one command

```bash
python -m jobs.fetch_groww --slug <groww-slug> --dry-run   # check first
python -m jobs.fetch_groww --slug <groww-slug>
```

Groww's scheme page is server-rendered and carries the whole portfolio, so this
reaches any of 1,973 funds with no download and no browser. Find the slug in
`https://groww.in/mf-sitemap.xml` — **it is not derivable from the fund's name**,
because Groww keeps whatever the fund was called before it was renamed.

Where an AMC parser already covers a scheme, its workbook stays the source of
record: `m3_lookthrough.weights.latest_disclosure` prefers it over a newer page
while it is inside the 45-day staleness threshold, so the coverage tier fills
gaps rather than displacing better data (V1-46).

It pays for that reach with the ISIN column, which the page does not have:
**8.96% of HDFC Flexi Cap unresolved against 0.00% from the AMC's own file**,
and 22.63% of PPFAS, which holds foreign equity and certificates of deposit that
only an ISIN resolves. Every such disclosure is stamped `source_tier =
'aggregator'`, and a scheme the AMC tier already covers is skipped rather than
downgraded.

### The AMC tier — the five houses with a parser

```bash
# 1. find your fund house in config/amc_disclosure_index.yaml (all 52 are there)
# 2. download its monthly portfolio workbook
# 3. drop it in data/inbox/ and:
python -m jobs.ingest_inbox
```

It works out which house published the file, which scheme each sheet describes,
and loads all of them. 88 schemes from one Kotak workbook, 91 from one Nippon,
about 70 seconds each. A sheet it cannot identify with certainty is reported and
skipped, never guessed at.

**Kotak and ICICI no longer need the download.** V1-03 said discovery needed a
browser since every AMC page is JavaScript-rendered; that was the wrong
conclusion — a JS front-end implies a JSON backend, and the backend is neither
authenticated nor challenged:

```bash
python -m jobs.fetch_amc --amc kotak --list        # what is published, fetch nothing
python -m jobs.fetch_amc --amc kotak --period 2026-08 --kind fortnightly
python -m jobs.ingest_inbox
```

That is the whole loop, with no hand-download and no CAPTCHA — Kotak's own
workbook, whose website dropdown answers a portfolio request with a Radware
challenge this project will not solve (V1-32). The CAPTCHA guards the page, not
the data. **21 schemes loaded from a file nobody touched.**

Those listings are also archives, which the coverage tier is not: Kotak's runs
to **April 2013**, 70 monthly disclosures, so a fund loaded this way can be
backfilled. ICICI publishes one 25 MB ZIP of ~146 workbooks, and
`jobs.ingest_inbox` now expands a ZIP into its workbooks itself — flattened,
guarded against a member path escaping the inbox, never overwriting a file
already there. That path is tested but has not yet been run against a live
ICICI file.

The other 50 AMCs are one adapter each. The house you hold is the one worth
writing, and everything else has the coverage tier above.

## Keeping the units check honest

```bash
python -m jobs.fetch_aum --list --years 4    # what AMFI has published
python -m jobs.fetch_aum                     # load the newest quarter
python -m jobs.fetch_aum --quarter 2026-03-31
```

§10's V2 reconciles a disclosure's summed market value against an AUM from
**outside the file being checked**, and quarantines when they disagree — it is
what catches a 100x unit error. It had never run: `scheme_aum` did not exist.

AMFI's scheme-wise average AUM fills it, joined on `AMFI_Code` with no name
matching, **12,388 rows** covering 99.5% of the schemes that have a disclosure.
It is a quarterly average rather than a month-end balance, so it sits a few
percent from a portfolio through ordinary market movement; `basis` travels with
the figure and V2 widens its tolerance to match. A 100x error still fails it by
11,321%.

## Benchmarks

```bash
python -m jobs.fetch_index --catalogue      # NSE's 259 indices, rarely changes
python -m jobs.fetch_index --resolve        # index funds and ETFs, from their names
python -m jobs.fetch_index --declared       # active funds, from their own disclosures
python -m jobs.fetch_index --held           # total-return levels for every one in use
python -m scripts.show_fund_xray --scheme INF179K01UT0
```

The same figures are on the fund page, `/view/fund_xray_header?scope_id=<ISIN>`,
linked from every scheme in Holdings. The page and the script both call M2's
`fund_windows`, so they cannot disagree; checked against HDFC Flexi Cap, they
match to the digit (5y alpha 8.42%, beta 0.85).

A benchmark comparison needs two things: which index a fund is measured
against, and that index's total-return series. The first comes from a fund's
name where the name carries it and from the fund's own monthly disclosure where
it does not; where both answer, they have agreed on every one of 275 share
classes. The second comes from NSE, one request per index per year, and a
refresh fetches only what it does not already have.

Only total-return series are used. A price index leaves out dividends, so a
fund compared against one looks better than it is by roughly the index's
dividend yield — about 1.3% a year on the Nifty 50.

## What is stale

```bash
python -m jobs.status            # offline, instant
python -m jobs.status --check    # also ask the AMCs that can be asked
```

Per fund house: how many schemes are behind, by how many months, and the exact
command that would fix it — `jobs.fetch_amc` where a discovery adapter exists,
the AMC's own page where one does not. SEBI allows ten days after the month end,
so nothing is called late before then.

The unit is the **scheme**, not the house. Kotak's August file carried 21 of its
96 schemes, and a report keyed on the house's newest disclosure called that
"current" — an error that grows more confident the larger the fund house is.

## Modules

| | |
|---|---|
| **M0 data** | built — fetch, parse, resolve, validate, load |
| **M1 ledger** | built — CAS parsing, FIFO lots, XIRR/TWRR, reconciliation |
| **M3 look-through** | built — exposure, overlap, concentration, duplication, nested funds, marginal contribution |
| **M6 views** | built — nine views (eight portfolio, one fund), CSV export, loopback API |
| M2 fund x-ray | partly built — return windows, risk statistics, rolling returns, Sharpe and Sortino, and alpha, beta, tracking error and capture against a total-return index |
| M4 risk | specified, not built |
| M5 market | specified, not built |
| Tax engine | not built; rates live in a human-verified config and are never invented |

M2 was rebuilt on 2026-09-16 as two files and no SQL, reading NAV through the
`MarketDataProvider` that already existed. Its risk-free rate (S13) and its
index series (S12) have since landed, so the benchmark- and rate-dependent
statistics compute wherever that data reaches — the S13 and S12 entries under
the defects below say how far that is. The rest of its spec — the three-window
model, peer ranks, the manager dossier, turnover — is blocked on data rather
than effort.

M4 and M5 still have no code. All three previously had a package each —
Protocols plus a fake per protocol, 2,416 lines — whose only importers were the
two tests that checked each contract against its own fake. They were deleted on
2026-09-16. Their
specifications are unchanged, kept with the other specs outside the public
repository, which is where an unbuilt module belongs: a Protocol with one
implementation, and that implementation a test double, is a placeholder with a
type annotation, and it cost a compile, a typecheck and a lint on every commit
to keep.

## What is not true yet

- **Nobody has used this.** Including its author. Every figure carries its own
  as-of date, staleness and coverage precisely so you can judge how far to trust
  it, and the answer for now is "not with money that matters".
- **47 of 52 AMCs have no disclosure loaded.** The machinery to load one is
  built; the files have not been downloaded.
- **Two of the eleven specified views are absent**, deliberately — sector
  tilt and the holdings treemap need M5's sector taxonomy and company data,
  and a view that always renders empty is a broken feature pretending to be a
  data problem. The fund page, marginal contribution and the size profile
  landed on 2026-09-23.
- **Nothing runs end to end against a real ledger in CI.** Zone B needs a key,
  and the golden-file verifier covers the arithmetic instead.

## Known defects, measured and unfixed

Ordered by what they cost.

0. ~~**A unit re-denomination read as a 900% gain.**~~ **Closed 2026-09-18.**
   55 schemes carried a 10:1 or 100:1 split, clustered on three dates. Nothing
   divided it out, so ICICI Prudential Overnight Fund — a fund that cannot
   move 1% in a day — reported 14.8x over seven years, and every volatility,
   drawdown and Sharpe built on those series was garbage.

   `rescale_splits` brings a series onto one scale before anything reads it,
   restricted to the ratios AMCs actually use. ICICI Overnight now reports
   **5.18%/yr against Kotak Overnight's 5.16%** — two funds that were never
   supposed to disagree. Discontinuities in `nav_adj` went **56 to 1**.

   The one left is `INF174KA1DB4`, which drops 10.0727 to 0.0001 in a single
   row. That is a clean power of ten and is NOT a split; it is a dying fund's
   last row, and it stays visible as the defect it is.

0. **`scheme_idcw` is empty; most IDCW returns are now derived instead.**
   9,187 of 19,598 schemes are `idcw_payout` or `idcw_reinvest` and the table
   holds **0 rows**. `build_nav_adj` writes `nav_adj = nav` when a scheme has no
   events, so the column is fully populated and identical to raw NAV — a
   total-return series in name only. Any return computed from it is short by the
   whole distributed amount; for a daily-IDCW plan that is the entire return,
   which is how a liquid fund reports 0.00%.

   **Largely solved 2026-09-18, with no fetcher.** A Growth option and an IDCW
   option of one plan hold ONE portfolio at one TER, so the Growth series is
   already a record of what the IDCW plan earned. `build_nav_adj` derives from
   it when no declarations exist: `nav_adj(t) = nav(anchor) * G(t)/G(anchor)`.
   Verified on Kotak Liquid Daily-IDCW, whose full-series return went from
   **4.97% on raw NAV to 64.81% adjusted**.

   Scope, stated honestly. 4,468 of the 4,595 IDCW schemes with NAV have such
   a sibling, but only **604** have more than one NAV row of their own — the
   rest are a single point, where no return exists to correct. Of those 604,
   **522** now carry a real total-return series. The remainder follow
   automatically as NAV history backfills; the derivation is already in place.
   A real **S14** is still what the 127 schemes with no Growth sibling need.

   Of the three that bounded M2, all three now produce numbers — one of them
   only partly:

   - **S14** IDCW — solved without a fetcher, from the Growth sibling. Above.
   - **S13** risk-free rate — **closed.** `rbi.org.in` answers an automated
     client with HTTP 418, and nothing here defeats a bot check; but RBI's
     statistics portal, `dbie.rbihub.in`, serves the same auction table to an
     ordinary request. `config/risk_free.yaml` holds all **772** 91-day T-bill
     auction yields from 2011-04-06 to 2026-04-08 — every auction, because a
     quarterly sample is out by up to 4.50 percentage points at a window
     start: the cut-off went from 7.24% to 12.02% inside Q3 2013. Sharpe and
     Sortino compute for any window starting on or after 2011-04-06.
   - **S12** index levels (TRI) — **closed for everything NSE publishes.**
     `benchmark_id` is set on **1,819** of 19,598 schemes (1,389 with NAV),
     pointing at 129 indices, and **93 of those have a total-return series
     loaded** — 348,174 levels from 2011. Alpha, beta, tracking error and
     capture compute today for **1,220 schemes, 163 of them active funds.**

     The mapping comes two ways. `--resolve` reads an index out of a fund's
     name, which reaches index funds and ETFs. `--declared` reads the
     benchmark an active fund's own disclosure states — Kotak, Nippon, HDFC
     and PPFAS all print one — which reaches funds whose names do not carry
     it. Where both gave an answer, on 275 share classes, they agreed every
     time.

     Two funds show the result has the right shape. ICICI Prudential Nifty 50
     Index Fund Direct, over 1, 3 and 5 years: beta 1.00, tracking error
     0.03–0.05%, alpha −0.23% to −0.29% — the index minus a fee, which is the
     only shape an index fund's alpha can take. HDFC Flexi Cap against the
     NIFTY 500 TRI its disclosure names: beta 0.80–0.86, tracking error around
     4–5%, and alpha of 5.70% over 3 years and 8.42% over 5 — an active fund
     that has earned its fee.

     Three ceilings are not effort problems. NSE publishes no total-return
     series for its G-Sec, SDL or arbitrage indices — 36 of the 129. Over
     half of all schemes are debt funds, benchmarked mostly to CRISIL
     indices, which are not published free. And declared benchmarks exist
     only for the five fund houses with a disclosure loaded.

     One limitation is a modelling choice rather than a gap. A scheme has one
     current benchmark, and a 5-year alpha is measured against it for the
     whole window. SEBI moved many benchmarks in 2021, so for a fund that
     changed, the pre-2021 part of a long window is compared against an
     index it was not then measured against.

0. ~~**For 54 of NSE's 259 indices, the catalogue and the level series mint
   different ids.**~~ **Closed 2026-09-23.** `index_id_for` keeps a trailing
   "Index" that `index_key` strips, so a level series whose name left the word
   off minted a second id that no scheme pointed at. The catalogue now mints an
   id once, and a level series attaches to the registered row by key; a key
   matching two rows raises. It never fired: every benchmarked index with such
   a name is a debt index, and NSE publishes no TRI for those. Only a manual
   `--backfill` of one could have reached it.

0. **`rebuild_weights` commits, so it cannot compose into a caller's
   transaction.** A job that loaded holdings and then rebuilt weights would
   have its partial work committed by a library it called.
   `m3_lookthrough/persist.py` is the module that owns M3's other commits.
0. **The rebuild re-reads the 9,143 rows it just wrote.** `materialise_weights`
   holds the weights and classes and discards them; `load_issuer_weights` then
   issues 192 queries for the same data — 24ms of a 195ms rebuild. Returning
   them is a behaviour change: a scheme whose holdings vanish currently keeps
   surfacing its stale rows, because `materialise_weights` returns 0 without
   deleting.

0. ~~**Most debt holdings are classed as `equity`.**~~ **Closed 2026-09-23.**
   18.0% of all equity-classed weight sat on bond, CP and CD ISINs, so every
   equity-scoped figure (concentration, `overlap_equity_pct`, marginal HHI, the
   size profile) counted debt as equity. Two causes in the shared reader: debt
   headings it did not know (`Certificate of Deposit`, `Commercial Paper`,
   `Non Convertible Debentures`) fell back to equity, and Kotak's listing-status
   heading (`Listed/Awaiting listing…`) replaced the instrument heading above it
   and read as equity. Fixed at parse time (reader 5), plus one precedence rule:
   a row whose own name resolved to cash is cash under a borrowed heading.
   All 204 AMC disclosures re-derived as new revisions: 2,759 rows moved
   (2,491 to debt, 178 to cash), no equity share (`INE…01`) left equity, and
   the debt share of equity weight fell to 0.22% — Kotak's REITs, which Kotak
   itself files under equity. The aggregator page re-derives on its next fetch.
0. **`scheme_aum` retracts by DELETE, because it has no revision.** V1-54 scoped the
   delete to one quarter, but invariant 2 forbids even an `UPDATE` of a fact row and
   this table deletes them. The fix is `revision`/`is_current` as every other fact
   table has, which moves the primary key and touches `aum_for`, the
   `INSERT OR REPLACE` and the restatement counter. A slice, not a patch.

1. **State development loans have no issuer.** 1,553 Cr in one fund. §8.4
   forbids bucketing them with sovereign paper because a state is a real
   borrower; giving them real issuers needs either a hardcoded state-code table
   (data this project would be inventing) or a cascade that creates issuers
   (which V1-02 deliberately refused). A decision, not an implementation.
2. **Covered calls classify as `equity`.** 44 rows in ICICI Multi Asset with
   negative market values. A written option is a derivative; this is why that
   fund reports `warn`.
3. ~~**`checks.py:96` claims V3 blocks the look-through. Nothing does.**~~
   **Closed 2026-09-23.** A quarantine now blocks, as MODULE_3 §5.4 specifies:
   `latest_disclosure` never picks a quarantined disclosure, so a scheme uses
   its last one that passed or, with none, shows as `__NO_DISCLOSURE__` with a
   caveat naming it (V1-66). A warning — V3 among them — does not block, and
   the comment now says so.
4. ~~**A fund inside a fund is not looked through.**~~ **Closed 2026-09-18.**
   §6's recursion is built: a `__MFUNIT__` holding expands into the issuers of
   the fund it names, depth-capped at 2 and cycle-guarded. The opaque bucket
   went from 6.15% of an illustrative portfolio to 1.84%, closure unchanged.
   What remains bucketed is honest: 17 of the 53 funds held as units have no
   disclosure of their own, and a unit staged without an ISIN (the aggregator
   tier) has nothing to resolve against.
5. ~~**204 disclosures predate the AUM witness.**~~ **Closed 2026-09-23.** The
   reader-5 re-derive gave every AMC disclosure a new revision, and V2 ran on
   all of them. 17 fail it — 13 Kotak, 4 Nippon, portfolio totals 25% to 640%
   off AMFI's quarterly-average AUM — and are now `quarantined`, which since
   defect 3 closed means excluded: 16 schemes have no usable disclosure.
6. **Four latent defects in the fetch and status layers.** `extra_headers` can
   override the User-Agent the robots check used; the retry loop replays POSTs;
   `jobs/status.py:standings` picks a parser from an unordered set when a house
   has two; `_index`'s cache outlives a config change. None is reachable today
   (V1-48).
7. **`data_only=True` returns None for a workbook Excel never cached.** Would
   reproduce V1-36's silent row-drop. Not observed; worth a loud check when a
   file of that shape appears.
8. ~~**Three latent defects in S12's benchmark path.**~~ **Closed 2026-09-23.**
   Index levels are now refused when not positive, as NAVs are. A window with
   too little overlap keeps its `benchmark_id`, so "not enough data" no longer
   reads as "no benchmark". And a year that returns no levels is marked parsed
   rather than left `pending` — as is the index catalogue, a path the first
   fix missed and the warehouse, checked afterwards, showed up.
9. ~~**Every re-run of the S12 backfill re-archives what it already has.**~~
   **Closed 2026-09-23.** NSE stamps each response with a per-request
   `RequestNumber`, so the same year hashes differently on every fetch and
   §3.1's content-addressed archive never recognised a repeat: one full re-run
   stored 1,369 duplicate files, 47 MB. A run now skips every year before an
   index's latest loaded one — those were fetched after they ended — and
   re-fetches only that latest year, which may be partial, and anything later.
   Checked live: Nifty 50 went from 16 requests to 1, and the archive grew by
   one file of new data. `--full` still re-fetches everything, to pick up a
   restated level.

   A refresh of all 129 benchmarked indices now costs about 724 requests
   instead of 2,064. This entry used to estimate 250, and that was wrong: 576
   of the 724 go to the 36 indices NSE publishes no series for, which look
   unfetched every time because nothing records "asked, got nothing". They add
   nothing to the archive — an empty answer deduplicates — so what is left is
   requests, not duplication. Removing it needs that record, which is a schema
   change.

## Next

The coverage machinery is done. What is left is mostly not machinery:

- **Stop V2 quarantining young funds.** Quarantine now excludes a
  disclosure, and several of the 16 schemes it excludes look like the check's
  own false positives: a fund launched inside or just before the quarter AMFI
  averaged is compared, at month-end, with an average that understates it
  (Kotak Nifty Alpha Low Volatility 30 first priced 8 days before the quarter
  closed: +641%). The established funds 80-177% off (Kotak Banking and PSU
  Debt, Infrastructure & Economic Reform, Dynamic Term) look like real
  mismatches and should stay out.
- **Remember which indices have no series**, so a refresh stops asking NSE
  for them — 576 of its 724 requests. Needs a small schema change.
- **More discovery adapters**, one per house, as funds are actually held.
  Each also brings that house's declared benchmarks: run
  `python -m jobs.fetch_index --declared` after loading its disclosures.
- **Run ICICI's ZIP end to end** — the expansion is built and tested, never
  run against a live file.
- **The tax engine, M4 risk and M5 market.**

## Reading this repository

`docs/CLAUDE.md` has the ten invariants everything else defers to, and
`docs/ledger.md` and `docs/lookthrough.md` the extra rules for the two modules
that carry the correctness gates. The module specifications, the build plan and
the decision log are kept out of the public repository; the reasoning behind a
change travels in its commit message instead, which is why those run long —
including the ones that record a mistake and its correction.
