# Pre-LIVE 遙測／研究語意修正 — 報告（2026-10-10）

- 起點 HEAD `e921152`，終點 HEAD `7a59755`（分支 `codex/db-resilience-and-stoploss-priority`），6 個本地 commit，未 push。
- 過程中 bot 一直停止（process 數 0），沒有啟動 LIVE 或 DRY-RUN，沒有下單或撤單，`.env` 沒動。
- 本目錄內容都未 commit：`readiness.json`、`overhead_storage_benchmark.{py,json}`、`dry_run_replay/`（舊 schema 回放）。

## UNRESOLVED（使用者 CK 的決定，皆不阻擋 LIVE）

1. **研究用報價新鮮度門檻**：held-side 與進場 baseline 都用 `QUOTE_MAX_DELIVERY_DELAY_SEC`（2 s），也就是 snapshot 既有的市場新鮮度；保護門檻 `QUOTE_STALE_SEC`（30 s）不變。如果偏好較寬的門檻，可以離線放寬，原始年齡都有記錄。
2. **BOOK_EMPTY 在 episode 裡算 deterioration**：理由是持有 token 沒有 bid，無法出場。這是預先登錄的定義，請確認。
3. **Chainlink RTDS raw 更新頻率**：這次不准跑 DRY-RUN，所以沒用真實資料量測（UNVERIFIED）。年齡每列都有記錄，第一個 LIVE 市場即可量到。
4. 先前已列的未決事項不變：kill-switch 對持倉的語意、stale-feed 清倉政策。

## S0 基線（VERIFIED）

- HEAD `e921152`，tracked tree 乾淨，process 0，free 約 15 GiB，full suite 1545 passed。
- 前一輪工程項目都在 HEAD：
  - weekend 開關：`d379f56`，測試隔離 `d50db8c`
  - taker-exit instrument_id 修正：`cc1cd49`
  - PnL 重複計算修正：`b0c383f`，另有 `a8ff2bc` 釘住 TP-exit 形狀
  - PROTECTIVE_EVAL_GAP：`233fd3e`

## S1 稽核發現

- **Binance 來源（VERIFIED）**：snapshot 的 `btc_spot` 只來自 Binance aggTrade（`spot_pricer` → `_prediction_btc_research_history`）。
- **Chainlink 來源（VERIFIED）**：raw 流已被 runtime 讀取，欄位為 `_polymarket_chainlink_price`、`_ts`（本地接收）與 `_observation_ts`。新鮮度規則已存在：本地接收年齡 < `_RAW_SPOT_FRESHNESS_SEC`（10 s）。snapshot 原本沒有寫入 Chainlink spot。
- **空 bid 側（VERIFIED）**：adapter 以 `bid=0.001, size=0` 的新鮮報價送出（`adapter_overrides.py`，`drop_quotes_missing_side=False`）。所以 BOOK_EMPTY 可以用既有的 `latest_quote_depth_by_inst` 判斷，不用改報價路徑。
- **既有 per-side 新鮮度（VERIFIED）**：snapshot 早就有 `market_quote_up_fresh` / `down_fresh`。legacy `market_quote_fresh` 要求兩側都新鮮。
- **STOP_TIMING 缺口**：原本已有進場 bid/ask、出場深度、Binance spot。缺 Chainlink spot、Chainlink cross、held-side bid 年齡與狀態、`exit_l2_fresh`、進場 baseline 的有效性判斷，以及進場時的 Binance baseline。
- **PROTECTIVE_EVAL_GAP 缺口**：缺 `cause_class`、`held_side`、`cooldown_active`、`gap_end`。

## S2/S3 修正

| 項目 | 內容 | 證據 |
|---|---|---|
| A1 Binance 相對移動 | `binance_adverse_move_bps`，以第一筆 BUY fill 時的 Binance spot 為基準 | `bot/stop_timing_telemetry.py:217`、`research/early_warning_timeline.py:251` |
| A2 Chainlink spot vs strike | `chainlink_cross_state`，平手結算 UP；有 `ADVERSE_AT_ENTRY` 標記；stale 一律記 UNKNOWN | 同上；`spot_cross` |
| A 不產生 Binance strike cross | 任何地方都沒有 Binance 對 strike 的 cross 欄位；basis 只做描述統計 | `test_tie_settles_up_..._binance_is_not_a_strike_cross` |
| B held-side 新鮮度 | FRESH_BID / STALE / BOOK_EMPTY / UNKNOWN，只看該 token 自己的報價；持有側取自 `live_inventory_cost` | `bot/prediction_research_snapshot.py:57,69,571` |
| 進場 baseline | 第一筆 fill；stale、修補過的盤口、空 bid 都會讓 `entry_executable_bid=null` | `bot/stop_timing_telemetry.py:112`、`bot/fill_ledger.py:197` |
| TOKEN_DD | 進場可成交 bid − 目前 held-side 可成交 bid（Decimal）；狀態為 VALUE / UNKNOWN / EXIT_UNAVAILABLE | `_early_warning_snapshot` |
| 出場深度 | 既有欄位加上 `exit_l2_fresh`；欄位名稱對照見 `project_overview.md` | `stop_timing_telemetry.py:410` |
| C 回撤／回復／第二段 | 離線工具 `research/early_warning_timeline.py`：PRIMARY 與 HYSTERESIS（X−2 ticks，持續 ≥5 s）、flap_count、缺口 censored | `:112`、測試 23 個 |
| PROTECTIVE_EVAL_GAP | 新增 cause class 與研究欄位；結束時由 watchdog 發出 `_END`，exit path 不做 I/O | `bot/protective_exit.py:67,273` |
| schema | 每列寫 `snapshot_schema_version=2`，manifest 的 `schema_versions` 也有 | `bot/research/provenance.py:29` |

## S4 驗證

- **測試**：full suite 1618 passed（新增 73 個），`git diff --check` OK。
- **執行路徑不變（VERIFIED）**：
  - LIVE harness 在三種情況下下單完全相同：遙測不存在、遙測拋例外、遙測正常記錄。
  - 新 input 本身爆炸（型別錯、None），下單也相同。
  - 交易程式碼沒有任何地方讀取新 key（已 grep 確認）。
- **DRY-RUN 不會宣稱拿不到的欄位**：
  - snapshot 寫 `held_side=null`，沒有任何 held-side 欄位。
  - DRY-RUN 從不評估 STOP_TIMING。
- **舊 schema 回放（VERIFIED）**：用 `run_1791602735_398a83d0` 唯讀回放啟動市場的 shadow fill。0.15 那段回撤在 4.68 s 回復，和 DRY-RUN 報告一致；Chainlink 正確顯示 UNKNOWN（v1 沒有記錄）。
- **耗時**：每次 capture 約 120 µs，新增部分如下。

  | 情況 | p50 | p99 |
  |---|---|---|
  | 空手 | +4.3 µs | +8.9 µs |
  | 持倉 | +12.7 µs | +15.3 µs |
  | STOP_TIMING observe（整體，非新增） | 43 µs | 54 µs |

  一秒迴圈內沒有新增 I/O、鎖或事件流。
- **儲存**：

  | 情況 | 每列新增 | 佔 4.7 KB 平均列 | 每小時新增 |
  |---|---|---|---|
  | 空手 | +309 B | +6.6% | 約 +0.75 MiB |
  | 持倉 | +497 B | +10.5% | 約 +1.2 MiB |

  每小時數字以約 2,540 列／小時計算。研究 DB 目前約 175 MiB，上限 500 MB；1+4 一輪約多 1.5 MiB，影響可忽略。為了壓縮，已移除 `held_instrument_id`。

## 給 LIVE 的資訊

- **要使用的 HEAD**：`7a597551bd596e9e734cdcd8ef14a6b75d91e8c9`。
- **manifest 應記錄**：`run_id`、`git_commit`（等於上面的 HEAD）、`git_dirty_tracked=false`、`schema_versions.snapshot_schema_version=2`、`config_hash`。
- **第一個 LIVE 市場要確認**（屬驗證，不是阻擋條件）：
  - STOP_TIMING_POSITION_OPENED 有 `entry_executable_bid` / `entry_bid_state` / `chainlink_*` / `held_side_bid_state`。
  - snapshot 有 `held_side`。
  - 量出 Chainlink 年齡的分布。
- **磁碟**：目前 free 14 GiB；研究 guard 是 10 GiB，低於時改為每 15 s 取樣。

## Coverage ledger

| 項目 | 狀態 |
|---|---|
| S0 baseline + 前一輪工程項目 | DONE |
| S1 snapshot writer / STOP_TIMING 稽核 | DONE |
| A1 Binance 相對移動 | DONE |
| A2 Chainlink spot cross（平手、ADVERSE_AT_ENTRY、stale） | DONE |
| A Chainlink spot 寫入 snapshot | DONE |
| B held-side / per-side 新鮮度（含 BOOK_EMPTY） | DONE |
| 進場 baseline（第一筆 fill、同時鐘、UNKNOWN 規則） | DONE |
| 出場深度欄位驗證 | DONE |
| schema 相容性 + version + manifest | DONE |
| 遙測安全（例外、None、慢、執行完全相同） | DONE |
| 耗時 p50/p99 | DONE |
| 儲存估計 | DONE |
| C 離線回撤／回復／第二段（PRIMARY + HYSTERESIS + 缺口） | DONE |
| canonical timeline + context | DONE |
| PROTECTIVE_EVAL_GAP 可觀測性 | DONE |
| tests A–H | DONE |
| docs（`project_overview.md`） | DONE |
| Chainlink 真實更新頻率 | UNRESOLVED（需要 LIVE 資料） |
| 新鮮度門檻選擇、BOOK_EMPTY 是否算 deterioration | UNRESOLVED（待 CK 確認，不阻擋） |
