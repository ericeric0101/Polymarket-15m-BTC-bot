# Early Entry + Dynamic Profit-Lock Backtest

- Cached public markets: 200; candidate entries: 684 across six non-pooled configs.
- Chronological split: 36 development dates / 20 holdout dates, 65/35 by date.
- Entry is a 5-second public trade-print VWAP after Binance 1-minute candle signal (ENTRY_EXECUTION_PROXY); it is not a guaranteed executable ask.
- Exit triggers use timestamped SELL-side public prints (EXIT_PROXY_PUBLIC_SELL_PRINT), not historical bid/BBO; sparse-path uncertainty is reported.
- Fixed $5 notional per market/config. Fee/slippage assumptions are sensitivity cases, not verified venue costs. No live strategy or parameters changed.
- Current 180s/5bps live protective logic is NOT FULLY REPLAYABLE; this comparison is a hold-to-settlement research baseline.

## Direct answers

1. Gross (pre-fee) settlement EV, 120/0 vs 180/5: 0.8497 vs 0.0965 USDC/trade; difference 0.7533. Paired-market difference is 0.1635 across 66 markets.
2. 120/2 EV=0.6437 vs 180/5=0.0965 (difference 0.5472); entry=0.6843, accuracy=0.7596. See entry_config_summary.csv and early_vs_current.csv.
3. Mean entry 120/0=0.6402, 180/5=0.7485; 120/0 is 10.8352 cents cheaper on unpaired config samples (-0.0431 paired price difference).
4. Accuracy difference (120/0 minus 180/5)=-0.0334 (weekday -0.0316, weekend -0.0219); median MFE/MAE=48.5953%/-33.4611% vs 26.4557%/-27.7778% (sampled prints, not BBO).
5. Best dynamic-only exit selected on weekday development is TRAIL_5 on 120/5; weekday holdout EV/PF=0.2832/1.8445; its date-block EV CI=-0.3106 to 0.5529. Compare fixed TP variants in the matrices; not a live recommendation.
6. Weekday vs weekend, 120/5 + TRAIL_5, 1% fee stress EV=0.1641 vs -0.7817; positive-to-negative rate=0.2750 vs 0.4500, median giveback=1.3864 vs 1.7071 USDC. Sample sizes are in the weekday/weekend matrices.
7. Development-selected holdout candidates are listed below; none selected using holdout. See development_holdout.csv for all requested metrics.
8. TP10 on 180/5: hits=57/73, hit rate=0.7808, median hit time=202.0000s, EV=-0.4122. On 180/5 hold, MFE-positive then settlement-negative rate=0.2192; MFE>=5/10/15/20-specific rates are in ever_positive_then_negative.csv (both total-trade and threshold-hit denominators).
9-12. Weekend MFE/MAE, giveback, positive-to-negative rates and entry-price bands are in weekday_weekend_path.csv, peak_to_exit_giveback.csv, ever_positive_then_negative.csv, and entry_price_interaction.csv.
13. Top development candidates evaluated without holdout selection: see chronological holdout lines below and bootstrap confidence intervals in bootstrap.csv.

## Development-selected top candidates (development and untouched holdout)

- 120/5 + TP5 (development_weekday): N=24, EV=0.2114, ROI=0.0423, PF=2.0046, DD=5.0500, avg win/loss=0.4401/-5.0500.
- 120/5 + TP5 (holdout_weekday): N=16, EV=0.0290, ROI=0.0058, PF=1.0919, DD=5.0500, avg win/loss=0.3676/-5.0500.
- 120/5 + TRAIL_5 (development_weekday): N=24, EV=0.0847, ROI=0.0169, PF=1.3074, DD=5.1500, avg win/loss=0.5085/-0.9445.
- 120/5 + TRAIL_5 (holdout_weekday): N=16, EV=0.2832, ROI=0.0566, PF=1.8445, DD=5.3650, avg win/loss=0.8246/-1.3413.
- 120/0 + TP20 (development_weekday): N=56, EV=0.4744, ROI=0.0949, PF=1.8767, DD=5.3674, avg win/loss=1.1373/-5.0500.
- 120/0 + TP20 (holdout_weekday): N=32, EV=0.3579, ROI=0.0716, PF=1.5669, DD=10.1000, avg win/loss=1.1304/-5.0500.

## Interpretation limits

- No historical executable ask/bid or L2 is available in this public cache. Public prints may be stale, sparse, and not fillable at the displayed price.
- MFE/MAE are sampled print excursions. Intratimestamp ambiguity is replayed optimistic/pessimistic or excluded; gap uncertainty is not interpolated.
- The 1% notional fee and 1c/2c slippage are stress scenarios. Results are descriptive, not live PnL forecasts.
- Weekend is comparison-only; no weekend policy or live entry/exit/size/stop/TP parameter was changed.
- Hypothesis A — earlier cheaper entry: Suggestive. Lower price / accuracy / EV decomposition is in entry_economics_decomposition.csv; proxy-based retrospective evidence is not execution proof.
- Hypothesis B — dynamic profit-lock vs fixed TP: Cannot assess. A positive point estimate in a small holdout is only suggestive, not proof of superiority.
- Hypothesis C — weekday-only risk-adjusted improvement: Cannot assess. Weekend policy remains unchanged.
- Overall direction — move research toward 120s/0–2bps plus dynamic protection: Suggestive for further shadow/prospective research only; not enough to change live policy.
