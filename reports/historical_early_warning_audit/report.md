# Historical Early-Warning / Stop-Loss Feature Integration Audit（FINAL）

日期：2026-10-10（+08）。全程唯讀：bot 停止；SQLite 一律 `mode=ro` 或 `immutable=1`；沒有改 runtime、`.env`、門檻、stop 邏輯或 schema；沒有寫入 runtime/shadow 事件；沒有 commit/push。唯一的網路行為：用既有工具的 `fetch_one`，以唯讀 GET 抓取官方結果缺失的 3 個市場（不載入 `.env`、不帶 secrets），快取在 `official_resolution_cache/`。分析腳本放在 session scratchpad，與資料目錄分開。

證據標記：**VERIFIED**／**INFERRED**／**UNVERIFIED**／**INSUFFICIENT**。

**事前登錄：** `frozen_spec.md`，sha256 `0e098be7ea5b72153a09ac5379f189bbbdf0246edf5adfcc501cf6793adc4d11`，2026-10-10T01:17:35Z 寫入，早於任何 scorecard。結束時重新驗證 hash = OK。凍結清單共 60 條規則（46 單一／persistence + 14 組合），之後沒有增、刪或調整任何規則。本報告中所有標 EXPLORATORY 的內容都不能進入 shortlist。

---

## 1. 資料盤點與 cohort（S1）— 詳見 `cohort_coverage.csv`

| 來源 | 覆蓋 | 解析度 | 時間戳語意 | 品質 |
|---|---|---|---|---|
| A LIVE 持倉（fills） | 128 持倉（125 歷史 + 3 prospective）、14 個台北日 | event | journal 主機時鐘 | VERIFIED |
| C Binance | 128/128 | 約 1 s | 本地接收時間 | VERIFIED |
| D Chainlink spot／官方 60 s TWAP | 9 月 125/125；10 月只有 TWAP | 約 1 s | 本地接收時間 | 數值 VERIFIED；freshness PARTIAL |
| E token executable bid + engine net | 116/128 | **9 月約 21.3 s 網格**；10 月 snapshot 1 s（只有 bid） | journal 主機時鐘 | VERIFIED（稀疏） |
| F entry score／candidate policy | score 128/128；`CANDIDATE_POLICY_SHADOW` 只從 10-09 起才有 | — | — | candidate policy INSUFFICIENT |
| G sigma／diffusion-z | **9 月 LIVE 持倉期間完全沒有記錄**；只有 10 月 3 筆 | — | — | **INSUFFICIENT** |
| H markout | 1/3/5/10/30 s，128/128 | — | — | VERIFIED |
| I 官方結果 | **OFFICIAL_VERIFIED=128、RUNTIME_ONLY=0、UNKNOWN=0** | — | — | VERIFIED |
| J breaker 送單 | 20 持倉、5 天 | 送單時間 | journal 時鐘 | 只有送單 VERIFIED；9 月沒有記錄 eligibility components |
| K prospective telemetry | 3 持倉、1 天 | 約 5 s + 轉換點 | 主機時鐘 + monotonic | VERIFIED |

- `reference_1s` 的 `polymarket_bbo` 與 snapshot 的 `up_mid` 是 midpoint，且 UP/DOWN token 交替出現、沒有 token 標識 → **UNUSABLE**，不做 token 訊號，也不做經濟計算。VERIFIED（同一秒數值在 30/68、44/58 之間跳動）。
- 新發現兩個資料品質問題，都是 VERIFIED：
  1. 14 筆持倉的 taker-exit SELL fill 被 journal 標成配對 token 的 instrument_id。從成交價看，賣的是持有的 token（INFERRED）；沒有任何市場同時買過兩個 token，所以以市場為單位納入這些 SELL。
  2. journal 的 `MARKET_CYCLE_PNL` 對 TP 出場的持倉重複計算結算（例：1789387200 記為 +6.47，用 fills 重算是 +0.85）。本報告一律用 fills + 官方 payout 重算。
- **今天 3 個市場的官方結果（新取得）：** 1791586800、1791587700、1791589500 皆為 DOWN（Gamma `resolved`）。與 runtime 標籤一致，所以上一份報告中「官方結果 UNVERIFIED」這一項現在已 VERIFIED。

**Cohort：** DEV = 2026-09-10…09-25（9 天、56 持倉）；HOLDOUT = 09-27…09-30（4 天、69 持倉；09-28 單日就有 41 筆）；PROSPECTIVE = 10-10（3 筆）。
- 時間特徵：跨 sizing／breaker 版本合併（特徵語意相容）。
- PnL：同時報 USDC、per share、per notional。
- 9 月的 cohort 異質性：11 筆 fast-follow 進場；breaker 版本在 09-25、09-27、09-29 變過；sizing 是 L2 cap（>0.70 約 $5.5 notional）而非 share_v1。

```
COHORTS_USED=Sep LIVE 2026-09-10..09-30 (DEV+HOLDOUT, 125); PROSPECTIVE 2026-10-10 (3, separate)
COHORTS_EXCLUDED=DRY/shadow positions (not pooled; sensitivity SKIPPED); Tier-3/excluded data (none used)
WHY=executability claims require actual LIVE exposure and recorded executable bids
```

## 2. Feasibility gate（S2）

| 項目 | 值 |
|---|---|
| 持倉 | 128（歷史 125 + prospective 3） |
| 虧損 / 獲利 | 37 / 91（9 月 loser 比例 0.28） |
| recoverable winners（曾 net<0、最後 ≥0） | 73 |
| severe（觀察到 net ≤ −2 / −3 / −4） | 33 / 25 / 12 |
| breaker 送單持倉 | 20（5 天；其中 19 筆送單後有 SELL 成交；5 筆有 FOK 拒單） |
| 獨立日 | 14（9 月 13 天） |
| 訊號後 executable bid 的覆蓋 | 依規則不同為 36%–100%（9 月網格約 21 s） |

依 small-sample 規則：
- 可以做：Binance、token、PnL、strike/TWAP cross、TTE、markout、entry 特徵的 scorecard。
- **INSUFFICIENT**：sigma 與 diffusion-z（組合 I/J）、low score（只觸發 1 筆）、wide spread（4 筆）、candidate policy、週末比較（UTC 週末只有 1 個獨立日）。
- `HOLDOUT_VALID = YES`：HOLDOUT 有 4 天；token 系列在 HOLDOUT 都有 ≥5 筆觸發、跨 3 天。

## 3. Scorecard 重點（S3/S4；完整欄位見兩份 scorecard CSV，以下是 ALL_SEPT）

| 規則 | 觸發/天 | loser 覆蓋 | winner 誤報 | lead vs −2（中位，s） | lead vs breaker（中位，s，n） | bid 覆蓋 | NET（vs 實際） | NET vs hold |
|---|---|---|---|---|---|---|---|---|
| Binance≥3 bps（A） | 83/13 | 0.97 | 0.54 | 68.7 | 113（18） | 0.63 | +21.1 | +10.1 |
| Binance≥5 bps（B） | 68/13 | 0.89 | 0.41 | 56.9 | 77（16） | 0.69 | +9.4 | +15.9 |
| Binance≥10 bps | 36/11 | 0.69 | 0.13 | 15.2 | 29（10） | 0.81 | +21.5 | +19.4 |
| token≥0.05 | 64/12 | 0.91 | 0.36 | 64.4 | 133（18） | 0.97 | +29.8 | +18.8 |
| token≥0.10 | 49/11 | 0.91 | 0.19 | 21.1 | 106（18） | 0.96 | +36.2 | +28.0 |
| token≥0.15（C） | 40/11 | 0.89 | 0.10 | 0.0（同時） | 75（18） | 0.98 | +37.3 | +30.5 |
| token≥0.15 持續 15 s | 38/11 | 0.83 | 0.10 | −15.1 | 80（16） | 0.92 | +37.0 | +30.6 |
| Binance≥3 ∧ token≥0.15（E） | 39/11 | 0.89 | 0.09 | 0.0 | 52（18） | 0.95 | +35.9 | +28.4 |
| PnL≤−2（無條件） | 30/10 | 0.77 | 0.03 | 0（定義上） | 43（17） | 0.97 | +26.3 | +28.9 |
| Chainlink spot cross | 47/12 | 0.94 | 0.16 | 6.6 | 39（17） | 0.79 | +23.4 | +19.5 |
| TWAP cross | 34/11 | 0.69 | 0.11 | **−22.3（比 −2 晚）** | 26（12） | 0.56 | **−7.9** | −6.8 |
| TTE≤120 | 47/12 | 0.51 | 0.32 | −128 | 15（2） | 0.36 | +8.3 | +8.7 |
| 負 30 s markout | 61/13 | 0.60 | 0.44 | 150 | 171（12） | 0.89 | **−12.6** | −25.5 |

NET 的計算方式：在訊號後 25 s 內第一筆 engine row 的 `net_if_exit` 出場，減去實際已實現 PnL。若實際出場早於訊號，視為 censored（不計）。從不使用 midpoint。

**訊號後的軌跡**（HYPOTHETICAL-HOLD，censored at exit；`summary.json`）：
- token≥0.15 觸發後，虧損單的 net 中位數在 +5/+15/+30 s 約 −2.0，最終 −4.03。
- recoverable winners 從 −1.35 回到 +2.36，TP winners 從 −1.46 回到 +0.87。
- 真正的虧損單沒有在 60 s 內加速惡化，但最後仍虧到約兩倍；誤報則是慢慢恢復。這個區分在短窗口內無法從 PnL 本身看出（INFERRED）。

## 4. Winners / losers（S4；`winner_false_positive_table.csv`、`loser_coverage_table.csv`）

- **whipsaw：** token≥0.15 讓 9 筆贏家提早出場（whipsaw 成本 28.4），換到 30 筆虧損單提早出場（省下 65.7）。token≥0.05 讓 31 筆贏家出場（成本 70.0）。Binance≥3 的贏家誤報率是 54%。TP winner 被提早出場的比率：token≥0.15 為 0.11，Binance≥3 為 0.49。
- **loser 覆蓋與最早有效警示：**
  - Binance≥3：最早，比 −2 早約 69 s、比 breaker 早約 113 s，但誤報太多。
  - token≥0.10：比 −2 早約 21 s。
  - token≥0.15：在 21 s 網格上與 −2 同時。原因是 bid 往往一次跳空一個網格。
  - TWAP cross：最晚，比 −2 還晚 22 s。
- **EARLIEST_RELIABLE_WARNING 排序**（兼顧誤報）：token≥0.10～0.15 > Binance≥10 > Binance≥3/5（太早、太雜）> PnL≤−2 > TWAP。

## 5. Breaker bottleneck（S5；`breaker_delay_table.csv`，n=20）

- 中位數：第一個 warning → −2 是 **68.7 s**（訊號偵測端）；−2 → 送單是 **53.0 s**，PnL 再惡化中位 **−1.30**（政策確認端：trend+TTE+persistence+votes，9 月無法拆開）；送單 → 成交 **2.5 s**；5/20 筆有 FOK 拒單。
- 10 月的 2 筆（components 有記錄）：TREND 35–46 s → TTE 27–42 s → execution 2.3／24 s。
- **MAIN_BREAKER_BOTTLENECK = MIXED。** 9 月的主要可避免延遲是「政策確認」（INFERRED，無法拆成 TREND/TTE/VOTES）；10 月 n=2 是 TREND→TTE；execution 只是次要。

## 6. 角色分析（S5，描述性）

- **Binance vs token：** 可識別的虧損單中 BINANCE_LEADS 23、SIMULTANEOUS（<21 s 網格）7、TOKEN_LEADS 3。token 晚於 Binance 的中位數約 35 s（INFERRED；token 端是 21 s 網格，屬 OBSERVED_GRID_APPROX）。
- **spot vs TWAP：** Binance≥3 → TWAP cross 中位 67.6 s（n=35）；Chainlink spot cross → TWAP cross 35.4 s（IQR 26–46，n=34）；−2 → TWAP cross 24.7 s（n=24）。TWAP 是落後的確認訊號，不是 early warning。
- **sigma：** INSUFFICIENT（9 月沒有記錄）。
- **TTE：** E 的觸發多數在 TTE>300（虧損 25／贏家 6）。加上 TTE≤120 後誤報降到 0.02，但 loser 覆蓋掉到 0.26、NET 只剩 +3.0 → 判 WEAK。
- **距離 strike（進場時）：** <2 bps 5L/8W；2–5 bps 13L/27W；5–10 bps 11L/30W；≥10 bps 6L/25W。描述性，n 小。
- **進場 markout：** 30 s markout ≤−0.045 的桶中 loser 15/35（43%）；≥0 的桶中 14/64（22%）。描述上有關聯，但單獨作出場規則 NET −12.6；與 token 組合（M）的 NET +16.4，低於單用 token 的 +37.3 → 判 WEAK。
- **score：** |score|<0.12 只有 1 筆 → INSUFFICIENT；0.12–0.25 是 7L/7W，≥0.25 是 27L/83W（描述性）。
- **價格桶：** ≤0.70 是 21L/21W（10 股桶），token≥0.15 觸發 21 筆；>0.70 是 14L/69W，觸發 19 筆。同樣的 −$2 在 10 股約等於 0.20/股，在 5.5 股約等於 0.36/股，因此 PnL 系列依 size 而定，token 系列較不依 size。
- **微結構：** 9 月進場 spread 幾乎都是 0.01；出場深度沒有記錄；FOK 拒單 5/20。SIGNAL_QUALITY 與 EXECUTABILITY 分開看：Binance 訊號的 bid 覆蓋只有 63–81%，原因是 bid 網格太稀，而不是訊號本身。2 筆 qty<5 屬於 NON_ACTIONABLE。
- **週末：** 9 月 UTC 週末只有 1 個獨立日（09-27）→ **INSUFFICIENT**。沒有重複舊報告的 market-level 偽重複。

## 7. Gate 結果與 shortlist（S6）

60 條規則中有 14 條通過全部 gate，**全部屬於同一族：executable-bid／PnL 惡化**（token≥0.05/0.10/0.15/0.20、token≥0.15 持續 5/15 s、PnL≤−1/−1.5/−2、C/D/E/F、M）。Binance 系列全部因 bid 覆蓋 <80% 未通過；TWAP、TTE、sigma、markout、entry 特徵全部未通過。各規則的失敗原因列在 `summary.json` 的 `gates`。

穩健性：
- 留一日法（leave-one-day-out）NET 最小值全部 >0。
- day-blocked bootstrap 95% CI 排除 0 的有：token≥0.10 [1.5, 78.2]、token≥0.15 [7.0, 68.0]、token≥0.15 持續 15 s [7.0, 67.8]、持續 5 s、E、PnL≤−1.5。
- 包含 0 的有：token≥0.05 [−19.4, 80.0]、PnL≤−1、PnL≤−2、token≥0.20、M。
- **EXPLORATORY 敏感度**（延後一格約 21 s 才出場）：token 系列在 DEV 與 HOLDOUT 的 NET 仍為正。

Shortlist 方法（事前規則）：在通過 gate 的規則中，依 **DEV** 的 NET 排名取前 3，移除完全重複的組合。

| | 規則 | DEV／HOLDOUT NET | bootstrap 95% CI | 主要風險 | 信心 |
|---|---|---|---|---|---|
| **CANDIDATE_A** | `TOKEN_dd_ge_0.05`（bid 較進場時 bid 跌 ≥0.05） | +18.7／+11.1 | **[−19.4, 80.0] 含 0** | 31 筆贏家被提早出場，whipsaw 70 對 saved 100；幾乎是 1 tick 等級的噪音 | **LOW** |
| **CANDIDATE_B** | `TOKEN_dd_ge_0.10` | +15.7／+20.5 | [1.5, 78.2] | 16 筆贏家出場；在 21 s 網格上比 −2 早約 21 s | LOW–MEDIUM |
| **CANDIDATE_C** | `PERSIST_token_ge_0.15_15s` | +15.0／+22.0 | [7.0, 67.8] | 在 21 s 網格上，5/15 s persistence 與不加 persistence 幾乎相同；比 −2 晚約 15 s；比 breaker 早約 80 s | MEDIUM–LOW |

共同限制：
1. 三者是**同一個特徵**（executable bid 相對進場 bid 的回撤）的不同門檻，彼此不是獨立證據。
2. 出場價使用觸發當下的那筆 bid，沒有偵測延遲；上述「延後一格」敏感度仍為正。
3. top-of-book 深度未知，FOK 可能被拒（breaker 歷史 5/20）。
4. 9 月 cohort 有 FF 污染、breaker 版本變動、L2-cap sizing。
5. 檢查了 60 條規則，多重比較的選擇偏誤存在。
6. PROSPECTIVE 3 筆：A +1.7、B +1.0、C −1.2 → INSUFFICIENT。

不得 productionize。

## 8. 特徵族排名

| 族 | 早 | loser 覆蓋 | 誤報控制 | 可執行 | 資料覆蓋 | 穩健 | 總評 |
|---|---|---|---|---|---|---|---|
| token bid 回撤 | 中 | 強 | 中（≥0.10） | 強 | 中（21 s） | 中 | **PROMISING** |
| PnL 門檻（無條件） | 中低 | 中 | 強 | 強 | 中 | 中 | PROMISING（與 token 同源） |
| Binance spot | **強** | 強 | 弱 | 弱（bid 覆蓋） | 強 | 中 | PROMISING（timing），execution WEAK |
| spot strike cross（Chainlink） | 中 | 強 | 中 | 弱 | 強 | 弱 | WEAK |
| TWAP cross | 弱（落後） | 中 | 中 | 弱 | 強 | 弱 | WEAK（只作確認） |
| sigma | — | — | — | — | 無 | — | INSUFFICIENT |
| TTE | 弱 | 弱 | 中 | 弱 | 強 | 弱 | WEAK |
| 距離 strike | — | — | — | — | 中 | — | INSUFFICIENT（描述） |
| entry score | — | — | — | — | 強 | — | INSUFFICIENT |
| markout | 強（進場即知） | 弱 | 弱 | 強 | 強 | 弱 | WEAK |
| liquidity／depth | — | 弱 | — | — | 弱 | — | INSUFFICIENT |

## 9. 下一輪 12+ markets 的 prospective shadow 計畫（不交易）

- **Shadow（不交易）：** `EARLY_WARNING_A/B/C_TRIGGERED`，即 token bid 相對進場 bid 的回撤 ≥0.05/≥0.10，以及 ≥0.15 持續 15 s。逐筆記錄 first-trigger 時間、當時的 executable bid／net／TTE。之後與 −2 threshold、TWAP cross、breaker eligible、實際出場、官方結果比對。
- **需要補的欄位（目前缺）：**
  1. 持倉期間的 executable bid 需要 ≤5 s 規律網格。10 月 snapshot 已有 1 s bid，但 `STOP_TIMING` 只記轉換點；需確認 snapshot 在持倉期間持續寫入。
  2. 進場時的 executable bid baseline 寫進 position-opened row（現在要從 `FILL_MARKOUT` join）。
  3. 出場 size 的 top-of-book 深度（held token 的 bid depth）。
  4. taker-exit SELL fill 的 instrument_id identity bug（記錄問題，非交易邏輯）。
- **不要新增：** 高頻 Binance telemetry。現有 1 s snapshot 已足夠，再加只是重複。
- **下一輪 LIVE 不得依這些候選交易。**

## 10. 最終判定

```
A BINANCE_EARLIER_THAN_MINUS2=YES（Binance≥3：中位 68.7 s，n=29；≥5：56.9 s；超過 2×21 s 網格；但贏家誤報 41–54%）
B BINANCE_EARLIER_THAN_BREAKER=YES（113 s n=18；77 s n=16）
C TOKEN_EARLIER_THAN_BINANCE=NO（Binance 先 23、同時 7、token 先 3；以可識別的虧損單計）
D SPOT_PLUS_TOKEN_BETTER_THAN_EITHER=NO（E 的 NET +35.9 ≈ token≥0.15 的 +37.3；多擋掉 1 筆贏家）
E PERSISTENCE_5_15_30S_REDUCES_FALSE_ALERTS=MIXED（Binance≥3：誤報 0.54→0.48→0.37，覆蓋 0.97→0.91→0.89；token：21 s 網格下 5/15 s 無差別，30 s 讓 NET 轉負）
F SIGMA_ADDS_INFORMATION_AFTER_SPOT_TOKEN=INSUFFICIENT
G TTE_ADDS_INFORMATION=WEAK
H ENTRY_MARKOUT_ADDS_INFORMATION=WEAK
I TWAP_CROSS_USEFUL_AS_EARLY_WARNING=CONFIRMATION_ONLY（比 −2 晚 22–25 s；NET 為負）
J MAIN_BREAKER_BOTTLENECK=MIXED（−2→送單中位 53 s、PnL −1.30；9 月無法拆成 TREND/TTE/VOTES；10 月 n=2 為 TREND→TTE）
K ANY_CANDIDATE_READY_FOR_PROSPECTIVE_SHADOW=YES（只限 shadow）
L ANY_CANDIDATE_READY_FOR_PRODUCTION_SELL_AUTHORITY=NO
```

## 11. Coverage ledger

| 階段 | 狀態 | 備註 |
|---|---|---|
| S1 資料盤點 + cohort | DONE | DRY/shadow 敏感度 SKIPPED |
| S2 canonical timeline + feasibility | DONE | 9 月 token/PnL 為 21 s 網格；sigma INSUFFICIENT |
| S3 凍結 scorecard | DONE | 60 條規則 × DEV/HOLDOUT/ALL/PROSPECTIVE |
| S4 lead、persistence、winners/losers | DONE | recovery 15 s 常因網格無觀察 → 有 obs 計數 |
| S5 bottleneck + 角色分析 | PARTIAL | 9 月 breaker components 無法拆；candidate policy、sigma、週末 INSUFFICIENT；出場深度未記錄 |
| S6 holdout、排名、shortlist、計畫 | DONE | HOLDOUT_VALID=YES，但 HOLDOUT 被 09-28 主導 |

輸出：`report.md`、`summary.json`、`frozen_spec.md`（+ `.sha256`）、`position_timeline.csv`、`single_feature_scorecard.csv`、`candidate_combo_scorecard.csv`、`winner_false_positive_table.csv`、`loser_coverage_table.csv`、`breaker_delay_table.csv`、`cohort_coverage.csv`、`official_resolution_cache/`。
