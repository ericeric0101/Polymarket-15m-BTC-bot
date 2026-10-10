# FULL MONDAY VS WEEKEND — INTERIM REGIME COMPARISON

Snapshot: `data/analysis_snapshots/20261005_185454_+0800` at 2026-10-05T18:54:54.651854+08:00. Baseline: `c8eeaaacb660a32fd1be305115ed31fd1c67c5fa`; branch unchanged. Snapshot copies were made with SQLite online backup API from `mode=ro` sources; both destination `quick_check` values are `ok`. Source mtimes were identical before/after backup.

## 1. Snapshot safety

Snapshot manifest: `data/analysis_snapshots/20261005_185454_+0800/snapshot_manifest.json`. Journal destination 2,225,152,000 bytes; research destination 611,336,192 bytes. No writes were issued to either source.

## 2. Collection growth since morning

| Measure | Morning | New | Delta |
|---|---:|---:|---:|
| strategy_runs rows | 404 | 450 | +46 |
| lead_lag_decisions rows | 128,399 | 140,853 | +12,454 |
| prediction snapshot payload rows (ResearchStore dedupe) | 103,836 | 116,206 | +12,370 |
| settlement summary rows | 250 | 288 | +38 |
| unique prediction markets | 198 | 245 | +47 |
| Monday prediction markets (including incomplete/active) | 27 | 74 | +47 |
| settled SHADOW_SIM_SETTLED rows (all history) | 88 | 111 | +23 |

There were 48 new completed Monday slots after the morning cutoff. Among them, 38 had a canonical summary plus a matched prediction timeline, but only 17 yielded usable T−300 outcomes; the slot audit classifies 6 SYNCHRONIZED_USABLE, 5 SYNCHRONIZED_LOW_QUALITY, 27 INTERRUPTED, 9 OTHER and 1 MISSING_MARKET. The 18:45 slot was still active. This is **COLLECTION_GROWTH_PARTIAL**: raw data grew materially, but outcome-grade T−300 and uninterrupted capture did not grow at the expected slot rate.

## 3. Monday cohort completeness

Monday has 75 completed expected slots; counts: `23 SYNCHRONIZED_USABLE, 6 SYNCHRONIZED_LOW_QUALITY, 30 INTERRUPTED, 14 OTHER, 2 MISSING_MARKET`. `OTHER` means there were prediction and/or settlement records but no matched canonical summary/timeline pair. Of the 48 later slots alone, counts were 6 usable, 5 low quality, 27 interrupted, 9 other and 1 missing. Current active/unsettled slot is `btc-updown-15m-1791197100` starting 2026-10-05T18:45:00+08:00. A synchronized market counts as high usable only with canonical settlement, matching run timeline, ≥600 sec timeline span, ≥25% joint-fresh rows and T−300 checkpoint; multi-run market is INTERRUPTED. These operational quality criteria are stated explicitly; they do not imply listing failures. `MISSING_MARKET` means absent from snapshot records.

| Taipei start | slug | classification | prediction rows | runs |
|---|---|---|---:|---:|
| 2026-10-05 00:00 | `btc-updown-15m-1791129600` | SYNCHRONIZED_USABLE | 562 | 1 |
| 2026-10-05 00:15 | `btc-updown-15m-1791130500` | OTHER | 582 | 1 |
| 2026-10-05 00:30 | `btc-updown-15m-1791131400` | SYNCHRONIZED_USABLE | 554 | 1 |
| 2026-10-05 00:45 | `btc-updown-15m-1791132300` | SYNCHRONIZED_USABLE | 672 | 1 |
| 2026-10-05 01:00 | `btc-updown-15m-1791133200` | SYNCHRONIZED_USABLE | 530 | 1 |
| 2026-10-05 01:15 | `btc-updown-15m-1791134100` | OTHER | 615 | 1 |
| 2026-10-05 01:30 | `btc-updown-15m-1791135000` | SYNCHRONIZED_USABLE | 589 | 1 |
| 2026-10-05 01:45 | `btc-updown-15m-1791135900` | SYNCHRONIZED_USABLE | 543 | 1 |
| 2026-10-05 02:00 | `btc-updown-15m-1791136800` | SYNCHRONIZED_USABLE | 549 | 1 |
| 2026-10-05 02:15 | `btc-updown-15m-1791137700` | SYNCHRONIZED_USABLE | 391 | 1 |
| 2026-10-05 02:30 | `btc-updown-15m-1791138600` | SYNCHRONIZED_USABLE | 487 | 1 |
| 2026-10-05 02:45 | `btc-updown-15m-1791139500` | OTHER | 397 | 1 |
| 2026-10-05 03:00 | `btc-updown-15m-1791140400` | SYNCHRONIZED_USABLE | 585 | 1 |
| 2026-10-05 03:15 | `btc-updown-15m-1791141300` | SYNCHRONIZED_USABLE | 565 | 1 |
| 2026-10-05 03:30 | `btc-updown-15m-1791142200` | INTERRUPTED | 447 | 2 |
| 2026-10-05 03:45 | `btc-updown-15m-1791143100` | SYNCHRONIZED_USABLE | 545 | 1 |
| 2026-10-05 04:00 | `btc-updown-15m-1791144000` | SYNCHRONIZED_USABLE | 659 | 1 |
| 2026-10-05 04:15 | `btc-updown-15m-1791144900` | OTHER | 553 | 1 |
| 2026-10-05 04:30 | `btc-updown-15m-1791145800` | INTERRUPTED | 543 | 2 |
| 2026-10-05 04:45 | `btc-updown-15m-1791146700` | SYNCHRONIZED_USABLE | 630 | 1 |
| 2026-10-05 05:00 | `btc-updown-15m-1791147600` | SYNCHRONIZED_USABLE | 570 | 1 |
| 2026-10-05 05:15 | `btc-updown-15m-1791148500` | SYNCHRONIZED_USABLE | 418 | 1 |
| 2026-10-05 05:30 | `btc-updown-15m-1791149400` | OTHER | 554 | 1 |
| 2026-10-05 05:45 | `btc-updown-15m-1791150300` | SYNCHRONIZED_USABLE | 657 | 1 |
| 2026-10-05 06:00 | `btc-updown-15m-1791151200` | SYNCHRONIZED_LOW_QUALITY | 220 | 1 |
| 2026-10-05 06:15 | `btc-updown-15m-1791152100` | INTERRUPTED | 175 | 2 |
| 2026-10-05 06:30 | `btc-updown-15m-1791153000` | MISSING_MARKET | 0 | 0 |
| 2026-10-05 06:45 | `btc-updown-15m-1791153900` | SYNCHRONIZED_LOW_QUALITY | 311 | 1 |
| 2026-10-05 07:00 | `btc-updown-15m-1791154800` | INTERRUPTED | 372 | 2 |
| 2026-10-05 07:15 | `btc-updown-15m-1791155700` | INTERRUPTED | 176 | 3 |
| 2026-10-05 07:30 | `btc-updown-15m-1791156600` | SYNCHRONIZED_USABLE | 475 | 1 |
| 2026-10-05 07:45 | `btc-updown-15m-1791157500` | INTERRUPTED | 266 | 2 |
| 2026-10-05 08:00 | `btc-updown-15m-1791158400` | INTERRUPTED | 119 | 2 |
| 2026-10-05 08:15 | `btc-updown-15m-1791159300` | INTERRUPTED | 252 | 2 |
| 2026-10-05 08:30 | `btc-updown-15m-1791160200` | SYNCHRONIZED_LOW_QUALITY | 136 | 1 |
| 2026-10-05 08:45 | `btc-updown-15m-1791161100` | INTERRUPTED | 140 | 3 |
| 2026-10-05 09:00 | `btc-updown-15m-1791162000` | SYNCHRONIZED_USABLE | 418 | 1 |
| 2026-10-05 09:15 | `btc-updown-15m-1791162900` | MISSING_MARKET | 0 | 0 |
| 2026-10-05 09:30 | `btc-updown-15m-1791163800` | OTHER | 16 | 1 |
| 2026-10-05 09:45 | `btc-updown-15m-1791164700` | INTERRUPTED | 186 | 2 |
| 2026-10-05 10:00 | `btc-updown-15m-1791165600` | OTHER | 133 | 2 |
| 2026-10-05 10:15 | `btc-updown-15m-1791166500` | INTERRUPTED | 239 | 2 |
| 2026-10-05 10:30 | `btc-updown-15m-1791167400` | INTERRUPTED | 297 | 2 |
| 2026-10-05 10:45 | `btc-updown-15m-1791168300` | SYNCHRONIZED_USABLE | 590 | 1 |
| 2026-10-05 11:00 | `btc-updown-15m-1791169200` | OTHER | 455 | 1 |
| 2026-10-05 11:15 | `btc-updown-15m-1791170100` | SYNCHRONIZED_USABLE | 334 | 1 |
| 2026-10-05 11:30 | `btc-updown-15m-1791171000` | SYNCHRONIZED_USABLE | 371 | 1 |
| 2026-10-05 11:45 | `btc-updown-15m-1791171900` | INTERRUPTED | 395 | 2 |
| 2026-10-05 12:00 | `btc-updown-15m-1791172800` | INTERRUPTED | 96 | 3 |
| 2026-10-05 12:15 | `btc-updown-15m-1791173700` | OTHER | 212 | 1 |
| 2026-10-05 12:30 | `btc-updown-15m-1791174600` | INTERRUPTED | 323 | 2 |
| 2026-10-05 12:45 | `btc-updown-15m-1791175500` | INTERRUPTED | 95 | 2 |
| 2026-10-05 13:00 | `btc-updown-15m-1791176400` | INTERRUPTED | 85 | 3 |
| 2026-10-05 13:15 | `btc-updown-15m-1791177300` | OTHER | 141 | 2 |
| 2026-10-05 13:30 | `btc-updown-15m-1791178200` | INTERRUPTED | 228 | 2 |
| 2026-10-05 13:45 | `btc-updown-15m-1791179100` | INTERRUPTED | 305 | 2 |
| 2026-10-05 14:00 | `btc-updown-15m-1791180000` | INTERRUPTED | 192 | 3 |
| 2026-10-05 14:15 | `btc-updown-15m-1791180900` | INTERRUPTED | 219 | 2 |
| 2026-10-05 14:30 | `btc-updown-15m-1791181800` | SYNCHRONIZED_LOW_QUALITY | 65 | 1 |
| 2026-10-05 14:45 | `btc-updown-15m-1791182700` | INTERRUPTED | 260 | 2 |
| 2026-10-05 15:00 | `btc-updown-15m-1791183600` | INTERRUPTED | 293 | 2 |
| 2026-10-05 15:15 | `btc-updown-15m-1791184500` | INTERRUPTED | 170 | 2 |
| 2026-10-05 15:30 | `btc-updown-15m-1791185400` | OTHER | 111 | 2 |
| 2026-10-05 15:45 | `btc-updown-15m-1791186300` | SYNCHRONIZED_LOW_QUALITY | 258 | 1 |
| 2026-10-05 16:00 | `btc-updown-15m-1791187200` | INTERRUPTED | 263 | 2 |
| 2026-10-05 16:15 | `btc-updown-15m-1791188100` | OTHER | 344 | 1 |
| 2026-10-05 16:30 | `btc-updown-15m-1791189000` | INTERRUPTED | 351 | 2 |
| 2026-10-05 16:45 | `btc-updown-15m-1791189900` | INTERRUPTED | 215 | 3 |
| 2026-10-05 17:00 | `btc-updown-15m-1791190800` | SYNCHRONIZED_LOW_QUALITY | 459 | 1 |
| 2026-10-05 17:15 | `btc-updown-15m-1791191700` | INTERRUPTED | 389 | 2 |
| 2026-10-05 17:30 | `btc-updown-15m-1791192600` | OTHER | 306 | 1 |
| 2026-10-05 17:45 | `btc-updown-15m-1791193500` | INTERRUPTED | 199 | 2 |
| 2026-10-05 18:00 | `btc-updown-15m-1791194400` | OTHER | 354 | 1 |
| 2026-10-05 18:15 | `btc-updown-15m-1791195300` | SYNCHRONIZED_USABLE | 410 | 1 |
| 2026-10-05 18:30 | `btc-updown-15m-1791196200` | INTERRUPTED | 225 | 2 |
| 2026-10-05 18:45 | `btc-updown-15m-1791197100` | UNSETTLED_AT_SNAPSHOT (excluded from terminal outcomes) | 403 | 1 |


## 4. Payload comparability

- MONDAY: 16,415 selected timeline rows; 1 payload key signatures; probability model labels `existing_twap_average_approx_v1`; explicit research/prediction/lifecycle/trace schema version tags `{'research_schema_version': [], 'prediction_schema_version': [], 'lifecycle_schema_version': [], 'trace_schema_version': []}`; core field non-null counts `{'required_move_sigma': 16003, 'required_move_bps': 16223, 'p_up_ex_market': 16082, 'market_mid_up': 8269, 'market_mid_down': 8360, 'btc_fresh': 16415, 'btc_return_5s_bps': 10762, 'btc_return_10s_bps': 10863, 'btc_return_30s_bps': 10798, 'joint_fresh': 16415, 'settlement_state_side': 16415}`.
- WEEKEND: 72,228 selected timeline rows; 1 payload key signatures; probability model labels `existing_twap_average_approx_v1`; explicit research/prediction/lifecycle/trace schema version tags `{'research_schema_version': [], 'prediction_schema_version': [], 'lifecycle_schema_version': [], 'trace_schema_version': []}`; core field non-null counts `{'required_move_sigma': 71113, 'required_move_bps': 71474, 'p_up_ex_market': 71408, 'market_mid_up': 34464, 'market_mid_down': 35039, 'btc_fresh': 72228, 'btc_return_5s_bps': 49053, 'btc_return_10s_bps': 49801, 'btc_return_30s_bps': 50093, 'joint_fresh': 72228, 'settlement_state_side': 72228}`.

**Core payload: CONFIRMED_BY_PAYLOAD.** Both cohorts use event type `PREDICTION_RESEARCH_SNAPSHOT`, one identical key signature, and probability model `existing_twap_average_approx_v1`. Required_move_sigma, required_move_bps, p_up_ex_market, both market mids, BTC 5/10/30 returns, settlement state and each listed freshness flag are present as keys; each of `market_mid_up/down_fresh`, `market_quote_up/down_fresh`, `p_ex_fresh`, `sigma_ex_market_fresh`, `btc_fresh`, `twap_fresh` and `joint_fresh` is non-null in all 16,415 Monday / 72,228 weekend selected rows (true/false are values, not missing). Canonical settlement summaries expose `canonical_settlement_side`, `settlement_reference_source`, `settlement_reference_is_canonical`, and `summary_ts` in both cohorts (59/59 and 144/144 summaries). **PROVENANCE MANIFEST ABSENT** remains distinct from **PAYLOAD SCHEMA INCOMPATIBLE**. Run-level commit/config/schema provenance is absent; this is not evidence that the payloads are incompatible.

## 5. Data quality

| Cohort | markets | SYNCHRONIZED_USABLE / SYNCHRONIZED_LOW_QUALITY / INTERRUPTED | median coverage | P10 coverage | median largest gap sec | P90 gap sec | median joint fresh % | P10 joint fresh % | median snapshots | usable T300/T180/T120/T60/T30 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| MONDAY_EARLY | 21 | 17/1/3 | 0.977 | 0.898 | 25.5 | 178.0 | 53.7 | 43.4 | 549 | 17/21 / 18/21 / 18/21 / 18/21 / 18/21 |
| MONDAY_LATER | 38 | 6/5/27 | 0.970 | 0.821 | 238.0 | 362.1 | 45.8 | 10.5 | 259 | 17/38 / 21/38 / 25/38 / 29/38 / 34/38 |
| MONDAY_ALL | 59 | 23/6/30 | 0.973 | 0.858 | 178.0 | 330.1 | 50.6 | 15.6 | 334 | 34/59 / 39/59 / 43/59 / 47/59 / 52/59 |
| ALL_WEEKEND | 144 | 96/26/22 | 0.978 | 0.882 | 10.6 | 102.1 | 56.6 | 22.5 | 566 | 124/144 / 125/144 / 124/144 / 122/144 / 136/144 |
| TIME_MATCHED_WEEKEND | 111 | 71/21/19 | 0.978 | 0.854 | 11.0 | 115.7 | 49.7 | 21.0 | 563 | 94/111 / 94/111 / 92/111 / 90/111 / 103/111 |

Weekend has 144 matched settled markets: 96 high-quality, 26 low-quality and 22 interrupted. No weekend missing-slot count is asserted because the report does not construct a complete expected weekend market-start universe.

Compared with morning weekday quality: morning 21 markets had coverage median .977, P10 .898; gap median 25.5s/P90 178.0s; joint fresh median 53.7%/P10 43.4%; snapshots median 549. Early quality reproduces the morning medians (coverage .977/P10 .898, max gap 25.5s, 549 snapshots, joint freshness 53.7%). Later quality **DETERIORATED**: P10 largest gap rose to 362s, median snapshots fell to 259, P10 joint freshness fell to 10.5%, and only 6/38 later markets met the high-quality rule; 27/48 later slots were multi-run INTERRUPTED. Coverage median remained high because market timelines are merged across runs, so it must be read together with interruption/gap statistics.

## 6. Monday early vs later collection

Early is completed Monday starts through 06:30, the latest completed start at morning snapshot; later is 06:45 onward. Cohort metrics are shown in `stability.csv` and below.

| Cohort | markets | T300 N/flips/rate | <0.5σ T300 N/flips/rate | market/analytic T300 Brier | ≥1 repricing | shadow N/win rate/avg PnL | avg loser | capital efficiency |
|---|---:|---|---|---|---|---|---:|---:|
| MONDAY_EARLY | 21 | 17/2/0.118 | 6/1/0.16666666666666666 | 0.13032/0.13637 | 16/21 | 14/0.7857142857142857/0.4756991660123669 | -5.966666666666667 | 0.010092969475673464 |
| MONDAY_LATER | 38 | 17/0/0.000 | 0/0/None | 0.01121/0.00445 | 9/38 | 23/0.782608695652174/0.09826744654098675 | -6.16 | 0.00207193693556725 |
| MONDAY_ALL | 59 | 34/2/0.059 | 6/1/0.16666666666666666 | 0.07360/0.07355 | 25/59 | 37/0.7837837837837838/0.2410794485031306 | -6.0875 | 0.005095116604371962 |
| ALL_WEEKEND | 144 | 124/15/0.121 | 56/13/0.23214285714285715 | 0.09220/0.11724 | 117/144 | 41/0.8536585365853658/0.17858663516934262 | -5.48921568627451 | 0.004416525925630773 |
| TIME_MATCHED_WEEKEND | 111 | 94/13/0.138 | 46/12/0.2608695652173913 | 0.11189/0.13051 | 86/111 | 30/0.8666666666666667/0.29661747805677424 | -5.483823529411765 | 0.007163203492883733 |

## 7. Terminal flip risk

`flip_calibration.csv` summarizes independent market outcomes by cohort/checkpoint with Wilson 95% intervals, mean market and analytic probabilities, and paired Brier scores. Primary comparison at T−300:
| Cohort | flips/N | rate | Wilson 95% CI |
|---|---:|---:|---|
| MONDAY_ALL | 2/34 | 0.059 | [0.016, 0.191] |
| ALL_WEEKEND | 15/124 | 0.121 | [0.075, 0.190] |
| TIME_MATCHED_WEEKEND | 13/94 | 0.138 | [0.083, 0.222] |

No difference is called unless intervals/sample sizes support it; current primary verdict will be determined from N and overlap, not raw point estimates. Hour restriction includes all weekend markets in Taipei hours represented by Monday; weighted descriptive estimates are reported in CSV `weight_estimate`.

## 8. Required_move_sigma

Fixed six bins, T−300/T−180/T−120/T−60, per market; small bins with N<10 are explicitly exploratory. See `sigma_bps.csv` for counts, flips, rates and Wilson intervals for early/later/all Monday, weekend and hour-restricted weekend. Low sigma association assessed only across the primary <0.5, 0.5–1 and 1–2σ bins.

## 9. Required_move_bps

Existing absolute-distance bins 0–2, 2–5, 5–10, 10–20, >20 bps at T−300/T−180; zero values remain in 0–2 and are counted separately from missing values. At T−300 weekend is 12/37=32.4% in 0–2 bps, 2/47=4.3% in 2–5, 1/29=3.4% in 5–10; Monday is 1/5, 0/6, 1/6 in those bins. Weekend raw-bps and sigma bins show the same low-distance/higher-flip ordering. Monday N is too small to determine whether sigma normalizes better than raw bps. Full Wilson CIs, T−180 and missing/zero counts are in `sigma_bps.csv`.

## 10. Market vs analytic p_ex

`flip_calibration.csv` includes T−300/T−180/T−120/T−60/T−30 and supplemental checkpoints (explicitly tagged `SUPPLEMENTAL_NONCANONICAL_REPORT_ONLY`) with outcome N, flips, rate, Wilson CI, each model’s available score N, paired-score N, paired observed rate, mean market/analytic probabilities on the paired subset, paired market/analytic Brier and analytic-minus-market Brier. Separate model-specific N and observed rates are retained. Missing/freshness-invalid scores are not imputed. p_ex is used only as an analytic probability, not a lead signal.

## 11. Same-price structural risk

Market-aligned leader price uses existing fixed 0.55–0.90 price buckets; structural groups use existing sigma bins aggregated to <1σ versus ≥1σ. T−300 outcome N, flips and Wilson intervals are in `sigma_bps.csv`. Sparse/empty cells mean no incremental signal can be judged; no thresholds were optimized.

## 12. Large repricing behavior

| Cohort | markets with ≥1 / total | fraction | Wilson 95% CI | raw 30s windows | coalesced events | median/P90 per affected |
|---|---:|---:|---|---:|---:|---|
| MONDAY_EARLY | 16/21 | 0.762 | [0.549, 0.894] | 512 | 75 | 4.0/7.0 |
| MONDAY_LATER | 9/38 | 0.237 | [0.130, 0.392] | 159 | 20 | 2.0/5.0 |
| MONDAY_ALL | 25/59 | 0.424 | [0.306, 0.551] | 671 | 95 | 3.0/7.0 |
| ALL_WEEKEND | 117/144 | 0.812 | [0.741, 0.868] | 3385 | 442 | 3.0/7.0 |
| TIME_MATCHED_WEEKEND | 86/111 | 0.775 | [0.689, 0.843] | 2548 | 335 | 3.0/7.0 |

The event count is serially dependent; market-level affected fraction is primary.

## 13. BTC fast-warning evidence

Uses only persisted 5s/10s/30s BTC returns and disagreement flags on rows with `btc_fresh=true`, joint-fresh market data, and a future 27–33s market observation. “Large move” uses the existing per-market 75th-percentile absolute BTC return threshold from the repo forensic helper; no new indicator or threshold was invented. `sigma_bps.csv` reports N, market counts, repricing/state-flip rates, and intervals. For MONDAY_ALL, disagreement-window ≥10c rates are 8.1% vs 9.7% for agreement; the horizon-level large-move comparisons are below. The observed rates are similar and vary by horizon. Windows overlap and are not independent, so overall **NO_EVIDENCE** for a BTC fast warning; associations are not causal.

| Cohort | BTC horizon | Large move windows / N | ≥10c after large | ≥10c after non-large | Short state flips after large / non-large |
|---|---:|---:|---:|---:|---:|
| MONDAY_ALL | 5s | 1830/7167 | 151/1830 (8.3%) | 9.2% | 89 / 154 |
| MONDAY_ALL | 10s | 1874/7275 | 163/1874 (8.7%) | 9.0% | 107 / 144 |
| MONDAY_ALL | 30s | 1917/7315 | 180/1917 (9.4%) | 9.1% | 143 / 115 |
| ALL_WEEKEND | 5s | 9762/35612 | 867/9762 (8.9%) | 9.3% | 421 / 834 |
| ALL_WEEKEND | 10s | 9806/36156 | 937/9806 (9.6%) | 9.1% | 432 / 842 |
| ALL_WEEKEND | 30s | 9680/36433 | 889/9680 (9.2%) | 9.2% | 508 / 789 |
| TIME_MATCHED_WEEKEND | 5s | 6757/24803 | 632/6757 (9.4%) | 10.1% | 333 / 661 |
| TIME_MATCHED_WEEKEND | 10s | 6818/25292 | 678/6818 (9.9%) | 9.9% | 338 / 674 |
| TIME_MATCHED_WEEKEND | 30s | 6832/25570 | 659/6832 (9.6%) | 10.0% | 411 / 621 |

## 14. Entry performance

Settled `SHADOW_SIM_SETTLED` entries are deduplicated using the repository rule (latest event per simulation_id); rows and unique markets are both reported in `entry_tail.csv`. No cohort has multiple entries for a market (0 markets with >1 entry). Classifications are separated as VERIFIED_EX_MARKET_EDGE, MARKET_FOLLOWING and UNVERIFIED. Shadow entries are not live fills.

## 15. Tail-loss structure

`entry_tail.csv` reports loser N, average/median/worst loser, total gross winner PnL, absolute loss-to-winner-PnL ratio, loser medians for entry price, sigma, bps, time-left and available BTC returns, plus winner/loser PnL per capital-minute rows. Monday loss share is 84.5% of gross winners vs weekend 81.8%; Monday has 8 losers (−$6.09 average, −$7 worst) vs weekend 6 (−$5.49, −$5.50). Small samples do not justify a stop rule.

## 16. Capital efficiency

Uses canonical capital-minutes = entry capital × elapsed minutes through SHADOW_SIM_SETTLED event time. MONDAY_ALL: 37 trades, 1,750.7 capital-minutes, +$8.92 gross, +$0.00510 PnL/capital-minute; ALL_WEEKEND: 41 trades, 1,657.9 capital-minutes, +$7.32, +$0.00442; hour-restricted weekend: 30 trades, 1,242.3 capital-minutes, +$8.90, +$0.00716. Winner/loser efficiency split is in `entry_tail.csv`. No holding-time optimization is performed.

## 17. Time-of-day matched comparison

Weekday hour distribution (market starts): `{0: 3, 1: 3, 2: 3, 3: 4, 4: 3, 5: 3, 6: 3, 7: 4, 8: 4, 9: 2, 10: 3, 11: 3, 12: 3, 13: 3, 14: 4, 15: 3, 16: 3, 17: 3, 18: 2}`. The hour-restricted weekend distribution (same supported hours) is: `{0: 5, 1: 6, 2: 6, 3: 4, 4: 3, 5: 3, 6: 4, 7: 3, 8: 8, 9: 5, 10: 6, 11: 8, 12: 8, 13: 8, 14: 6, 15: 7, 16: 8, 17: 6, 18: 7} (Monday cohort: {0: 3, 1: 3, 2: 3, 3: 4, 4: 3, 5: 3, 6: 3, 7: 4, 8: 4, 9: 2, 10: 3, 11: 3, 12: 3, 13: 3, 14: 4, 15: 3, 16: 3, 17: 3, 18: 2})`. TIME_MATCHED_WEEKEND restricts weekend markets to represented hours `[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]`; exact hour weights are calculated from Monday and weekend hour proportions and shown as descriptive weighted estimates in `flip_calibration.csv`. This is hour-of-day adjustment only; weekday/weekend and other confounders remain.

## 18. Stability since morning

The stability table in `stability.csv` reports early/later/all/weekend/time-matched key metrics and includes explicit per-metric movement-class columns. Movement classes: T300 flip rate TOO_NOISY; <0.5σ T300 TOO_NOISY; market T300 Brier TOO_NOISY; analytic T300 Brier TOO_NOISY; ≥1 repricing fraction DIVERGING_FROM_WEEKEND (likely capture-sensitive); shadow win rate STABLE; average shadow PnL TOO_NOISY; average loser DIVERGING_FROM_WEEKEND; capital efficiency TOO_NOISY. These labels describe early-to-later movement, not significance.

## 19. What changed since the morning analysis

- **STRENGTHENED:** Monday T−300 outcome sample grew from 17 to 34 independent markets; primary common sigma bins now have Monday pooled N=6, 8 and 9.
- **WEAKENED:** morning low-sigma pattern did not gain usable later `<0.5σ` observations (later N=0 at T−300); later collection was mostly interrupted and had lower freshness/shorter market coverage.
- **UNCHANGED:** core payload comparability is CONFIRMED_BY_PAYLOAD; manifests remain absent; no strategy or code changes.
- **REVERSED:** the all-Monday T−300 point rate fell from 2/17=11.8% early to 2/34=5.9% pooled; this is not a statistically established reversal because intervals overlap and the late cohort is low-quality.
- **STILL_UNDERPOWERED:** T−300 N=34 (<50); sigma-bin N, same-price cells, calibration comparisons, BTC warning and tail-loss differences.
- **REPRICING:** affected markets fell from 16/21=76.2% early to 9/38=23.7% later, but later capture quality is poor; do not interpret this as a regime effect.

## 20. Current answers

Q1. Monday terminal flip risk different from weekend? **INSUFFICIENT_N.** T−300 Monday 2/34=5.9% (Wilson 95% CI 1.6–19.1%); weekend 15/124=12.1% (7.5–19.0%); hour-restricted weekend 13/94=13.8% (8.3–22.2%). Intervals overlap.

Q2. Does required_move_sigma replicate on Monday? **INSUFFICIENT_N.** Weekend T−300 declines across `<0.5σ` 13/56=23.2%, `0.5–1σ` 2/40=5.0%, `1–2σ` 0/21. Monday pooled is 1/6, 1/8, 0/9; later `<0.5σ` has N=0. Early does not establish consistency and later adds no evidence.

Q3. Does sigma add information beyond market price? **INSUFFICIENT_N.** In existing side-aligned price buckets, Monday T−300 contributes only 6 markets total across `<1σ` versus `≥1σ` groups (4 vs 2; 0 flips in both); individual cells are smaller. No incremental claim is supportable.

Q4. Does market probability calibrate better than p_ex on Monday? **MIXED, still low precision.** Use the paired score subset, since T−300 market scores exist for only 21 of 34 outcome markets while analytic scores exist for 34. Across canonical checkpoints, analytic-minus-market paired Brier signs change: analytic is marginally lower at T−300 (−0.00005) and T−60 (−0.00403); market is lower at T−180 (+0.01178), T−120 (+0.00523), and T−30 (+0.00149). Paired N is 15–21, so classification is **MIXED** with low precision. On its model-available sample, analytic mean p_ex exceeds the observed rate at 5/5 checkpoints; model-specific N/observed rates and the paired observed rate are in `flip_calibration.csv`.

Q5. Does Monday have different ≥10c repricing behavior? **Recorded incidence is lower, but regime conclusion is INSUFFICIENT_N due unequal data quality.** Monday 25/59=42.4% (30.6–55.1%) vs hour-restricted weekend 86/111=77.5% (68.9–84.3%). The later Monday timelines are much less complete, so a missed event opportunity can explain part of the gap.

Q6. Does shadow entry performance differ? Monday 29/37=78.4% wins vs weekend 35/41=85.4%; hour-restricted weekend 26/30=86.7%. Entries are one per market. Monday classifications: 9 VERIFIED_EX_MARKET_EDGE, 9 MARKET_FOLLOWING, 19 UNVERIFIED; weekend: 4, 19, 18. Sample sizes and shadow-only status do not support an independent-alpha conclusion.

Q7. Are loss tails different? **SIMILAR_TAIL_STRUCTURE, differential INSUFFICIENT_N.** Monday wins 29/37 but has 8 losers averaging −$6.09, median −$5.85, worst −$7.00; weekend wins 35/41 with 6 losers averaging −$5.49, median −$5.50, worst −$5.50. Both show high win rates alongside large loss tails; Monday’s tail is directionally worse on small N.

Q8. Are differences robust after Taipei time-of-day matching? **No robust difference established.** Hour-restricted weekend retains 111/144=77.1% of all weekend markets (discarding 23.0%, not excessive for this descriptive check); T−300 N=94 and 13/94=13.8%, overlapping Monday’s interval; Monday-hour-weighted rate is 13.8% (see `weight_estimate`); sigma’s weekend shape persists descriptively but Monday bin Ns remain too small. Hour matching does not remove other confounding.

Q9. Did the additional ~12 hours strengthen or weaken morning conclusions? **It weakened confidence in the morning pattern.** T−300 N doubled to 34, but pooled flip rate fell to 5.9%, late `<0.5σ` was unavailable, and 27/48 later slots were interrupted. More raw rows did not yield comparable-quality independent observations.

Q10. Is there evidence strong enough to justify changing live strategy now? **NO.** No entry, stop, holding, sizing, or strategy change was made.

## 21. Sample milestone

**EARLY_SAMPLE** — MONDAY_ALL has 34 usable independent T−300 markets, below the ~50 MID_CHECK_READY milestone. Snapshot rows do not count as independent markets.

## 22. Next collection target

Current usable Monday T−300 N is **34**; next target is **50** (+16). Recent later yield was 17 usable T−300 markets from 48 completed slots (35.4%). At four 15-minute market starts/hour, the rough additional collection estimate is **11.3 hours**. This assumes the same yield; data-quality recovery could change it.

## 23. Final research verdict

**DATA_QUALITY_PROBLEM_REQUIRES_INVESTIGATION** — the later cohort has 27/48 interrupted slots, 9 additional unmatched/other slots, only 6 high-quality synchronized slots, and markedly worse gap/freshness distributions. Continue the frozen collection without strategy changes; interpret regime metrics only after comparable coverage returns.

Safety confirmations: new offline snapshot created? **YES**; both destination quick_check passed? **YES**; source code modified? **NO**; git commit? **NO**; git push? **NO**; running bot stopped? **NO**; running bot restarted? **NO**; another bot launched? **NO**; active DB mutated by analysis? **NO**; active Parquet mutated? **NO**; live strategy changed? **NO**.
