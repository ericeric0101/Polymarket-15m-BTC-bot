# Required-path probability replay

Offline-only descriptive analysis. No live authority; Polymarket mid is never an estimator input.

## Dataset and eligibility

- TWAP DB exact observations: 54 rows across 12 markets.
- BTC 1-second history: 1702 1-second bars, 1998 sec span (85.2% observed-second coverage), 2026-10-01T11:11:39+00:00 to 2026-10-01T11:44:56+00:00.
- Observations with a canonical settlement label: 50; paired analytic/empirical rows: 12.
- Primary coverage threshold: 100%; 90% and 95% are partial-path sensitivity diagnostics only.
- Historical candidate windows must end before the evaluation timestamp. The primary estimate requires every 1-second interval; missing seconds are never forward-filled. Partial-coverage sensitivity averages only observed seconds and is not an exact full-path reconstruction.
- Historical path windows overlap heavily. Raw candidate counts are not independent samples; cluster-level uncertainty is by market, and this one-day dataset cannot support stable intervals.
- Status: `INSUFFICIENT_HISTORY` for any broader probability claim. This capture is an illustrative pipeline/replay check, not a stable empirical distribution.

## Existing field semantics

- `remaining_avg_decision_boundary` / `required_future_avg_to_flip`: exact final-window remaining average price which makes the full official window average equal strike, computed from observed partial integral; equality resolves UP under repository convention.
- `required_move_usd` / `required_move_bps`: boundary minus current `path_spot`, in USD and basis points.
- `required_move_sigma`: existing analytic standardized-distance diagnostic for the same boundary/horizon; it is not itself a path-average empirical probability.
- `required_move_mode`: primary rows require `EXACT_FINAL_WINDOW_BOUNDARY`; `PRE_FINAL_STRIKE_PROXY` and `UNAVAILABLE` are excluded.
- `observed_final_window_avg` and `observed_final_window_sec`: already-observed 60-second window average and duration; `remaining_final_window_sec` is the remaining time for the empirical path horizon.
- `settlement_state_side`: current official-TWAP-vs-strike side. `p_up_ex_market` is the existing analytic probability; empirical path probability uses only BTC paths and boundary, not market mid.

## Results

| Market | Observations | Current/settled side | Example horizon | Boundary / move | Required σ | Analytic flip p | Empirical flip p (100%) | Eligible paths |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| btc-updown-15m-1790782200 | 10 | UP / UP | 30.0s | $84,079.32 → $83,527.76; -551.56 USD / -65.60 bps | 33.64σ | 0.0000 | — | 0 |
| btc-updown-15m-1790783100 | 4 | UP / UP | 31.0s | $84,089.43 → $84,076.16; -13.27 USD / -1.58 bps | 0.80σ | 0.0840 | — | 0 |
| btc-updown-15m-1790811900 | 4 | DOWN / DOWN | 31.0s | $83,556.97 → $83,579.04; +22.07 USD / +2.64 bps | 1.33σ | 0.0105 | — | 0 |
| btc-updown-15m-1790812800 | 4 | DOWN / DOWN | 31.0s | $83,487.75 → $83,624.37; +136.63 USD / +16.37 bps | 8.26σ | 0.0000 | — | 0 |
| btc-updown-15m-1790813700 | 4 | DOWN / DOWN | 31.0s | $83,445.85 → $83,526.89; +81.04 USD / +9.71 bps | 4.90σ | 0.0000 | — | 0 |
| btc-updown-15m-1790819100 | 4 | DOWN / DOWN | 30.0s | $83,462.50 → $83,624.83; +162.33 USD / +19.45 bps | 9.97σ | 0.0000 | — | 0 |
| btc-updown-15m-1790820000 | 4 | UP / UP | 31.0s | $83,554.61 → $83,365.89; -188.72 USD / -22.59 bps | 11.39σ | 0.0000 | — | 0 |
| btc-updown-15m-1790820900 | 4 | DOWN / DOWN | 31.0s | $83,451.68 → $83,660.44; +208.76 USD / +25.02 bps | 12.62σ | 0.0000 | — | 0 |
| btc-updown-15m-1790821800 | 4 | DOWN / DOWN | 31.0s | $83,388.96 → $83,507.38; +118.42 USD / +14.20 bps | 7.16σ | 0.0000 | — | 0 |
| btc-updown-15m-1790852400 | 4 | DOWN / DOWN | 31.0s | $83,833.67 → $83,993.21; +159.54 USD / +19.03 bps | 9.60σ | 0.0000 | 0.0000 | 7 |
| btc-updown-15m-1790853300 | 4 | DOWN / DOWN | 30.0s | $83,821.23 → $83,901.08; +79.85 USD / +9.53 bps | 4.88σ | 0.0000 | 0.0000 | 30 |
| btc-updown-15m-1790854200 | 4 | UP / UP | 31.0s | $83,924.44 → $83,683.96; -240.48 USD / -28.65 bps | 14.46σ | 0.0000 | 0.0000 | 33 |

### Required-move-sigma bands

Rates below are forecast-time estimates: empirical values are the mean of each observation's historical full-path window flip frequency, not the realized outcome frequency across live trades. Repeated observations within a market are correlated.

| Required move | Exact observations | Full-path estimates | Mean historical-path flip frequency | Mean analytic flip p |
|---|---:|---:|---:|---:|
| LT_1_SIGMA | 1 | 0 | — | 0.0840 |
| 1_TO_2_SIGMA | 3 | 0 | — | 0.0037 |
| 2_TO_3_SIGMA | 0 | 0 | — | — |
| GT_3_SIGMA | 50 | 12 | 0.0000 | 0.0000 |

## Answers

1. **Exact required-average math:** the replay consumes the calculator's exact partial-integral boundary; the underlying identity is `(window × strike − observed_seconds × observed_average) / remaining_seconds`. It is consistent with the implementation, which uses the same boundary for UP/DOWN because settlement compares the full average to strike.
2. **Analytic versus empirical:** this dataset is too short and path windows overlap, so observed differences are illustrative only. See per-observation CSV; no stable error comparison is claimed.
3. **Historical flip rate by sigma:** see the sigma-band table and `observation_probability.csv`; the sample is not independent by second and is insufficient to infer a reliable rate.
4. **Bias direction:** not determinable from this capture. It cannot establish systematic overestimation, underestimation, or calibration.
5. **Conclusion:** not enough history for a probability-quality conclusion. The path-average estimator is operationally testable, but only 3 markets have current 1-second history preceding exact observations; many windows share the same short session.

## Distinguish the three questions

- **SETTLEMENT MATH:** deterministic boundary calculation; exact rows define the remaining average needed to cross strike.
- **PROBABILITY ESTIMATION:** analytic probability versus historical path-frequency estimate; currently `INSUFFICIENT_HISTORY` for a stable conclusion.
- **TRADING EDGE:** not tested. There is no BBO/ask execution, fees, depth, or fills in this probability replay; no live action is authorized.

## Coverage / data quality

- `data_quality.csv` reports candidate path count, rejected windows, coverage-sensitivity counts, recomputes the required-average boundary from the observed integral, and verifies the strict no-lookahead condition per observation.
- `paired_comparison.csv` gives analytic-minus-empirical paired Brier/log-loss deltas with bootstrap resampling by `market_slug`; it will be empty when no exact canonical, paired evaluation rows exist.
- For primary path-average probability, missing seconds are not filled and incomplete windows are rejected. Under partial-coverage sensitivities, the synthetic average is normalized over observed seconds only, so it is not an exact reconstruction of the missing full path.
- `endpoint_flip_probability_diagnostic_only` is deliberately separate from the primary remaining-path-average probability.
- Volatility conditioning uses the current analytic sigma to select a LOW/MID/HIGH tercile of historical windows with a complete preceding 60-second realized-volatility sample; fewer than 100 paths causes explicit unconditional fallback.
- Momentum conditioning is omitted because the current history is too short and correlated to justify another split.

## What would make this useful

Continue accumulating timestamped 1-second history across multiple weeks and volatility regimes. A practical review gate is not a raw count of overlapping windows: require multiple independent market-days, adequate coverage in each horizon/volatility band, and market-cluster confidence intervals narrow enough to distinguish the empirical estimate from the analytic estimate. Keep this research-only until it also survives executable-price, fee, depth, and forward validation.
