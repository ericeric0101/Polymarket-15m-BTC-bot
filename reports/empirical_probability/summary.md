# Offline empirical probability research

No live runtime, strategy, or authority is changed by this analysis.

- TWAP DB: `data/research/twap_forward_shadow.db` (read-only)
- BTC source: `data/btc_history`; cached Binance BTCUSDT 1m OHLCV only
- Historical window: rolling 56 days, strictly earlier than each evaluation timestamp
- Data coverage: 2026-07-27 to 2026-09-20
- Evaluation mode markets: exact=9, pre-final proxy=9
- Minute-close data supports only coarse 60s/120s forward returns; 5/10/15/30s are unavailable.
- Exact final-window remaining-average boundaries are not estimable from minute closes; those rows are excluded from empirical scoring.
- Polymarket mid is not an executable price; these are probability scores, not PnL or edge estimates.
- Vol-conditioned estimates use low/mid/high trailing-vol regimes; sparse regimes fall back to unconditional and are flagged.
- Bootstrap resampling unit is market slug. Small canonical evaluation sample means intervals may be very wide.

## Metric summary — modes reported separately

| Checkpoint | Required-move mode | Source | Markets | Brier | Log loss | ECE | Direction accuracy |
|---:|---|---|---:|---:|---:|---:|---:|
| T−120 | PRE_FINAL_PROXY | analytic_p_up | 8 | 0.0018 | 0.0226 | 0.0216 | 1.0000 |
| T−120 | PRE_FINAL_PROXY | p_up_empirical | 8 | 0.0092 | 0.0793 | 0.0741 | 1.0000 |
| T−120 | PRE_FINAL_PROXY | p_up_empirical_vol_conditioned | 8 | 0.0009 | 0.0193 | 0.0188 | 1.0000 |
| T−120 | PRE_FINAL_PROXY | market_mid_probability_up | 8 | 0.0024 | 0.0310 | 0.0297 | 1.0000 |
| T−120 | EXACT_FINAL_WINDOW | analytic_p_up | 0 | — | — | — | — |
| T−120 | EXACT_FINAL_WINDOW | p_up_empirical | 0 | — | — | — | — |
| T−120 | EXACT_FINAL_WINDOW | p_up_empirical_vol_conditioned | 0 | — | — | — | — |
| T−120 | EXACT_FINAL_WINDOW | market_mid_probability_up | 0 | — | — | — | — |
| T−60 | PRE_FINAL_PROXY | analytic_p_up | 0 | — | — | — | — |
| T−60 | PRE_FINAL_PROXY | p_up_empirical | 0 | — | — | — | — |
| T−60 | PRE_FINAL_PROXY | p_up_empirical_vol_conditioned | 0 | — | — | — | — |
| T−60 | PRE_FINAL_PROXY | market_mid_probability_up | 0 | — | — | — | — |
| T−60 | EXACT_FINAL_WINDOW | analytic_p_up | 0 | — | — | — | — |
| T−60 | EXACT_FINAL_WINDOW | p_up_empirical | 0 | — | — | — | — |
| T−60 | EXACT_FINAL_WINDOW | p_up_empirical_vol_conditioned | 0 | — | — | — | — |
| T−60 | EXACT_FINAL_WINDOW | market_mid_probability_up | 0 | — | — | — | — |
| T−30 | PRE_FINAL_PROXY | analytic_p_up | 0 | — | — | — | — |
| T−30 | PRE_FINAL_PROXY | p_up_empirical | 0 | — | — | — | — |
| T−30 | PRE_FINAL_PROXY | p_up_empirical_vol_conditioned | 0 | — | — | — | — |
| T−30 | PRE_FINAL_PROXY | market_mid_probability_up | 0 | — | — | — | — |
| T−30 | EXACT_FINAL_WINDOW | analytic_p_up | 8 | 0.0000 | 0.0013 | 0.0013 | 1.0000 |
| T−30 | EXACT_FINAL_WINDOW | p_up_empirical | 0 | — | — | — | — |
| T−30 | EXACT_FINAL_WINDOW | p_up_empirical_vol_conditioned | 0 | — | — | — | — |
| T−30 | EXACT_FINAL_WINDOW | market_mid_probability_up | 8 | 0.0002 | 0.0063 | 0.0063 | 1.0000 |
| T−15 | PRE_FINAL_PROXY | analytic_p_up | 0 | — | — | — | — |
| T−15 | PRE_FINAL_PROXY | p_up_empirical | 0 | — | — | — | — |
| T−15 | PRE_FINAL_PROXY | p_up_empirical_vol_conditioned | 0 | — | — | — | — |
| T−15 | PRE_FINAL_PROXY | market_mid_probability_up | 0 | — | — | — | — |
| T−15 | EXACT_FINAL_WINDOW | analytic_p_up | 8 | 0.0000 | 0.0000 | 0.0000 | 1.0000 |
| T−15 | EXACT_FINAL_WINDOW | p_up_empirical | 0 | — | — | — | — |
| T−15 | EXACT_FINAL_WINDOW | p_up_empirical_vol_conditioned | 0 | — | — | — | — |
| T−15 | EXACT_FINAL_WINDOW | market_mid_probability_up | 8 | 0.0009 | 0.0130 | 0.0125 | 1.0000 |
| T−10 | PRE_FINAL_PROXY | analytic_p_up | 0 | — | — | — | — |
| T−10 | PRE_FINAL_PROXY | p_up_empirical | 0 | — | — | — | — |
| T−10 | PRE_FINAL_PROXY | p_up_empirical_vol_conditioned | 0 | — | — | — | — |
| T−10 | PRE_FINAL_PROXY | market_mid_probability_up | 0 | — | — | — | — |
| T−10 | EXACT_FINAL_WINDOW | analytic_p_up | 8 | 0.0000 | 0.0000 | 0.0000 | 1.0000 |
| T−10 | EXACT_FINAL_WINDOW | p_up_empirical | 0 | — | — | — | — |
| T−10 | EXACT_FINAL_WINDOW | p_up_empirical_vol_conditioned | 0 | — | — | — | — |
| T−10 | EXACT_FINAL_WINDOW | market_mid_probability_up | 8 | 0.0009 | 0.0130 | 0.0125 | 1.0000 |
| T−5 | PRE_FINAL_PROXY | analytic_p_up | 0 | — | — | — | — |
| T−5 | PRE_FINAL_PROXY | p_up_empirical | 0 | — | — | — | — |
| T−5 | PRE_FINAL_PROXY | p_up_empirical_vol_conditioned | 0 | — | — | — | — |
| T−5 | PRE_FINAL_PROXY | market_mid_probability_up | 0 | — | — | — | — |
| T−5 | EXACT_FINAL_WINDOW | analytic_p_up | 8 | 0.0000 | 0.0000 | 0.0000 | 1.0000 |
| T−5 | EXACT_FINAL_WINDOW | p_up_empirical | 0 | — | — | — | — |
| T−5 | EXACT_FINAL_WINDOW | p_up_empirical_vol_conditioned | 0 | — | — | — | — |
| T−5 | EXACT_FINAL_WINDOW | market_mid_probability_up | 8 | 0.0009 | 0.0130 | 0.0125 | 1.0000 |

## Requested checkpoint comparison

| Checkpoint | Mode | N markets | Analytic Brier | Empirical Brier | Vol-conditioned Brier | Market-mid Brier |
|---:|---|---:|---:|---:|---:|---:|
| T−120 | PRE_FINAL_PROXY | 8 | 0.0018 | 0.0092 | 0.0009 | 0.0024 |
| T−120 | EXACT_FINAL_WINDOW | 0 | — | — | — | — |
| T−60 | PRE_FINAL_PROXY | 0 | — | — | — | — |
| T−60 | EXACT_FINAL_WINDOW | 0 | — | — | — | — |
| T−30 | PRE_FINAL_PROXY | 0 | — | — | — | — |
| T−30 | EXACT_FINAL_WINDOW | 8 | 0.0000 | — | — | 0.0002 |
| T−15 | PRE_FINAL_PROXY | 0 | — | — | — | — |
| T−15 | EXACT_FINAL_WINDOW | 8 | 0.0000 | — | — | 0.0009 |
| T−10 | PRE_FINAL_PROXY | 0 | — | — | — | — |
| T−10 | EXACT_FINAL_WINDOW | 8 | 0.0000 | — | — | 0.0009 |
| T−5 | PRE_FINAL_PROXY | 0 | — | — | — | — |
| T−5 | EXACT_FINAL_WINDOW | 8 | 0.0000 | — | — | 0.0009 |

## Interpretation

Usable empirical predictions: 8 of 241 selected checkpoint rows; 8 are pre-final proxy rows and 0 exact-boundary rows.
Selected mode rows: PRE_FINAL_PROXY=9, EXACT_FINAL_WINDOW=36, UNAVAILABLE=196.
Empirical-evaluable outcomes: UP=2, DOWN=6; hit rate is not stable evidence at this sample size.
T−120 p_up_empirical Brier − analytic_p_up = 0.0074; market-cluster bootstrap 95% CI [0.0024, 0.0130], N=8 markets.
T−120 p_up_empirical Brier − market_mid_probability_up = 0.0068; market-cluster bootstrap 95% CI [0.0022, 0.0120], N=8 markets.
T−120 p_up_empirical_vol_conditioned Brier − analytic_p_up = -0.0009; market-cluster bootstrap 95% CI [-0.0027, 0.0001], N=8 markets.
T−120 p_up_empirical_vol_conditioned Brier − market_mid_probability_up = -0.0015; market-cluster bootstrap 95% CI [-0.0039, -0.0001], N=8 markets.
Q1 — Empirical vs analytic: not established; only eight paired T−120 proxy markets.
Q2 — Empirical vs market mid: not established; mid is non-executable and this sample is too small.
Q3 — Effective checkpoint/regime: cannot be established; sub-minute and exact final-window rows are not estimable from 1m OHLCV.
Q4 — Stable residual structure for ML: not demonstrated.
Classification: probability-model comparison is underpowered; settlement math alpha is undetermined; no standalone executable edge is shown.
ML_JUSTIFIED = INSUFFICIENT_DATA

Momentum-conditioned model was skipped: current evaluation panel and checkpoint labels are too sparse to justify another conditioning split.
