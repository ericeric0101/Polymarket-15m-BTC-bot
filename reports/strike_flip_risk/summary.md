# Strike Proximity / Late Flip Risk 研究

## 資料覆蓋

- 市場：200（weekday 100、weekend 100）；Gamma decisive winners：200。
- Public cache retrieval：trades {'success': 197, 'empty': 3}；price history {'empty': 29, 'success': 171}。Gamma metadata success=200。
- strike provenance：Gamma official=0；verified Polymarket TWAP/Chainlink reference=6；相鄰市場 Binance proxy=194；unavailable=0。
- Binance BTCUSDT 1m candles loaded=80640。BTC is a historical proxy, not Polymarket's Chainlink settlement feed. No sub-minute interpolation is used; T-30s/15s/5s/1s remain unavailable.
- BTC boundary proxy vs verified local strike: N=6, median abs error USD=36.926, P90=62.219, max=73.685.
- Previous local settlement reference vs next verified strike adjacent pairs=6; exact 15m pairs in sampled market set=0 (see CSV).
- Gamma cache contained no Price-To-Beat field in the sampled markets. Verified local Polymarket crypto TWAP opening reference was used where present; Binance boundary close is a low-confidence fallback only.

- Strike-confidence limitation: only six sampled markets have verified local TWAP openings; 194 use a Binance one-minute boundary proxy. That proxy's error against verified strikes has median and P90 several bps, so pooled `<1/2/5 bps` results are not canonical strike findings.

## 核心結果（低信度 Binance boundary proxy 樣本；探索性，不可解讀為 canonical strike 結論）

- Mean lower-bound crossing count per market: weekday 1.340, weekend 1.180.
- T+3m weekend-minus-weekday mean absolute distance: -3.450 bps (95% bootstrap CI -5.314 to -1.776; market permutation p=0.001).
- T+5m weekend-minus-weekday mean absolute distance: -5.191 bps (95% bootstrap CI -7.242 to -3.270; market permutation p=0.001).
- T-2m weekend-minus-weekday mean absolute distance: -5.980 bps (95% bootstrap CI -9.398 to -3.085; market permutation p=0.001).
- T-1m weekend-minus-weekday mean absolute distance: -5.691 bps (95% bootstrap CI -8.985 to -2.432; market permutation p=0.001).
- T-5m leader flips before Gamma final winner: weekday 28.7%, weekend 29.6%; difference CI -0.116 to 0.133, p=1.000.
- T-2m leader flips before Gamma final winner: weekday 10.6%, weekend 9.1%; difference CI -0.098 to 0.069, p=0.812.
- T-1m leader flips before Gamma final winner: weekday 6.4%, weekend 6.1%; difference CI -0.075 to 0.070, p=1.000.
- Lower-bound crossings/weekend minus weekday: -0.160; 95% bootstrap CI -0.634 to 0.295, p=0.495.
- ET 06–12 at T-1m: weekday mean abs proxy distance=12.565bps, flip=8.0% (n=25); weekend=6.707bps, flip=4.0% (n=25). Proxy-only; small cells.

### Near-strike share (LOW_PROXY only)

- T+3m: <1bps weekday 12.8% (n=94), weekend 35.0% (n=100); <2bps weekday 20.2% (n=94), weekend 48.0% (n=100); <5bps weekday 53.2% (n=94), weekend 72.0% (n=100).
- T+5m: <1bps weekday 9.6% (n=94), weekend 28.0% (n=100); <2bps weekday 14.9% (n=94), weekend 41.0% (n=100); <5bps weekday 37.2% (n=94), weekend 67.0% (n=100).
- T-2m: <1bps weekday 4.3% (n=94), weekend 18.0% (n=100); <2bps weekday 5.3% (n=94), weekend 29.0% (n=100); <5bps weekday 27.7% (n=94), weekend 54.0% (n=100).
- T-1m: <1bps weekday 5.3% (n=94), weekend 16.0% (n=100); <2bps weekday 6.4% (n=94), weekend 25.0% (n=100); <5bps weekday 25.5% (n=94), weekend 53.0% (n=100).

## Distance-conditioned flip rates

- T-1m <1bps: weekday n=5 flip=40.0%; weekend n=14 flip=28.6%.
- T-1m 1-2bps: weekday n=1 flip=0.0%; weekend n=9 flip=0.0%.
- T-1m 2-5bps: weekday n=18 flip=11.1%; weekend n=28 flip=3.6%.
- T-1m 5-10bps: weekday n=24 flip=8.3%; weekend n=17 flip=0.0%.
- T-1m >10bps: weekday n=46 flip=0.0%; weekend n=30 flip=3.3%.

## ET 06–12 與模型

- `hour_block_flip_risk.csv` compares weekday/weekend distance and flip rate at each core timestamp, including ET 06–12.
- `weekend_effect_models.csv` reports exploratory weekend-only and adjusted T-1m logistic models; adjusted model includes distance, prior-only safety sigma, recent crossings and hour blocks. Coefficients/approximate Wald intervals are not causal evidence.
- Weekend coefficient change is descriptive; interpret only if both models use the same rows. Both 95% intervals cross zero; there is no evidence here of a positive weekend flip-risk effect. Sparse cells and 200-market sampling limit power.
- The adjusted T-1m weekend log-odds coefficient uses the same 198 markets as the weekend-only model; the coefficient and change are in `weekend_effect_models.csv`. Here it moves farther below zero rather than shrinking toward zero, but both intervals cross zero.
- ET 06–12 cross-tabs are in `hour_block_flip_risk.csv`; strike distance and flips inherit the low-confidence proxy limitation.

## 180 秒策略關聯

- 180s/5bps, <1bps: n=0, win=n/a, mean net PnL=$n/a, mean entry=n/a.
- 180s/5bps, 1-2bps: n=0, win=n/a, mean net PnL=$n/a, mean entry=n/a.
- 180s/5bps, 2-5bps: n=0, win=n/a, mean net PnL=$n/a, mean entry=n/a.
- 180s/5bps, 5-10bps: n=36, win=77.8%, mean net PnL=$0.582, mean entry=0.723.
- 180s/5bps, >10bps: n=33, win=75.8%, mean net PnL=$-0.526, mean entry=0.780.
- 180s/5bps proxy sample correlations: |trend| vs net PnL=0.003; |distance| vs net PnL=0.003; entry price vs net PnL=0.128; |trend| vs |distance|=1.000 (n=69). 高度共線且為描述性 print-based 指標，不能判定 distance 比 trend 更能解釋損益；本研究也沒有同期可比的 liquidity feature。
- Time in <1bps zone (observed minute bars / market): weekday 1.060 min, weekend 3.720 min; low-confidence proxy-derived.
- Time in <2bps zone (observed minute bars / market): weekday 2.000 min, weekend 5.450 min; low-confidence proxy-derived.
- Time in <5bps zone (observed minute bars / market): weekday 5.530 min, weekend 9.280 min; low-confidence proxy-derived.

## 相鄰市場與限制

- Verified previous-market settlement reference vs next-market strike: N=6; median absolute difference=$5.578 (0.718bps), P90=$39.192; within $1 match=2/6. The sample is sparse and only tests available local consecutive markets.
- 1-minute crossing counts are lower bounds and cannot resolve multiple flips within a candle. Gamma winner remains outcome truth; Binance is only the spot path proxy.
- Historical strikes absent from Gamma/local verified cache are reconstructed from the last closed Binance minute at the market boundary and marked low-confidence. Do not promote these to canonical strike.
- No live rules were changed. This is descriptive research; near-strike buckets are exploratory and not live thresholds.
- Bootstrap/permutation comparisons use markets as units (1,000 deterministic resamples); small samples and sampling design limit power.

## Interpretation

- Strike proximity is a plausible mechanism for late flip exposure, but weekend causality and incremental predictive value require the supplied tables' confidence intervals, matched samples, and the explicit data-quality labels.
- `evidence_verdict.csv` gives explicit Supported / Suggestive / Not supported / Cannot assess labels. Current strike coverage is inadequate for canonical proximity conclusions; distance-vs-liquidity cannot be assessed because no matched liquidity feature is in this dataset.
- Strategy trend and strike distance are nearly collinear in this sample, and outcome uses public trade-print proxies rather than executable BBO. The available correlation comparison cannot establish which is more explanatory or causal.


## 假說判定

| 假說 | 判定 | 依據與限制 |
|---|---|---|
| H1 weekend closer to strike | Suggestive (proxy only) | Weekend proxy distance is lower at key timestamps, but 194/200 strikes are Binance proxies and verified-strike sample has no weekend markets. |
| H2 weekend spends more time near strike | Suggestive (proxy only) | LOW_PROXY danger-zone duration is descriptively longer on weekends; Binance boundary error is several bps, so canonical rates cannot be established. |
| H3 weekend has more crossings | Not supported (proxy sample) | LOW_PROXY weekend-minus-weekday total crossings=-0.16042553191489373; CI includes zero and minute counts are lower bounds. |
| H4 weekend leader persistence is lower | Not supported (proxy sample) | LOW_PROXY T-1m flip rates do not show a weekend increase; difference CI includes zero; winner leader depends on proxy strike. |
| H5 weekend late flip rate is higher | Not supported (proxy sample) | T-5m/T-2m/T-1m proxy contrasts are near zero or negative with intervals crossing zero; low power and strike-source limitation remain. |
| Weekend effect shrinks after distance/safety adjustment | Cannot assess | Both logistic weekend intervals cross zero and the adjusted coefficient moves farther negative, not toward zero; estimates are imprecise and not causal. |
| Distance explains strategy danger better than trend/liquidity | Cannot assess | Trend and distance are highly collinear; outcome is trade-print proxy, and no matched liquidity feature or out-of-sample comparison exists. |
| Canonical weekend strike-proximity effect | Cannot assess with current strike coverage | Only 6 verified local strikes (all weekdays); 0 Gamma official strikes. Obtain canonical Chainlink/RTDS strikes for a balanced sample. |