# 獨立第二意見／紅隊稽核 — Polymarket BTC 15-Minute Trading Bot

- TS（UTC）：`20261008T121546Z`
- HEAD：`9352e6939bf7d2978e0e6747c1ceba148a5c8579`，branch `codex/db-resilience-and-stoploss-priority`
- 工作樹：Pass 0 時只有未追蹤的 `reports/research_analysis/*`（不影響 runtime）。稽核結束時相同，另加 `reports/second_opinion/`。
- 稽核期間 bot **沒有執行**（`evidence/00_ps.txt`）。
- AUDIT_COMPLETENESS=**PARTIAL**。全部 pass 都有做，但多個區域只是抽樣；見 §2。

> **稽核者自己造成的事件（必須揭露）。** Pass B 時，我的一個暫存腳本在 zsh 下沒有正確分詞。腳本裡的 `rm -rf $P` 沒加引號，結果刪掉了 7 個已追蹤的測試檔，並在原位置建立了空目錄。我在約 1 分鐘內發現。這些檔案在 Pass 0 時是乾淨的（與 HEAD 相同），所以用 `rmdir` 加上 `git show HEAD:<path> > <path>` 原樣還原，沒有動到 index 或 HEAD。還原後逐檔比對 `git hash-object == git rev-parse HEAD:<path>`，權限也一致（644）。這已違反「不得改動 reports/second_opinion 以外的檔案」規則。詳見 `work_*/notes_pass_C.md` 的 INCIDENT 段落；腳本已加固。

---

## 1. 執行摘要

**判定：** CODE_READY=NO、RESEARCH_READY=NO、TINY_LIVE_READY=NO、FULL_LIVE_READY=NO。沒有已驗證的 P0。已驗證的 P1 有 7 項。

**最高風險：**
1. **一條可送真單的路徑沒有 dry-run 防護。** `_maybe_maker_urgent_exit`（`bot/taker_exit.py:1396-1617`）會直接 `submit_order`。它只看 `stop_loss_enabled` 和 `maker_urgent_exit_enabled`，不看 test_mode。dry-run 用的也是真的、已認證的 exec client，而且 dry-run 時 RiskEngine 被 bypass（`bot/launcher.py:828`）。目前它沒有觸發，只因為 profile 設了 `STOP_LOSS_ENABLED=0`。營運者打算重新開啟停損，一旦開啟，這條路徑就會在 dry-run 中生效。
2. **flip-hazard 研究需要的標準證據，在整個 native-v2 期間都被丟棄了。** 從 2026-10-04 18:25Z 起，研究庫超過 500 MB 上限。之後 `TWAP_STRIKE_CROSS`、`TMINUS_CHECKPOINT`、`TWAP_PROJECTED_SIDE_CHANGE`、`SETTLEMENT_PATH_THRESHOLD_CROSS`、`MARKET_OPENING_TWAP_SAMPLE` 全部被抑制；10/5、10/6、10/7（UTC）一筆都沒有。同一段時間，每天約 200 MB 的 1 Hz 預測快照卻照常寫入。上限保護的對象剛好反了。
3. **「平日翻轉率高於週末」不成立。**
   - 先前報告用 Fisher 檢定得到 p=0.00129，但它把市場當成獨立樣本。
   - 以「天」做置換，只有 6 種排列，最小可達 p=0.167，不可能顯著。
   - 週末被「完整度」篩掉的市場，T-300 翻轉率是 19/104；留下來的是 0/46。篩選本身與結果相關。
   - 把所有有新鮮 TWAP 的市場都算進來，T-300：週末 12.5–12.9%，週一 13.7%。
4. **停損關閉時，所有逐倉位虧損上限都一起關閉。** 622e97e 讓 `STOP_LOSS_ENABLED=0` 連帶停用 `absolute_max_loss_breaker`、catastrophic stop、endgame TWAP exit 和 invalidation recovery。此外 dry-run 根本不會跑 taker exit。所以 10 月期間沒有任何真正執行過的停損證據。
5. **儲存沒有上限（UNBOUNDED）。**
   - TradeJournal 每天約 200–290 MB，其中九成以上是診斷資料。
   - 研究庫是 1.25 GB，超過上限 2.4 倍；預測快照仍沒有上限。
   - 每個週期都會產生一份完整 3.17 GB 的備份影像。
   - 剩餘空間 12–16 GB。
   - HEAD 的 3 h 備份頻率和 REQUIRED 事件白名單，**從未在生產環境跑過**（9352e69 是在 bot 停止後才 commit）。

**我不同意先前結論的地方：**
- K4：weekday 效應不成立，見上。
- K11：資料不足以支撐 hazard model。
- 「Oct 6–7 是 native v2」：實際只有 2026-10-06 16:30:58Z 到 10-07 15:27:43Z，共 74 個 slug。
- `required_move_sigma` 不是擴散 z-score。它用的 σ 會隨 TTE 縮小：σ×max(0.3, TTE/600)，下限 0.20。所以在 TTE<600 s 時，z 會被放大最多 3.33 倍。
- POST_B 世代只有 3 個市場，其中只有 1 個是乾淨的。

---

## 2. 覆蓋範圍與限制

| 區域 | 狀態 | 說明 |
|---|---|---|
| Pass 0：HEAD／status／ps／df／大小／schema | EXAMINED_FULLY | `evidence/00_*` |
| Ph1 架構 | SAMPLED | 從程式碼重建；`market_runtime` 的 prewarm 部分未逐行讀 |
| Ph2 Git 歷史 | EXAMINED_FULLY（關鍵 10 個 commit）；SAMPLED（其他） | 對 8 個 commit 做了修補前測試 |
| Ph3 進場 | SAMPLED | 細讀 `run_bot.py:2096-2560` 和 `3525-3600`；`maker_engine` 全讀；`quote_service` 抽樣；`side_decision.py` 未讀 |
| Ph4 停損 | SAMPLED | gating 全部追過；`exit_engine` 門檻數學未逐行讀 |
| Ph5／Ph6 研究 | EXAMINED_FULLY（市場層 T-k 翻轉、z 分箱、選擇偏誤） | 未建立 hazard model；未做小時配對 |
| Ph7 新鮮度 | SAMPLED | snapshot writer 全讀；`spot_pricer` 部分讀；研究腳本只用 grep |
| Ph8 rollover／L2 | SAMPLED | L2 retry 生命週期與測試全讀；handoff／prewarm 抽樣 |
| Ph9 儲存 | EXAMINED_FULLY（journal 備份、TWAP guard、成長量測）；SAMPLED（retention 腳本） | |
| Ph10 runtime 效率 | SAMPLED | 只用日誌（10/7 07:34–23:28 +08）；沒做 profiling |
| Ph11 復原 | SAMPLED | 只讀程式碼；沒做當機模擬 |
| Ph12 實單安全 | EXAMINED_FULLY（4 個 submit 點、模式旗標）；SAMPLED（reconciliation） | 不連網路，無法驗證交易所端 |
| Ph13 測試品質 | SAMPLED | 跑了 140 個目標測試；完整測試套件未逐一確認 hermetic，所以沒跑 |
| Ph14 耦合 | EXAMINED_FULLY（Outcome／FF 殘留引用 grep） | |

**限制：**
- 不能連網路，所以無法驗證交易所的實際持倉和掛單。
- 日誌只涵蓋 10/7 一天；沒有出現錯誤，不代表其他日子也沒有。
- 以 `mode=ro` 開啟 WAL DB 時，會更新 `-shm` 的 mtime（內容沒變）。
- Tier 2 的 provenance 用了先前的重算 CSV 作為輸入，但我做了一致性處理：要求每一列的 TWAP 分類都是 FRESH_*。

---

## 3. 架構（依 HEAD 原始碼重建）

**資料來源：**
- Polymarket CLOB websocket：QuoteTick 和 OrderBookDeltas（經 `adapter_overrides` 做有界的 L2 發布，每 0.25 s 一次、10 檔）
- Polymarket Chainlink 原始現貨價和官方 60 s TWAP（結算參考：`polymarket_chainlink_twap_60s_ws`）
- Binance websocket（作為 path spot 的備援）
- Gamma REST（取得市場和 strike）
- Polygon RPC（redeem）

**策略管線：** discovery → admission → side decision（score／lock）→ fair = **market mid** → MakerEngine 被動報價 → `evaluate_buy_entry_controls` → depth-risk sizing（L2 不新鮮時數量為 0）→ `submit_maker_quote`

- dry-run 時：`ORDER_DRY_RUN_SUBMITTED` 加上 shadow 模擬成交。
- live 時：先寫入 durable intent，再 `submit_order`。
- 出場：TP／recycle sell、taker／urgent exit（由 STOP_LOSS 控制）、hold-to-redeem、auto-redeem（dry-run 時跳過）。

**Runtime：**
- 單一 Nautilus TradingNode owner loop。
- 每 3 h 做一次排程 node rollover（`launcher.py:681`）；有曝險時延後。
- 背景執行緒：auto-redeem、BTC1S writer、journal worker、LeadLagDB writer。

**持久化：**
- `logs/trade_journal.db`：WAL；保存訂單／策略事件與 session PnL。
- `backups/trade_journal.db`：每個週期一份完整 SQLite online backup。
- `data/research/twap_forward_shadow.db`：沿用舊的 LeadLagDB schema。
- 另有 BTC1S parquet。

**復原：** `_rehydrate_inventory_state_on_startup`（Nautilus cache 的持倉，加上 DB 的成本）；`_restore_market_risk_guards_from_trade_db_on_startup`（只算 ORDER_FILLED）。

```mermaid
flowchart LR
  WS[Polymarket WS\nQuote+L2] --> AO[adapter_overrides\nbounded L2/coalesce]
  AO --> DE[Nautilus DataEngine/Cache]
  DE -->|on_order_book_deltas| L2TS[l2_update_ts_by_inst\nlocal time.time]
  DE -->|quote_tick| Q[_get_quote_for_instrument\n(no age gate, synth sides)]
  CL[Chainlink raw spot + 60s TWAP] --> SP[spot_pricer]
  BN[Binance WS] -.fallback path_spot.-> SP
  SP --> SD[side_decision / lock]
  Q --> ME[MakerEngine\nfair=mid, AS buffer=0]
  L2TS --> DR[depth_risk cap\nmissing_l2 -> qty 0]
  ME --> EC[evaluate_buy_entry_controls\nstrike/TWAP/score gates]
  SD --> EC
  EC --> DR --> OS[order_submission\n(dry-run gate)]
  OS -->|live| EX[PolymarketExecClient]
  OS -->|dry-run| SIM[shadow_simulation]
  TX[taker_exit\n(is_simulation gate)] --> EX
  UX[urgent_exit\n(NO dry-run gate)] --> EX
  REC[startup rehydrate\ncache.positions_open] --> UX
  SP --> PRS[prediction snapshot 1Hz] --> RDB[(research DB\nLeadLagDB schema)]
  TW[twap_forward_shadow] --> RDB
  OS --> TJ[(trade_journal.db)]
  TJ --> BK[(backups full image)]
```

ASCII 備援版本：
```
WS -> adapter(L2/coalesce) -> DataEngine/Cache -> {l2_ts map, quote_tick}
Chainlink/TWAP (+Binance fallback) -> spot_pricer -> side_decision
quote_tick -> MakerEngine(fair=mid) -> entry_controls -> depth_risk(L2 fresh?) -> order_submission -> [dry-run: sim] / [live: exec]
taker_exit(gated by dry-run) -> exec ; urgent_exit(NOT gated) -> exec
journal(WAL) -> full-image backup ; research DB <- prediction 1Hz + twap shadow
```

**單一權威表（authority）：**

| X | 權威 | 是否有兩個權威？ |
|---|---|---|
| Side | `side_decision`／`active_side` + lock（run_bot） | 單一。但 invalidation counter 是每個 slug 一個，見 P2-001 |
| Fair value | `market_mid`（run_bot.py:2131-2142）；digital fair 只當遙測 | 單一（刻意設計） |
| Entry price | MakerEngine `quote_bid`，再經 entry-quality placement | 單一 |
| L2 freshness | `l2_update_ts_by_inst`（market_runtime.py:574-587） | 單一，K8 確認 |
| Position state | `live_inventory_cost`（fill ledger）＋ Nautilus cache positions（復原時） | **兩個**：復原時讀 cache，平時讀 ledger。成本未知時會設 avg_entry=0 |
| Strike | `market_strike_cache_by_slug`＋驗證狀態 | 單一，未驗證時進場 fail closed |
| Settlement | 官方 TWAP 的 canonical side（`MARKET_TWAP_SUMMARY`）；`post_trade` 以 outcome 計算 redeem | 研究與 PnL 各用一個，屬可接受的雙軌 |
| BBO | `cache.quote_tick` 經 `_get_quote_for_instrument` 修補（補齊缺邊、修正交叉） | 單一，但會**捏造**報價，見 P2-006 |

---

## 4. Commit 表（Phase 2）

| SHA | 意圖 | 分類 | 測試是否在修補前失敗 | Runtime 證據 |
|---|---|---|---|---|
| e4e87ee | L2 retry 狀態初始化 | STRONG_FIX | 是，3/4（KeyError） | 10/7 13:26Z 起有執行；日誌中沒有 retry_at 錯誤（視窗內） |
| 9f60f63 | 新鮮度時鐘域修正 | LIKELY_CORRECT | 是，22 個失敗（行為性） | 10/6 16:30Z 起有 v2 列；**讀取端沒有強制檢查版本** |
| 3b50637 | 移除 FF 執行權（Phase A） | LIKELY_CORRECT | 只是 ImportError，屬 API 層，**不能證明行為被覆蓋** | Phase A 之後只有前一個 run 留下 1 筆 FF intent；之後只有觀測型遙測 |
| 33896ce | 移除 Outcome／HL（Phase B） | PARTIALLY_VERIFIED | 是，7/7（行為性） | 只跑了 31 分鐘（14:56–15:27Z），git_dirty=true |
| 1fa2ba0 | 備份失敗隔離 | LIKELY_CORRECT | 是，8 個失敗 | 最終備份成功 6.16 s |
| b034b4e | 降低備份寫入放大（900 s） | LIKELY_CORRECT | 是，5 個失敗 | 日誌顯示 interval=900 |
| 8309152 | 保留政策 | 未驗證 | 未執行 | — |
| 49aa9ca | 3 h rollover | LIKELY_CORRECT | 是，3 個失敗 | 週期 10831–10835 s |
| 9352e69 | 研究庫上限與 3 h 備份 | **RISKY** | 只是 AttributeError（API 層） | **從未在 runtime 執行**；仍然沒有上限 |
| 622e97e | 停用停損 | **RISKY**（範圍比訊息大） | 有測試修改 | 連 absolute max loss 斷路器也一起關掉 |

---

## 5. 各 Phase 發現（3–14）

**Ph3 進場**
- fair 用的是 mid。
- BUY 的 EV 門檻刻意排除逆選擇成本（`maker_engine.py:497-510, 577-586`）。所以這道門檻在結構上幾乎總是「半個 spread 減手續費」。真正的 edge 只來自選邊邏輯。
- 以下情況 fail closed：strike 未驗證、TWAP degraded、L2 超過 2 s（深度 → 數量 0，見 `run_bot.py:3566-3572`）。
- BBO 本身沒有年齡門檻：缺一邊時會用 1 tick 補，交叉時會修成 1 美分 spread。
- 多重進場的防護：每市場買入上限、pending taker exit；重啟後只還原 ORDER_FILLED。
- 會跨 rollover 保留：journal 中的 guard counts。

**Ph4 停損**
- 目前生效值：`STOP_LOSS_ENABLED=0`（profile 第 4 行與 .env；載入順序 profile→.env，shell 優先）。
- 程式預設值是 True。所以如果換了 profile 或刪掉該鍵，停損和 urgent exit 會靜默重新啟用。
- invalidation 規則：spot 沒有越過 strike±2 bps，且持有 token 的 mid ≤ 0.36，連續 3 次確認。
- 計數器每個 quote cycle 會累加 2 次（P2-001）。
- 判定：**NOT_READY**。
  - 沒有真的執行過停損。
  - 沒有任何虧損上限。
  - 停損研究只有 shadow 資料。
  - 稽核前已知的 whipsaw 問題沒有新的樣本外證據。

**Ph5 翻轉**（全部以市場為單位，各 tier 分開，見 `evidence/13_flip_stats.txt`）

| Tier | 截點 | 翻轉 | Wilson 95% | N_markets | N_days | 資料性質／階段 |
|---|---|---|---|---|---|---|
| 1 | T-300 | 13 | [0.138, 0.352] | 57 | 2（10/6 部分、10/7） | native v2；PRE_A 17／POST_A 39／POST_B 1 |
| 1 | T-180 | 7 | [0.062, 0.236] | 56 | 2 | 同上 |
| 1 | T-120 | 6 | [0.049, 0.211] | 57 | 2 | 同上 |
| 2 週末 | T-300 | 0 | [0, 0.077] | 46 | 2 | 重算（TWAP 已解析）；PRE_A |
| 2 平日 | T-300 | 10 | [0.115, 0.336] | 49 | 2 | 同上 |
| 2 週末／平日 | T-180 | 1／7 | [0.004, 0.113]／[0.071, 0.267] | 46／49 | 2／2 | 同上 |
| 2 週末／平日 | T-120 | 0／7 | [0, 0.077]／[0.072, 0.272] | 46／48 | 2／2 | 同上 |

- 以天為單位置換：6 種分組，最小 p=0.167，**無法達到顯著**。
- 每組只有 2 天時，day-bootstrap 會退化，不具意義。
- 排除原因（可重疊，共 480 個時段）：
  - gap>10s：187
  - 邊界不完整：182
  - 無結算：108
  - 時間跨度不足：107
  - TWAP 未解析：62
  - 無資料：56
  - startup_partial：52
  - raw_pre_v2：15
- 選擇偏誤（`evidence/14`）：

  | 週末市場 | T-300 翻轉 | TWAP 穿越 ≥2 次的比例 |
  |---|---|---|
  | 被排除 | 19/104 | 0.33 |
  | 被納入 | 0/46 | 0.15 |

- 敏感度分析（有標示，不是主要結果）：所有有新鮮 cutoff 的市場（不分 tier）。

  | 截點 | 10/3 | 10/4 | 10/5 | 10/6 | 10/7 |
  |---|---|---|---|---|---|
  | T-300 | .125 | .129 | .137 | .194 | .255 |
  | T-180 | .027 | .031 | .135 | .138 | .130 |

- 時區：UTC（slug 的 epoch）。如果改用美東時間定義週末，分組邊界會不同，這次沒做。
- 多重檢定：比較家族共 5 個截點 × 2 組 × 各分箱；沒有任何一個可以作為預先登錄的檢定。全部屬於探索性。
- 最小可偵測效應（MDE）：在天層級置換下，N_days=4 時沒有任何效應可達 α=0.05。
- 結論：**這只是關聯，且有選擇偏誤；不能推論因果，也不能主張顯著。**

**Ph6 σ**（`evidence/15`）
- 計算方式：線性距離 |S−K|／(S·σ_decay·√T)。
  - σ 取 120 個 tick，去均值；tick 間隔不規則；只用過去資料。
  - 經 floor／ceiling 與 TTE 衰減處理。
  - 在最終視窗內，對「平均價」用的是 √τ，但應該用 √(τ/3)。
  - path spot 會在 Chainlink 與 Binance 之間切換。
- 觀察：
  - z≥2 時，Tier 1 沒有翻轉（T-300 為 0/17）。
  - 中間分箱不單調，但 CI 完全重疊，可以用小 N 解釋。
  - 觀察到的翻轉率不跟隨 Φ(−z)。例如 Tier 2 T-300 [0.5,1) 分箱：0/32，而 Φ 平均是 0.237。
- 建議：
  - 一個目標做一個模型（h 秒內穿越、到期前穿越、結算翻轉、穿越後回落）。
  - 用以市場路徑為單位、在到期時截尾的離散時間 hazard 模型。
  - 共變數：模型一致的 z（不做衰減；TWAP 平均用正確的變異數；單一參考價）、TTE、vol regime、spread／depth、小時。
  - 以 reflection 2Φ(−z) 作為基準。
  - 評估：day-blocked CV，看 log-loss、Brier 與校準曲線。
- 目前資料能否支撐：**不能**。
  - 10/5–10/7 的標準穿越事件遺失。
  - 只有 1 Hz，而且開盤約 19 s 是盲區。
  - Tier 1 只有 57 個市場，約 1 天。

**Ph7 新鮮度**
- 修補後：writer 使用本地接收時間計算傳輸年齡，同源時鐘計算數值年齡；負年齡視為無效，不會被夾成 0。
- 還沒做到的：
  - 沒有任何讀取端強制要求 `freshness_clock_semantics_version`（grep 只在 writer 找到）。
  - `market_mid_fresh` 用的是 OR（UP 或 DOWN 任一邊新鮮即可）。
  - BTC1S 遙測的 `source_receive_p50_ms=-414` 仍是跨時鐘域相減的結果（屬遙測，不是 gate）。
- 提醒：joint freshness 高，**不等於**報價在同一時間可成交。

**Ph8 rollover／L2**
- retry_at 修正已涵蓋所有生產者路徑：stopping、unsubscribed、due、snapshot（`adapter_overrides.py:907-918`，唯一入口）。
- L2 戳記是每個 token 一個，取本地時間；缺少或過期時 fail closed。
- 3 h 週期在 10/7 執行了 3 次；停止到重啟約 15 s。
- 但預測快照最大 gap 的 p90 是 195 s。原因是 watchdog／營運者手動停止，而且這些 gap 和波動大的市場相關（見選擇偏誤）。

**Ph9 儲存**

| 檔案 | 用途 | 大小 | 成長量 | 保留 | 重要性 | RPO |
|---|---|---|---|---|---|---|
| logs/trade_journal.db | 標準交易／風險記錄＋**診斷** | 3.17 GB | 每天 200–290 MB 酬載 | 無清理 | CANONICAL（混雜） | WAL 持久 |
| backups/trade_journal.db | 完整影像 | 3.17 GB | 每週期一份完整影像 | 1 份 | DERIVED | HEAD 為 3 h（runtime 實際跑的是 900 s）；**同一個 volume** |
| data/research/twap_forward_shadow.db | 研究 | 1.25 GB（上限 500 MB） | 每天約 150–230 MB | 無 | 混合 | — |
| data/analysis_snapshots/freshness_audit_* | 稽核快照 | 2.75＋0.96 GB | 固定 | 手動 | DERIVED | — |
| data/analysis_archives/*.tar.zst | 冷封存 | 約 1.43 GB | 固定 | 手動 | DERIVED | — |

- 備份前有空間預檢：需要 2×image＋tmp。
- APFS 歸因：只是**假說**。
  - 本機快照：TM 4 個，加上 os.update 3 個（含 MSUPrepareUpdate）。
  - 稽核期間 bot 未執行、我也沒刪任何東西，可用空間卻從 12 GiB 自行回升到 16.46 GB。
  - 要證實，需要 extent 層級的歸屬，或在快照建立／thinning 前後量測 df 與 du 的差值。
- 結論：**UNBOUNDED**。

**Ph10 效率**（日誌實測）
- DataEngine qlat p95 約 11–16 ms，queue 深度 0–1，BTC1S 0 丟棄。
- 推論：主要成本在診斷資料的持久化，例如 `EVENT_LOOP_CONSUMER_TIMING` 每列約 22.7 KB，以及每週期 3 GB 的完整備份。不是策略本身的運算。

**Ph11 復原**（INFERRED，只讀程式碼）

| 情境 | 重複 | 遺失 | 錯誤重建 | 錯誤成本 | 錯誤市場 |
|---|---|---|---|---|---|
| 正常關機／SIGTERM | 否（會取消已追蹤的 maker 單；有最終備份，已驗證日誌） | 否 | 否 | 否 | 否 |
| 當機且有掛單中的 maker | **可能**：孤兒 GTC 不會被取消，guard 只算成交（INFERRED） | 否 | 可能 | 可能 | 否 |
| 部分成交後當機 | 可能 | 否 | qty 來自 cache | 成本來自最近一次送單價或 0（INFERRED） | 否 |
| 有持倉時重啟 | 否 | 否 | 會重建＋強制只賣（INFERRED） | 可能為 0 | 否（只看目前 slug） |
| 斷電 + WAL | 否 | 最後未提交的交易 | 否 | 否 | 否 |
| 備份只發布一半 | 否（tmp 加 os.replace，已驗證程式碼） | 否 | 否 | 否 | 否 |
| rollover 後重啟 | 只還原目前 slug 的 guard（INFERRED） | — | — | — | — |

**Ph12 實單安全**
- 只要沒有 `--live` 就是 dry-run（fail-closed）；live 需要輸入 'yes'，並取得 process lock。
- dry-run 時：exec client 是真的、已認證；RiskEngine 被 bypass；`urgent_exit` 沒有防護（P1-001）。
- client order id 用毫秒時間戳，不具冪等性。
- 有 kill switch（cancel 失敗時觸發）和每日 session guard。
- `STOP_LOSS=0` 時沒有任何逐倉虧損上限。
- 沒有在啟動時與交易所比對 open orders 並取消孤兒單。
- 憑證：`.env` 已被 gitignore；沒有印出或讀取 secret 值。
- dry-run 成交模型比現實樂觀之處：全量成交、沒有延遲、沒有 post-only reject。但它要求 ask 必須觸及我方價格，這點偏保守。

**Ph13 測試品質**
- 8 個 commit 中，有 6 個的測試在修補前會「行為性」失敗。3b50637 和 9352e69 只是 API 層失敗。
- 缺少的高價值測試：
  1. dry-run 下對每個 submit 呼叫點斷言「不會送到 exec」，包含 STOP_LOSS=1 加上已重建持倉的情況。
  2. invalidation 確認要以「時間」或「每個 cycle」為單位：同一個 cycle 內呼叫 buy 和 sell 兩次，計數器只能加 1。
  3. 當機注入：先寫入 intent、送單，在成交前 kill，再重啟。斷言不會出現第二筆 BUY，且孤兒單會被取消或納管。
  4. 研究庫超過上限時：斷言 REQUIRED 事件仍會寫入；預測快照會被限速；空間低於 X GB 時，選用寫入者會停止。
  5. 讀取端遇到沒有版本欄位或 v1 的列時要報錯或排除。
  6. 磁碟滿時：journal 的 BUY 必須 fail closed。

**Ph14 耦合**
- 研究庫沿用 LeadLagDB 的 schema，裡面還有 `hyperliquid_market_id` 欄位。
- FF 相容程式碼還在 recovery／order_events／journal 的關鍵事件集合中。
- `.env` 裡還有 1 個已退役的鍵。
- 預測快照、TWAP shadow、forward shadow 和 stop forensics 共用同一個 DB。
- health 指標不作為進場 gate，但會顯示 Storage CRITICAL，同時又顯示 tradable=YES。

---

## 6. 發現清單

| id | sev | layer | 標題 | status | 證據 | 影響路徑 | 可能性 | 衝擊 | 建議 | 現在需改碼 | 信心 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| P1-001 | P1 | A/D | urgent exit 可送真單，卻沒有 dry-run 防護 | VERIFIED（程式碼） | taker_exit.py:1396-1617；launcher.py:828,1091-1094；app_config.py:980,1016 | dry-run 加 STOP_LOSS=1 | 低（目前停用）→ 啟用停損後為中 | 真實 SELL、繞過 RiskEngine | 在 `submit_order` 單一入口斷言 `not test_mode`；urgent exit 加 dry-run 判斷 | 是：重新啟用停損前必須改 | 高 |
| P1-002 | P1 | B/D | 標準 TWAP 穿越／檢查點事件從 10/4 18:25Z 起遺失 | VERIFIED（DB） | evidence/05,06,07 | flip-hazard 研究 | 已發生 | 10/5–10/7 無法重建穿越時間 | 從 1 Hz 快照部分重建並標示解析度；HEAD 白名單要做 soak 驗證 | 否（HEAD 已修；需要驗證） | 高 |
| P1-003 | P1 | B | weekday／weekend 推論無效：偽重複＋與結果相關的篩選 | VERIFIED | evidence/13,14；先前報告第 27 行（Fisher p） | 研究結論 | — | 錯誤的 regime 結論 | 以天為單位推論；預先登錄；多週資料 | 否 | 高 |
| P1-004 | P1 | B | `required_move_sigma` 不是擴散 z-score | VERIFIED（程式碼） | forecast_state.py:119-127；live_entry_research.py:348-359；spot_pricer.py:1155-1270 | 難度分箱、hazard 共變數 | — | 錯誤校準 | 新增模型一致的 z 欄位 | 是（研究欄位） | 高 |
| P1-005 | P1 | B | v2 版本欄位沒被強制；native v2 只有約 23 小時 | VERIFIED | prediction_research_snapshot.py:118；grep；evidence/11 | 研究讀取端 | 中 | v1／v2 混算 | 讀取端強制檢查版本 | 是（腳本） | 高 |
| P1-006 | P1 | C | maker EV 門檻排除逆選擇；9/30 之後沒有真實成交 | VERIFIED | maker_engine.py:497-510,577-586；evidence/01 | 實單就緒度 | — | 進場 edge 未經證實 | 用真實成交校準 markout；先以極小部位 live 收集資料 | 否 | 高 |
| P1-007 | P1 | C/D | STOP_LOSS=0 會移除所有逐倉虧損上限；dry-run 從不跑 exit | VERIFIED | 622e97e diff；taker_exit.py:119-123 | 實單風險 | 高 | 每市場最多損失全部本金 | 讓 absolute max loss 與 STOP_LOSS 脫鉤 | 是（實單前） | 高 |
| P2-001 | P2 | A/C | invalidation 計數器每個 cycle 加 2 次 | VERIFIED（程式碼） | run_bot.py:2276/2402；maker_engine.py:573-639 | 鎖邊／invalidation | 高 | 持續期門檻只剩約一半 | 改用時間判定 | 是 | 高 |
| P2-002 | P2 | D | 儲存無上限；journal 九成以上是診斷資料 | VERIFIED | evidence/05,08,09 | 磁碟 | 高 | 磁碟滿、備份停止 | 拆分診斷 DB、設硬上限、限速 | 是 | 高 |
| P2-003 | P2 | D | HEAD 的備份／guard 改動從未跑過；備份與主檔在同一 volume | VERIFIED | git log；日誌 interval=900 | 備份 | — | RPO 未驗證 | 做 soak run；異地備份 | 否 | 高 |
| P2-004 | P2 | B | 開盤約 19 s 盲區；187/480 個市場有 >10 s gap；POST_B 只有 1 個乾淨市場 | VERIFIED | evidence/12 | 研究覆蓋 | 高 | 可用樣本少 | 修正擷取時機 | 是 | 高 |
| P2-005 | P2 | A | runtime 來源不可重現（git_dirty=true，diff hash 為 null；10/5 前沒有 manifest） | VERIFIED | evidence/runs_phase.json | 世代歸屬 | 高 | 無法對應實際 runtime 程式碼 | 每次 run 封存 dirty diff | 是 | 高 |
| P2-006 | P2 | A | BBO 會被捏造（補缺邊、修正交叉） | VERIFIED | order_runtime.py:100-119 | fair／econ | 低（有 L2 gate） | 用錯誤 fair 報價 | 缺邊或交叉時直接拒絕 | 是 | 中 |
| P2-007 | P2 | A | client order id 用毫秒時間戳，不具冪等性 | VERIFIED（程式碼）；碰撞為 INFERRED | order_submission.py:523；taker_exit.py:1013,1603 | 實單 | 低 | 重複或拒單 | 持久化的 uuid／序號 | 是（實單前） | 中 |
| P3-001 | P3 | A | 舊 schema／FF 相容程式碼殘留 | VERIFIED | evidence/18 | 維護 | — | 混淆 | 清理 | 否 | 高 |
| P3-002 | P3 | D | health 不是 gate，但顯示 CRITICAL 時同時顯示 tradable=YES | VERIFIED | health.py:1；日誌 | 營運 | — | 誤讀狀態 | 文件說明或改顯示方式 | 否 | 高 |
| P3-003 | P3 | B | BTC1S 遙測出現負的接收延遲 | VERIFIED（日誌） | terminal_bot.log BTC1S 行 | 遙測 | — | 誤導 | 改為同源時鐘 | 否 | 中 |

**可疑但未驗證：**
- **S-1（P0 候選）：** dry-run 時，從交易所 reconcile 回來的真實錢包持倉會被重建進 `live_inventory_cost`，再由 urgent exit 賣出。
  - 要驗證需要：(a) STOP_LOSS=1；(b) 錢包持有目前市場的 token；(c) 確認 Nautilus 在 dry-run 時會填入 `cache.positions_open`。
  - DB 中從未出現 `STARTUP_INVENTORY_REHYDRATED`（evidence/20）。
- **S-2（P1 候選）：** 當機後的孤兒 GTC 掛單加上重新下單，造成重複曝險。
  - 需要用 fake venue 做當機注入測試。
- **S-3（P2）：** APFS／TM 快照保留了被替換掉的舊備份區塊。
  - 需要 extent 層級的證據，或在快照建立／thinning 前後量測 df／du 差值，並排除 os.update 快照的影響。

---

## 7. 先前結論檢驗（K1–K12）

| K | 判定 | 證據 | 最強反論 | 什麼結果會改變判定 |
|---|---|---|---|---|
| K1 Outcome 已不需要 | CONFIRM | 33896ce 的測試在修補前 7/7 失敗；grep 找不到 runtime import | 舊 schema 和 FF 相容程式碼還在 | 若發現 runtime 還有呼叫 |
| K2 maker 獨立於 FF | CONFIRM（POST_A） | evidence/21；`FAST_FOLLOW_EXECUTION_ENABLED=False` | PRE_A 世代仍受污染（529 次 ownership skip） | 若 POST_A 出現 FF intent |
| K3 新鮮度 bug 已修 | PARTIAL | 9f60f63 測試在修補前失敗 | 讀取端不強制版本；BTC1S 仍有負延遲 | 讀取端強制版本＋端到端測試 |
| K4 平日翻轉率真的較高 | UNRESOLVED（先前的顯著性主張 REJECT） | evidence/13,14 | T-180／T-120 方向一致 | 4–6 週、預先登錄、以天置換後 p<0.05 |
| K5 sigma 有用 | PARTIAL | evidence/15：z≥2 時幾乎不翻轉 | 沒有校準、計算方式不一致 | 改用一致的 z 並做校準 |
| K6 停損未驗證 | CONFIRM | 沒有執行證據；P1-007 | 有 shadow forensics | 樣本外的 shadow 加上極小部位實單 |
| K7 3 h rollover 安全 | PARTIAL | 3 個完整週期 | gap 與波動市場相關；HEAD 改動未跑 | 用 HEAD 做 24 h soak，且 gap 小於 10 s |
| K8 中性 L2 正確 | CONFIRM | 程式碼與測試（evidence/03） | 戳記只代表「有收到」，不代表 book 有效 | 若出現 book 交叉卻被視為新鮮 |
| K9 APFS 造成儲存放大 | UNRESOLVED | 先前實驗＋evidence/19 | 有 os.update 快照；可用空間會自行回升 | extent 層級證據 |
| K10 TWAP 庫在抑制證據 | CONFIRM（生產期間）；HEAD 已修但未驗證 | evidence/05 | 1 Hz 快照可部分重建 | 若 HEAD 實跑時 REQUIRED 事件仍缺 |
| K11 資料足以建 hazard model | REJECT | P1-002、P2-004；57 個 Tier 1 市場 | 1 Hz 快照可以近似 | 多週、完整標準事件、補上開盤盲區 |
| K12 不適合無限制實單 | CONFIRM | P1-001、006、007 | — | — |

---

## 8. 就緒判定

- **CODE_READY = NO**
  - 沒有 P0，但有 P1-001／P1-007 和疑似的狀態重複路徑（S-2）。
  - 3b50637 和 9352e69 的測試沒有行為性覆蓋。
- **RESEARCH_READY = NO**
  - 標準事件遺失（P1-002）、選擇偏誤（P1-003）、z 定義（P1-004）、版本未強制（P1-005）、樣本只有約 1–4 天。
- **TINY_LIVE_READY = NO**。缺少：
  - 每個 submit 點的 dry-run 防護。
  - 不依賴 STOP_LOSS 的逐倉虧損上限。
  - 冪等的 order id。
  - 啟動時對交易所 open orders 做比對與取消。
  - 有上限的儲存加上 primary 的空間防護（目前只有 backup 有預檢）。
  - 明確的停損政策。
  - 已有的部分：notional 約 5.5 USDC、kill switch、process lock、L2 fail-closed、strike／TWAP fail-closed。
- **FULL_LIVE_READY = NO**。另外還需要多週、多 regime 的樣本外資料，以及真實成交證據。
  - 進場證據：WARNING（只有 dry-run）
  - 成交真實度：FAIL（9/30 之後零真實成交）
  - 停損證據：FAIL
  - 儲存安全：FAIL
  - runtime 穩定度：PASS（日誌實測）
  - 復原安全：WARNING
  - 統計穩健度：FAIL
  - regime 穩健度：FAIL

---

## 9. 下一步（依影響、急迫度、信心、工作量排序）

1. **在 `submit_order` 單一入口加上 dry-run 斷言，並讓 urgent exit 受 dry-run 控制，附測試。**
   - 原因：P1-001。前提：無。
   - 成功指標：test_mode 下，所有出場路徑 0 次 exec 呼叫（測試在修補前會失敗）。
   - blocks_tiny_live=yes；blocks_research=no。
2. **讓 absolute max loss 斷路器與 STOP_LOSS 脫鉤。**
   - 成功指標：STOP_LOSS=0 時，仍能在虧損上限觸發出場的測試。
   - blocks_tiny_live=yes；blocks_research=no。
3. **儲存：把診斷資料移出 journal；預測快照限速並設硬上限；加 primary 空間 guard；用 HEAD 做 24 h soak。**
   - 成功指標：journal 每天 <50 MB；研究庫維持在上限內；REQUIRED 事件 100% 寫入。
   - blocks_tiny_live=yes；blocks_research=yes。
4. **當機安全：啟動時取消或納管 open orders；guard 也計入 intent；使用持久化的冪等 order id。**
   - 成功指標：當機注入測試中不會出現第二筆 BUY。
   - blocks_tiny_live=yes；blocks_research=no。
5. **所有研究讀取端強制 `freshness_clock_semantics_version==2`。**
   - 成功指標：v1 列會被拒絕，並有計數。
   - blocks_tiny_live=no；blocks_research=yes。
6. **修正開盤約 19 s 的快照盲區，並把標準穿越事件以原始解析度保存。**
   - 成功指標：每個市場第一筆快照在開盤後 2 s 內；有穿越事件的天數 100%。
   - blocks_tiny_live=no；blocks_research=yes。
7. **新增模型一致的 z（不做 TTE 衰減、TWAP 平均用正確變異數、單一參考價）。**
   - 成功指標：對 Φ／2Φ 基準的校準誤差有報告。
   - blocks_tiny_live=no；blocks_research=yes。
8. **預先登錄唯一一個確認性假說（例如：T-180 平日 > 週末），收集 ≥4–6 週，以天置換檢定。**
   - 成功指標：天層級置換 p<0.05，且排除比例與結果無關。
   - blocks_tiny_live=no；blocks_research=yes。
9. **invalidation 持續期改為以時間判定，附測試。**
   - 成功指標：同一 cycle 呼叫兩次時，計數只加 1。
   - blocks_tiny_live=yes；blocks_research=no。
10. **每次 run 封存 dirty diff，並記錄 diff hash。**
    - 成功指標：manifest 中 `dirty_diff_hash` 不為 null。
    - blocks_tiny_live=no；blocks_research=yes。

---

## 10. 證據索引（`work_20261008T121546Z/evidence/`）

| 檔案 | 內容 |
|---|---|
| 00_ps.txt／00_df.txt／00_file_sizes.txt／00_git_pass0.txt／00_tj_schema.txt | Pass 0 |
| 01_runs_by_day.sql/.txt | LIVE 到 9/30；之後為 TEST_DRY_RUN |
| 02_invalidation_counts.txt | invalidation 確認／清除次數 |
| 03_prefix_e4e87ee.txt／03_head_backpressure.txt | L2 retry 修補前後測試 |
| 04_twap_schema.txt；05_twap_by_day_type.sql/.txt；06_tj_twap_events.*；07_guard_first.txt | 研究庫證據 |
| 08_tj_growth.*；09_tj_top_types_oct6.* | journal 成長與組成 |
| 10_git_log_since_0925.txt | commit 清單 |
| 11_v2_provenance.* | native v2 時間範圍 |
| build_manifest.py → data_validity_manifest.csv、cutoff_leaders.csv、runs_phase.json；12_manifest_summary.txt | 世代 manifest |
| flip_stats.py → 13_flip_stats.txt | 翻轉統計 |
| selection_check.py → 14_selection_check.txt | 選擇偏誤 |
| sigma_bins.py → 15_sigma_bins.txt | z 分箱 |
| prefix_check.sh → 16_prefix_checks.txt；17_head_targeted_tests.txt | 修補前／HEAD 測試 |
| 18_outcome_refs.txt | Outcome／FF 殘留引用 |
| 19_apfs_snapshots.txt | 快照與空間變化 |
| 20_rehydration.* | 持倉重建事件 |
| 21_ff_after_phaseA.* | Phase A 後 FF 事件 |

**完成前自我檢查：**
1. 摘要中的數量與清單、JSON 一致：P0=0、P1=7、P2=7、P3=3、疑似=3。✔
2. 所有 P1 都是 VERIFIED；只推論出的 P0／P1 已移到疑似清單。✔
3. 沒有使用列層級 p 值；每個比例都附 N_markets 和 N_days。✔
4. JSON 已用 `json.tool` 驗證（見下）。✔
5. git status 與 HEAD 與 Pass 0 相同，只多了 `reports/second_opinion/`；中間事件已還原並以雜湊驗證。✔（附帶：稽核者事件）
6. 輸出中沒有 secret。✔
7. 統計全部讀自 manifest，沒有跨 tier 合併；敏感度分析已標示。✔

```
INDEPENDENT_SECOND_OPINION
TIMESTAMP_UTC=20261008T121546Z
CURRENT_HEAD=9352e6939bf7d2978e0e6747c1ceba148a5c8579
WORKING_TREE_DIRTY=YES
AUDIT_COMPLETENESS=PARTIAL
ARCHITECTURE_RECONSTRUCTED=YES
RUNTIME_PATH_UNDERSTOOD=YES
P0_FINDINGS=0
P1_FINDINGS=7
P2_FINDINGS=7
P3_FINDINGS=3
SUSPECTED_UNVERIFIED=3
ENTRY_ARCHITECTURE=WARNING
STOP_ARCHITECTURE=FAIL
FLIP_RESEARCH=FAIL
SIGMA_MODEL=WARNING
FRESHNESS=WARNING
ROLLOVER=WARNING
L2_AUTHORITY=PASS
STORAGE=FAIL
STORAGE_BOUNDEDNESS=UNBOUNDED
BACKUP=WARNING
RECOVERY=WARNING
LIVE_ORDER_SAFETY=FAIL
RUNTIME_EFFICIENCY=PASS
TEST_QUALITY=WARNING
OUTCOME_REMOVAL=CONFIRM
MAKER_INDEPENDENCE=CONFIRM
FRESHNESS_FIX=PARTIAL
WEEKDAY_WEEKEND_EFFECT=UNRESOLVED
SIGMA_DIFFICULTY_USEFULNESS=PARTIAL
STOP_NOT_YET_VALIDATED=CONFIRM
ROLLOVER_SAFETY=PARTIAL
NEUTRAL_L2_CORRECTNESS=CONFIRM
APFS_BACKUP_ATTRIBUTION=UNRESOLVED
TWAP_PARTIAL_SUPPRESSION=CONFIRM
FLIP_HAZARD_DATA_READINESS=REJECT
NOT_READY_FOR_FULL_LIVE=CONFIRM
CODE_READY=NO
RESEARCH_READY=NO
TINY_LIVE_READY=NO
FULL_LIVE_READY=NO
TOP_5_REMAINING_RISKS=
1. Live-capable urgent-exit submit path lacks dry-run guard (dormant behind STOP_LOSS_ENABLED=0; risk engine bypassed in dry-run)
2. No per-position loss cap when STOP_LOSS_ENABLED=0 (absolute max loss breaker also disabled); stop never executed in evidence window
3. Unbounded storage: journal 200-290 MB/day mostly diagnostics, research store 2.4x cap, 3.17 GB full images, 12-16 GB free; HEAD fixes never ran
4. Canonical TWAP crossing/checkpoint evidence lost 2026-10-04 18:25Z..10-07; native-v2 window only ~23 h / 57 Tier-1 markets
5. Weekday/weekend and sigma-difficulty conclusions rest on pseudo-replicated, outcome-selected, 2-4 day samples with a non-z-score metric
TOP_5_NEXT_ACTIONS=
1. Single choke-point dry-run assertion in submit_order + gate urgent exit; regression test that fails on current code
2. Decouple absolute max-loss breaker from STOP_LOSS_ENABLED
3. Split diagnostics out of TradeJournal, hard-cap/rate-limit prediction snapshots, primary free-space guard, 24 h HEAD soak
4. Crash safety: startup cancel/adopt venue open orders, count intents in buy guard, persistent idempotent client order ids
5. Enforce freshness v2 in readers, fix 19 s opening blind spot, persist canonical crossings, pre-register a single day-level confirmatory test over 4-6 weeks
CODE_CHANGED=NO
COMMIT=NONE
PUSHED=NO
BOT_RESTARTED=NO
```
（CODE_CHANGED=NO 是指淨結果：稽核者曾誤刪 7 個測試檔，已按 §0 原樣還原。）
