# Canonical BTC15m offline research analysis

Offline-only analysis. No live authority, strategy threshold, entry, exit, stop,
or session policy has changed. `session_regime` is determined **only** by the
market start time in Asia/Taipei.

## Session regime

- Current batch: **MIXED**
- Weekend markets: **144**
- Weekday markets: **21**
- Weekend batch label: **WEEKEND_REPLICATION_BATCH_1** when the batch is the
  four synchronized October 3 markets; it is an out-of-regime replication, not
  a refutation of weekday observations.

## Signal comparison by regime

- p_ex lead: N=336, lead >2s=9, lag >2s=223,
  median=-3.7987300157546997 seconds. This is reported only for the selected
  regime; no weekday/weekend raw-event pooling is performed.
- `>= +0.10` residual: N=5056, markets=76,
  mean future 30s mid move=-0.009218651107594938. Compare
  this only with a within-regime residual cohort.
- Absolute repricing is in `repricing_events.csv` (5c/10c apples-to-apples
  thresholds). `normalized_repricing_events.csv` is a secondary top-20%-within-
  regime view and does not replace those absolute thresholds.

## Regime interpretation

The current batch is **MIXED**. Any difference from historic weekday
findings is classified as `POSSIBLE_REGIME_DEPENDENCE` until it is replicated
within each regime. `regime_comparison.csv` provides effect direction and
magnitude without asserting significance.

## Next tests

1. **Regime replication:** compare 30-second repricing conditional on
   normalized BTC 10-second shock separately for weekday and weekend markets.
2. **Liquidity mechanism:** compare spread, quote cadence, and fresh-snapshot
   rate before attributing lead/lag differences to a predictor.
