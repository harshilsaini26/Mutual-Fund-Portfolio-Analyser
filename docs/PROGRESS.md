# Progress

Where the project actually is. Numbers here are measured from the warehouse and
the test suite, not remembered — if one looks stale it is, and it should be
re-measured rather than trusted.

**Last updated:** 2026-09-13 · 1,075 tests passing

> This file was deleted in `0bd425b` when the repository was published, and
> restored on request. It is public now, so it says what the project does and
> does not do — it is not a session log.

---

## Coverage

| | |
|---|---|
| Schemes in the AMFI universe | 19,598 |
| Schemes with a loaded portfolio | **183** |
| **ISINs a look-through can answer for** | **991** |
| Holding rows | 10,300 |
| AMC formats with a parser | 5 — HDFC, ICICI, Kotak, Nippon, PPFAS |
| **AMCs that fetch themselves** | **2** — Kotak, ICICI |
| **Schemes reachable without one** | **1,973**, via the coverage tier |

The gap between 183 schemes and 991 ISINs is V1-37: a disclosure describes a
*scheme*, and every share class of that scheme — Direct, Regular, Growth, each
IDCW variant — holds the identical portfolio.

**All three reference funds are covered:**

| | rows | unresolved |
|---|---|---|
| HDFC Flexi Cap | 83 | **0.00%** |
| Kotak Pioneer | 55 | **0.00%** |
| ICICI Multi Asset | 290 | 1.79% |

PPFAS Flexi Cap, the fifth fund with a disclosure, is also at **0.00%**.

Disclosure quality across all 183: **111 `ok`, 72 `warn`, 0 quarantined.** Most
warnings are V8 (a negative value on something not classified as a derivative —
the covered-call defect below). No parse is being stored that disagrees with
the file it came from.

Unresolved across the whole warehouse is **1.80%** of value, down from 14% at
the start of the day.

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
backfilled. ICICI still publishes one 25 MB ZIP of ~146 workbooks and §6.5's
member staging is not built, so a human still extracts the member.

The other 50 AMCs are one adapter each. The house you hold is the one worth
writing, and everything else has the coverage tier above.

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
| **M3 look-through** | built — exposure, overlap, concentration, duplication |
| **M6 views** | built — six views, CSV export, loopback API |
| M2 fund analytics | contracts only |
| M4 risk | contracts only |
| M5 market | contracts only |
| Tax engine | not built; rates live in a human-verified config and are never invented |

## What is not true yet

- **Nobody has used this.** Including its author. Every figure carries its own
  as-of date, staleness and coverage precisely so you can judge how far to trust
  it, and the answer for now is "not with money that matters".
- **47 of 52 AMCs have no disclosure loaded.** The machinery to load one is
  built; the files have not been downloaded.
- **Five of the eleven specified portfolio views are absent**, deliberately —
  each needs a module that does not exist, and a view that always renders empty
  is a broken feature pretending to be a data problem.
- **Nothing runs end to end against a real ledger in CI.** Zone B needs a key,
  and the golden-file verifier covers the arithmetic instead.

## Known defects, measured and unfixed

Ordered by what they cost.

1. **State development loans have no issuer.** 1,553 Cr in one fund. §8.4
   forbids bucketing them with sovereign paper because a state is a real
   borrower; giving them real issuers needs either a hardcoded state-code table
   (data this project would be inventing) or a cascade that creates issuers
   (which V1-02 deliberately refused). A decision, not an implementation.
2. **Covered calls classify as `equity`.** 44 rows in ICICI Multi Asset with
   negative market values. A written option is a derivative; this is why that
   fund reports `warn`.
3. **`checks.py:96` claims V3 blocks the look-through. Nothing does.** No module
   reads `validation_status`. Either the block should exist or the comment
   should not claim it.
4. **A fund inside a fund is not looked through.** ICICI's Gold ETF and PPFAS's
   overseas holdings resolve to `__MFUNIT__` — correctly disclosed, not
   analysed. That is §10's nested look-through, V2 work.
5. **§10's V2 has never run.** `scheme_aum` is not built, so the units check
   — the 100x guard — records "not evaluated" on all 205 disclosures rather
   than reconciling anything. It is a fetch-and-load slice, not a patch: AMFI
   publishes monthly AAUM per scheme at a templatable URL (surveyed in V1-44).
6. **Four latent defects in the fetch and status layers.** `extra_headers` can
   override the User-Agent the robots check used; the retry loop replays POSTs;
   `jobs/status.py:standings` picks a parser from an unordered set when a house
   has two; `_index`'s cache outlives a config change. None is reachable today
   (V1-48).
7. **`data_only=True` returns None for a workbook Excel never cached.** Would
   reproduce V1-36's silent row-drop. Not observed; worth a loud check when a
   file of that shape appears.

## Next

The coverage machinery is done. What is left is not machinery:

- **More discovery adapters**, one per house, as funds are actually held.
- **§6.5 member staging**, so ICICI's 25 MB ZIP stops needing a human.
- **V2**: M2 fund x-ray, the tax engine, §10 nested look-through.

## Reading this repository

`docs/DECISIONS.md` is the useful file — append-only records of every
departure from the spec, every defect the specs themselves contained, and what
was decided instead — including the ones that were wrong and were corrected
later. `PLAN.md` has the slices and their acceptance gates; `CLAUDE.md` has the
ten invariants everything else defers to.
