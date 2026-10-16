# 統一歷史策略研究

## Data

- 研究期間：2026-07-27 至 2026-09-20，市場分層時區 America/New_York；公開市場按週、日期、weekday/weekend 與 6 小時 ET 區塊分層抽樣。
- 公開樣本：200 個（預期平日／週末各 100）；Gamma 已解析結果 200；BTC 開盤 K 線 200；BTC K 線筆數 80640。
- API 狀態：Gamma success=200；trades success/empty/failed=197/3/0；prices success/empty/failed=171/29/0。
- BTC source：Binance BTCUSDT 1-minute OHLCV。沒有插值成秒級；無法驗證 live 約 6s/20s EMA，也不是 Polymarket 用於結算的 Chainlink 60s TWAP。
- Settlement truth：Gamma `closed` 且 outcomePrices 為決定性 1/0；entry 價用 signal 後 public trade prints，不是 historical ask/BBO；未出現成交 print 即 no-fill。
- 交易日誌：只讀 `logs/trade_journal.db`。公開市場特徵成功 join 到本地 paired trade rows：35；有完整 Gamma winner + token-side mapping 的 stop-loss counterfactual：1 筆。
- Historical BBO/L2 未取得；spread、depth、真實可成交性與 order-book impact 不可回測。

## Liquidity：weekday vs weekend

- 平均 trade count：weekday 928.81、weekend 851.87（差 -76.93999999999994；95% market bootstrap CI [-217.25, 44.08000000000004], permutation p=0.25337331334332835）。
- 平均 share volume：weekday 27185.07943323、weekend 23133.9945088（差 -4051.0849244300007；95% CI [-10740.636271510004, 1515.1614354599988], p=0.21689155422288856）。方向上週末較低，但 CI 含 0，不能說已證明差異。
- Weekly blocked volume：8 個 paired weeks 中 7 週週末較低；median weekly difference=-4746.659820733976 shares；95% block bootstrap CI=[-10026.021964927884, 2595.0118717443906], exact sign-flip p=0.2578125。
- Low-liquidity composite tail share：weekday 0.26、weekend 0.41；週末樣本較常落在樣本內低三分位，但這是 rank-based composite、100/group 小樣本，不能單獨作為因果解釋。
- settlement 前 60/30/15 秒、ET 時段交互作用、每週 paired 結果請見 `public_time_to_resolution_comparison.csv`、`public_weekend_hour_comparison.csv`、`public_weekly_blocked.csv`。

## Simple strategy：180 秒等候、5 bps 門檻

- All sample：N=73; win=0.7671; entry=0.7485; EV/trade=0.0929; net=$6.7826; ROI=0.93%; 95% block CI EV=[-1.1303, 1.3169]；gross PnL=$14.0826，假設費用=$7.3000（notional 的 1% 情境，不代表歷史實際 fee）。
- Development：N=44; win=0.7727; entry=0.7584; EV/trade=0.0659; net=$2.9010; ROI=0.66%; 95% block CI EV=[-1.6676, 1.7795]。
- Holdout：N=29; win=0.7586; entry=0.7336; EV/trade=0.1338; net=$3.8816; ROI=1.34%; 95% block CI EV=[-1.5642, 1.8267]。Holdout 的點估計為正，但 EV CI 跨 0；不能宣稱已驗證正 EV。
- Break-even win rate 約等於平均 entry price 0.7485310477686228；觀察 win rate 0.7671232876712328。信賴區間與樣本依賴性仍不足以排除零 edge。
- 60/120/180/240/300 秒（5 bps，5s VWAP，零滑點，fee stress）all-sample net PnL：60s=$25.6526 (n=45, win=73.33%, entry=0.6880); 120s=$20.9338 (n=60, win=75.00%, entry=0.7250); 180s=$6.7826 (n=73, win=76.71%, entry=0.7485); 240s=$13.7370 (n=78, win=78.21%, entry=0.7644); 300s=$-47.1024 (n=91, win=76.92%, entry=0.7917)。240 秒 holdout 點估計較高，但 development 為負，屬不穩定選參數，不是可部署優勢。
- 180 秒 holdout confidence intervals：win rate [0.6764705882352942, 0.8571428571428571]; EV/trade [-1.090097548253921, 1.3218934083765705]; ROI [-0.10900975482539209, 0.13218934083765704]，以日期為 block bootstrap。
- 180 秒 threshold holdout table見 `trend_threshold_sensitivity.csv`：0/2/5 bps 的點估計方向偏正、信賴區間跨 0；10/15/20 bps 樣本很少且點估計偏負。不要把 development 最佳值當成 live 門檻。
- Entry-price、trend-strength bucket、signal disagreement 與 weekday/weekend performance 分別在 `entry_price_buckets.csv`、`trend_strength_buckets.csv`、`signal_disagreement.csv`、`weekday_weekend_backtest.csv`。

## Controls / incremental information

- BTC 180s direction: N=62; win=0.6774; entry=0.6523; EV/trade=0.2994; net=$18.5635; ROI=2.99%.
- Polymarket leader: N=63; win=0.6825; entry=0.6555; EV/trade=0.3980; net=$25.0709; ROI=3.98%.
- Always UP: N=57; win=0.5439; entry=0.5702; EV/trade=-0.8306; net=$-47.3432; ROI=-8.31%.
- Always DOWN: N=53; win=0.4717; entry=0.4762; EV/trade=-0.5963; net=$-31.6028; ROI=-5.96%.
- Random side: N=57; win=0.5614; entry=0.5519; EV/trade=-0.3532; net=$-20.1324; ROI=-3.53%.
- 1m/3m EMA proxy: N=63; win=0.6190; entry=0.6459; EV/trade=-0.7491; net=$-47.1911; ROI=-7.49%.
- BTC vs market leader agreement/disagreement 的 accuracy 只供描述；disagreement 樣本較小，且不是可成交報價比較，不能確認 BTC signal 有穩定增量資訊。

## Local bot outcomes and exits

- Local journal 目前僅有平日已實際成交市場；週末本地交易樣本為 0，因此 local PnL 無法估週末效果。
- 有完整 winner/token mapping 的 stop-loss trade：1 筆；counterfactual 在 `local_stoploss_counterfactual.csv`。本次結果不足以概括止損是救風險還是過早出場。
- Local liquidity/PnL join 的樣本小且依賴公開 market history；請見 `local_public_join.csv`、`local_liquidity_vs_pnl.csv`。不應因 regime 切片差異直接放寬/新增 live gate。
- Current profile `FIRST_ENTRY_MAX_TIME_LEFT_SEC=780` 約等於開盤後 120 秒首次進場窗口；此處 180 秒 simple baseline 是另一個純研究策略。Live bot 有訊號、economics、止盈/止損與 execution gate，結果不可視為 apples-to-apples。

## Evidence verdict

- **資料支持（描述性）**：本次分層樣本中，週末平均成交量/筆數較低，且週末低 composite liquidity 區比例較高；weekly/market CI 與 p-values 尚不支持穩定差異，hour/settlement slices 亦需視為探索性。
- **提示但不確定**：180 秒 BTC trend 5 bps 策略在本樣本 development/holdout 點估計皆為正，但日期 block CI 跨 0、樣本只有 73 筆成交；無 robust positive-EV 結論。
- **未獲支持**：目前證據不支持某個 observation window 或 threshold 已有可泛化優勢；240 秒的 holdout 好結果與負 development 不一致。
- **現有資料無法評估**：歷史 ask/BBO、L2 depth、真實 taker fill/slippage、6s/20s EMA、Chainlink 開盤/結算價同步，以及大部份本地交易的 hold-to-settlement counterfactual。

## Limitations / reproduction

- Public universe 是每組 100 個分層樣本，不是期間內所有市場普查；API empty/failure 保留在 `public_market_sample.csv`、`fetch_diagnostics.csv` 與 `data_quality.csv`，不當作零成交。
- Fees：同時提供 zero-fee 與明示 1% notional stress scenario；後者不是聲稱歷史費率。交易 prints VWAP 仍不保證該價可成交。
- 8 週、8 個 paired weekly blocks 的檢定力有限；multivariate count OLS 是探索性，未套 Negative Binomial。matplotlib 不在此 venv，沒有輸出 PNG charts。
- 可用已快取資料離線重跑：`scripts/analyze_weekend_liquidity.py --offline` 與 `scripts/backtest_simple_trend_hold.py --offline`；BTC cache 位於 `data/btc_history/`。
- 本報告是歷史研究，不修改 live trading behavior，也不構成 live 參數建議。
