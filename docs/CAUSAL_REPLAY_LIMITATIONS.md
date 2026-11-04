# Causal Replay Limitations

* **Not fully observed.** No receipt time exists for the ob200 archive or for this bot. The
  `best_supported` book clock is `ts` + a median delay measured elsewhere.
* **The third-party clock is not ours.** Tardis.dev's collector is at a documented location
  that may differ from any deployment, and its clock synchronization with Bybit is unknown.
  Measured delays may include clock offset.
* **`actual_receipt_tardis` is a different feed** (50 levels at about 20 ms, no `u`). Its
  result is 3 confirmed, 2 of 7 surviving. The control
  on the same feed gives the same survivors, which points to the feed, not the clock. It is
  one day, and it does not show what the ob200 archive would do on an observed clock.
* **A fixed median hides jitter.** The book delay is constant in `best_supported`, while the
  measured maximum is 530.542 ms.
* **Trade/book interleaving within a batch is unknown.** There is no trade `seq` in the CSV,
  and pushes net changes inside 100 ms.
* **Uniform delay scenarios are uninformative for survival** because they keep relative order.
* **Delay values in sensitivity scenarios are assumptions**, not calibrated to any system.
* **One symbol-day.** Survival of 7 signals on one clock says nothing about any other day.
* **Thresholds are unchanged and untuned.** The results are not evidence of predictive value,
  and no returns were computed.
* **Processing time** is modeled by one 5 ms scenario only.
* **Execution attribution** can label a level decrease as a cancel when the trade arrives
  after the book update on an availability clock. A live consumer has the same ambiguity.
* **Other operating systems** were not tested. Python 3.12 and 3.13 were tested on Windows.
