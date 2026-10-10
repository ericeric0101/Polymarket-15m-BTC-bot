# Post-LIVE Deep Audit（FINAL）— cross vs conditional absolute breaker、虧損歸因、週末／波動 regime

日期：2026-10-10（Asia/Taipei）。全程唯讀：bot 已停止（無 python/run_bot 程序；`logs/trade_journal.db.writer.lock` 內 pid=82587 已不存在），所有 SQLite 以 `mode=ro`／`immutable=1` 開啟，未 VACUUM/checkpoint、未複製 journal、未改 runtime/config/schema、未新增 shadow event、未連網、未 commit/push。分析腳本放在 session scratchpad（資料目錄之外），只輸出本目錄五個檔案。

證據標記：**VERIFIED**（直接讀自資料／程式碼）、**INFERRED**（由 VERIFIED 推論，附推論鏈）、**UNVERIFIED**（需網路或資料未記錄）、**INSUFFICIENT**（樣本不足）。

---

## 0. Run 辨識（判讀先寫在最前）

`strategy_runs` 中 telemetry commit（`6b4d829`，2026-10-10 00:35 +08）之後有 5 個 LIVE run_id，皆為 HEAD `f1a134e`。前兩個（01:03–06:35 +08）是台北週六 observation-only，零下單（已由 `reports/overnight_no_entry_20261010/` 處理）；`run_1791585417_658f4b3c`（06:37）是乾淨 HEAD、3 分鐘即中止、無 ended_at。06:40 起才套用未 commit 的 weekend-unlock patch。

**判讀：** 最新的 LIVE run 是 **同一個 process** `process_1791585579_54482_04a0880d`（pid 54482），內含兩個 cycle／run_id：`run_1791585596_c07cfcd9`（cycle 1）與 `run_1791589295_6eae032b`（cycle 2，最新 run_id）。cycle 1→2 不是重啟，而是 23:40:46 UTC `QUOTE_WATCHDOG_TRIGGERED(quote_subscription_timeout)` 觸發的 process 內重建（`stop_request_source=quote_watchdog_recovery`）。VERIFIED（`COLLECTION_LIFECYCLE` pid/cycle_idx）。因此本報告以整個 process 為「本次 run」；若只看最新 run_id，只有 1 筆持倉（1791589500），兩種讀法的表格都以 `run_cycles` 欄區分。

```
RUN_ID=process_1791585579_54482_04a0880d (cycles run_1791585596_c07cfcd9 + run_1791589295_6eae032b)
START_TIME=2026-10-09T22:40:30Z (2026-10-10 06:40:30 +08)
END_TIME=2026-10-10T00:16:12Z (2026-10-10 08:16:12 +08)
HEAD=f1a134e999edd3f2e03fff80f22a67b8a13242ec + dirty diff b8ae4cdf…7be5 (bot/entry_session_policy.py 週末 BUY 解鎖；未 commit)
NUMBER_OF_COMPLETE_MARKETS=5（ENTRY 3、NO_ENTRY 2）
NUMBER_OF_WARMUP_OR_PARTIAL_MARKETS=3（1791585000 warm-up、1791588600 被 cycle 重建切斷、1791591300 停機截斷）
```

## 1. Cohort 邊界（Section 2）

| 邊界 | Commit | 時間（+08） | 與本次的關係 |
|---|---|---|---|
| settlement authority | `bbbdc09` | 10-09 12:09 | 本次在其後 |
| share_v1 sizing | `7640a44` | 10-09 19:17 | 本次在其後（0.69→10 股；0.74/0.82→5.5 股，VERIFIED） |
| protective-exit pause fix | `14ec69f` | 10-09 20:22 | 本次在其後 |
| multiplier 語意（第二 sizing 邊界） | `4a8ea1a` | 10-09 21:45 | 本次在其後 |
| stop-timing telemetry | `6b4d829` | 10-10 00:35 | 本次在其後 |
| 本次 HEAD | `f1a134e` + dirty `b8ae4cdf` | 10-10 00:53 | — |

歷史 LIVE 比較集（`flip_absolute_retained_audit` 的 125 市場，2026-09-10→09-30，13 天，官方 Gamma 結果）全部在 share_v1／bbbdc09 **之前**，因此只做 per-share／per-notional 比較，不合併 PnL。Tier：同時段 TWAP 比較使用 native v2 `PREDICTION_RESEARCH_SNAPSHOT`（second-opinion 所稱 Tier 1 性質）；Binance 波動比較是 proxy，不屬於 tier 系統，另行標示；未使用任何 Tier 3／被排除資料。

## 2. 市場分類與 PnL（Section 3）

| 市場（+08 開始） | 類別 | 持倉 | 進場 | 股數 | 成本 | 已實現 PnL | runtime 結果（來源，margin） |
|---|---|---|---|---|---|---|---|
| 06:30 1791585000 | INVALID_PARTIAL（warm-up） | — | — | — | — | 0 | UP（canonical_twap，+12.95 bps） |
| 06:45 1791585900 | NO_ENTRY | — | — | — | — | 0 | UP（canonical_twap，+0.72 bps） |
| 07:00 1791586800 | ENTRY | DOWN | 0.69 | 10.0 | 6.90 | **+2.8003** | DOWN（canonical_twap，−1.09 bps） |
| 07:15 1791587700 | ENTRY | UP | 0.82 | 5.5 | 4.51 | **−3.1549** | DOWN（**canonical_twap_deferred_relabel**，−0.95 bps） |
| 07:30 1791588600 | INVALID_PARTIAL（cycle 重建切斷） | — | — | — | — | 0 | UP（+1.72 bps） |
| 07:45 1791589500 | ENTRY | UP | 0.74 | 5.5 | 4.07 | **−3.1454** | DOWN（**canonical_twap_deferred_relabel**，−0.41 bps） |
| 08:00 1791590400 | NO_ENTRY | — | — | — | — | 0 | DOWN（deferred_relabel，−0.13 bps） |
| 08:15 1791591300 | INVALID_PARTIAL（停機） | — | — | — | — | — | — |

- 合計已實現 **−3.5000 USDC**（per notional：−3.50/15.48 = −22.6%）。VERIFIED（fills 重算與 `MARKET_CYCLE_PNL` 一致：例 1791587700 = 5.4945×0.25 − 5.4945×0.82 − fee 0.01854 − 殘量 0.0055×0.82 = −3.1549）。
- 進場細節（VERIFIED，`ORDER_SUBMIT`/`ORDER_FILLED`/`FILL_MARKOUT`）：

| 市場 | score_abs | entry TTE | strike | 進場 TWAP | 距 strike（held 方向） | legacy σ | depth cap / multiplier | fill latency | 手續費 |
|---|---|---|---|---|---|---|---|---|---|
| 1791586800 DOWN | 0.250 | 580.8 s | 82561.02 | 82547.97 | +1.44～1.58 bps | 0.21 | 10 股；size_multiplier 1.0 | 16.3 s（分兩筆 9.04+0.96） | 0（maker） |
| 1791587700 UP | 0.403 | 582.9 s | 82551.99 | 82608.79 | +6.9 bps | 0.75 | 5.5 股；1.0 | 6.6 s | 進 0；出 0.0185 |
| 1791589500 UP | 0.301 | 578.9 s | 82559.15 | 82607.18 | +5.8 bps | 0.68 | 5.5 股；1.0 | 11.0 s | 進 0；出 0.0095 |

- **官方結果：UNVERIFIED。** 版本化官方 resolution cache（`data/research_export/official_resolution/`，最後 fetch 2026-10-09 04:16Z）不含今天的市場，且禁止連網。兩筆虧損市場都是 near-tie（|margin| < 1 bps，近 tie band = 1.0 bps）並由 deferred relabel 標成 DOWN；若官方結果相反，持有到結算會贏，breaker 反而造成損失。這是本報告最大的單一不確定性。
- 殘量：1791586800 剩 0.01 DOWN 股（`PROTECTIVE_EXIT_UNSELLABLE`，若官方 DOWN 約值 0.01 USDC）；兩筆虧損各剩 0.0055 UP 股（若 DOWN 則歸零）。之後每次 `AUTO_REDEEM_RUN` 的 `redeemable_positions_found=0`；venue/錢包狀態 UNVERIFIED（需網路）。

## 3. 技術有效性（Section 4）— **run 技術上有效，附條件**

| 項目 | 結果 | 標記 |
|---|---|---|
| 重複 BUY/SELL | 無；每市場 1 次 BUY（`ORDER_SKIP_MARKET_BUY_LIMIT` 生效），1791586800 的 9.04+0.96 是同一張單的兩筆 fill | VERIFIED |
| ghost/orphan | `ORDER_CANCEL_REJECTED` 1 次（23:05:20，該 BUY 已在 23:05:19 成交，屬良性競態）；無 orphan 事件 | VERIFIED（本地） |
| 本地／venue 不一致 | 本地無跡象；venue 端 | UNVERIFIED |
| oversell／負庫存 | 無；SELL 9.99/10、5.4945/5.5（sellable haircut） | VERIFIED |
| share_v1 正確性 | 0.69→10 股、0.74/0.82→5.5 股；5.5×0.75=4.12<5.5 正確 skip | VERIFIED |
| 意外的 balance skip | 無 | VERIFIED |
| reject／retry | 1791587700 breaker 第一張 FOK@0.23 被拒（"couldn't be fully filled"），經 20 s reject cooldown 後 23:28:22 重送，23:28:24 以 **0.25** 成交（比第一張價好）；一次 post-only crosses book（BUY，良性） | VERIFIED |
| telemetry 例外 | exceptions=0、slow_calls=0、capped=0、sink_rejected=0；STOP_TIMING 事件 43 筆、payload 91.5 KB | VERIFIED |
| stale quote | 所有 telemetry 列 `orderbook_state=FRESH_BY_PROTECTIVE_GATE`；1791587700 有 1 次 `orderbook_stale` degraded | VERIFIED |
| strike／reference 缺失 | 所有市場 strike 為 `polymarket_crypto_price_twap_open`、verified；每個市場都有 settlement 連結 | VERIFIED |
| protective-exit REAL path | 觀察到 2 次 `absolute_max_loss_breaker` 實單（LIVE） | VERIFIED |
| DEGRADED 狀態 | 有。每筆持倉 20–22 次 `PROTECTIVE_EXIT_DEGRADED(reference_feed_stale_partial_trend_inputs)`；1791587700 持倉期間 TWAP WS 斷線（23:16 service restart 1012、23:21:04 silent stall、兩次 timeout），`TWAP_REFERENCE_DEGRADED` age 56.9 s，telemetry `obs_interval_sec=82.8 s`（23:20:17→23:21:40 這段沒有 protective evaluation 走到 telemetry 掛點） | VERIFIED；「保護性評估中斷約 83 s」為 INFERRED（telemetry 掛在 `ExitPolicyEngine.evaluate` 之後） |
| pause／kill switch | 無 pause/kill 事件 | VERIFIED |
| cycle 重建 | 23:41:06–23:42:10 約 64 s 沒有 strategy（quote watchdog recovery），當時庫存 0 | VERIFIED；若持倉時發生的影響 UNVERIFIED |

儲存：

```
DB_SIZE_BEFORE=UNVERIFIED（最近量測：2,390,507,520 B @ 2026-10-09T21:25Z，前一個 run 進行中）
DB_SIZE_AFTER=2,417,172,480 B（2305.2 MiB）
DB_GROWTH_MIB<=25.4（自 21:25Z 起的上界，含前一 run 最後約 70 分鐘）；本 process 列位元組估計 ≈10.5 MiB（INFERRED，不含索引）
MIB_PER_ACTIVE_HOUR≈6.6（列位元組估計，INFERRED）；上界≈15.8
STOP_TIMING_EVENT_COUNT=43
STOP_TIMING_ESTIMATED_SIZE≈91.5 KB payload
BACKUP_RESULT=PASS（sqlite_online_backup 完成於 00:16:17Z；本次以 immutable=1 跑 PRAGMA quick_check = ok，22.4 s）
BACKUP_SIZE=2,417,127,424 B
FREE_SPACE_BEFORE≈12.33 GiB（21:25Z 快照）/ MIN=UNKNOWN（無連續監測）/ AFTER=12.60 GiB（df）
ICLOUD_SYNC=UNVERIFIED（repo 不在 iCloud 路徑；未檢查 CloudDocs）
```

另：graceful exit 的 journal retention 兩次都以 `KeyboardInterrupt` 中止（22:35Z、00:16Z），在 `day_id_bounds` 階段；backup 在中止前已完成。VERIFIED（`logs/journal_retention.log`）。

## 4. 每筆持倉時間線：cross vs conditional absolute breaker（Section 5）

術語：`conditional_absolute_loss_breaker`，設定 `absolute_max_loss_usdc=2.00`、`min_hold=60 s`（VERIFIED，run manifest `safe_config`）。觸發式（`absolute_breaker_components` 鏡像）：min_hold ∧ price_adverse ∧ adverse_trend_confirmed ∧ net≤−2.00 ∧ (TTE≤120 ∨ (persistence≥15 s ∧ votes 2/2))。`adverse_trend_confirmed` = locked-side invalidated 或強反向訊號，**不是** strike cross。

時間基準：`obs_wall_ts` = protective evaluation 時的主機 wall clock（觀察的 event time），一般 ≈5 s 網格（終局較密）。第一次觀察到的 cross 是下界。1 s canonical snapshot（`PREDICTION_RESEARCH_SNAPSHOT`）用來補路徑。完整欄位見 `stop_timing_table.csv`。

### 4.1 1791587700（UP @0.82×5.5，−3.1549）

| 時間（UTC） | TTE | 事件 | bid | net_if_exit | TWAP 距 strike（held 方向） |
|---|---|---|---|---|---|
| 23:20:17 | 583 | ENTRY | 0.82 | −0.06 | +6.88 bps |
| 23:21:40 | 500 | DEGRADED（TWAP 停 56.9 s；前一次觀察在 82.8 s 前）；min_hold、price_adverse | 0.70 | −0.73 | 未知 |
| 23:23:40 | 380 | votes 2/2 第一次為真 | 0.62 | −1.17 | +3.76 |
| **23:26:31** | **208** | **LOSS_THRESHOLD 第一次為真** | **0.43** | **−2.19** | **+3.91（仍有利）** |
| 23:26:48 | 193 | persistence≥15 s | 0.20 | −3.42 | +3.40 |
| 23:27:19 | 162 | ADVERSE_TREND 第一次為真 | 0.17 | −3.59 | +0.98 |
| **23:27:34** | **146** | **第一次 ADVERSE CROSS** | 0.19 | −3.48 | −0.22 |
| 23:28:00 | 120 | TTE≤120 → **BREAKER ELIGIBLE** → FOK@0.23 送出 | 0.23 | −3.26 | −1.43 |
| 23:28:02 | — | FOK 被拒（無法全數成交） | — | — | — |
| 23:28:22→24 | 98 | 重送 → **FILL @0.25** | 0.23 | 實現 −3.15 | −1.52 |

- 第一次 cross 比 loss threshold **晚 62 s**；cross 時 net 已是 −3.48（超過門檻）。cross→eligible +26 s；cross→成交 +50 s。
- **BREAKER_BOTTLENECK：TREND（46 s：208→162）→ TTE/VOTES（42 s：162→120）→ EXECUTION（24 s：FOK 拒單 + 20 s cooldown）。** 依現行規則，這些延遲都是刻意設計。TTE/VOTES 段的歸因屬 INFERRED：162→120 之間其餘條件都已為真，votes 必定為假（或 persistence 重置）。
- 等待有沒有幫助：從 threshold 到成交，bid 0.43→0.25；崩跌主要發生在門檻後 17 s 內（0.43→0.20）。之後等待到 TTE 120 並沒有讓情況更差（0.17→0.23→成交 0.25）。

### 4.2 1791589500（UP @0.74×5.5，−3.1454）

| 時間 | TTE | 事件 | bid | net | TWAP 距 strike |
|---|---|---|---|---|---|
| 23:50:21 | 579 | ENTRY（開倉時 reference DEGRADED age 5.2 s；1 s markout −0.065） | 0.64 | −0.62 | 未知 |
| 23:51:02 | 538 | votes 2/2 | 0.50 | −1.38 | +5.80 |
| **23:56:58** | **182** | **LOSS_THRESHOLD** | **0.35** | **−2.18** | **+3.15（仍有利）** |
| 23:57:33 | 146 | ADVERSE_TREND | 0.15 | −3.25 | +0.93 |
| **23:57:50** | **131** | **ADVERSE CROSS** | 0.17 | −3.15 | −0.41 |
| 23:58:00 | 120 | TTE≤120 → ELIGIBLE → FOK@0.17 | 0.17 | −3.15 | −1.19 |
| 23:58:02.8 | — | FILL @0.17 | — | 實現 −3.15 | — |

- cross 比 threshold **晚 51 s**；cross→eligible +11 s；cross→成交 +14 s。
- **BOTTLENECK：TREND（35 s）→ TTE（27 s）。** `abs_persistence_ge_15s` 整段從未出現 first-true（VERIFIED；telemetry 未停用、無 capped），所以 persistence∧votes 分支不可能成立，只能等 TTE 分支。執行 2.3 s。
- 等待：bid 0.35→0.17。崩跌在 threshold 後約 35 s 內完成；cross 之後 PnL 幾乎不變（+0.001）。

### 4.3 1791586800（DOWN @0.69×10，+2.8003，贏家；作為對照）

- **LOSS_THRESHOLD 在 TTE 350 為真**（bid 0.45，net −2.49）；votes 同時為真、persistence 在 TTE 335 為真，但 ADVERSE_TREND 從未為真，所以 breaker **從未 eligible**。之後回升，TP 在 23:14:25 以 0.97 成交 9.99 股。VERIFIED。
- 第一次 ADVERSE CROSS 在 TTE 71.8（bid 0.65）；5 s 時仍 adverse，15 s 時因 reference DEGRADED 而 None，30 s 時仍 adverse。TWAP 約在 TTE 22 才重新回到有利側，那時已 TP 出場，所以 recross 被 censor。這次 cross 是**假警報**：cross 當下 token bid 從 0.65 漲到 0.86，因為 Binance 對 DOWN 有利（`btc_return_30s` −3.3 bps）。

## 5. 每筆虧損的關鍵停損問題（Section 6）

| 問題 | 1791587700 | 1791589500 |
|---|---|---|
| A. cross 發生時虧損是否明顯小於門檻？ | **否**：cross 時 net −3.48，threshold 早 62 s 已觸及 | **否**：cross 時 net −3.15，threshold 早 51 s |
| B. 早多少、多虧多少 | 不適用（cross 較晚）；threshold→cross 期間 PnL 再惡化 1.29 | 不適用；惡化 0.97 |
| C. cross 後是否回歸？ | 否（TWAP 持續 adverse 到結算；結算 margin −0.95 bps） | 否（−0.41 bps） |
| D. 立即／5／15／30 s 出場會被 whipsaw 嗎？ | 在虧損單上不會（持續 adverse）；在贏家單上全部會 | 同左 |
| E. cross 時 σ 分桶 | 0.378 → <0.5 | 0.444 → <0.5 |
| G. breaker 狀態 | 相對 cross 大致同時（+26 s）；相對 threshold 晚 88 s；先被 TREND、再被 TTE/VOTES 擋住；另有執行延遲 24 s | 相對 cross 大致同時（+11 s）；相對 threshold 晚 63 s；被 TREND、TTE 擋住 |

F. 高 σ 與「cross 後不回歸」：3 次 cross 中，唯一在 0.5–1 桶的（贏家，0.66）回歸了；兩次 <0.5 的沒有回歸。這只是描述，n=3 → **INSUFFICIENT**。

**新 telemetry 能否證明 adverse cross／σ／TTE 是比 loss breaker 更早的警訊？→ NO（n=2 虧損單，加 1 次假警報）。** VERIFIED 時序：兩筆虧損的 token bid 都在 60 s 官方 TWAP 跨過 strike **之前**就崩跌（1 s snapshot：1791587700 在 TTE 201→185 時 bid 0.35→0.24，TWAP 仍 +3.75→+3.04 bps）。INFERRED 鏈：Binance spot 先跌 4–5 bps → token 在數秒內重新定價 → 60 s TWAP 落後 45–60 s 才跨越。60 s TWAP cross 是**落後**指標，不是領先指標。

## 6. Base rate（Section 7）

以成交價作為市場隱含勝率：p = 0.69、0.82、0.74。

- 預期虧損筆數 = 0.31+0.18+0.26 = **0.75**；P(0 輸)=0.419、P(1)=0.427、P(2)=0.140、P(3)=0.015 → **P(≥2 輸)=0.154**（描述性，不是檢定）。
- 以市場價計，持有到結算的 EV ≈ 0（maker 掛在 bid，相對 mid 約 −0.005/股）。
- 歷史 LIVE（125 市場、13 天、share_v1 前）：勝率 74.4% vs 隱含 75.5%，持有 PnL −0.011/股。價格 >0.70 的桶 72/83（隱含 67.1）；≤0.70 的桶 21/42（隱含 27.3）。VERIFIED。
- 結論：今天 1 勝 2 敗在一般變異範圍內（約 1/6 機率）；**RANDOM_SMALL_SAMPLE 是有效的主因候選**。

## 7. 虧損分解（Section 8）

| 持倉 | PRIMARY | SECONDARY | 證據 | 信心 |
|---|---|---|---|---|
| 1791587700 | RANDOM_SMALL_SAMPLE（低波動下約 1σ 的反向移動，翻轉 near-tie 市場） | STOP_TOO_LATE（相對 threshold；+0.96～+0.99/股差）、REVERSAL_WHIPSAW 不成立、EXECUTION 輕微（FOK 拒單但成交價更好） | 進場時距 strike 6.9 bps；session 1 分鐘 SD 1.58 bps → 剩 10 分鐘約 6 bps 的 1σ（INFERRED）；結算 margin −0.95 bps；threshold 23:26:31 bid 0.43 → 成交 0.25 | MEDIUM |
| 1791589500 | RANDOM_SMALL_SAMPLE | STOP_TOO_LATE（相對 threshold，約 0.99）、ENTRY 逆選擇（30 s markout −0.145，歷史 14% 尾端） | 距 strike 5.8 bps；margin −0.41 bps；`FILL_MARKOUT` | MEDIUM |

SETTLEMENT_PATH 是兩筆共同的風險修飾項：兩筆都靠 near-tie deferred relabel，官方 UNVERIFIED。不採用的歸因：VOLATILITY_SPIKE（波動反而低）、STRIKE_PROXIMITY（虧損單沒有比歷史更靠近 strike，分別在第 57／50 百分位；贏家才是最近的，第 8 百分位）、LIQUIDITY_SPREAD（spread 0.01，與歷史相同）、SIZING（share_v1 正確）。

## 8. 進場品質（Section 9，只列表、不檢定）

| 市場 | 結果 | score_abs | 價格 | TTE | 距 strike | σ | spread | ask depth |
|---|---|---|---|---|---|---|---|---|
| 1791586800 | W | 0.250 | 0.69 | 581 | 1.4 | 0.21 | 0.01 | 599 |
| 1791587700 | L | 0.403 | 0.82 | 583 | 6.9 | 0.75 | 0.01 | 702 |
| 1791589500 | L | 0.301 | 0.74 | 579 | 5.8 | 0.68 | 0.01 | 1213 |

1) 虧損單「更弱」嗎？在 score／距離／σ 上相反：虧損單更強（n=3，描述性）。2) 群聚：兩筆虧損都是 UP、都是價格 >0.70（5.5 股桶）；n 太小。3)/4) NO_ENTRY：1791585900（`directional_first_entry_gate`；最佳候選 UP@0.55、TTE 179；TWAP 跨越 8 次；結果 UP。**HYPOTHETICAL** 會贏）；1791590400（DOWN maker 掛 0.67–0.70×7.5 皆未成交；後續 DOWN@0.80 候選被 share_v1 skip（4.12<5.5）；結果 DOWN，margin −0.13 bps。**HYPOTHETICAL** 會贏）；partial 1791588600 中 DOWN@0.86 被 share_v1 skip，結果 UP（**HYPOTHETICAL** 避開了一筆虧損）。沒有證據顯示 filter 有系統性保護作用。

## 9. 反事實停損（Section 10；清單凍結，全部列出）

定義：每個策略 = 現行策略 + 一個額外出場觸發；若觸發晚於實際出場，就等於實際（censored）。價格使用觸發時**實際記錄的 bid**：主值用 telemetry（engine 當下的 bid），敏感度用 1 s snapshot top-of-book（深度未驗證、不含 fee）。從不使用 midpoint。

| 策略 | 贏家 1791586800 | 1791587700 | 1791589500 | 合計（主值） | 合計（snapshot 敏感度） |
|---|---|---|---|---|---|
| A 現行實際 | +2.80 | −3.155 | −3.145 | **−3.50** | −3.50 |
| B 立即 cross 出場 | −0.40（bid 0.65）／snap +1.50 | −3.465 | −3.135 | −7.00 | −5.10 |
| C cross+5 s | +1.30／snap +1.40 | −3.52／snap −2.97 | −3.135 | −5.36 | −4.71 |
| D cross+15 s | telemetry UNKNOWN（ref DEGRADED）／snap +1.70 | −3.465／snap −3.355 | censored = −3.145 | −4.91（贏家用 snap） | −4.80 |
| E cross+30 s | +2.30 | −3.245 | censored = −3.145 | −4.09 | −4.09 |
| F cross+σ≥0.5+TTE≤120 | −0.40／snap +1.50（cross 時 σ 0.66） | censored | censored | −6.70 | −4.80 |
| G cross+σ≥1+TTE≤60 | censored（TP 先成交） | censored | censored | −3.50 | −3.50 |
| 持有到結算（runtime 結果） | +3.10 | −4.51 | −4.07 | **−5.48** | — |

- 觸發數：B/C 3 筆、D/E 2–3 筆、F 1 筆、G 0 筆。省下的虧損：B–F 在虧損單上 −0.37～+0.19（在 bid 噪音內）。whipsaw 成本：B–F 讓贏家少賺 0.5～3.2。提早出場的贏家：B–F 各 1 筆。避開的虧損單：0 筆。
- breaker 相對持有省了 1.98 USDC，**前提是官方結果 = runtime 結果（UNVERIFIED）**。
- bid 在觸發時刻跳動很大（同一秒內 telemetry 與 snapshot 可差 0.19），差異都在噪音內。**不從本樣本推薦任何 production 規則。**

## 10. 週末 regime（Sections 11–14）

### 10.1 週末定義

- Taipei 週末：Sat 00:00 → Mon 00:00 +08 = **Fri 16:00 → Sun 16:00 UTC**。
- UTC 週末：Sat 00:00 → Mon 00:00 UTC = Sat 08:00 → Mon 08:00 +08。
- 本次 run（22:40–00:16 UTC = Sat 06:40–08:16 +08）**在 Taipei 週末內，但在 UTC 週末外**（只有最後 16 分鐘落在 UTC Sat），對應紐約時間 Fri 18:40–20:16。這是時區定義的混淆，不是「週末行情」的定義。

### 10.2 舊週末分析重審 → **OLD_WEEKEND_ANALYSIS_VALIDITY = WEAK**

來源：`reports/research_analysis/regime/weekday_weekend_pre_phase_a_final_20261007_105427_+0800.md`（主檔）、`preliminary_regime_comparison/`、`weekend_replication_batch_1/`、`regime_comparison_early_weekday/`、`project_overview.md` 第 5 節（12.9%／3.0%／0% σ 曲線、T-300 6.1% 等數字）、second-opinion 審計、`strike_flip_risk/`（公開 200 市場研究）。

理由（全部 VERIFIED 自原檔）：
1. 週末主樣本只有 **一個週末**（Oct 3/4，54 市場，2 個獨立日）；平日是 Oct 6/7（2 日）。
2. Fisher／Wilson 都以 market 為獨立單位，忽略日內相依。second-opinion 的 day-level permutation 最小 p=0.167，**不可能顯著**。
3. 排除標準不對稱：週末是 "corrected clean"，Monday 先整批排除、再逐市場重審；maker-qualified 33/54 vs 71/75，選樣不同。
4. regime 用 Taipei 日曆定義；hour matching 每組 N=37，原報告自評 TOO_SMALL。
5. 平日有 FF execution contamination = MATERIAL，config 不可比。
6. 公開 200 市場研究（market permutation）的 leader flip **沒有**週末差異（T-5m 28.7% vs 29.6%，p=1.0），與「週末翻轉低」相矛盾。
7. 引用的比率是原始的 market-level 描述性比率，不是 day-level 驗證效果。

舊報告未修改。

### 10.3 今天 vs 過去：波動（同一 UTC 時窗 22:40→00:16，Binance BTCUSDT proxy，非結算用 Chainlink）

| 組別 | 獨立日數 | 1m SD 中位數（bps） | 高低區間中位數（bps） | 今天的百分位（SD／區間） |
|---|---|---|---|---|
| **今天** | 1 | **1.58** | **14.1** | — |
| 先前同為「Taipei 週六早上」（UTC 週五） | 8 | 2.30 | 32.4 | 0.12／0.00 |
| UTC 週末夜（Sat/Sun） | 16 | 2.42 | 32.8 | 0.25／0.12 |
| 平日夜（Mon–Thu） | 33 | 3.38 | 50.1 | 0.00／0.00 |

今天 >5 bps 的 1 分鐘跳動 2 次、>10 bps 0 次；lag-1 自相關 +0.03。資料：07-27→09-20 為 1m JSON，10-01→10-09 為 1s 聚合（10-02/04/06–08 的覆蓋率 <0.9，已排除）。**TODAY_VOL_PERCENTILE_AMONG_WEEKENDS ≈ 0.12–0.25（低）**。單一 session，不作顯著性宣稱。

結算相關的 Chainlink TWAP 同時段比較：先前可用的同時段市場只有 8 個（週末僅 1 個；10-02 UTC 週五晚，也就是最精確的「上週六早上」對照，完全沒有資料）→ **INSUFFICIENT**。描述性數字：今天每市場 TWAP 跨越 8/2/3/2/1/4 次（平均 3.3），先前 8 個可用市場平均 1.5；今天的 TWAP 區間 2.0–8.5 bps，先前 5.9–21.9。INFERRED：波動低，但價格貼著 strike，所以跨越次數反而多。今天 5 個完整市場的結算 margin 全部在 ±1.1 bps 內（0.72、−1.09、−0.95、−0.41、−0.13）。

波動的兩種相反效應：(a) 更多 cross：今天 cross 多，但原因是貼近 strike，不是波動高；(b) cross 後回歸：今天市場熱門方（T-580）6/6 有 adverse cross、3 回歸 3 輸；先前 6 個中 4 有 cross、2 回歸 2 輸 → **INSUFFICIENT**。

### 10.4 流動性／微結構與 strike 幾何

- spread：今天所有市場的 snapshot 中位數 0.01，先前同樣 0.01；進場 spread 0.01（歷史第 97 百分位，因為歷史幾乎都是 0.01）。進場 ask depth 599–1213，高於歷史中位數 220。出場：一次 FOK 在 0.23 對 5.49 股無法全數成交。**LIQUIDITY_REGIME_TODAY = SIMILAR**（出場深度 INSUFFICIENT）。
- token 對 BTC 的反應：token 跟著 Binance 在數秒內重新定價，60 s TWAP 落後 45–60 s。Binance 相對 Chainlink 的 basis ≈ +8.7 bps（VERIFIED，submit payload）。
- strike 幾何：進場距離 1.4／6.9／5.8 bps；虧損單距離在歷史第 57／50 百分位。**虧損單並沒有比歷史更貼近 strike**，因此 strike proximity 不構成「週末效應」的混淆。持倉後 TWAP 最小距離：1791587700 −1.52 bps、1791589500 −1.82 bps。最大不利（bid 計）：−3.57／−3.25；最大有利：0／−0.55。贏家：最大不利 −2.40、最大有利 +2.30。

### 10.5 「今天的虧損是否異常？」

matched cohort → **INSUFFICIENT**（3 筆持倉、1 天）。對歷史 LIVE（per-share）：今天 per-share 結果 W +0.28、L −0.57、L −0.57（breaker 後）；歷史勝率 74.4% 與隱含機率相符。今天不算異常（INFERRED）。day-blocked bootstrap 無法做（獨立日 = 1）。

## 11. 其他可能解釋（Section 15；假設清單，不是結論）

檢查了 16 項（forking-paths 警告：從多條路徑中挑出任何一條都可能是偶然）：

| 假設 | 是／否 | 證據 |
|---|---|---|
| 方向群聚 | 部分是（兩筆虧損都是 UP，處於緩跌 session） | n=2 |
| 時間群聚 | 是（23:15 與 23:45 兩個市場，45 分鐘內） | n=2 |
| 進場時點 | 否（TTE ≈580，歷史中位數 600） | FILL_MARKOUT |
| 週末流動性 | 否 | spread 相同 |
| reference source 切換 | 否（TWAP 斷線期間未切換；有 DEGRADED） | TWAP_REFERENCE_DEGRADED |
| final-window target switch | 否（虧損單在 final window 前就出場） | telemetry |
| σ 模型 decay／floor | 未定：legacy σ 0.4–0.7，但 z_diffusion 偶有 5–14 的跳值 | 假設 |
| strike 品質 | 否（verified、authoritative） | MARKET_STRIKE_LOCKED |
| token 落後 BTC | 否（token 領先 TWAP） | 1 s snapshot |
| 停損時 spread 擴大 | 否（0.01），但 0.23 的深度不足 | FOK reject |
| 進場成交品質 | 可能（30 s markout −0.115／−0.145，歷史 14% 尾端） | FILL_MARKOUT |
| depth-risk 交互作用 | 否（shadow only） | — |
| score 校準漂移 | INSUFFICIENT | n=3 |
| candidate-policy vs production 不一致 | 未檢查（`CANDIDATE_POLICY_SHADOW` 未讀） | SKIPPED |
| 結算 TWAP 路徑 vs 終點 spot | near-tie relabel；官方 UNVERIFIED | MARKET_SETTLEMENT |
| freshness／degraded 期間 | 是（83 s 評估中斷，損失影響未見） | telemetry |

給下一輪的新假設：(H1) 60 s TWAP cross 落後 token／spot 約 45–60 s；spot-based 警訊可能較早，但假警報率未知。(H2) 低波動 + 貼近 strike 的 regime 中，0.74–0.82 的價格對約 1σ 的移動很敏感，結算常落在 near-tie。(H3) maker 進場的逆選擇在 30 s markout 尾端。

## 12. 最終判定（Section 16）

```
A WHY_DID_TODAY_LOSE（依證據強度）:
  1 RANDOM_SMALL_SAMPLE + near-tie 結算（MEDIUM；P(≥2 輸)=0.154；margins −0.95/−0.41 bps）
  2 flip-reversal：低波動下貼近 strike 的約 1σ BTC 移動；token 在 TWAP 之前就重新定價（MEDIUM）
  3 stop timing：breaker 被 TREND→TTE 擋住，相對 threshold 每筆多虧約 1 USDC（LOW–MEDIUM；贏家也觸及過 threshold）
  4 execution：一次 FOK 拒單 24 s，但成交價更好（LOW）
  5 entry selection（LOW）、liquidity（LOW）、volatility（LOW；波動是低的）
B TODAY_WEEKEND_VOLATILITY_VS_PRIOR_WEEKENDS=LOWER（Binance proxy、同一 UTC 時窗；Chainlink 比較 INSUFFICIENT）
C TODAY_FLIP_BEHAVIOR_VS_PRIOR_WEEKENDS=INSUFFICIENT
D OLD_WEEKEND_ANALYSIS_VALIDITY=WEAK
E CROSS_PROVIDED_EARLIER_WARNING_THAN_LOSS_THRESHOLD=NO（n=2 虧損單；cross 比 threshold 晚 51–62 s；贏家的 cross 是假警報）
F ABSOLUTE_BREAKER_TOO_LATE=MIXED（相對 threshold 晚 63–88 s，約 −1 USDC/筆；相對 cross 大致同時；只用門檻出場會砍掉贏家 −2.49 vs +2.80）
G SIGMA_ADDED_USEFUL_INFORMATION=INSUFFICIENT
H IS_FLIP_STOP_V1_NOW_JUSTIFIED=NO
I 下一輪 12+ markets 重點：見 §13
J OPEN SAFETY ITEMS：見 §14
```

## 13. 下一輪 12+ markets 的指標

1. 每筆持倉：token bid 崩跌（例：較進場 −0.20）／Binance spot 反向 ≥3 bps／60 s TWAP cross／loss threshold 四者的時間差（秒），以及贏家上的假警報率。
2. loss threshold 被觸及後回升的比率（threshold 觸及時的 net vs 最終），衡量純門檻出場的 whipsaw 成本。
3. breaker 延遲分解：threshold→trend→(TTE 或 persistence∧votes)→eligible→submit→fill，以秒與 USDC 計；FOK 拒單率與 retry 延遲。
4. near-tie（|margin|<1 bps）市場比例，以及官方結果 vs runtime 標籤一致率（需先跑 `fetch_official_resolutions`）。
5. 進場 1 s／30 s markout 與結果的關係，以及每筆持倉 protective evaluation 的最大間隔（DEGRADED 期間）。

## 14. Open safety items（是否阻擋下一輪）

沒有發現已證實的執行 bug。下列項目應在下一輪前決定或確認：
1. **本次 run 帶著未 commit 的 dirty patch**（週末 BUY 解鎖，`b8ae4cdf`）。provenance 有記錄，但 HEAD 不等於實際執行的程式碼。應決定 commit 或 revert（治理項目；建議在下一輪前處理）。
2. **持倉中 protective evaluation 約 83 s 中斷**（TWAP WS 斷線 → DEGRADED，1791587700）。這是已知的 P1「stale reference 政策 UNRESOLVED」，現在在 LIVE 中實際觀察到；這次沒有損失影響。
3. **quote watchdog 觸發 process 內重建**，約 64 s 沒有 strategy；這次庫存 0。若發生在持倉中，保護性出場是否中斷 → UNVERIFIED，應在下一輪前確認。
4. FOK protective exit 被拒 → 20 s reject cooldown；這次結果有利，但可能代價高。
5. graceful-exit retention 兩次 KeyboardInterrupt（非阻擋）。free 12.6 GiB，位於 WARNING 區（critical guard 10 GiB）。
6. 官方結果 UNVERIFIED（兩筆 near-tie）。在任何 PnL 結論之前應先補官方 resolution。

## 15. Coverage ledger

| Section | 狀態 | 備註 |
|---|---|---|
| S1 識別 + 完整性（§0、§3） | DONE | storage before = UNVERIFIED；venue = UNVERIFIED |
| S2 每筆持倉時間線（§4、§5） | DONE | TTE/VOTES 歸因含 INFERRED |
| S3 base rate + 虧損分解（§6、§7） | DONE | 官方結果 UNVERIFIED |
| S3 進場品質（§8） | DONE（描述性） | — |
| S4 反事實（§9） | DONE | 深度未驗證；bid 噪音大 |
| S5 舊週末重審（§10.2） | DONE | — |
| S5 波動 regime（§10.3） | PARTIAL | Binance proxy DONE；Chainlink 同時段 INSUFFICIENT；10-02/04/06–08 Binance 覆蓋不足 |
| S5 流動性／strike 幾何（§10.4） | PARTIAL | 出場深度、imbalance 未保留 |
| S5 matched cohort（§10.5） | INSUFFICIENT | n=3、1 天 |
| S6 其他解釋（§11） | PARTIAL | `CANDIDATE_POLICY_SHADOW` 未檢查 |
| S6 最終判定（§12–14） | DONE | — |

輸出：`report.md`、`summary.json`、`market_table.csv`（column 顯示時空欄會位移，請用 CSV 工具開）、`stop_timing_table.csv`、`weekend_regime_comparison.csv`。注意 `stop_timing_table.csv` 的 `PNL_CHANGE_CROSS_TO_LOSS_THRESHOLD` 是 threshold 的 net 減 cross 的 net；因為 threshold 早於 cross，正值表示 threshold 當時的 PnL 比 cross 時好。
