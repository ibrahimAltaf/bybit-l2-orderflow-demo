# Tardis Timestamp Reconciliation

Reproduce: `python scripts/verify_timestamp_reconciliation.py`
(stdlib only). Output: `evidence/timestamp_reconciliation.json`.

## Exact fields

| Source | Field | Unit | Precision on this file |
|---|---|---|---|
| Bybit trade CSV | `timestamp` | Unix seconds, decimal text | decimal places: {'0': 2, '1': 30, '2': 276, '3': 1942, '4': 19097} |
| Tardis.dev trades CSV | `timestamp` | integer microseconds | 0 values are not whole milliseconds |

Join: exact trade ID. Bybit values convert to integer microseconds with `Decimal`, no float
and no rounding. Prices and sizes compare as `Decimal` (2787 prices and 0 sizes differ as text only). Side is lower-cased.
Mismatches in price, size or side: 0.

## Result over 21,347 trade pairs

| Measure | Value |
|---|---|
| Raw maximum absolute difference | **900 µs (0.9 ms)** |
| Raw signed range (Bybit − Tardis) | 0 to 900 µs |
| Both floored to whole ms, maximum difference | **0 ms** |
| Pairs that differ after flooring both to ms | 0 |
| Every Tardis value equals Bybit floored to ms | True |

Distribution of the raw signed difference (µs: count): {'0': 2250, '100': 2041, '200': 2008, '300': 2330, '400': 2408, '500': 2033, '600': 1923, '700': 2037, '800': 2228, '900': 2089}

## Explanation

* **0.9 ms is correct.** It is the exact maximum raw difference, reported by
  `scripts/verify_tardis_trades.py`.
* **0 ms was a truncated display.** `provider_cross_check.json` had a field
  `timestamp_abs_diff_ms` computed as `abs(diff_us) // 1000`, which floors 0–900 µs to 0.
  It is not a measurement of agreement to the microsecond. The field is now
  `timestamp_abs_diff_us`, in exact microseconds.
* **Why the two differ at all:** Tardis.dev values are whole milliseconds, while Bybit's CSV
  carries 0.1 ms. All pairs agree at millisecond precision, so the difference is precision,
  not a disagreement about the trade.

Boundary tests are in `tests/unit/test_reconciliation.py` (exact 0.1 ms conversion,
refusal of sub-microsecond values, and a 1 µs difference straddling a millisecond).
