# 最終研究迭代報告（2026-10-09 04:53 UTC）

> 範圍：Stage 0–7 全部完成，各自獨立 commit，**未 push、bot 全程停止、未重啟**。
> 判讀規則在 Stage 3 之前就已凍結，詳見 `reports/work_handoff/research_and_settlement_handoff_20261009T040608Z.md` 及其 ADDENDUM：
> - 主要指標：每筆交易的平均 PnL。
> - 分析單位：市場；不確定性用以「天」為區塊的 bootstrap（2000 次、seed 11，僅限 ≥3 天的格子）。
> - DRY-RUN 單獨不能升級任何標籤。
> - 0.30／0.75／480 三個門檻已凍結，不做門檻搜尋。
>
> 資料：
> - 結果來源：官方結果快取 `official_resolutions_20261009T041657Z.json`（檔案 sha256 `7795b2c9…`，內容 sha256 `5356cf95…`，954 個市場）。
> - 研究資料集：`market_outcomes_5356cf95f81e.csv`（sha256 `ea744003…`）。
> - 中間產物：`reports/final_research_iteration/20261009T042008Z/`（未納入版控）。
> - 可重現性：`scripts/reproduce_research_iteration.py` 兩次獨立重跑，5 份結果摘要（digest）完全一致。
>
> 以下表格中，「LIVE」指 2026-09-10..09-30（11 個交易日，真實成交）；「DRY-RUN」指 2026-10-01..10-08（8 天，shadow 模擬成交，屬樂觀假設）。除非另外註明，結果來源一律是 **Polymarket 官方結算**。

---

## 1. 結算缺陷的驗證與修正（Stage 1，commit `bbbdc09`）

**缺陷（已從原始碼獨立驗證）：**
- 位置：`bot/lifecycle_runtime.py::_record_market_settlement`。
- 原本以 `latest_external_spot >= strike` 決定以下所有項目：`MARKET_SETTLEMENT.outcome`、cycle PnL、session／regime guard、shadow 結算。
- 同一個函式裡其實已經算出權威的 TWAP 標籤，卻沒有拿來用。

**修正內容：**

| 項目 | 修正前 | 修正後 |
|---|---|---|
| runtime 結算來源 | 外部 spot ≥ strike | 權威 TWAP 標籤（60 秒視窗，tick 年齡 ≤10 秒）；不合格時記為 `UNKNOWN`，**不會退回用 spot** |
| 有部位但結果是 UNKNOWN | 用 spot 猜一個結果 | 只寫入 pending 的 `MARKET_SETTLEMENT`；不寫 `MARKET_CYCLE_PNL`、不更新 guard；之後由啟動時的 Gamma 補結算處理 |
| `compute_settlement_summary` | 從 spot 和 strike 推導 | 由呼叫端傳入 outcome；收到 UP／DOWN 以外的值會報錯 |
| shadow 與 stop／trend／forward／TWAP shadow | spot，或以 spot 作為備援 | 只用權威標籤；UNKNOWN 時不結算 |
| 事件欄位 | 無 | `settlement_provenance`：`outcome_source`、參考價來源、是否為權威標籤、資料年齡；spot 只保留為 `latest_spot_diagnostic` |

**測試：** 新增 `tests/test_settlement_authority.py`，共 13 個案例。
- 在修正前的 `5f40b14`（用 `git archive` 解壓到 /tmp）上 **13 個全部失敗**，且都是行為層面的失敗。例如：應為 DOWN 卻得到 UP；session guard 記成 +1.0，應為 −3.0。
- 修正後全部通過。

**影響範圍（以官方結果比對）：**
- journal 標籤與官方不一致的市場，**全部發生在 LIVE 期間沒有部位的市場**。
- 108 個有交易的 LIVE 市場：標籤翻轉 **0 次**，所以 LIVE 交易的實際勝負不受影響。
- 受影響的是研究資料、DRY-RUN shadow 結算，以及 guard 的歷史紀錄。

## 2. 官方／TWAP／journal 不一致表（Stage 2）

| 比較 | 期間 | 模式 | 比對市場數 | 不一致 | 比率 | 結果來源 |
|---|---|---|---|---|---|---|
| journal `MARKET_SETTLEMENT.outcome` vs 官方 | 09-10..10-08 | 全部 | 895 | 68 | 7.6% | 官方 |
| ─ LIVE 期間 | 09-10..09-30 | LIVE | 462 | 44 | 9.5% | 官方 |
| ─ DRY-RUN 期間 | 10-01..10-08 | DRY-RUN | 433 | 24 | 5.5% | 官方 |
| 權威 TWAP vs 官方 | 有 TWAP 摘要的天數 | 全部 | 437 | 2 | 0.46% | 官方 |

- **2 筆 TWAP 不一致：** 最後一個 TWAP tick 的年齡為 2.5–4.4 秒，且兩筆都接近平手（near-tie）。這是剩餘風險。
  - 可能的修法（**未實作**）：等收到結算時間之後的 tick 再定標籤。
- **journal 內部一致性：** 同一市場出現互相衝突的結算列 = 0。
- **研究資料的結果來源：** 954 個市場全部採用 `POLYMARKET_OFFICIAL`。
  - 信心等級：HIGH 952、HIGH_OFFICIAL_TWAP_CONFLICT 2。

## 3. 修正後的 LIVE PnL

| 期間 | 模式 | N 部位 | N 天 | 總 PnL | 每筆平均 | 勝率（Wilson 95%） | 天區塊 bootstrap 95%（每筆平均） | 結果來源 |
|---|---|---|---|---|---|---|---|---|
| 09-10..09-30 | LIVE | 125 | 11 | **−$9.28** | −$0.074 | 74.4% [66.1, 81.2] | [−0.59, +0.37] | 官方 + 實際成交 |

- **journal 的 `MARKET_CYCLE_PNL` 合計 +$15.37 不可信。** 這是一個**新發現、本次未修**的 ledger 缺陷，有兩種情況：
  1. SELL 成交後，剩餘庫存被重複計算。例：市場 1789745400，journal 記 +$6.76，重建結果為 +$0.94。
  2. 結算時庫存遺失。例：市場 1790568000，journal 記 $0，重建結果為 −$6.60。
- **重建驗證：** 23 筆有鏈上 REDEEM 的部位中，21 筆與剩餘股數吻合；另外 2 筆的 redeem 紀錄不完整。

## 4. 修正後的 DRY-RUN PnL

| 期間 | 模式 | N 市場 | N 天 | 總 PnL | 每市場平均 | 天區塊 bootstrap 95% | 結果來源 |
|---|---|---|---|---|---|---|---|
| 10-01..10-08 | DRY-RUN（shadow，樂觀成交） | 266 | 8 | **−$9.60** | −$0.036 | [−0.30, +0.40] | 官方 |
| 同上（journal 標籤，僅供對照） | DRY-RUN | 266 | 8 | +$17.27 | +$0.065 | — | journal（有缺陷） |

- 266 個市場中有 20 個被重新標籤，**整體 PnL 由正轉負**。

| 進場價格區間 | DRY-RUN N 市場／N 天 | 每市場平均 PnL |
|---|---|---|
| [0.6, 0.7) | 57／6 | −$0.61 |
| [0.7, 0.8) | 86／8 | −$0.13 |
| [0.8, 0.9) | 117／8 | +$0.26 |

## 5. score 0.30 的穩健性（SCORE_030）

變數相關矩陣（先報告）：
- LIVE：price–score 0.68、price–distance 0.55。
- DRY-RUN：price–sigma 0.68、price–score 0.53。

| 模式 | 期間 | 通過（N 市場／天） | 未通過（N 市場／天） | 平均 PnL：通過 vs 未通過 | 差值 | 天區塊 95% | 每日方向 +／− | 結果來源 |
|---|---|---|---|---|---|---|---|---|
| LIVE | 09-10..09-30 | 93／11 | 32／5 | +0.140 vs −0.697 | +0.84 | [−0.25, +2.18] | 3／2 | 官方 |
| DRY-RUN | 10-01..10-08 | 148／8 | 118／5 | +0.146 vs −0.264 | +0.41 | [−0.12, +0.92] | 4／1 | 官方 |

**結論：WEAK_CANDIDATE。**
- 兩個模式方向一致，但都只是「多數天」同向，且信賴區間都包含 0。

## 6. 進場價格分析（ENTRY_PRICE_075）

| 模式 | 期間 | 通過（N 市場／天） | 未通過（N 市場／天） | 原始差值 | 天區塊 95% | 控制後係數 | 控制判定 | 每日方向 +／− |
|---|---|---|---|---|---|---|---|---|
| LIVE | 09-10..09-30 | 65／11 | 60／11 | +1.29 | **[+0.22, +2.14]** | +1.44 | SURVIVES | 7／4 |
| DRY-RUN | 10-01..10-08 | 166／8 | 100／6 | +0.50 | [−0.13, +0.96] | +0.05 | PROXY | 3／3 |

- LIVE 的控制變數：|score|、TTE、distance。DRY-RUN 的控制變數：|score|、TTE、sigma。結果來源：官方。

**結論：UNRESOLVED。**
- LIVE 上控制後效果依然存在，且信賴區間不含 0。
- 但 DRY-RUN 在控制 sigma 後效果幾乎消失（price 與 sigma 的相關係數 0.68），而且每日方向不一致。
- 依凍結規則，兩個模式必須分別成立才能升級，因此維持 UNRESOLVED。

## 7. TTE 分析（TTE_480）

| 模式 | 期間 | 通過（N 市場／天） | 未通過（N 市場／天） | 原始差值 | 天區塊 95% | 控制後係數 | 控制判定 | 每日方向 +／− |
|---|---|---|---|---|---|---|---|---|
| LIVE | 09-10..09-30 | 30／10 | 95／11 | +0.02 | [−1.98, +1.44] | −0.33 | PROXY（符號翻轉） | 4／6 |
| DRY-RUN | 10-01..10-08 | 77／7 | 189／8 | +1.08 | [−0.09, +2.16] | +1.40 | SURVIVES | 4／3 |

**結論：PROXY_FOR_OTHER_FEATURES。**
- 這個判定是由 LIVE 決定的：原始效果約等於 0，控制後符號翻轉。
- DRY-RUN 的效果在控制後存在，但依凍結規則 DRY-RUN 不能升級。
- 必須誠實說明：這個標籤的依據是「LIVE 上幾乎沒有效果」，而不是「已證明 TTE 是其他特徵的代理」。

## 8. 候選策略（僅限樣本內）

規則：score ≥0.30、price ≥0.75、TTE ≤480。

| 模式 | 期間 | 規則 | N 市場／天 | 每筆平均 | 總計 | 最大回撤 | 結果來源 |
|---|---|---|---|---|---|---|---|
| LIVE | 09-10..09-30 | 三個條件全部 | 17／6 | −$0.16 | −$2.77 | $7.47 | 官方 |
| LIVE | 同上 | 去掉 TTE 條件 | 59／11 | **+$0.59** | +$34.70 | $4.73 | 官方 |
| LIVE | 同上 | 全部交易 | 125／11 | −$0.07 | −$9.28 | — | 官方 |
| DRY-RUN | 10-01..10-08 | 三個條件全部 | 40／7 | +$0.46 | +$18.49 | $9.89 | 官方 |
| DRY-RUN | 同上 | 去掉 TTE 條件 | 121／8 | +$0.10 | +$12.34 | $22.82 | 官方 |
| DRY-RUN | 同上 | 全部交易 | 266／8 | −$0.04 | −$9.60 | — | 官方 |

- 在 LIVE 上，三條件組合**比被排除的交易更差**。在 DRY-RUN 上則較好。
- 以上全部是樣本內結果，**不能用來挑選規則**。「去掉 TTE」這個版本是看過資料之後才發現的，只能作為之後前瞻驗證的預先登記假設。
- `CANDIDATE_ENTRY_POLICY_V1`（三條件、`policy_version` 欄位）已作為純研究用的 shadow 寫入程式（`b9a1e98`），對下單沒有任何權限。

## 9. 翻轉與反轉（flip／reversal）

此節分析的對象是「剩餘時間 T 時領先的一方，最後輸掉」的機率，也就是最終翻轉。結果來源一律為官方。

| 來源 | 期間 | N 天 | T-300 | T-180 | T-120 | T-60 |
|---|---|---|---|---|---|---|
| native v2 路徑 | 10-06..10-08 | 3 | 24.4%（78 市場） | 13.2%（76） | 10.3%（78） | 6.4%（78） |
| historical pre-v2 路徑（僅 FRESH） | 10-02..10-06 | 5 | 15.3%（274） | 9.7%（258） | 7.8%（256） | 3.9%（254） |

- **選擇偏誤：** historical 中路徑不完整的列翻轉率明顯偏高。以 T-300 為例，不完整列 27.7%，完整列只有 8.9%。所以 historical 的數字只能當作下限參考。
- **「穿越後又回來」很少：** native T-300 只有 6.2%。
- **預測方向變化次數遠多於實際穿越：** 469 個市場平均 21.7 次方向變化，實際 TWAP 穿越只有 1.17 次。
- **DRY-RUN 進場後的走勢**（145 個完整路徑，7 天）：
  - 進場後出現反向穿越的比例：32.4%。
  - 有反向穿越時，最後真的輸的比例：66.0%（47 個）。
  - 沒有反向穿越時，最後輸的比例：0%（0／98）。
- **平日 vs 週末：UNRESOLVED。**
  - native 只有平日資料。
  - historical 只有 2 個週末天；平日、週末配對比較 3/6–4/6；可行的排列只有 10 種，最小 p 值為 0.1。

## 10. legacy sigma 的限制

- 在 sigma ≥2 時，翻轉率為 0–6%，所以有分辨能力：**PARTIALLY_USEFUL**。
- 但它與 distance（|TWAP−strike|）高度重疊；distance ≥10 bps 時翻轉率同樣接近 0。它和價格的相關也高（0.68）。
- 它是啟發式指標，不是經過校準的機率；也不能取代 diffusion z。

## 11. z_diffusion

- 資料只有 1 天（10-08）、8 個市場、26 列，**只能做描述**：`Z_DIFFUSION_SAMPLE_DAYS=1`、`FLIP_MODEL_READY=NO`。
- 理論上的變異數推導已驗證：
  - 視窗開始前：(T−W) + W/3。
  - 視窗內：τ/3。
- 實證校準要等更多天的 native v2 資料。

## 12. 市場校準（5B，以 UP token 為準，每個市場在每個時間點只取一個觀察）

以下只列 historical pre-v2 路徑的結果（10-02..10-06，4–5 天，結果來源為官方）。native 的格子大多不足 3 天，只能描述，完整內容見 `stage5/stop_analysis.json`。

| TTE | 價格區間 | N 市場／天 | 隱含機率 | 實際勝率 | 差值 | 天區塊 95% |
|---|---|---|---|---|---|---|
| 300 | [0, 0.2) | 68／5 | 0.081 | 0.162 | +0.081 | [+0.044, +0.127] |
| 300 | [0.2, 0.4) | 36／5 | 0.294 | 0.278 | −0.016 | [−0.142, +0.062] |
| 300 | [0.8, 1.0) | 67／4 | 0.915 | 0.940 | +0.025 | [+0.012, +0.035] |
| 180 | [0.2, 0.4) | 14／4 | 0.278 | 0.429 | +0.151 | [−0.008, +0.381] |
| 120 | [0.6, 0.8) | 14／4 | 0.698 | 0.857 | +0.159 | [−0.012, +0.303] |
| 60 | [0.8, 1.0) | 75／5 | 0.978 | 1.000 | +0.022 | [+0.015, +0.035] |

- historical 在 T-300 低價區（0–0.2）實際勝率較高，但 native 在 T-600 低價區的結果是 0／12，兩者方向不一致，**不能說存在穩定的錯誤定價**。
- 對停損最相關的 0.2–0.6 中間區間：市場大致是公平定價（差值的信賴區間都包含 0）。
  - 也就是說，用 bid 賣掉虧損部位**大致等於公平價再扣掉半個價差**。停損的價值主要在於降低變異，不是產生 alpha。

## 13. 停損 vs 持有（5A）

**實際生效的 runtime 設定**（透過 AppConfig 載入 profile + `.env` 驗證，沒有啟動 bot）：

| 設定 | 值 |
|---|---|
| `stop_loss_enabled`（論點型的自適應停損） | **False** |
| `absolute_max_loss_enabled` | **True**，$2.00，最少持有 60 秒 |
| `catastrophic_stop_loss_enabled` | **True**，$0.40，需確認 2 次，最小 |score| 0.50 |

- 本次沒有修改任何門檻。

**LIVE**（09-10..09-30，官方結果）：
- 實際停損 8 筆（6 天）：其中只有 1 筆「若持有到底會贏」。停損 − 持有 = **+$8.26**。
- 主動賣出（maker）75 筆：賣出 − 持有 = +$9.66。
- 樣本太小，只能描述。

**DRY-RUN 回放**（226 個 shadow 部位，7 天，10-01..10-08，官方結果）：
- 使用 tier P 路徑與 runtime 同一個 `StopCandidateShadow`。
- 以 bid 作為 taker 出場價，**未扣手續費、未考慮深度**。

| 停損規則（在第一個候選點出場） | 觸發數 | 觸發後若持有會贏 | 每部位平均 停損−持有 | 天區塊 95% | 最大回撤 停損／持有 | 單筆最差 停損／持有 | 標準差 停損／持有 |
|---|---|---|---|---|---|---|---|
| ADVERSE_CROSS | 77 | 25 | +0.024 | [−0.15, +0.19] | 52.9／58.9 | −6.8／−7.0 | 2.60／3.39 |
| ADVERSE_CROSS_PERSIST_15S | 67 | 20 | +0.066 | [−0.09, +0.23] | 57.9／58.9 | −7.0／−7.0 | 2.72／3.39 |
| HARD_LOSS_EQUIVALENT（$2，60 秒） | 80 | 28 | +0.161 | [−0.15, +0.33] | **32.2／58.9** | −6.8／−7.0 | **2.33／3.39** |

**依狀態拆解**（第一個反向穿越候選點，DRY-RUN，屬探索性）：

| 剩餘時間 TTE | N 候選／天 | P(最後輸) | 每筆 停損−持有 |
|---|---|---|---|
| < 120 秒 | 21／6 | 0.86 | +0.51 |
| 120–300 秒 | 21／5 | 0.71 | +0.49 |
| 300–480 秒 | 28／7 | **0.50** | **−0.86** |

- 較早出現的反向穿越常常會回來，太早停損反而虧。接近結算才出現的反向穿越大多就此成定局。
- 這只是要在之後前瞻驗證的假設，**不是規則**。

**判讀：**
- **STOP_ALPHA = INSUFFICIENT。** 三種規則的平均差值都是正的，但天區塊信賴區間都包含 0。LIVE 只有 8 筆。
- **STOP_RISK_CONTROL = UNRESOLVED（方向有利）。** HARD_LOSS 版本把最大回撤從 58.9 降到 32.2，標準差也降低。但：
  - 只有 DRY-RUN、7 天的資料；
  - 單筆最差幾乎沒變，因為價格會直接跳空；
  - 回撤指標沒有信賴區間。

**另外完成：**
- 停損 shadow 框架已寫入程式（`STOP_SHADOW_CANDIDATE`／`STOP_SHADOW_RESOLUTION`）。
- 測試證明 shadow 路徑**無法**呼叫任何下單、取消或修改委託的函式。
- `ADAPTIVE_STOP` 維持關閉。

**新增研究寫入的儲存預估：**
- 候選策略 shadow：只在「決策狀態轉換」時寫入，約 ≤1.5k 列／天、<1 MB／天。
- 停損 shadow：每個部位最多 3 個候選 + 解析列；以 DRY-RUN 實際約 33 個部位／天計，約 130 列／天，上限約 400 列／天，<0.5 MB／天。
- 欄位分級：除 `current_reason` 為 OPTIONAL 之外，其餘皆為 REQUIRED。

## 14. Retention 實作（Stage 7，commit `a9b3f70`）

**開關：**
- `RESEARCH_RETENTION_ON_EXIT`，**預設 OFF**（`.env` 中沒有設定）。
- `RESEARCH_RETENTION_EXIT_TIMEOUT_SEC`，預設 300 秒。
- 啟動時 launcher 會在 log 寫出「最多增加 N 秒關機時間」。

**觸發條件：**
- `run_integrated_bot()` 現在會回傳明確的結束原因。
- 只有 `operator_stop`（Ctrl+C）或 `clean_return_without_rollover`（Nautilus 自行處理 SIGINT／SIGTERM 後正常返回）才會觸發。
- 只會由 `main()` 呼叫，**3 小時的節點輪替（rollover）永遠不會觸發**。測試會實際跑兩次輪替再按 Ctrl+C 來驗證這點，另有 AST 檢查。

**執行順序**（獨立子程序，並持有 maintenance lock）：
1. 確認寫入器已在 strategy teardown 中關閉。
2. 用 lsof 確認沒有任何程序持有 DB，父程序也算在內。
3. 對研究 DB 做 WAL checkpoint。
4. 先做一次 dry-run 計畫並寫入 log；若出現異常（例如未來時間戳記）就中止。
5. 只為「已結束的過去日期」切分 C 層分區，**今天永遠不處理**。
6. 重新驗證每個分區：檔案雜湊、列數、內容雜湊、integrity check。
7. 重新計算 iCloud 上 A／B／P 備份的雜湊並比對。
8. 每天一個 transaction 刪除；transaction 內會再算一次雜湊。
9. 回報釋放的邏輯與實體位元組數。

本次**不做壓縮（VACUUM）**。

**失效保護（fail-safe）：**
- 驗證失敗、雲端不可用、有其他程序持有 DB、逾時、第二次按 Ctrl+C → **一律不刪除**。
- 刪除到一半被中斷 → transaction 自動 rollback。
- 若既有分區已損壞，**絕不靜默重建**，留給操作者檢查。
- 當天的匯出尚未完成時，只跳過那一天，下次結束時再試。
- 整個流程可以重複執行（idempotent）。

**測試：** `tests/test_exit_retention.py` 共 20 個，涵蓋規格要求的 15 項，另加：子程序端對端、真實 SIGINT 中止、launcher 的逾時與二次 Ctrl+C、非正常結束不觸發。
- 測試 9 抓到一個測試 fixture 的連線外洩（`with sqlite3.connect()` 不會關閉連線）。這也證實了「有程序持有 DB 就拒絕刪除」的保護確實會作動。

**目前真實的研究 DB：** 已是空的（319 KB），read-only 計畫的候選天數為 0，所以第一次結束時不會做任何事。

## 15. 仍不確定的部分

- **樣本量：** LIVE 11 天、125 筆；DRY-RUN 8 天、266 筆。多數格子的信賴區間都包含 0。DRY-RUN 的成交是樂觀假設。
- **z_diffusion：** 只有 1 天的資料。native v2 路徑只有 3 天，且全是平日。
- **停損回放：** 未扣手續費，沒有深度或滑價模型。`model_probability` 在回放中沒有傳入，所以 model-vs-market 的優勢沒有評估到。
- **journal ledger：** cycle PnL 缺陷尚未修正，journal PnL 不能當作真值。
- **TWAP 接近平手：** 有 2 筆 TWAP 與官方不一致，等待結算後 tick 的修法尚未實作。
- **historical 路徑：** 不完整路徑造成的選擇偏誤，使 historical 翻轉率偏低。
- **LIVE 與 DRY-RUN 不同期：** 兩者分屬 9 月與 10 月，市況可能不同。

## 16. 已可進入未來 DRY-RUN shadow 驗證的項目

1. `CANDIDATE_ENTRY_POLICY_V1`（凍結門檻 0.30／0.75／480）：與目前策略並排記錄。
2. 停損候選三種規則與其解析結果：持續在 DRY-RUN 中累積。
3. 預先登記的假設，必須用**新資料**判讀：
   - H1：price ≥0.75 在控制 sigma 後是否仍有效。
   - H2：「score ≥0.30 且 price ≥0.75」（不含 TTE）的前瞻表現。
   - H3：反向穿越發生在 TTE <120 秒 vs 300–480 秒時，最後輸的機率差異。
   - H4：HARD_LOSS 規則降低回撤的效果。
4. 累積 ≥3 天（最好 ≥7 天、包含週末）的 native v2 路徑後，再評估 z_diffusion 與翻轉模型。

## 17. 現在絕對不能上線的項目

- 正式環境的進場門檻變更（score 0.30、最低價 0.75、TTE 480）。
- 啟用自適應停損；重新調整 hard-loss／catastrophic 門檻。
- 重新調整 maker 經濟參數或 L2 門檻。
- live 模式，包括小額 live。
- 以 journal PnL 作為績效依據。

## 18. 與先前結論不同的地方

1. 「LIVE journal PnL 為 +$15.37」→ 修正後 LIVE 為 **−$9.28**（125 筆）。先前報告的 −$19.79 是因為標籤覆蓋不完整（只有 111 筆）。
2. 「DRY-RUN shadow 有獲利（每市場 +$0.065）」→ 用官方結果後變成 **−$0.036**，正負號改變。
3. 「進場價 0.75–0.80 區間有獲利」→ 在 DRY-RUN 轉為負；只有 ≥0.80 仍為正。
4. 「三條件組合（0.30／0.75／480）較好」→ 在 LIVE 上反而比被排除的交易差；TTE 條件被判定為 PROXY。
5. 「journal 停損只有 5 筆」→ 用官方結果與完整重建後是 8 筆，停損 − 持有 = +$8.26。但 DRY-RUN 的天區塊信賴區間包含 0，**不構成停損 alpha 的證據**。
6. 平日／週末效應：先前的 regime 比較報告曾討論過平日與週末的差異；依凍結規則，目前只能判定為 **UNRESOLVED**（週末只有 2 天，最小可得 p 值為 0.1）。
7. 新發現的缺陷：journal cycle-PnL ledger（尚未修正）；TWAP 接近平手的剩餘風險。

---

```
FINAL_RESEARCH_ITERATION AUDIT_COMPLETENESS=FULL
SETTLEMENT_BUG_VERIFIED=YES SETTLEMENT_BUG_FIXED=YES OFFICIAL_MARKETS_CHECKED=954 JOURNAL_OUTCOME_MISMATCH_N=68 JOURNAL_OUTCOME_MISMATCH_RATE=0.076 TWAP_OUTCOME_MISMATCH_N=2 TWAP_OUTCOME_MISMATCH_RATE=0.0046 LIVE_PNL_CORRECTED=-9.28 DRY_RUN_PNL_CORRECTED=-9.60
SCORE_030_STATUS=WEAK_CANDIDATE ENTRY_PRICE_075_STATUS=UNRESOLVED TTE_480_STATUS=PROXY_FOR_OTHER_FEATURES CANDIDATE_POLICY_IN_SAMPLE_ONLY=YES CANDIDATE_POLICY_LIVE_DEPLOYED=NO
WEEKDAY_WEEKEND_EFFECT=UNRESOLVED LEGACY_SIGMA_STATUS=PARTIALLY_USEFUL Z_DIFFUSION_SAMPLE_DAYS=1 FLIP_MODEL_READY=NO
ADAPTIVE_STOP_ENABLED=NO ABSOLUTE_BREAKER_ENABLED=YES CATASTROPHIC_BREAKER_ENABLED=YES STOP_ALPHA_EVIDENCE=INSUFFICIENT STOP_RISK_CONTROL_VALUE=UNRESOLVED STOP_SHADOW_FRAMEWORK=IMPLEMENTED CANDIDATE_ENTRY_SHADOW=IMPLEMENTED
RETENTION_ON_EXIT_IMPLEMENTED=YES RETENTION_DEFAULT_ENABLED=NO RETENTION_FAIL_SAFE_TESTS=PASS FULL_TESTS=1262_passed_0_failed DIFF_CHECK=PASS
SAFE_FOR_FUTURE_DRY_RUN_SHADOW_VALIDATION=YES SAFE_TO_CHANGE_PRODUCTION_ENTRY_THRESHOLDS=NO SAFE_TO_ENABLE_ADAPTIVE_STOP=NO SAFE_FOR_TINY_LIVE=NO
COMMITS=bbbdc09,2671661,b9a1e98,4390041,a9b3f70 PUSHED=NO BOT_RESTARTED=NO
```
