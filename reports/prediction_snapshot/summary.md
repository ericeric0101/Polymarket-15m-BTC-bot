# Synchronized prediction snapshot analysis

Joint fresh coverage: 0/0 (0.0%)
Entry sync coverage: 0/0 (0.0%)
Residual 30s hypothesis: INSUFFICIENT_DATA (extreme episodes: positive=0/0 markets, negative=0/0 markets)
BTC disagreement hypothesis: INSUFFICIENT_DATA
Runtime impact: telemetry logs capture/enqueue p95 latency and research queue/drop counters; no live authority
Live authority: NO — research only

## Track A — residual vs 30s UP-mid repricing

Current and future observations require fresh UP midpoint. Episodes combine same-market, same-sign observations within 15 seconds; confidence intervals resample markets as clusters. Bins are fixed, not optimized. Values are decimal probability points.

| Residual bin | Observations | Episodes | Markets | Mean repricing | Median | Directional hit | Market-cluster bootstrap 95% CI |
|---|---:|---:|---:|---:|---:|---:|---:|
| <= -0.10 | 0 | 0 | 0 | — | — | — | —–— |
| -0.10 to -0.05 | 0 | 0 | 0 | — | — | — | —–— |
| -0.05 to -0.02 | 0 | 0 | 0 | — | — | — | —–— |
| -0.02 to +0.02 | 0 | 0 | 0 | — | — | — | —–— |
| +0.02 to +0.05 | 0 | 0 | 0 | — | — | — | —–— |
| +0.05 to +0.10 | 0 | 0 | 0 | — | — | — | —–— |
| >= +0.10 | 0 | 0 | 0 | — | — | — | —–— |

Extreme event episode rows are in `extreme_residual_episodes.csv`; all paired observations are in `residual_30s.csv`.

## Track B — BTC 10s agreement/disagreement

Held-side change is UP mid change for UP and its negation for DOWN. Adverse means held-side repricing below zero.

| Group | Observations | Episodes | Markets | Mean held-side change | Adverse rate | ≤−5¢ rate | ≤−10¢ rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| AGREES | 0 | 0 | 0 | — | — | — | — |
| DISAGREES | 0 | 0 | 0 | — | — | — | — |

## Entry snapshots

Eligible forced snapshots: 0; complete sync: 0. Per-entry p_ex-minus-ask is included only when p_ex and that side's quote are fresh. Settlement labels are secondary context; 30-second repricing is primary.

## Interpretation / falsification

- H1 requires at least 30 distinct markets in each positive-extreme and negative-extreme cohort before classification; otherwise `INSUFFICIENT_DATA`. If sufficiently sampled and neither side has directional hit-rate above 50%, classify `NOT_SUPPORTED`.
- H2 requires at least 30 distinct markets in each BTC group; otherwise `INSUFFICIENT_DATA`. It is descriptively supported only if disagreement has a higher adverse repricing rate than agreement.
- 1 Hz observations are correlated. Episode/market counts are shown alongside observation counts; no live strategy conclusion follows from this report.
