# 週末 vs 平日：BTC 15 分鐘市場流動性、執行與 PnL

## Dataset

- Canonical journal：41,637 order rows、132,582 strategy rows；ok。
- Market rows：245（local journal markets 235；public-history markets 10）；public trades：9,399。
- Journal 實際記錄跨 7 個 UTC 日期、235 個 slug；目前市場週末數 ET=0、UTC=0。
- 查詢 window：2026-07-27T04:00:00+00:00 — 2026-09-21T03:59:59+00:00。
- Journal 實際事件範圍：2026-09-07T11:48:10.161617+00:00 — 2026-09-18T18:15:49.418635+00:00；市場週末時區：America/New_York。
- SELL fill missing PnL：3；excluded shadow rows：4753。

## Weekday vs weekend

| Metric | weekday n / mean | weekend n / mean | difference | 95% bootstrap CI | permutation p |
|---|---:|---:|---:|---:|---:|
| public_trade_count | 5 / 1121.6000 | 5 / 758.2000 | -363.4000 | [-742.0350, -33.7500] | 0.1349 |
| public_volume_shares | 5 / 28831.6228 | 5 / 21014.0207 | -7817.6021 | [-16797.9495, 386.6756] | 0.1439 |
| median_trade_size | 5 / 9.5666 | 5 / 8.6181 | -0.9485 | [-3.2038, 1.4665] | 0.5147 |
| max_no_trade_interval_sec | 5 / 33.2000 | 5 / 38.4000 | 5.2000 | [-19.8000, 28.8000] | 0.5962 |
| realized_volatility | 4 / 0.8570 | 4 / 0.6789 | -0.1781 | [-0.5758, 0.1859] | 0.4933 |
| spread | 223 / 0.0100 | 0 / n/a | n/a | n/a | n/a |
| quote_age_sec | 223 / 1.2156 | 0 / n/a | n/a | n/a | n/a |
| depth_5c | 33 / 221.4233 | 0 / n/a | n/a | n/a | n/a |
| entry_slippage | 31 / -0.0255 | 0 / n/a | n/a | n/a | n/a |
| exit_slippage | 28 / 0.0343 | 0 / n/a | n/a | n/a | n/a |
| fill_rate | 59 / 0.9955 | 0 / n/a | n/a | n/a | n/a |
| order_fill_probability | 89 / 0.5843 | 0 / n/a | n/a | n/a | n/a |
| partial_fill_probability | 89 / 0.1348 | 0 / n/a | n/a | n/a | n/a |
| cancel_probability | 89 / 0.2135 | 0 / n/a | n/a | n/a | n/a |
| cancel_latency_sec | 19 / 138.1108 | 0 / n/a | n/a | n/a | n/a |
| submit_to_fill_sec | 52 / 135.7863 | 0 / n/a | n/a | n/a | n/a |
| pnl | 32 / 0.1909 | 0 / n/a | n/a | n/a | n/a |
| hold_sec | 28 / 346.8747 | 0 / n/a | n/a | n/a | n/a |

## Public market sample

- Sample frame：2026-07-27 至 2026-09-20，時區 America/New_York；母體為此期間每 15 分鐘一個預期 BTC market slot，獨立於 local journal。
- 選取／查詢：weekday 5 個（history 可用 5）、weekend 5 個（history 可用 5）；按每個日期、週別及 6 小時時段分層；市場存在性與 identifiers 由 Gamma 驗證。
- 指標比較只使用取得相應 public history 的市場；未找到市場或 API 失敗的樣本保留在 public_market_sample.csv 診斷，不視為零成交。
- 每組樣本數低於 30 時結果只作 pilot 描述，不據此判定穩定差異。

## PnL and left-tail

- Weekday paired sell trades n=32; total=6.1083 USDC; win=0.8125; P10/P5/P1=-3.3743/-4.0506/-4.5441.
- Weekend paired sell trades n=0; total=n/a USDC; win=n/a; P10/P5/P1=n/a/n/a/n/a.
- Weekday profit factor / max drawdown / average win / average loss: 1.2824 / 10.7679 / 1.0669 / -3.6053; worst=-4.6948.
- Weekend profit factor / max drawdown / average win / average loss: n/a / n/a / n/a / n/a; worst=n/a.
- Stop-loss exits n=2, total PnL=-7.3559, P1=-3.9164; emergency exits n=0, total PnL=n/a, P1=n/a.
- PnL uses journaled realized_net_usdc on SELL fills, FIFO-paired to observed BUY lots. Missing PnL and settlement/redemption outcomes are not imputed.

## Public historical retrieval

- Requested markets：10；identity resolved：10。
- Gamma success：10；local identity used while Gamma unavailable：0；identifier mismatch：0。
- Trade fetch success/empty/failed/not-fetched：10 / 0 / 0 / 0；matching trades：9399。
- Excluded identity-mismatched trades：0。
- Price fetch success/empty/failed/not-fetched：8 / 2 / 0 / 0；price points：88。
- Partial public histories：0；DNS-failed stages：0；markets with DNS failure：0；HTTP-failed stages：0。
- API 回應成功但零筆 trade/price 會標示 empty；網路失敗、識別碼缺失或錯配會保留 structured diagnostics，不會當成零成交。

## Interpretation and limitations

- Weekend is defined by America/New_York Saturday/Sunday; UTC sensitivity is included per market in market_level.csv.
- Statistical differences are associations, not causal evidence; markets are the resampling/comparison units for public liquidity. Empty weekday/weekend groups mean no difference test is available.
- weekday_vs_weekend_utc.csv repeats comparisons with UTC Saturday/Sunday to show timezone sensitivity; primary grouping is the requested timezone.
- Compare time_to_resolution.csv for settlement windows. Public trade size and local spread are separate metrics.
- Historical L2 is not available in this journal. Historical price/trade feeds do not reconstruct BBO/depth or fill probability; unknown fields stay empty.
- Local depth observations come only from fill-conditioned markout payloads, so they are selected observations, not an unbiased market-time sample.
- Parameter sensitivity reports observed STRATEGY_START config values only; no unsupported counterfactual replay or live parameter recommendation.
- Public retrieval warnings/not-fetched markets: 0. Structured cache lives in data/polymarket_history/.

## Outputs

- market_level.csv
- trade_level.csv
- execution_level.csv
- weekday_vs_weekend.csv
- weekday_vs_weekend_utc.csv
- public_fetch_diagnostics.csv
- public_market_sample.csv
- time_to_resolution.csv
- parameter_sensitivity.csv
- liquidity_regime.csv
