---
paths:
  - "src/m3_lookthrough/**/*.py"
  - "tests/**/test_*lookthrough*.py"
---

# Look-through rules (M3)

Full spec: `docs/MODULE_3.md`.

- **Closure is asserted at runtime**, not only in tests:
  `Σ exposures == Σ position values + direct`, ±₹1.
- **Aggregate on `pct_normalised`**, never raw `pct_to_nav` (sums to 98–102%).
- **Join on `issuer_id`, never `isin`.** Reliance equity and a Reliance NCD are one
  exposure.
- **Synthetics excluded from concentration denominators**, included in exposure.
- **`ClassificationBasis` is a required argument.** No default.
- **One `mcap_basis` across all schemes** in a single aggregation.
- **Portfolio XIRR comes from M1 cashflows.** Never average per-scheme XIRRs.
- FoF recursion: depth 2, cycle-guarded, `disclosed` weights for underlying schemes.
- Result ordering: descending exposure, tie-break `issuer_id`. Determinism matters for
  M6's cache.
