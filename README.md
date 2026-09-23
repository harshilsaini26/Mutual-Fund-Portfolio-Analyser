# Mutual Fund Portfolio Analyser

[![gate](https://github.com/harshilsaini26/Mutual-Fund-Portfolio-Analyser/actions/workflows/ci.yml/badge.svg)](https://github.com/harshilsaini26/Mutual-Fund-Portfolio-Analyser/actions/workflows/ci.yml)
[![licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

**See what your mutual funds actually own.**

A self-hosted analytics workbench for Indian mutual fund investors. It looks through
the funds you hold to the companies underneath, measures how much of your money is the
same stock bought several times over, and judges each fund against the index it is
supposed to beat — on your own machine, with your transactions encrypted and never sent
anywhere.

---

## The problem

Five equity funds look like diversification. Opened up, they are often one portfolio
bought five times — the same large caps in slightly different proportions, with five
sets of fees. No factsheet shows this, because a factsheet describes one fund and
overlap is a property of the *set*.

And the numbers a fund reports about itself are hard to put side by side. Returns come
over different periods in different documents, the benchmark is named in a scheme
document few people read, and whether a fund manager earned the fee is rarely answered
in the same place as what the fund costs.

## What it answers

**How much of my portfolio is the same money twice?** From two real disclosures:

```
HDFC Flexi Cap  x  Nippon India Growth Mid Cap
    overlap        13.32%          17 shared companies of 156
    duplicated     6.66% of the portfolio held through more than one fund
```

**Did the fund earn its fee?** HDFC Flexi Cap against the NIFTY 500 total-return index
its own disclosure names:

```
  risk-adjusted       sharpe   sortino      rf
  1y                   -0.10     -0.14   5.51%
  3y                    0.80      1.15   6.82%
  5y                    1.13      1.61   3.29%

  vs NSE:NIFTY_500_TRI    bench   beta   t.err   alpha     up   down
  1y                      2.83%   0.86   3.85%   1.04%   0.82   0.89
  3y                     11.67%   0.80   4.66%   5.70%   0.63   0.91
  5y                     10.57%   0.85   4.56%   8.42%   0.63   0.97
```

**Is my index fund doing its one job?** An index fund should track its index and cost
only its fee. ICICI Prudential Nifty 50 Index Fund, over 1, 3 and 5 years: beta 1.00,
tracking error 0.03-0.05%, alpha -0.23% to -0.29% — the index, less a small fee, which
is the only result an index fund should produce.

**What have I actually made?** Every transaction from your consolidated account
statement, lot by lot: XIRR, time-weighted return, realised and unrealised gains, and a
reconciliation against the statement's own closing balance.

---

## Capabilities

| | |
|---|---|
| **Look-through** | Exposure by *company*, not by security, so an issuer's shares and bonds count once. Overlap, duplication, concentration (HHI and effective number of holdings), funds held inside other funds expanded to their own holdings, and the marginal contribution of each fund to the whole. |
| **Fund x-ray** | Returns over 1, 3 and 5 years and since launch; volatility, drawdown and recovery; rolling-return distributions; Sharpe and Sortino against the 91-day T-bill rate in force when each window began; alpha, beta, tracking error, information ratio and up/down capture against a total-return benchmark. |
| **Ledger** | CAS statement import, FIFO lots, §112A grandfathering, XIRR and TWRR, reconciled to the statement's closing units. |
| **Views** | A local browser interface with CSV export. Every chart carries its as-of date, staleness, coverage and unresolved share, in a footer that cannot be switched off. |

### Coverage today

| | |
|---|---|
| Schemes in the AMFI universe | 19,598 |
| Schemes with a loaded portfolio | 192, answering for 1,050 share classes |
| Fund houses read directly from their own files | 5 — HDFC, ICICI, Kotak, Nippon, PPFAS |
| Funds reachable through the aggregator tier | 1,973 |
| Schemes with a benchmark index | 1,819 |
| Schemes with full benchmark analytics | 1,220, 163 of them actively managed |

[`docs/PROGRESS.md`](docs/PROGRESS.md) has the current figures, how each was measured,
and the defects that are known and not yet fixed.

---

## Why the numbers can be trusted

Most of the engineering is not the arithmetic. It is refusing to show a number that
looks right and is not.

- **Exact arithmetic, end to end.** Money, units, NAVs and weights are `Decimal`
  throughout. SQLite silently turns a `DECIMAL` column into a float, sums text as float,
  and sorts `"5000"` above `"25000"`; each of those is guarded, and each was found as a
  defect first.
- **Nothing disappears.** A holding that cannot be identified is shown as unresolved; a
  fund with no published portfolio is shown as undisclosed. Both stay on screen, and the
  unresolved share is a pass/fail criterion, not a log line.
- **Absent is not zero.** A return that cannot be computed shows as a dash, never
  `0.00%`. A fund with too short a history for a statistic gets no statistic.
- **Every file is checked against itself.** Each disclosure is reconciled against the
  total the fund house printed in it, and against AMFI's independent AUM figure; a 100x
  unit error fails both.
- **Benchmarks are total-return only.** A price index leaves out dividends and flatters
  every fund compared against it by about 1.3% a year on the Nifty 50. Price series are
  never used for alpha.
- **Raise, don't round.** A redemption larger than the units held means a missing
  statement, and the ledger stops rather than guessing.

The ledger and the look-through engine are **mutation tested** — deliberate defects are
injected and every one must be caught (26 of 26 and 35 of 35). A separate verifier
recomputes the reference portfolio without importing any of the code it checks.

---

## Getting started

Python 3.11+, and SQLCipher for the encrypted ledger: `apt install libsqlcipher-dev` on
Debian/Ubuntu, `brew install sqlcipher` on macOS.

```bash
git clone https://github.com/harshilsaini26/Mutual-Fund-Portfolio-Analyser.git
cd Mutual-Fund-Portfolio-Analyser
python -m venv .venv && source .venv/bin/activate    # Windows: .venv/Scripts/activate
pip install -e ".[dev,cas]" -c requirements.lock
```

`requirements.lock` pins the whole dependency graph, transitive packages included.

### Load the market data

```bash
export MF_CONTACT_EMAIL=you@example.com         # sent as the From: header on every request

python -m jobs.fetch_nav                        # today's NAVs, every scheme
python -m jobs.build_entity_master              # AMFI's company list -> issuers
python -m jobs.fetch_aum                        # scheme AUM, for the units check
python -m jobs.fetch_index --catalogue --resolve --declared --held   # benchmarks
```

### Add the funds you hold

Kotak and ICICI publish through an interface that can be queried directly:

```bash
python -m jobs.fetch_amc --amc kotak --period 2026-08
python -m jobs.ingest_inbox
```

For any other fund house, download its monthly portfolio workbook into `data/inbox/` and
run `python -m jobs.ingest_inbox`. It identifies the fund house and every scheme in the
file on its own — 88 schemes from one Kotak workbook, 91 from one Nippon — and skips,
with a reason, any sheet it cannot identify with certainty.
[`config/amc_disclosure_index.yaml`](config/amc_disclosure_index.yaml) links the
disclosure page for all 52 AMCs.

For a fund from a house with no reader yet, the aggregator tier reads its public scheme
page instead:

```bash
python -m jobs.fetch_groww --scheme INF179K01UT0
```

It reaches any fund Groww lists, at the cost of an ISIN column, so it resolves fewer
holdings than a fund house's own file; that file is always preferred while it is current.

### Import your statement and look

```bash
python -m jobs.import_cas --file statement.pdf --user USER-01
python -m jobs.serve                            # browser UI on 127.0.0.1:8765
python -m scripts.show_fund_xray --scheme INF179K01UT0
python -m jobs.status                           # what is stale, and the command that fixes it
```

No statement to hand? `python -m scripts.show_lookthrough --equal 1000000` weights every
disclosed scheme equally to show the shape of the output. It labels itself illustrative
and refuses to save anything.

### Configuration

| Variable | Default | What it does |
|---|---|---|
| `MF_CONTACT_EMAIL` | `unset@example.invalid` | `From:` header on every outbound request |
| `MF_DATA_ROOT` | `./data` | where the warehouse, ledger and raw archive live |
| `MF_WAREHOUSE` | `$MF_DATA_ROOT/warehouse/canonical.db` | a specific warehouse file |
| `MF_LEDGER` | `$MF_DATA_ROOT/ledger/personal.db` | the encrypted personal ledger |
| `MF_CAS_PASSWORD` | unset | the optional real-statement test only; everything else prompts |

---

## Privacy and security

**Your data stays yours.** Transactions, units and folios live in a SQLCipher-encrypted
ledger, created readable only by you, which refuses to open without a key rather than
falling back to plain text. Market data — NAVs, disclosures, indices — is public and kept
separately. There is no account, no telemetry and no server beyond the one you start on
your own machine; the single JavaScript library is vendored with a recorded checksum
rather than loaded from a CDN.

**Polite by construction.** Every request is rate-limited per site, respects
`robots.txt`, identifies its operator, and is archived under its SHA-256 before anything
reads it, so any job can be re-run safely. Nothing here defeats a CAPTCHA or bot check;
where a site presents one, the project uses a published data interface or asks you to
download the file.

**Untrusted input is treated as untrusted.** Disclosure files come from outside, so
names are escaped before they reach a page, spreadsheet formulas are neutralised on
export, archive members cannot write outside their folder, and every response carries a
content security policy. Each of those is a regression test written against a
demonstrated exploit.

---

## Quality gate

```bash
python -m pytest -q                             # 1,341 tests, hermetic, no network
python -m ruff check src/ tests/ scripts/ jobs/
python -m mypy                                  # strict
python -m scripts.verify_v0_ledger --check      # the independent ledger verifier
```

[CI](.github/workflows/ci.yml) runs exactly these on every push.

## Architecture

```
src/m0_data/          fetch, parse, resolve, validate, load     the market warehouse
src/m1_ledger/        statement parsing, lots, returns          your positions
src/m2_fund/          returns, risk, benchmark analytics        fund x-ray
src/m3_lookthrough/   exposure, overlap, concentration          look-through
src/m6_views/         browser UI, export, local API             views
```

Dependencies run one way, and modules talk through typed interfaces rather than
reaching into each other's tables. The view layer computes nothing: a static check
enforces it, because a figure derived in a view is a second answer nobody can
reconcile. [`docs/CLAUDE.md`](docs/CLAUDE.md) sets out the ten invariants the rest of the
code defers to.

---

## Status

A working system at an early stage, published as-is. The data pipeline, ledger,
look-through and views are built; fund analytics are largely built; portfolio risk,
market context and tax calculation are specified and not yet built.

It has not yet been used with real money, including by its author. That is why every
figure it shows carries its as-of date, coverage and staleness: so you can judge how far
to rely on it. It is not investment advice, and it does not give any.

## Built with Claude

The specification, implementation, tests and documentation were written with
[Claude Code](https://claude.com/claude-code). The commit history records that work as
it happened — including the defects the tests caught in freshly written code, and the
estimates that turned out wrong and were corrected.

## Licence

[MIT](LICENSE).
