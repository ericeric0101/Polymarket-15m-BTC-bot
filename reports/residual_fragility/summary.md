# Residual repricing and entry fragility — offline research

TRACK A verdict: **MIXED** (30s residual→mid repricing; descriptive only)
TRACK B verdict: **MIXED** (loser-vs-winner fragility in the selected settled cohort)
Best candidate signal: **fresh p_ex − fresh market mid residual**, only as a research hypothesis; no live use justified.
Main instrumentation gap: **only 1/14 entries have a valid p_ex observation within 2.0s, and 0/14 have both p_ex and fresh market mid available at entry; executable ask/p_ex edge is unverified for 14/14.**
Live change justified? **NO**

這份報告是離線、描述性分析，不是可交易 edge/PnL，也不是獨立同分布樣本。每秒觀測高度重疊，信賴區間與 bootstrap 以 market_slug 分群。資料不足不等於「沒有趨勢」；以下同時列方向性觀察與可辨識限制。

## Track A — residual repricing

`residual_up = p_up_ex_market - fresh_market_mid_up`。Current 與 future 都要求 p_ex/source 有效且 market quote source age 在 0–2.0s；每個不合格 observation 單獨排除，不會丟掉整場。Future label 取目標 horizon ±1.5s 內最近的 fresh observation，實際 horizon 與 future quote age 均輸出。Mid 是市場 mid，不是可成交價格。

| Horizon | 有效配對觀測 | markets | corr(residual, future Δmid) | 判讀 |
|---|---:|---:|---:|---|
| 5s | 158 | 14 | 0.06265284988549741 | MIXED |
| 10s | 157 | 14 | -0.20357819355609813 | MIXED |
| 30s | 144 | 14 | 0.5553726096708657 | MIXED |
| 60s | 129 | 13 | 0.16802295414030433 | MIXED |

具體趨勢：30s 的 `>=+0.10` residual bin 平均 mid 重估 **0.21349999999999997**（N=10, markets=3, market-cluster CI 0.041999999999999996 to 0.43999999999999995）；相鄰 `+0.05..+0.10` bin 平均 **0.016034482758620672**。這是大正 residual 對 30 秒重估可能有訊息的具體線索，但 bins 不呈全域單調，極端 bin 僅 3 個市場。mid-only 加 residual 的 30s paired MAE delta=2.2106033740009953e-05（95% market-cluster CI -0.006929731578147732 to 0.006035095097723582）；區間跨零，增量尚未證實。完整 bins／CI 在 `residual_bins.csv`；模型比較在 `residual_model_comparison.csv`。OLS 是 in-sample 描述，不可聲稱因果或泛化。

## Track B — entry fragility

| Fragility flag | Losses：命中／可用 | Winners：命中／可用 |
|---|---:|---:|
| Low flip sigma <1σ | 1/1 | 3/6 |
| 10s BTC disagreement | 2/2 | 4/12 |
| Any side flip in 60s | 0/0 unavailable | 0/0 unavailable |
| Thin edge <=0.02 | 0/0 unavailable | 0/0 unavailable |

判讀：**structural fragility=MIXED**（1790920800 的 0.558σ 是可用值中最低，但另一輸家缺值；而低於 1σ 的可用樣本也包含贏家）；**BTC 10s disagreement=YES 作為風險標記但非決策規則**（2/2 輸家、4/12 贏家）；**side instability=NOT MEASURABLE**（本 cohort 的 30/60/120s active-side flip 記錄缺失）；**thin fair-value edge=NOT MEASURABLE**（沒有任何 entry 同時取得 strict fresh p_ex 與可用 fresh ask/mid）。

`entry_fragility.csv` 保留固定 sigma bins、BTC 5/10/30/60s disagreement、30/60/120s side flips、entry fair margin 與可用性；`fragility_matrix.csv` 的 flag count 是診斷，不是 live score。低 sigma 使用既定固定 bins 的 `<1σ`（合併 `<0.5` 與 `0.5–1`）；thin margin 按固定 ≤0.02 描述，不做門檻搜尋。由於模擬 fill 沒有實際成交保證，edge 優先用 entry 前 2 秒內 p_ex 與 side-specific fresh ask；缺 ask 才用同步 fresh mid 並標記 fallback。舊 p_ex 不冒充 entry edge。Analytic opposite probability 是 fresh p_ex 的持倉側補數；empirical probability 僅接受 canonical exact-path replay 的時間匹配值。

### Losers versus winners

| Low flip sigma <1σ | 1/1 | 3/6 |
| 10s BTC disagreement | 2/2 | 4/12 |
| Any side flip in 60s | 0/0 unavailable | 0/0 unavailable |
| Thin edge <=0.02 | 0/0 unavailable | 0/0 unavailable |

上述比例只描述這 14 個 settled shadow fills；兩筆輸家不能支撐任何規則。`winner_loser_fragility.csv` 提供 fixed bins 分組；沒有充分樣本的 bins 保留小 N，不合併、不調參。

## Known loser cases

- `btc-updown-15m-1790920800`: 嚴格 entry 前 fresh p_ex=None，edge=None (不可驗證); 最近 prior p_ex=0.7183256578099558、距 fill 3.1355879306793213s; internal fair=0.745 vs entry price 0.72 (fair-price=0.025000000000000022); opposite sigma=0.5579809595115114 (available sigma rank 1/7). First ≥5pp post-entry p_ex decline at +35.5s. Held-side p_ex <0.5 at +79.5s. Fresh mid <0.5 at +103.0s.
- `btc-updown-15m-1790939700`: 嚴格 entry 前 fresh p_ex=None，edge=None (不可驗證); 最近 prior p_ex=None、距 fill 17.683602333068848s; internal fair=0.775 vs entry price 0.77 (fair-price=0.0050000000000000044); opposite sigma=None (available sigma rank None/7). First ≥5pp post-entry p_ex decline at +3.6s. Held-side p_ex <0.5 at +154.2s. Fresh mid <0.5 at +154.2s.

兩案逐次 post-entry p_ex、mid、TWAP/spot 與 BTC returns 見 `known_loser_case_1790920800.csv`、`known_loser_case_1790939700.csv`。第一個已知 case 的 entry snapshot可能比 event feature 更同步；本報告不把 entry後首次穿越 0.5 解讀成可執行止損或最早可預測時點，只列第一筆觀測時間。

## Telemetry gap audit

嚴格 entry 時點缺 p_ex 的主因可由「最近 valid event 距 entry」與 p_ex 事件間隔（`telemetry_gap_audit.csv`）支持：1/14 在 2 秒內有 p_ex event，但同期 fresh market mid/ask 未形成 join；其餘 entries 的最近 p_ex 已超過 2 秒，其中多場 event 間隔中位數約 3–13 秒、p95 約 13–67 秒。這強烈指向資料事件 cadence/gap 與同步採樣限制；但只靠未寫入的 evaluation event，**不能再區分計算未觸發還是計算結果未被持久化**，所以不能完全歸因於 feed stale。模擬 fill quote 本身另有 freshness 記錄，多數是 fresh；不代表 p_ex 同時 fresh。整場 p_ex/market 覆蓋見 `data_quality.csv`。

## Verdict and next experiment

**CURRENT BEST PATH: E. insufficient telemetry** for a fair incremental-prediction claim. Track A contains visible descriptive relationship(s), especially at longer horizons if bins are directionally ordered, but only 14 markets and repeated observations make the cluster uncertainty decisive. Track B has risk-marker contrasts worth following, but only two losers and strict entry p_ex scarcity prevent fair winner/loser comparison. No live filter or stop change is justified.

Next experiments (maximum two):
1. Prospectively freeze the joined fresh residual → 5/10/30/60s fresh-mid repricing table and evaluate by whole-market/day blocks. Residual hypothesis is falsified if bins are not directionally monotonic or market-cluster CI stays centered around zero after independent market-days accrue.
2. Keep a shadow-only entry-fragility snapshot at simulated/real entry (p_ex, executable ask, required sigma, BTC returns, side flips) and compare future adverse repricing by fixed bins. Fragility hypothesis is falsified if the low-sigma/disagreement/thin-edge markers are equally common in winners and losers and show no association with subsequent adverse repricing.

Telemetry sufficiency: Track A **PARTIALLY** (some valid synchronized rows, small clustered market count); Track B **PARTIALLY** (entry/fill/shadow and BTC data exist, but only 1/14 strict synchronized p_ex entry features and two losers).
