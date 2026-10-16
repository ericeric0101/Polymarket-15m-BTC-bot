# Preliminary weekend capital-efficiency validation

Cohort: **PRELIMINARY_WEEKEND_CAPITAL_EFFICIENCY**. Offline snapshots only; this validates accounting mechanics, not a live strategy.

## Data availability

- TEST_DRY_RUN runs: 60; completed research markets: 47; completed markets with a settled shadow trade: 10; shadow fills: 10; settled shadow entries: 10; usable trades: 10.
- Live `ORDER_FILLED` records visible: 0; they are not pooled with simulated trades.
- Valid stake, entry timestamp, settlement timestamp, and gross PnL: 10 each. Excluded: 0; every reason is in `data_quality.csv`.
- Stopped lifecycle usable: 0. `BANKROLL_UTILIZATION_NOT_MEASURABLE`: no durable bankroll time series.

## Core metrics

- Gross PnL=-1.9749087595002672; capital-minutes=457.362044377625; gross PnL/$-minute=-0.0043180425305902.
- Actual simulated stake: min=5.375, median=5.5, mean=5.4875, max=5.5.
- Gross PnL/$-minute: mean=-0.0024023733697349347, median=0.02090929353845917, P25=0.017952071455624954, P75=0.025321836032062804, P90=0.03287386939491946.

## Entry timing

| Time left | N | Wins | Losses | Avg hold min | Capital-minutes | Gross PnL | Gross PnL/$-min |
|---|---:|---:|---:|---:|---:|---:|---:|
| 480–600s | 7 | 5 | 2 | 8.894871583439055 | 342.4525559624036 | -5.653830328127718 | -0.0165098207903241 |
| 360–480s | 3 | 3 | 0 | 7.010976639058855 | 114.90948841522138 | 3.678921568627451 | 0.032015820619910845 |
| >600s | 0 | 0 | 0 | None | 0 | 0 | None |
| 240–360s | 0 | 0 | 0 | None | 0 | 0 | None |
| 120–240s | 0 | 0 | 0 | None | 0 | 0 | None |
| <120s | 0 | 0 | 0 | None | 0 | 0 | None |

## Framework verdict

1. Q1 reconstruction: **YES** — 10 deduped settled shadow entries have non-imputed time, stake, and PnL.
2. Q2 capital lock measurable: **YES** — median=8.597589083512624 minutes.
3. Q3/Q4 timing bins: **YES**, but `PRELIMINARY_PATTERN_ONLY`; sparse bins are not recommendations.
4. Q5 outlier sensitivity: **MIXED** — mean and median plus `outliers.csv` are supplied.
5. Q6 stop capital release: **NOT_MEASURABLE** — no joined stopped-shadow lifecycle.
6. Q7 missing fields: **MIXED** — settled shadow accounting works; stop lifecycle and bankroll history are absent.
7. Q8 future weekday/weekend study: **MIXED** — core reconstruction is ready if weekday emits the same schema; stop and bankroll provenance need work.
