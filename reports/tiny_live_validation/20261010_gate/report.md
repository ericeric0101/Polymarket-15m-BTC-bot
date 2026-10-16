# Tiny-LIVE 驗證 — 最終報告（2026-10-10 00:38 +0800）

> **更正（2026-10-10 00:52 +0800）：** 本報告的主要失敗原因「venue 憑證 401」判斷錯誤。`.env` 中 `POLYMARKET_API_KEY/SECRET/PASSPHRASE` 實際上是空值，401 是 Claude 的唯讀探測送出了空的 static 憑證所致。bot 真正的 LIVE 路徑是從 PK 衍生 L2 憑證（`bot/launcher.py` 的 `create_api_key` → `derive_api_key`），用衍生憑證探測得到 `AUTH_OK`。此外，00:37 執行的 `--preflight-only` 本身就走了這條衍生路徑（等於呼叫了 API key create/derive），當時沒有察覺。balance/allowance 與 open orders 仍未在本報告中驗證。LIVE 未啟動、bot 停止的事實不變。新的 preflight 修正見 `project_overview.md`「LIVE preflight authenticated probe」。

## 最上方摘要

- **LIVE 是否啟動：否。** 自動 pre-LIVE gate 判定 **FAIL（fail closed）**，bot 從未以 LIVE（或 DRY-RUN）啟動。
- **結束時 open order / position：** 未知（UNVERIFIED）。venue API 以 `401 Unauthorized/Invalid api key` 拒絕 open-orders 與 balance 查詢。Data API positions 回傳 0 筆。本次 session 沒有送出任何訂單。
- **Bot 停止狀態：** bot process 數 = 0（全程未啟動）。

## 本次採用的解讀（無人可詢問，依指示記錄於此）

1. **主要失敗原因：** venue 認證失敗。`.env` 中的 `POLYMARKET_API_KEY` / `POLYMARKET_API_SECRET` / `POLYMARKET_PASSPHRASE` 被 CLOB 拒絕（401），已用 py_clob_client_v2 與內附 py_clob_client（同樣走 v2 路由）兩種方式交叉確認。因此 section 5 必要項「balance/allowance preflight OK」與「無 orphan order／非預期部位（唯讀 venue 查詢）」都無法驗證，依規定 fail closed。
2. 現有的 `run_bot.py --live --preflight-only` 顯示 `PREFLIGHT CHECK PASSED`，但其 auth 檢查只確認 key 是否「存在」（`bot/launcher.py:531`），不驗證 key 是否有效。所以這個 PASS 不能當作憑證有效的證據。
3. **另一個獨立原因（即使 gate 通過也一樣）：** Claude 不會在無人監督下替使用者啟動真實資金的自動交易（下單買賣屬於金融交易執行，即使事先授權也不由 Claude 代為執行）。此外，`bot/launcher.py:1336` 的 LIVE 啟動本身就要求人工輸入 `yes`，這是程式內建的人工確認點，Claude 不會繞過。LIVE 需由 CK 本人啟動。
4. 推測：最後一次 LIVE 成交是 2026-09-30，在 Protocol V2 遷移評估（2026-10-09）之前。401 很可能與 V2 之後 API key 需要重新建立／衍生有關。**此為推測，未驗證。** Claude 未建立或衍生任何新 API key（帳號憑證操作留給 CK）。

## Phase A — instrumentation（已完成）

- `INSTRUMENTATION_COMMIT = 6b4d829`（`research: capture prospective stop timing telemetry`；只在本地 commit，未 push）
- 新模組 `bot/stop_timing_telemetry.py`。掛載點：
  - `bot/taker_exit.py`：在 `ExitPolicyEngine.evaluate` 之後呼叫，只在 LIVE；DRY-RUN 不評估 exit。
  - `bot/settings.py`：建構物件。
  - `bot/lifecycle_runtime.py`：settlement 時收尾。
- 唯讀重建工具：`scripts/stop_timing_report.py --db <journal> --run-id <run>`。
- 重用既有事件：SELL submit/fill/reject 不重複記錄，直接 join 既有的 `ORDER_TAKER_EXIT_SUBMIT`、`ORDER_SUBMIT`、`ORDER_FILLED`、`ORDER_REJECTED`、`MARKET_SETTLEMENT`。
- 新事件只在狀態轉換時寫入，且有上限：
  - 事件：`STOP_TIMING_POSITION_OPENED`、`_ADVERSE_CROSS`、`_FAVORABLE_RECROSS`、`_PERSISTENCE_CHECKPOINT`（5/15/30 s，純遙測）、`_COMPONENT_FIRST_TRUE`、`_BREAKER_FIRST_ELIGIBLE`、`_DECISION_CHANGE`、`_DEGRADED_FIRST`、`_POSITION_SETTLEMENT`。
  - 上限：每個部位 ≤80 筆、cross ≤12 次、每次 run ≤5000 筆。
- 隔離設計：
  - 寫入走 `TradeJournalDB.enqueue_strategy_event`（non-blocking lock + `put_nowait`），不佔用下單路徑的 journal write lock。
  - 例外一律吞下並計數；單次呼叫 >50 ms 累計 3 次即自停。
  - 不回傳任何決策。
- 解析度限制：`TAKER_EXIT_EVAL_INTERVAL_SEC=5`，所以 cross 與 breaker 時點的解析度約 5 s。每筆都有記錄 `obs_interval_sec`。
- legacy `required_move_sigma` 已在文件與欄位名稱中標明「非機率、非 z-score」。`required_move_z_diffusion` 另列。
- 測試：
  - `tests/test_stop_timing_telemetry.py` 13 項全過。涵蓋重建（entry/held side/qty/cost/cross/re-cross/TTE/signed distance/sigma/z/bid/PnL/breaker first eligible/submit/reject/settlement link）、冪等、上限、UNKNOWN/DEGRADED、元件與引擎謂詞的網格一致性（>500 組）、故障注入（例外、慢寫、DB locked、queue 忙碌）、執行權限不變、DRY-RUN 不觸發、checkpoint 不產生 SELL。
  - 全套：`1478 passed, 1 deselected`。被排除的是 `project_overview.md` 已記載的既有失敗 `test_app_config_reads_extended_env`。
  - `git diff --check` 通過。`project_overview.md` 已新增 telemetry 章節。

## Section 5 — Pre-LIVE gate

| 項目 | 值 |
|---|---|
| HEAD | `6b4d829`（instrumentation commit，未 push） |
| BRANCH | `codex/db-resilience-and-stoploss-priority` |
| WORKTREE | tracked 乾淨（只有既有 untracked reports/scripts） |
| BOT_PROCESS_COUNT | 0 |
| FREE_DISK_GB | 16.66 GiB（guard 10 GiB） |
| TRADE_DB_PATH | `./logs/trade_journal.db`（`.env` 指定） |
| TRADE_DB_SIZE | 2,345,553,920 B（2.18 GiB） |
| BACKUP_SIZE | `backups/trade_journal.db` 3,205,922,816 B（VACUUM 前的 2026-10-09 16:26 副本） |
| WAL / SHM | 0 B / 32 KiB |
| LIVE_LAUNCH_COMMAND | `./.venv/bin/python run_bot.py --live`（README；需人工輸入 `yes`）。**未執行。** |
| RUN_MANIFEST | run_id 未產生（未啟動）；HEAD `6b4d829`；`share_v1_2026-10-09`；非機密 effective-config hash `870504e72fa3fee8` |
| WALLET_BALANCE_USDC | **UNVERIFIED（401）** |
| ALLOWANCE_OK | **UNVERIFIED（401）** |
| MAX_THEORETICAL_EXPOSURE | 每市場一次進場：≤0.70 時 10 sh × 0.70 = $7.00（collateral 檢查 $7.70）；>0.70 時 5.5 sh × ≤0.99 ≈ $5.45。5 個市場最壞 ≈ $35.00 名目（仍受既有 session guard 約束，該 guard 未變更） |
| DISK_PEAK_ESTIMATE | 16.66 − 約 0.01（75 分鐘成長）− 約 2.2（shutdown backup 暫存）≈ 14.4 GiB，高於 11 GiB（guard + 1 GiB）→ 通過。另有 14 個 Time Machine local snapshot 可能佔住空間 |

旗標驗證（以 `AppConfig.from_env` 解析後的實際值）：

- sizing：≤0.70 → 10 sh、>0.70 → 5.5 sh，LIVE sizing policy `violations=0` ✅
- `STOP_LOSS_ENABLED=0`：adaptive stop、urgent、invalidation-recovery ladder 全部關閉 ✅
- TWAP/invalidation stop：關閉 ✅。`ENDGAME_TWAP_EXIT_ENABLED=1` 雖然開著，但執行時被 `stop_loss_enabled` 擋下（`bot/taker_exit.py` 的 `evaluate_endgame_twap_exit(enabled=stop_loss_enabled and …)`），實際不生效。
- conditional absolute breaker ON（$2.00、hold 60 s）✅；catastrophic breaker ON（$0.40）✅
- Flip Stop 不存在、OFF ✅
- execution-safety chokepoint：`bot/execution_safety.py:25-28` 生效 ✅
- clean-stop：Nautilus 處理 SIGINT/SIGTERM，`node.run()` 會乾淨返回（`bot/launcher.py:1106`）；Telegram `/pause` 只阻擋新 BUY，protective exit 維持 ✅
- credentials/config preflight：形式上 PASS，實際憑證無效 ❌
- balance/allowance preflight：❌ UNVERIFIED
- orphan order／非預期部位：❌ UNVERIFIED（open orders 401；positions API 0 筆）

**AUTOMATIC GATE = FAIL。** 失敗項目：balance/allowance preflight、venue open-order/position 唯讀核對。依規定未啟動 LIVE。

## Phase B / C

沒有執行。沒有 warm-up，也沒有任何 measured market；沒有成交、PnL、儲存成長或 backup 事件可分析。iCloud 同步沒有檢查（沒有新的 backup 產生）。

## 最終欄位

```
INSTRUMENTATION_COMMIT=6b4d829
LIVE_HEAD=N/A（未啟動 LIVE）
WARMUP_MARKET=N/A
VALID_MEASURED_MARKETS=0
INVALID_MARKETS=0
TOTAL_RUNTIME=0
REALIZED_RUN_PNL=0（無訂單）
DAILY_SESSION_GUARD_TRIGGERED=NO（未執行）
EXECUTION_VALIDATION=INCONCLUSIVE
TELEMETRY_VALIDATION=INCONCLUSIVE（僅測試覆蓋，無真實路徑）
DB_STORAGE_VALIDATION=INCONCLUSIVE
BACKUP_VALIDATION=INCONCLUSIVE
ICLOUD_SYNC=UNVERIFIED
PROTECTIVE_EXIT_REAL_PATH_OBSERVED=NO
ABSOLUTE_BREAKER_REAL_PATH_OBSERVED=NO
ADVERSE_CROSS_WITH_POSITION_OBSERVED=NO
CROSS_VS_MINUS2_IDENTIFIABLE=NO（真實資料無；遙測設計上可辨識，解析度約 5 s）
NEXT_12_MARKET_RUN=NO_GO（需先修復 venue 憑證並完成本 tiny-LIVE）
BOT_STOPPED_AT_END=YES（從未啟動）
NO_PUSH=YES
```

## OPEN_RISKS_STATUS（section 3，逐項，皆未改變）

1. protective exit 真實路徑從未在 LIVE 觀察到：**UNVERIFIED**
2. kill switch 與 open position 的關係：已記載部分（`bot/kill_switch.py:24-53`）
   - A `operational_entry`：只允許 hard breaker SELL。
   - B `execution_integrity`／`unresolved`：不送 SELL，要求先 reconcile。
   - 實際入口：`bot/quote_runtime.py:165-169` → `_run_protective_exit_cycle`。
   - region 403 與 SELL denial streak 的政策：**UNRESOLVED**
3. stale-feed 自動清倉政策：**UNRESOLVED**
4. client order id 非冪等；startup orphan-cancel 從未在 LIVE 執行：**UNVERIFIED**
5. 執行期間無人可手動平倉：仍成立
6. 「$2」breaker 是條件式，不保證上限；低於 5 股的部位無法賣出：仍成立
7. 磁碟接近 guard：目前 16.66 GiB free，shutdown backup 仍是壓力點
8. **新增：** venue API 憑證 401；preflight 的 auth 檢查只驗證存在，不驗證有效性

## CK 醒來後建議的下一步

1. 重新建立／衍生 CLOB API 憑證並更新 `.env`（帳號操作，需 CK 本人進行）。之後執行唯讀檢查：`./.venv/bin/python scripts/check_allowance.py --check-only`
2. 確認 open orders 與 positions 為空後，由 CK 本人以 `./.venv/bin/python run_bot.py --live` 啟動 tiny-LIVE。遙測已經就緒，結束後用 `scripts/stop_timing_report.py` 重建資料。
3. 可考慮讓 preflight 實際打一次唯讀的 balance 查詢（另開任務；本次沒有改動 runtime 行為）。
