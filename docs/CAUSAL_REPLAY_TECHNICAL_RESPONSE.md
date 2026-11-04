# Causal Replay: Technical Response

Symbol and day: ORDIUSDT, 2026-10-01 UTC. Thresholds: `config/demo.yaml`, unchanged
(`38a83e45b281fa68…`). This is engineering evidence only and makes no
statement about profitability.

## Root cause

The original replay ordered book records by `cts` and trades by fill time. Bybit's message
`ts` is later than `cts` by up to 399 ms, so the replay
applied book data before the message had been generated.

* 4,196 of 6,086 baseline transitions used a book message whose `ts` was later than the decision, by up to 102.0 ms.
* All 7 baseline CONFIRMED signals did, by 2.0 to 59.0 ms.

The earlier no-lookahead checks were valid for what they tested (no later record on the
replay clock). They did not and could not show that the clock itself was causal.

## Is the corrected replay observed or modeled?

* **`best_supported` (same ob200 archive): MODELED.** Trades use recorded third-party
  receipt times joined by trade ID. Book records use `ts` + 36.943 ms, the
  median measured collector delay, because no receipt exists for level-200 messages.
* **`actual_receipt_tardis`: OBSERVED, at a third-party collector, on a different feed**
  (50 levels at 20 ms, no update IDs).
* **A fully observed historical causal replay of the ob200 archive cannot be proven from
  these archives.** They contain no receipt times, and this bot has no recording for
  that day.

## Seven-signal comparison: `best_supported`

Matched by setup correspondence (same direction, closest within
30 s, one-to-one, overlapping setup
intervals), not by list position.

| Baseline signal | Dir | Baseline UTC | Corrected UTC | Δ ms | Baseline u | Corrected u | Result | Reason |
|---|---|---|---|---|---|---|---|---|
| LONG-415 | LONG | 03:44:39.214 | 03:44:39.052 | -161.057 | 26831968 | 26831967 | SAME_SETUP | — |
| SHORT-833 | SHORT | 07:36:03.408 | 07:36:03.452 | 44.943 | 26877172 | 26877172 | SAME_SETUP | — |
| SHORT-1390 | SHORT | 11:52:58.590 | 11:52:58.651 | 61.943 | 26942560 | 26942560 | SAME_SETUP | — |
| SHORT-1785 | SHORT | 14:50:45.408 | 14:50:45.452 | 44.943 | 26996396 | 26996396 | SAME_SETUP | — |
| SHORT-1869 | SHORT | 15:20:52.356 | 15:20:52.252 | -103.057 | 27007155 | 27007154 | SAME_SETUP | — |
| SHORT-2101 | SHORT | 16:51:36.884 | 16:51:36.952 | 68.943 | 27034938 | 27034938 | SAME_SETUP | — |
| SHORT-2121 | SHORT | 16:56:41.705 | 16:56:41.752 | 47.943 | 27036367 | 27036367 | SAME_SETUP | — |

Result: 7 same setup, 0 changed,
0 disappeared, 0 new; 6,105 transitions versus
6,086. A negative delta is not a leak. It occurs where a setup confirms on
an earlier book update than in the baseline (compare the two `u` columns): with trades
already in place relative to the book, an earlier evaluation met the rules. The independent
check below covers these logs.

## Seven-signal comparison: `actual_receipt_tardis` (different feed)

| Baseline signal | Dir | Baseline UTC | Corrected UTC | Δ ms | Baseline u | Corrected u | Result | Reason |
|---|---|---|---|---|---|---|---|---|
| LONG-415 | LONG | 03:44:39.214 | NOT PRESENT | — | 26831968 | — | NOT_PRESENT | no setup of this direction in the period |
| SHORT-833 | SHORT | 07:36:03.408 | 07:36:03.392 | -15.181 | 26877172 | — | SAME_SETUP | — |
| SHORT-1390 | SHORT | 11:52:58.590 | NOT PRESENT | — | 26942560 | — | NOT_PRESENT | SHORT-1403 reached WATCH (BOOK_PRESSURE_REVERSAL) |
| SHORT-1785 | SHORT | 14:50:45.408 | NOT PRESENT | — | 26996396 | — | NOT_PRESENT | SHORT-1806 reached WATCH (no rejection flag); SHORT-1807 reached WATCH (SPREAD_TOO_WIDE) |
| SHORT-1869 | SHORT | 15:20:52.356 | 15:20:52.412 | 56.579 | 27007155 | — | SAME_SETUP | — |
| SHORT-2101 | SHORT | 16:51:36.884 | NOT PRESENT | — | 27034938 | — | NOT_PRESENT | SHORT-2128 reached WATCH (no rejection flag); SHORT-2129 reached IDLE (ISOLATED_SPIKE) |
| SHORT-2121 | SHORT | 16:56:41.705 | NOT PRESENT | — | 27036367 | — | NOT_PRESENT | SHORT-2149 reached WATCH (ALREADY_EXTENDED) |

Result: 2 same setup, 5 disappeared, 1 new,
3 confirmed.

Control `tardis_feed_exchange_clock` (same Tardis files, exchange timestamps, no receipt
clock): 3 confirmed, 2 same setup,
5 disappeared, 1 new. Comparing the control with the baseline
shows the effect of the feed; comparing it with `actual_receipt_tardis` shows the effect
of the clock. Surviving signals on the control: SHORT-1869, SHORT-833.
On recorded receipt: SHORT-1869, SHORT-833. Same set:
True. So on this
feed, moving to the observed receipt clock did not change which signals survive; the
disappearance comes from the different feed (depth and cadence), not the clock. The seven
signals are not robust to feed choice and should not be read as stable objects.

## All scenarios

| Scenario | Basis | Confirmed | Same setup | Changed | Gone | New | Transitions |
|---|---|---|---|---|---|---|---|
| actual_receipt_tardis | OBSERVED_AT_THIRD_PARTY_COLLECTOR | 3 | 2 | 0 | 5 | 1 | 6,184 |
| best_supported | MODELED | 7 | 7 | 0 | 0 | 0 | 6,105 |
| best_supported_processing_5ms | MODELED | 7 | 7 | 0 | 0 | 0 | 6,105 |
| book_slow_250ms | MODELED | 10 | 7 | 0 | 0 | 3 | 6,125 |
| collector_p99 | MODELED | 7 | 7 | 0 | 0 | 0 | 6,056 |
| cts_baseline | NON_CAUSAL_BASELINE | 7 | 7 | 0 | 0 | 0 | 6,086 |
| publication_time | PARTIAL_PUBLICATION_LOWER_BOUND | 7 | 7 | 0 | 0 | 0 | 6,111 |
| tardis_feed_exchange_clock | NON_CAUSAL_BASELINE | 3 | 2 | 0 | 5 | 1 | 6,193 |
| trade_slow_250ms | MODELED | 6 | 5 | 0 | 2 | 1 | 5,824 |
| uniform_100ms | MODELED | 7 | 7 | 0 | 0 | 0 | 6,111 |
| uniform_10ms | MODELED | 7 | 7 | 0 | 0 | 0 | 6,111 |
| uniform_250ms | MODELED | 7 | 7 | 0 | 0 | 0 | 6,111 |
| uniform_500ms | MODELED | 7 | 7 | 0 | 0 | 0 | 6,111 |
| uniform_50ms | MODELED | 7 | 7 | 0 | 0 | 0 | 6,111 |

Reading this table:
* Uniform delays add the same amount to both streams from `ts` and `T`, so they preserve
  relative order and cannot change decisions; they only shift timestamps. They are not
  evidence of robustness.
* Signals respond to the **relative** delay of book and trades. The asymmetric scenarios
  show this directly.
* Scenario delays are assumptions unless the basis column says otherwise. None is
  calibrated to this bot.

## Rejection flags, baseline vs `best_supported`

| Flag | Baseline | best_supported |
|---|---|---|
| ALREADY_EXTENDED | 150 | 151 |
| BOOK_PRESSURE_REVERSAL | 21 | 20 |
| CANDIDATE_TIMEOUT | 1 | 1 |
| CVD_DIVERGENCE | 94 | 95 |
| FLOW_REVERSAL | 99 | 100 |
| INSUFFICIENT_HISTORY | 6 | 6 |
| ISOLATED_SPIKE | 2382 | 2387 |
| SPREAD_TOO_WIDE | 28 | 28 |
| STALE_BOOK | 6 | 6 |

## Verification

| Check | Result |
|---|---|
| Baseline logs identical to the original evidence | True |
| Independent stdlib recomputation of clocked logs | 12/12 scenarios pass, 0 mismatches |
| Records ready before they happened | 0 |
| Prefix + falsified-future replay, baseline | True (1,875 transitions) |
| Prefix + falsified-future replay, `best_supported` | True (1,880 transitions) |
| Determinism, separate processes | True |

## Commands

```bash
python -m bybit_demo.cli verify-data
python scripts/run_causal_replay.py
python scripts/verify_causal_replay.py --all
python scripts/verify_no_lookahead.py --scenario best_supported
```

Logs: `evidence/causal_logs/<scenario>/`. Comparison: `evidence/causal_replay_comparison.csv`.
Assumptions: `docs/CLOCK_SEMANTICS_AND_ASSUMPTIONS.md`. Limits: `docs/CAUSAL_REPLAY_LIMITATIONS.md`.
