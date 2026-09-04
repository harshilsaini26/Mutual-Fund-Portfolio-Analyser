# Slice Zero fixtures

One small, coherent portfolio shared by every `Fake*` provider. The schemes M1
resolves, the holdings M3 dissolves and the prices M4 reads all describe the
same three funds, so the fakes cannot disagree with each other.

Deliberately included, because these are the cases that break naive code:

- **`__UNRESOLVED__`** in `AXIS-MID`, at 4.2% — above the 2% caveat threshold
  (`PLAN.md` §7 V1 gate, `MODULE_3.md` §5.3).
- **`__NO_DISCLOSURE__`** — `SBI-SC` is held but quarantined, so its full
  position value must land here rather than vanishing.
- **`__CASH__` / `__DERIV__`** synthetics, which belong in exposure but not in
  any concentration denominator (`MODULE_3.md` §8.2).
- **A shared issuer** (`ISS-HDFCBANK`) held by two funds *and* directly, which
  is what pairwise overlap and combined exposure are for.
- **Direct and Regular plans of the same fund** (`HDFC-TOP100-DIR` /
  `-REG`), the ~1%/year silent error if ever conflated.
- **A merged scheme** (`FRANKLIN-BLUE` into `HDFC-TOP100-DIR`) with a non-unit
  swap ratio, so lot carry-forward has something real to follow.
- **NAVs with 6-decimal precision** that no float can hold exactly.

Every numeric value is a decimal string or an unquoted decimal; `DecimalSafeLoader`
resolves both to `Decimal`. There are no floats anywhere in this directory, and
`tests/unit/test_fixtures.py` asserts it.
