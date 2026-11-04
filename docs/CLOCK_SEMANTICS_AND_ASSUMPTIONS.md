# Clock Semantics and Assumptions

## What each source contains

| Source | Field | Documented meaning | Present? |
|---|---|---|---|
| Bybit book archive | `cts` | "The timestamp from the matching engine when this orderbook data is produced." | Yes |
| Bybit book archive | `ts` | "The timestamp (ms) that the system generates the data" | Yes |
| Bybit book archive | `u` / `seq` | "Update ID" / "Cross sequence" | Yes |
| Bybit book archive | client receive time | — | **No** |
| Bybit trade archive | `timestamp` | Fill time. The WebSocket field `T` is "The timestamp (ms) that the order is filled"; the CSV column is not separately documented | Yes |
| Bybit trade archive | message `ts`, `seq`, receive time | Exist on the WebSocket; absent from the CSV | **No** |
| Tardis.dev CSV | `timestamp` | "timestamp provided by exchange in microseconds since epoch" | Yes |
| Tardis.dev CSV | `local_timestamp` | "message arrival timestamp in microseconds since epoch" | Yes |

`ts` is the system's generation time. It is the closest available field to publication,
but Bybit does not call it a publication or send time, and **it is never treated as
this bot's receipt time**. Event-level publication time for trades does not exist in the
archive, so any trade delay on the archive is modeled unless it comes from a recorded
receipt.

`local_timestamp` is a real recorded arrival time, but at Tardis.dev's collector
(documented location: GCP asia-northeast1), on Tardis.dev's clock. Clock synchronization
between that collector and Bybit is unknown. The Tardis book stream is `orderbook.50`,
not the archive's level 200.

## Measured delays

| Stream | Measure | Min | p50 | p99 | Max | Negative |
|---|---|---:|---:|---:|---:|---:|
| Bybit archive book | `ts - cts` (ms) | 0 | 24 | 99 | 399 | 0 |
| Tardis book (651,959 messages) | `local_timestamp - timestamp` (ms) | 34.794 | 36.943 | 38.977 | 530.542 | 0 |
| Tardis trades (21,347) | `local_timestamp - timestamp` (ms) | 36.581 | 39.054 | 60.148 | 101.044 | 0 |

These are empirical distances between two clocks at a third party, not calibrated one-way
latencies, and they say nothing about this bot's connection.

## Batching and ordering

* Level 200 is documented as a 100 ms push. On `ts`, consecutive archive deltas are about
  100 ms apart. On `cts` they can be 3 ms apart, because `cts` stamps the content of a
  batch, not its publication. Changes inside one push are netted and their order is unknown.
* The trade CSV has no `seq`, so trades cannot be interleaved exactly with book updates.
* Tardis.dev rows are in arrival order per file, but the two files come from separate
  streams (the collector uses multiple WebSocket connections), so their relative
  order is only as good as `local_timestamp`.

## Clock modes

| Mode | Book available at | Trade available at | Status |
|---|---|---|---|
| `cts_baseline` | `cts` | `T` | Non-causal baseline, comparison only |
| `publication_time` | `ts` | `T` as a lower bound; no publication field exists | Partial |
| `modeled_availability` | `ts` + documented delay | `T` + delay, or a recorded receipt | Modeled |
| `actual_receipt` | recorded `local_timestamp` | recorded `local_timestamp` | Observed at a third party |

`actual_receipt` refuses any record without a recorded receive time.

## Per-event fields

Matching time (`engine_ts_us` / `exchange_ts_us`), generation time (`system_ts_us`, books
only), recorded receive time (`local_ts_us`, only when recorded), effective availability
(`available_ts_us`), processing-ready time (`ready_ts_us`), source ordinal (`source_line`
and stream position), `u` and `seq`. Provenance and every assumption are in each log's
`run_header.clock` and `clock_report.json`.

## Ordering rules

* The detector consumes events at `ready_ts_us`. Rolling windows use the same time.
* The order is `(ready, trade before book, stream, position)`. The trade-first tie rule is
  ours and deterministic, not an exchange guarantee.
* A stream is one ordered connection: ready time is held non-decreasing within a stream
  (FIFO), and adjustments are counted in `clock_report.json`.
* A negative delay, or an event available before it happened, raises an error. A missing
  timestamp raises an error; it is never fabricated.
* `u` and `seq` continuity is judged on source order, independent of the replay clock. A
  stale update is skipped and cannot alter emitted records.


Sources (accessed 2026-11-04):
* Bybit V5 orderbook: https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook
* Bybit V5 public trade: https://bybit-exchange.github.io/docs/v5/websocket/public/trade
* Tardis.dev CSV data types: https://docs.tardis.dev/downloadable-csv-files/data-types
* Tardis.dev Bybit details: https://docs.tardis.dev/historical-data-details/bybit
