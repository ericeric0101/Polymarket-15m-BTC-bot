# PnL 資料流對帳報告（2026-10-10）

分支 `feat/effective-pnl`（以 `94f1c47` 為基底）。正式 journal 未改寫。LIVE bot（PID 91219，16:42 起）在 17:47 左右由操作員停止：journal 最後寫入 17:47–17:49，結束時的 journal retention 在 17:48 被 KeyboardInterrupt 中斷。本次工作沒有停止或重啟 bot，只讀取 `backups/` 的備份。

## 1. 資料範圍與來源

| 項目 | 值 |
|---|---|
| journal 快照 | 由 bot 自己的一致性備份 `backups/trade_journal.db`（2026-10-10 17:47 +08）以 SQLite backup API 複製，`PRAGMA quick_check = ok` |
| 成交期間 | 2026-09-08 15:53Z → 2026-10-10 09:43Z（263 筆 `ORDER_FILLED`，全部帶 slug、token、手續費欄位） |
| 計算基準時間 | `as_of = 2026-10-10 09:47:14Z`（快照最後一筆事件） |
| 官方結果 | Gamma `/events/slug/{slug}`：137 個市場全部已結算（`closed` 且 UMA `resolved`），含 `clobTokenIds` 與 `conditionId` |
| venue 現金 | Data API v2 `activity`：`TRADE` 18 頁、`REDEEM` 3 頁，均完整翻頁至錢包歷史起點（2026-02-20）；只保留 journal 交易過的市場：266 筆成交、31 筆贖回 |
| 證據快取 | 未納入版控（含鏈上交易 hash）；可用 `pnl_evidence_backfill.py fetch` 重新取得，取得時間記錄於檔內 |
| 逐市場明細 | `pnl_reconciliation_20261010T111015Z.csv`；彙總 `.json` |

篩選條件：slug 以 `btc-updown-15m-` 開頭，且 journal 有成交，或 `MARKET_SETTLEMENT.inventory_shares > 0`，共 135 個市場（2 個只有結算列、沒有可對帳的成交，不在投影中）。錢包內 2026-02 起的其他成交（非 bot journal 市場）不納入，避免外部操作混入策略績效。

## 2. 結果

`PnL = 賣出淨收入 + 贖回收入 − 買入總支出`，手續費只計一次。現金以 venue 成交為準；結果以官方為準。

| 依據（basis） | 市場數 | 有效 PnL |
|---|---:|---:|
| FILLS_FINAL（結算前已全部賣出） | 74 | +$7.98 |
| CASH_CONFIRMED（持倉已贖回，實收與持倉相符） | 30 | −$40.65 |
| OUTCOME_CONFIRMED（官方結果已知，尚未贖回） | 27 | +$3.12 |
| ESTIMATED / PENDING / OPEN | 0 | — |
| INCOMPLETE（排除） | 4 | — |
| **已確定合計** | **131** | **−$29.55** |

在可比對的 117 個市場上，journal 原始 `MARKET_CYCLE_PNL` 合計 **+$8.63**，有效 PnL 為 **−$26.71**，有 47 個市場差額 ≥ $0.05。另有 14 個已確定市場沒有任何 `MARKET_CYCLE_PNL`。

| 期間 | 市場 | journal 原始 | 有效 |
|---|---:|---:|---:|
| 2026-09 | 111 | +$10.20 | −$24.95 |
| 2026-10 | 6 | −$1.57 | −$1.76 |

待領取：22 個市場，合計 $1.45，最大一筆 $0.089，全部是賣單留下的零碎股，沒有大額未領取的獲利。

### 排除的市場（INCOMPLETE）

| 市場 | 原因 |
|---|---|
| 1788882300、1788955200、1788958800 | journal 無 BUY，venue 有 BUY（journal 開始前或外部的持倉） |
| 1789747200 | journal 無 BUY（BUY 未記錄），另有零價重複列 |

### 兩種重建來源的差異（保留不合併）

只用 journal 成交重建（加上官方結果與贖回）vs. venue 現金：合計差 **+$3.94**（129 個市場，37 個 ≥ $0.05）。

- **手續費口徑**：35 個市場、$3.47。venue 對 taker BUY 以 USDC 收手續費（`usdc_size` = 價格 × 股數 + 費用，股數不扣），bot 的費用模型以為是扣股數，所以 journal 少算了買入支出。贖回的實際股數也證實了這點（例如 1789136100 買 11 股、贖回 11 股）。
- **漏記成交**：venue 有 5 個市場的 0.97 掛單 SELL（take-profit）在 journal 沒有成交紀錄；另有 2 個 BUY 不在 journal。
- **小額差**：80 個市場、合計 $0.11，屬於手續費估算的捨入。

## 3. 健檢結論的驗證狀態

| 原健檢結論 | 狀態 | 依據 |
|---|---|---|
| D1 歷史 PnL 高估 | **已驗證，但金額修正**。原文「24 個市場、約 $15.8」只是當時的樣本；完整口徑是 117 個可比對市場、journal 比有效 PnL 高 $35.34 | 本報告 §2 |
| D2 沒有確認流程 | **部分成立**。對帳機制存在（`reconcile_redeem_cycle`、`backfill_redeem_activity`），但 `redeem_cash_usdc` 從未接通：24 筆 `REDEEM_EXECUTED` 全部沒有現金；後者只處理 bot 自己發出的贖回、最多 500 筆，且原地改寫 | 程式與資料 |
| D3 UNKNOWN 顯示成全額虧損 | **已驗證**。只發生在缺少 cycle PnL 的備援路徑，也就是 UNKNOWN 結算；修正前在隔離資料上重現 `−$7.00`，修正後顯示 `? pending` | 本報告測試 |
| D4 實收被估值蓋過 | **已驗證**，改為整個市場的現金式 PnL，不是單純「實收優先」 | 程式 |
| D5 累積 SUM 與單筆口徑不同 | **目前不成立**（938 個市場各只有一筆 cycle PnL）；已改為同一份投影的加總 | 測試 |
| D6 `exit × 全部數量` | **已驗證**，同樣只在缺 cycle PnL 的備援路徑；公式已移除 | 程式 |
| D7 一直顯示 claimable | **已驗證**，原因同 D2 | 程式 |
| 「Polymarket 結算慢導致錯誤」 | **不成立**。官方結果 89/89（現為 137/137）與 bot 方向一致；錯誤來自持倉／成交／手續費的帳 | 本報告 |
| `b0c383f` 修正幽靈庫存與重啟漏記 | **在有限樣本中未再現**。只有 6 個市場來自可確認載入修正版本的 LIVE run（3 個 `f1a134e`、3 個 `f1a134e-dirty`，後者內容無法完全確認），沒有幽靈庫存或重啟漏記；2 個市場有 ~$0.05 差額，來自手續費口徑。另外 129 個市場的 run 沒有記錄版本，**無法確認** | 本報告 JSON `post_fix_versions` |

### 本次新發現

1. **taker exit 的成交 token 記錯**（**更正 2026-10-10**：bot 記憶體裡的庫存帳本是正確的，錯的是 journal 的 `token_id` 欄位，以及 `cc1cd49` 之前的 payload instrument；啟動對帳會用這個欄位重播。已在 `fix/ledger-fee-token-guard` 修正，詳見 `project_overview.md`）：18 個市場的 SELL 成交記在對面的 token 上，但同一張單的 submit 列記的是買進的 token（例如 1790474400 的停損：submit token = 買入 token，原因 `stop_loss`）。投影改以 submit 為準。**執行期的成交處理也有同一個錯誤**，會讓 bot 帳上的庫存失真，這屬於交易行為修正，未在本次處理。
2. **零價成交**：3 筆 SELL 價格為 0。其中 2 筆是同一張單真實成交的重複列；1 筆是唯一紀錄（實際成交價 0.97，見 venue）。
3. **掛單 take-profit 成交漏記**：見 §2。
4. **venue 手續費為 USDC**：見 §2。bot 的 taker BUY 費用模型（扣股數）與 venue 不符，影響 bot 的成本、停損與 PnL 計算；屬於交易／風控行為，未在本次修改。

## 4. 程式修改與相容性

| 元件 | 修改 | 相容性 |
|---|---|---|
| `bot/settlement_evidence.py`（新） | 三種 append-only 證據：`MARKET_OUTCOME_CONFIRMED`、`VENUE_TRADE_CONFIRMED`、`REDEEM_CASH_CONFIRMED`，各有穩定的 `evidence_key` | 新事件類型；journal retention 的診斷清單不包含它們，會永久保留 |
| `monitoring/pnl_attribution.py` | 新增 `load_effective_market_pnl` / `summarize_effective_pnl`（唯讀開 journal） | 舊 `load_market_pnl_attributions` 介面不變 |
| `bot/settlement_confirmation.py`（新）+ `run_bot.py` / `settings.py` / `market_runtime.py` / `app_config.py` | 執行期 daemon worker，每 120 秒對已結束市場補抓證據，經 bot 自己的 journal writer 寫入；`SETTLEMENT_CONFIRMATION_ENABLED` 預設 1 | **不寫 `MARKET_CYCLE_PNL`、不碰 session guard、regime guard 或任何訂單狀態**；錯誤只記 log |
| `dashboard.py`、`dashboard_state.py` | 改用共用投影；持倉／結果／贖回三種狀態分開；PnL 標記依據（= ~ ? !）；累積數字標示期間與未確定數 | `TradeRecord` 只新增選用欄位；bot 行程內的 `alert_watcher`、Telegram 不受影響 |
| `scripts/live_dashboard.py`、`scripts/pnl_attribution_report.py` | 加入有效 PnL；原「交易損益」改名為「成交現金淨流（不含贖回）」 | 原欄位保留 |
| `scripts/pnl_evidence_backfill.py`（新） | fetch / report / apply / rollback | 見 §6 |
| `scripts/backfill_redeem_activity.py` | 標記 deprecated；原地改寫需加 `--legacy-in-place` | `--dry-run` 不變 |

### 仍讀原始 `MARKET_CYCLE_PNL`（bot 估值）的消費端

- **執行期風控（刻意保留）**：session PnL guard（含重啟重建 `reconstruct_session_pnl_state`）、regime guard。證據列不影響它們（有測試）。
- **其他行程內消費端**：`alert_watcher`（行程內 `TradeRecord` 的 `exit×qty` / `redeem−cost`，連續 3 次虧損會自動暫停）、Telegram 的累積 PnL。
- **研究腳本**：`hourly_attribution_report`、`realized_edge_report`、`pnl_reconcile_report`、`market_regime_report`、`stop_timing_report`、`forward_shadow_report` 等約 25 支仍讀原始估值，尚未遷移；它們的 PnL 數字應視為 bot 估值。

## 5. 風控評估（階段 3，未修改行為）

- **session guard 的口徑**：SELL 成交立即計入已實現，結算時加上估計的 payout。只用已確認 PnL 會把損失的認列延後數分鐘（官方結果）到數天（贖回），所以**不建議**。
- **估值誤差規模**：可確認修正版本的 6 個市場中，估值與有效 PnL 差 ≤ $0.06（手續費口徑）；修正前的歷史市場單筆最多差 $5.9（幽靈庫存）。
- **建議方案（需要你批准）**：對**尚未結束的 session**，當官方或 venue 證據與結算估值不同時，以市場為 key 冪等地套用一次差額；已結束的 session 不回寫。另外，taker BUY 手續費口徑與成交 token 記錯兩項，應該在交易邏輯層另案修正。

## 6. 部署、歷史回補與復原步驟（需批准，尚未執行）

### A. 部署程式（合併 `feat/effective-pnl`）

1. 停止 bot 與 dashboard。
2. 在主目錄 `git merge --no-ff feat/effective-pnl`，跑 `pytest`。
3. 重新啟動 bot（worker 隨 bot 啟動，`SETTLEMENT_CONFIRMATION_ENABLED=0` 可關閉）與 dashboard。
4. 復原：`git revert -m 1 <merge>`。證據列不影響任何執行期邏輯，留在 journal 裡也無害。

### B. 歷史回補（只寫證據列）

1. `python3 scripts/pnl_evidence_backfill.py fetch --out reports/pnl_reconciliation/evidence_cache_<UTC>.json`
2. `python3 scripts/pnl_evidence_backfill.py report --cache <cache>`：審閱逐市場清單。
3. 停止 bot，然後 `python3 scripts/pnl_evidence_backfill.py apply --cache <cache>`（dry-run，列出待寫入筆數）。
4. `python3 scripts/pnl_evidence_backfill.py apply --cache <cache> --confirm`。工具會：
   - 檢查 LIVE 鎖與 journal 寫入鎖；
   - 先備份到 `backups/pnl_evidence/` 並跑 `quick_check`；
   - 在單一交易中寫入，依 key 去重，並標記 `run_id = pnl_evidence_backfill_<UTC>`。
5. 復原：`python3 scripts/pnl_evidence_backfill.py rollback --batch-id <batch> --confirm`。只刪除該批次的證據列，執行前同樣會先備份；也可以直接還原 `backups/pnl_evidence/` 的備份。

### 已在備份副本上驗證

- 寫入 434 筆；重跑寫入 0 筆。
- 寫入後的投影與快取供給的結果逐市場相同；重複供給不重算。
- 單筆加總等於總額。
- 原始成交、結算、cycle PnL、session 狀態逐位元組不變。
- rollback 刪除 434 筆後，結果與寫入前相同；非回補批次的 rollback 會被拒絕。

## 7. 測試與限制

- 新增 `tests/test_effective_pnl.py`（25 項）；完整 suite：1,658 通過，1 個既有失敗（`test_app_config_reads_extended_env`，worktree 沒有 `.env`，在主目錄會通過）。
- 修改前的程式上，新測試因模組不存在而無法載入；D3 另以修改前的 dashboard 直接重現。

### 限制

- venue `usdc_size` 對 taker SELL 是否已扣手續費，沒有獨立證據，視為 venue 回報的現金腿。
- 成交配對規則：同 side、同 token、數量差 ≤ max(0.25, 5%)、時間差 ≤ 180 秒。
- 6 個修正後的市場樣本太小。
- 只修正 PnL 的顯示與報表，不修正 bot 帳上的庫存。
