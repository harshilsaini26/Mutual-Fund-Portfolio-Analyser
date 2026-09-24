# Slice Zero fixtures

The scheme master, NAVs, TER, benchmarks and mergers `FakeMarketDataProvider`
reads, so M1's tests run with no database.

Deliberately included, because these are the cases that break naive code:

- **Direct and Regular plans of the same fund** (`HDFC-TOP100-DIR` /
  `-REG`), the ~1%/year silent error if ever conflated.
- **A merged scheme** (`FRANKLIN-BLUE` into `HDFC-TOP100-DIR`) with a non-unit
  swap ratio, so lot carry-forward has something real to follow.
- **NAVs with 6-decimal precision** that no float can hold exactly.

Every numeric value is a decimal string; `DecimalSafeLoader` resolves it to
`Decimal`, and `tests/unit/test_fakes.py` asserts no float can enter.
