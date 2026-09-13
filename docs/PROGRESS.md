# Progress

Where the project actually is. Numbers here are measured from the warehouse and
the test suite, not remembered — if one looks stale it is, and it should be
re-measured rather than trusted.

**Last updated:** 2026-09-13 · **HEAD:** `b9b77be` · 66 ADRs · 945 tests passing

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

The gap between 183 schemes and 991 ISINs is V1-37: a disclosure describes a
*scheme*, and every share class of that scheme — Direct, Regular, Growth, each
IDCW variant — holds the identical portfolio.

**All three reference funds are covered:**

| | rows | unresolved |
|---|---|---|
| HDFC Flexi Cap | 83 | 0.35% |
| Kotak Pioneer | 55 | 3.28% |
| ICICI Multi Asset | 290 | 4.20% |

Disclosure quality across all 183: **90 `ok`, 93 `warn`, 0 quarantined.** Every
`warn` is V3 (unresolved above 2%) or V8 (a negative value on something not
classified as a derivative). No parse is being stored that disagrees with the
file it came from.

## How to add a fund

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

**Fetching is the manual step and will stay one for most houses.** AMFI
publishes a directory of every AMC's disclosure page but hosts none of the
files, each AMC renders its file list its own way, and Kotak answers a portfolio
request with a CAPTCHA this project will not solve (V1-32). PPFAS and HDFC fetch
automatically; the rest are a download.

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

1. **Entity-master coverage stops at listed equity.** The AMFI market-cap list
   is the only issuer source, so unlisted companies (Reliance Retail Ventures,
   SIDBI), foreign listings (Alphabet, Microsoft — 18.4% of PPFAS Flexi Cap) and
   securitisation trusts stay `__UNRESOLVED__`. This is now the single largest
   cause of unresolved value.
2. **State development loans have no issuer.** 1,553 Cr in one fund. §8.4
   forbids bucketing them with sovereign paper because a state is a real
   borrower; giving them real issuers needs either a hardcoded state-code table
   (data this project would be inventing) or a cascade that creates issuers
   (which V1-02 deliberately refused). A decision, not an implementation.
3. **Covered calls classify as `equity`.** 44 rows in ICICI Multi Asset with
   negative market values. A written option is a derivative; this is why that
   fund reports `warn`.
4. **`checks.py:96` claims V3 blocks the look-through. Nothing does.** No module
   reads `validation_status`. Either the block should exist or the comment
   should not claim it.
5. **A fund inside a fund is not looked through.** ICICI's Gold ETF and PPFAS's
   overseas holdings resolve to `__MFUNIT__` — correctly disclosed, not
   analysed. That is §10's nested look-through, V2 work.
6. **`data_only=True` returns None for a workbook Excel never cached.** Would
   reproduce V1-36's silent row-drop. Not observed; worth a loud check when a
   file of that shape appears.

## Next

The coverage machinery is done. What is left is not machinery:

- **Entity master beyond listed equity** — the highest-value fix, and the one
  that would take the two `warn` funds under V3's 2% threshold.
- **Load the AMCs actually held**, now a download each.
- **V2**: M2 fund x-ray, the tax engine, §10 nested look-through.

## Reading this repository

`docs/DECISIONS.md` is the useful file. 66 append-only records of every
departure from the spec, every defect the specs themselves contained, and what
was decided instead — including the ones that were wrong and were corrected
later. `PLAN.md` has the slices and their acceptance gates; `CLAUDE.md` has the
ten invariants everything else defers to.
