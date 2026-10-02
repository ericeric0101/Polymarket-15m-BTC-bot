# Prediction forensics — shadow-simulated BTC 15m cohort

Research-only, descriptive counterfactuals; not a live-performance estimate. One market_slug is counted once, so process restarts are not independent samples. The selected cohort is the 14-settlement group requested (12 wins, 2 losses); the journal now contains additional older/newer dry-run fills outside this specific cohort.

## Executive answer

1. **Is the bot predictive beyond market mid? — MIXED, currently NOT MEASURABLE at strict entry freshness.** The 14 fill-time market mids imply 10.24 expected wins; 12 were observed. Only 1/14 have a p_ex snapshot within the existing 2s freshness window at simulated fill (7/14 within a looser descriptive 12s window); this cohort cannot establish an independent advantage.
2. **Which signal appears earliest before repricing? — MIXED.** Among 16 same-direction crossing pairs within 60 seconds, p_ex crossed first in 8, market first in 7; median difference is 8.64s (positive means p_ex first). These are repeated transitions across 6 markets, not independent samples, so this is a hint—not a reliable lead claim.
3. **What distinguishes the two losers? — MIXED.** Both entries were UP and both had negative 10-second BTC returns at entry. The first had p_ex UP about 0.718 vs fill-mid 0.685, but below its 0.72 entry price and 3.1 seconds before fill; the second's nearest p_ex was about 0.791 but 17.7 seconds before fill. Neither has p_ex evidence inside the strict 2-second fill window.
4. **Can required-path probability help? — MIXED as a diagnostic, not a live veto.** It is present for only part of this early-entry cohort; a 1σ filter retained three wins and avoided one loss among the seven measurable rows, while rejecting three wins.
5. **Which current signal components add value? — MIXED, weak evidence.** BTC-only and structural-only sign matched 12/14 outcomes on the filled-entry subset, but this is selection-conditioned and not an ablation of every rejected candidate.
6. **What should be tested next? — Frozen, prospective test of fresh p_ex-minus-mid residual against 30s repricing, plus an adverse-BTC 10s exit-warning shadow replay; do not grant live authority yet.**

## Cohort and PnL

- Unique settled markets: **14**; wins **12**, losses **2**, win rate **85.7%**.
- Gross simulated PnL: **$14.54** (paper assumptions; no actual venue fill, fees, queue position or slippage).
- Payout decomposition: simulated entry cost **$80.55**, winning payout **$95.09**, losing entry cost lost **$10.85**; the 12 winners average $2.12, while two losers average $-5.43.
- Average winner: **$2.12**; average loser: **$-5.43**; break-even win rate from these average payoffs: **71.9%**.
- Fill-time market-side midpoint probabilities available: **14/14**; sum implies **10.24** expected wins vs **12** observed (difference **+1.76**, Poisson-binomial SD 1.623, P(K≥12)=0.225). The 1.76-win excess is not, by itself, compelling evidence against market pricing at N=14. This uses simulated fill mid on the purchased token (DOWN converted to UP axis); it is a midpoint benchmark, not executable ask or a fill guarantee.
- All **14/14** entries were on the market-favored side at the recorded simulated fill midpoint. The offline side ablation nevertheless yields the same selected direction with and without the market component on all 14 filled cases; this means market mid was not necessary to determine these realized directions, but does not establish ex-market alpha.
- Probability scores against final settlement: Brier/log-loss/direction accuracy are shown below; ex-market scoring includes only fresh, source-timestamped, bounded probabilities.

| Probability source | N | Brier | Log loss | Direction accuracy |
|---|---:|---:|---:|---:|
| market_fill_mid_all_fresh | 14 | 0.143 | 0.455 | 0.857 |
| market_fill_mid_on_p_ex_subset | 1 | 0.021 | 0.157 | 1.000 |
| p_ex | 1 | 0.007 | 0.089 | 1.000 |
- Paired p_ex vs market Brier delta (p_ex minus market on the same 1 strictly fresh-at-fill market): **-0.014**; negative favors p_ex. At N=1, this is not an estimable comparative result and must not be read as evidence of improvement.
- Entry price / market favorite decomposition: the entry-side quote and filled midpoint are in `entries.csv`. The two losses demonstrate that paying 0.72/0.77 for the favored side is not proof of positive independent edge.

## All 14 entries

| Market | Bought | Final | Entry | Market-side mid p | p_ex side ≤2s | p_ex ≤12s (exploratory) | latest p_ex side / age | Required σ | BTC 10s bps | Paper PnL |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1790920800 | UP | DOWN | 0.72 | 0.685 | NA | 0.718 | 0.718@3.136s | 0.558 | -2.172 | -5.35 |
| 1790921700 | UP | UP | 0.72 | 0.715 | NA | 0.779 | 0.779@3.318s | 0.743 | 2.875 | 2.14 |
| 1790923500 | UP | UP | 0.86 | 0.855 | 0.915 | 0.915 | 0.915@1.678s | 1.320 | 2.890 | 0.90 |
| 1790925300 | DOWN | DOWN | 0.74 | 0.715 | NA | NA | 0.764@39.698s | NA | 0.000 | 1.93 |
| 1790926200 | DOWN | DOWN | 0.78 | 0.770 | NA | NA | 0.864@32.820s | NA | 0.000 | 1.55 |
| 1790927100 | UP | UP | 0.80 | 0.795 | NA | 0.774 | 0.774@11.781s | 0.725 | -0.006 | 1.38 |
| 1790928900 | UP | UP | 0.85 | 0.845 | NA | NA | 0.900@12.226s | NA | 0.647 | 0.97 |
| 1790930700 | DOWN | DOWN | 0.66 | 0.655 | NA | NA | 0.651@69.265s | NA | -0.001 | 3.40 |
| 1790932500 | DOWN | DOWN | 0.64 | 0.625 | NA | NA | 0.544@28.690s | NA | -1.581 | 3.60 |
| 1790934300 | UP | UP | 0.68 | 0.615 | NA | 0.734 | 0.734@2.100s | 0.599 | -0.287 | 3.20 |
| 1790936100 | UP | UP | 0.59 | 0.575 | NA | NA | 0.555@36.878s | NA | -0.696 | 4.10 |
| 1790937900 | DOWN | DOWN | 0.89 | 0.885 | NA | 0.973 | 0.973@4.324s | 1.863 | 0.000 | 0.68 |
| 1790938800 | UP | UP | 0.78 | 0.755 | NA | 0.874 | 0.874@4.438s | 1.106 | -1.474 | 1.55 |
| 1790939700 | UP | DOWN | 0.77 | 0.750 | NA | NA | 0.791@17.684s | NA | -1.121 | -5.50 |

| Market time-left bin | N | Wins | Losses | Paper PnL |
|---|---:|---:|---:|---:|
| 480-600s | 13 | 11 | 2 | 13.57 |
| 240-360s | 1 | 1 | 0 | 0.97 |

### The two losses

- `btc-updown-15m-1790920800`: bought UP at 0.72; fill-mid side probability 0.685; p_ex ≤12s 0.718; latest prior p_ex 0.718 (3.136s old); model fair 0.745; BTC 10s/30s -2.172/NA bps; required-path sigma 0.558; side components market/BTC/structure=0.000/0.240/0.185, weights=0.0/0.58/0.42; final DOWN, paper PnL $-5.35.
  - First post-entry held-side probability <0.5: p_ex at +79.486s; fresh market mid at +103.050s; official TWAP state opposite at +121.097s. These are first observed crossings in available data, not a causal or executable stop recommendation.
- `btc-updown-15m-1790939700`: bought UP at 0.77; fill-mid side probability 0.750; p_ex ≤12s NA; latest prior p_ex 0.791 (17.684s old); model fair 0.775; BTC 10s/30s -1.121/0.118 bps; required-path sigma NA; side components market/BTC/structure=0.550/0.081/0.626, weights=0.63/0.22/0.15; final DOWN, paper PnL $-5.50.
  - First post-entry held-side probability <0.5: p_ex at +154.243s; fresh market mid at +154.243s; official TWAP state opposite at +176.818s. These are first observed crossings in available data, not a causal or executable stop recommendation.

For the last losing UP entry at 0.77: **the journal has no p_ex observation within 12 seconds of the simulated fill**. Its latest preceding valid p_ex was 0.791 approximately 17.7 seconds before fill, versus a fill midpoint of 0.75 and model fair 0.775 at entry. That earlier estimate is above 0.77 numerically, but is not synchronized closely enough to establish the true probability at fill. At fill the recorded fair exceeded price by only about 0.005/share, before model uncertainty. Thus the bot had a prior positive model indication, but no contemporaneous, independently validated edge at the simulated fill. The other loser’s closest p_ex was 0.718 about 3.1 seconds before fill; that too is outside the strict 2-second entry window.

## Winner / loser features

See `winner_loser_comparison.csv` for full feature availability. The focused comparison is:

| Result | Feature | N available | Mean | Median |
|---|---|---:|---:|---:|
| WIN | entry_price | 12 | 0.749 | 0.760 |
| WIN | market_side_probability | 12 | 0.734 | 0.735 |
| WIN | p_ex_side_at_entry | 1 | 0.915 | 0.915 |
| WIN | required_move_sigma | 6 | 1.059 | 0.924 |
| WIN | btc_return_10s_bps | 12 | 0.197 | -0.001 |
| WIN | btc_return_30s_bps | 11 | 0.183 | -0.006 |
| WIN | entry_crossings_last_60s | 12 | 0.250 | 0.000 |
| WIN | entry_distance_bps | 12 | 2.258 | -0.558 |
| LOSS | entry_price | 2 | 0.745 | 0.745 |
| LOSS | market_side_probability | 2 | 0.718 | 0.718 |
| LOSS | p_ex_side_at_entry | 0 | NA | NA |
| LOSS | required_move_sigma | 1 | 0.558 | 0.558 |
| LOSS | btc_return_10s_bps | 2 | -1.646 | -1.646 |
| LOSS | btc_return_30s_bps | 1 | 0.118 | 0.118 |
| LOSS | entry_crossings_last_60s | 2 | 1.000 | 1.000 |
| LOSS | entry_distance_bps | 2 | 2.733 | 2.733 |

There are only two losers and several entry-time path fields are unavailable, so numeric separation is exploratory, not a validated rule. The explicit prior active-side flip counts are null where the journal has no directional decisions; canonical TWAP/strike crossings are reported separately and must not be renamed as bot-side flips. Both losers had negative BTC 10-second returns at entry, but the corresponding “not opposing” counterfactual also rejects four winning trades; that is a candidate to investigate, not a justified gate.

## Early probability and market repricing

- Fresh model observations require `sigma_ex_market_fresh=true`, non-null `p_up_ex_market`, and source timestamp. Entry probability scoring additionally requires the observation itself to be no more than 2s before simulated fill; older values are shown separately as near-entry context only.
- Fresh market observations require source-age in `[0, 2.0]` seconds; stale rows are excluded, not treated as a delayed crossing.
- Measurable paired model/market 0.5 crossing cases: **16** over **6 unique markets** within the fixed 60-second same-direction matching window; p_ex led **8/16**, market led **7/16**, median model-to-market lead **8.64s**. These are transitions, not independent markets.
- Fresh model-to-bot active-side matches: **0**; model crossed first in **0**, bot decision first in **0**, median model-to-bot lead **NAs**. In this cohort no sampled active-side flip paired with a p_ex crossing, so relative model-to-bot lead is NOT MEASURABLE—not proof the bot did not change its decision. NONE/UNKNOWN intervals reset continuity; unmatched cases remain in `lead_lag_events.csv` as NOT_MEASURABLE.
- `repricing_prediction.csv` contains paired 5/10/30s fresh-mid changes after current mid, with ex-market residual and required-move context. It is not a tradable-edge/PnL test; no fabricated midpoint is used.

| Horizon | Paired observations / markets | market-cluster corr(p_ex − mid, future Δmid), 95% bootstrap CI | market-cluster corr(BTC 10s return, future Δmid), 95% bootstrap CI | residual directional hit |
|---|---:|---:|---:|---:|
| 5s | 97 / 13 | 0.037 [-0.446, 0.719] | 0.131 [-0.265, 0.564] | 0.584 |
| 10s | 99 / 14 | -0.168 [-0.707, 0.602] | -0.055 [-0.356, 0.154] | 0.558 |
| 30s | 89 / 13 | 0.618 [-0.204, 0.906] | -0.187 [-0.684, 0.458] | 0.583 |

Market-level OLS sensitivity on the same complete market set per horizon (mid-only vs added predictors):

| Horizon | Model | N markets | In-sample R² | RMSE |
|---|---|---:|---:|---:|
| 5s | A_mid_only | 13 | 0.200 | 0.019 |
| 5s | B_mid_plus_p_ex_residual | 13 | 0.321 | 0.017 |
| 5s | C_mid_plus_BTC_10s | 13 | 0.202 | 0.019 |
| 5s | D_mid_plus_required_sigma | 13 | 0.220 | 0.018 |
| 5s | combined | 13 | 0.358 | 0.017 |
| 10s | A_mid_only | 14 | 0.061 | 0.068 |
| 10s | B_mid_plus_p_ex_residual | 14 | 0.072 | 0.068 |
| 10s | C_mid_plus_BTC_10s | 14 | 0.062 | 0.068 |
| 10s | D_mid_plus_required_sigma | 14 | 0.066 | 0.068 |
| 10s | combined | 14 | 0.092 | 0.067 |
| 30s | A_mid_only | 13 | 0.238 | 0.059 |
| 30s | B_mid_plus_p_ex_residual | 13 | 0.387 | 0.053 |
| 30s | C_mid_plus_BTC_10s | 13 | 0.241 | 0.059 |
| 30s | D_mid_plus_required_sigma | 13 | 0.295 | 0.057 |
| 30s | combined | 13 | 0.522 | 0.047 |

These are descriptive, in-sample fits over market-aggregated rows; the combined model uses four predictors with a small number of markets and is especially prone to overfit. R² differences are not out-of-sample evidence of incremental alpha. The residual correlations and their market-cluster bootstrap intervals above are the more conservative primary read.

## Counterfactual diagnostics (not tuned)

| Filter | Kept | Wins | Losses | Shadow PnL | Winners rejected | Losers avoided | Feature available |
|---|---:|---:|---:|---:|---:|---:|
| A_ex_market_agrees | 1 | 1 | 0 | 0.90 | 0 | 0 | 1/14 |
| B_ex_at_least_market_side_probability | 1 | 1 | 0 | 0.90 | 0 | 0 | 1/14 |
| C_opposite_flip_ge_1_sigma | 3 | 3 | 0 | 3.13 | 3 | 1 | 7/14 |
| C_opposite_flip_ge_2_sigma | 0 | 0 | 0 | 0.00 | 6 | 1 | 7/14 |
| C_opposite_flip_ge_3_sigma | 0 | 0 | 0 | 0.00 | 6 | 1 | 7/14 |
| D_previous_60s_canonical_strike_crossings_lt_2 | 13 | 12 | 1 | 19.89 | 0 | 1 | 14/14 |
| E_BTC_10s_30s_not_opposing | 8 | 8 | 0 | 15.17 | 4 | 2 | 14/14 |

`excluded_slugs` is in the CSV. Missing-feature markets are not counted as genuine rejected winners/avoided losers. Fixed sigma cutoffs 1/2/3 are shown only where `required_move_sigma` exists. The 10s/30s momentum screen keeps 8/14, removes both losers but also rejects four winners; its small paper PnL improvement is only a hypothesis, not a validated rule.

## Required-path / timing interpretation

The entry-time `required_move_sigma`, mode and path fields are in `required_path_entry_risk.csv`. Most simulated fills occur well before the final 120 seconds; therefore a final-window exact-average boundary usually cannot be applied at entry. Do not infer that an early position is “safe” from a late checkpoint or from a settlement label.

## Signal ablation

`signal_ablation.csv` compares sign-only direction of full, market-only, minus-market, BTC-only and structural-only components from logged fill-side reasons when a decision event was not sampled near fill. The full signal is the side actually selected, so agreement with the filled side is tautological; this is conditional on filled trades, not every candidate. No weights are fitted. Missing market components remain unavailable.

| Component view | Signal observations | Settlement direction correct | Agrees with filled side |
|---|---:|---:|---:|
| full | 14 | 12 | 14 |
| market_only | 10 | 9 | 10 |
| minus_market | 14 | 12 | 14 |
| btc_only | 14 | 12 | 14 |
| structural_only | 14 | 12 | 14 |

## Prediction hypothesis verdict

**A — Market-favored outcomes explain the observed hit rate best, but not because the market component alone chose the direction.** All 14 simulated entries were on the market-favored side; market probability expected 10.24 wins and 12 occurred. In the offline ablation, removing the market component leaves all 14 selected directions unchanged, while market-only has 9/10 correct on the cases with a nonzero market component and minus-market also 9/10 on that same 10-market overlap. That is no demonstrated incremental edge for either component. The crossing analysis is mixed, strictly fresh p_ex at fill is present only once, and model-to-bot lead is not measurable. Thesis B is not established; Thesis C remains a plausible design description, not proven extra predictive value.

## Next experiment (maximum three)

1. Prospectively freeze a test of `p_up_ex_market - market_mid_up` vs +30s repricing: the market-mean in-sample R² rose 0.238→0.387 with the residual, while cluster-bootstrap correlation CI still crossed zero.
2. Keep a shadow-only “10s BTC move opposes held side” warning timeline: both losses showed it at entry, but the same screen would also reject four winners (counterfactual 8/14 kept).
3. Compare first adverse p_ex / mid / TWAP crossing and executable BBO depth on future losers and winners; here the first loser showed p_ex turning before recorded fresh-mid/TWAP, while the second's p_ex and market crossing were simultaneous, with active bot-side timing unavailable.

## Caveats and raw files

Shadow simulation assumes fills at recorded simulated prices and settlement payout; it does not model maker queue priority or actual execution. A 0.5 crossing is only a timing landmark, not a universal side reversal. `loser_timelines.csv` is sampled every 5 seconds from up to 120 seconds before entry through settlement; missing market quotes are blank. `data_quality.csv` documents row-level coverage.
