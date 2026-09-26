# 週末 vs 平日：BTC 15 分鐘市場流動性、執行與 PnL

## Dataset

- Canonical journal：88,779 order rows、231,503 strategy rows；ok。
- Local markets：412；public markets：0；public trades：0。
- Journal 實際記錄跨 12 個 UTC 日期、412 個 slug；目前市場週末數 ET=0、UTC=0。
- 查詢 window：2026-06-28T02:55:50.711941+00:00 — 2026-09-26T02:55:50.711941+00:00。
- Journal 實際事件範圍：2026-09-07T11:48:10.161617+00:00 — 2026-09-25T14:51:20.391746+00:00；市場週末時區：America/New_York。
- SELL fill missing PnL：3；excluded shadow rows：22184。

## Weekday vs weekend

| Metric | weekday n / mean | weekend n / mean | difference | 95% bootstrap CI | permutation p |
|---|---:|---:|---:|---:|---:|
| public_trade_count | 0 / n/a | 0 / n/a | n/a | n/a | n/a |
| public_volume_shares | 0 / n/a | 0 / n/a | n/a | n/a | n/a |
| median_trade_size | 0 / n/a | 0 / n/a | n/a | n/a | n/a |
| max_no_trade_interval_sec | 0 / n/a | 0 / n/a | n/a | n/a | n/a |
| realized_volatility | 0 / n/a | 0 / n/a | n/a | n/a | n/a |
| spread | 396 / 0.0099 | 0 / n/a | n/a | n/a | n/a |
| quote_age_sec | 396 / 1.1397 | 0 / n/a | n/a | n/a | n/a |
| depth_5c | 58 / 262.9898 | 0 / n/a | n/a | n/a | n/a |
| entry_slippage | 56 / -0.0191 | 0 / n/a | n/a | n/a | n/a |
| exit_slippage | 45 / 0.0220 | 0 / n/a | n/a | n/a | n/a |
| fill_rate | 101 / 0.9929 | 0 / n/a | n/a | n/a | n/a |
| order_fill_probability | 197 / 0.4670 | 0 / n/a | n/a | n/a | n/a |
| partial_fill_probability | 197 / 0.1320 | 0 / n/a | n/a | n/a | n/a |
| cancel_probability | 197 / 0.3046 | 0 / n/a | n/a | n/a | n/a |
| cancel_latency_sec | 60 / 62.2316 | 0 / n/a | n/a | n/a | n/a |
| submit_to_fill_sec | 92 / 108.0820 | 0 / n/a | n/a | n/a | n/a |
| pnl | 50 / 0.2293 | 0 / n/a | n/a | n/a | n/a |
| hold_sec | 45 / 286.8734 | 0 / n/a | n/a | n/a | n/a |

## PnL and left-tail

- Weekday paired sell trades n=50; total=11.4645 USDC; win=0.8400; P10/P5/P1=-2.8228/-3.7023/-4.4567.
- Weekend paired sell trades n=0; total=n/a USDC; win=n/a; P10/P5/P1=n/a/n/a/n/a.
- Weekday profit factor / max drawdown / average win / average loss: 1.4399 / 10.7679 / 0.8935 / -3.2578; worst=-4.6948.
- Weekend profit factor / max drawdown / average win / average loss: n/a / n/a / n/a / n/a; worst=n/a.
- Stop-loss exits n=4, total PnL=-11.7860, P1=-3.9066; emergency exits n=0, total PnL=n/a, P1=n/a.
- PnL uses journaled realized_net_usdc on SELL fills, FIFO-paired to observed BUY lots. Missing PnL and settlement/redemption outcomes are not imputed.

## Public historical retrieval

- Requested markets：412；identity resolved：412。
- Gamma success：0；local identity used while Gamma unavailable：1；identifier mismatch：0。
- Trade fetch success/empty/failed/not-fetched：0 / 0 / 1 / 411；matching trades：0。
- Excluded identity-mismatched trades：0。
- Price fetch success/empty/failed/not-fetched：0 / 0 / 1 / 411；price points：0。
- Partial public histories：0；DNS-failed stages：3；markets with DNS failure：1；HTTP-failed stages：0。
- API 回應成功但零筆 trade/price 會標示 empty；網路失敗、識別碼缺失或錯配會保留 structured diagnostics，不會當成零成交。

## Interpretation and limitations

- Weekend is defined by America/New_York Saturday/Sunday; UTC sensitivity is included per market in market_level.csv.
- Statistical differences are associations, not causal evidence; markets are the resampling/comparison units for public liquidity. Empty weekday/weekend groups mean no difference test is available.
- weekday_vs_weekend_utc.csv repeats comparisons with UTC Saturday/Sunday to show timezone sensitivity; primary grouping is the requested ET.
- Compare time_to_resolution.csv for settlement windows. Public trade size and local spread are separate metrics.
- Historical L2 is not available in this journal. Historical price/trade feeds do not reconstruct BBO/depth or fill probability; unknown fields stay empty.
- Local depth observations come only from fill-conditioned markout payloads, so they are selected observations, not an unbiased market-time sample.
- Parameter sensitivity reports observed STRATEGY_START config values only; no unsupported counterfactual replay or live parameter recommendation.
- Public retrieval warnings/not-fetched markets: 412. Structured cache lives in data/polymarket_history/.

## Outputs

- market_level.csv
- trade_level.csv
- execution_level.csv
- weekday_vs_weekend.csv
- weekday_vs_weekend_utc.csv
- public_fetch_diagnostics.csv
- time_to_resolution.csv
- parameter_sensitivity.csv
- liquidity_regime.csv
