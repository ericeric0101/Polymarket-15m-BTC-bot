# Bot Principle (Current Code Baseline)

本文件以目前 repo 代碼為準（特別是 `run_bot.py`），定義 bot 的真實運作邏輯、風險邊界、觀測面與改版原則。

## 1) 系統定位
- 交易標的：Polymarket `btc-updown-15m-*` 二元市場。
- 主要模式：Maker quote 為主，必要時以 Taker exit 退場。
- 核心目標：在控制庫存與拒單風險下，持續做被動報價賺 spread/rebate，而非單純方向押注。

## 2) 啟動與市場解析
- 啟動前會執行 preflight：
  - 檢查 Polymarket 驗證資訊（PK + API creds）。
  - 透過 Gamma API 找當前/鄰近 BTC 15m slug。
  - 解析 condition/token 組合為 instrument ids。
  - Redis 可用性檢查（若有）。
- Node 建立時只載入目標 BTC 15m instruments（`load_ids`），避免全市場噪音。
- 市場選擇優先順序：
  - `CURRENT`（已開始、未過期）。
  - 否則選最近 `FUTURE`。

## 3) 交易狀態機
- `WAITING`：沒有可交易 market，主動搜尋下一個 market。
- `ACTIVE`：允許正常 maker 雙向/指定方向掛單。
- `REDUCE_ONLY`：接近收盤，強制封鎖 BUY；尾盤可選擇再封鎖新 SELL。
- `SETTLING`：市場結束，取消所有 maker 單，等待 grace period。

狀態切換依 `current_market_end_timestamp` 與 `MAKER_MIN_MINUTES_TO_CLOSE` 決定，不再只依固定 12 分鐘 reload。

## 4) Maker 報價主流程
- 由 `on_quote_tick` 驅動，每次有效 quote 進入 `_quote_maker_orders`：
  - simulation guard（test mode/模擬保護）。
  - quote refresh 節流。
  - pending-cancel 清理與重試。
  - TTL 過期單撤單。
  - inventory/volatility/balance gate。
- fair value 計算：
  - `drift` 模式：外部 BTC spot 漂移修正。
  - `digital` 模式：若可解析 strike，使用 digital option 近似機率。
  - 之後加 inventory skew。
- 報價價格：
  - `quote_bid = fair - half_spread`
  - `quote_ask = fair + half_spread`
  - 再被動化（不主動 crossing）。
- 經濟性門檻：
  - `expected_net = spread_capture + expected_rebate - adverse_selection_buffer`
  - 未達 `MAKER_MIN_EXPECTED_NET_USDC` 不掛。

## 5) 下單安全邏輯（目前已落地）
- In-flight inventory 投影：下新單前會把同側未完成/未取消單算進去，避免超量。
- Requote 安全模式：價差需重掛時，先 cancel 舊單，等 ack/reconcile，不直接覆蓋新單。
- Pending-cancel reconcile：
  - 能確定已不在 open orders 才清除本地狀態。
  - `unknown` 狀態累積過多可觸發 kill switch。
- SELL 特殊保護：
  - 使用 `cache position` + `CONDITIONAL on-chain balance` 的保守最小值作可賣數量。
  - 可賣不足時降量，低於交易所最小值則跳過。

## 6) Fee、成本與 Taker Exit
- Fee 來源優先級：
  - CLOB `/fee-rate`（有本地快取與健康指標）。
  - market type 預設值。
  - legacy bps fallback。
- live fills 會回寫觀察到的有效 fee bps 供監控。
- Taker exit 觸發條件：
  - `take_profit`：`net_if_exit >= TAKER_EXIT_MIN_NET_USDC`
  - `stop_loss`：達最短持倉且淨值低於停損門檻（可在尾盤禁用）
  - `max_hold`：持倉過久強制退出
- `TAKER_EXIT_ONLY_ON_PROFIT=1` 可限制 taker 只在盈利條件下出場（但 `stop_loss/max_hold` 邏輯仍需另外關閉或設 0）。

## 7) Reject 與異常恢復
- `POST_ONLY_NOT_SUPPORTED`：自動降級關閉 post-only。
- `not enough balance / allowance`：
  - BUY 拒單：暫停全域 quote 一段時間。
  - SELL 拒單：只封鎖該 instrument 的 SELL，保留 BUY（可回收流動性邏輯）。
- `orderbook does not exist`：啟動 watchdog reload + 暫停 quoting。
- 連續拒單超閾值觸發 maker kill switch。

## 8) Quote Watchdog 與自動復原
- 監控兩類故障：
  - quote 長時間 stale。
  - 無效 tick 連續過多。
- 觸發後動作：
  - 取消 active maker orders。
  - 重新搜尋/重訂閱 BTC instrument。
  - 復原成功則清除 invalid 計數，失敗則記錄失敗事件。

## 9) Market rollover 與自動重建
- Lifecycle thread 在 `WAITING` 時主動找下一個 market。
- 若連續多次 miss（`MARKET_WAITING_MAX_MISSES`）且推定 instruments stale：
  - 會請求 node rollover，外層 `run_integrated_bot` 重建 node 週期。
- 另有定時 auto node rollover（可關）作保險機制。

## 10) 觀測與資料落盤
- 交易日誌 DB：`logs/trade_journal.db`
  - `strategy_runs`
  - `strategy_events`
  - `order_events`
- 主要分析腳本：
  - `scripts/trade_db_report.py`：基本 run 與 event 檢視。
  - `scripts/edge_attribution_report.py`：fill/reject/cancel、expected_net、taker 使用率。
  - `scripts/check_allowance.py`：USDC/CONDITIONAL allowance 與餘額檢查/更新。
  - `scripts/check_positions_and_redeem.py`：倉位與 redeem 檢查。
- rebate 報表：`execution/rebate_reporter.py` 每日輸出 csv/json。

## 11) 模組分層（實際使用 vs 研究/備援）
- 實際主路徑（正在用）：
  - `run_bot.py`
  - `execution/fee_rate_client.py`
  - `execution/rebate_model.py`
  - `execution/rebate_reporter.py`
  - `execution/parameter_tuner.py`
  - `execution/risk_engine.py`
  - `monitoring/trade_journal_db.py`
  - `monitoring/grafana_exporter.py`
  - `monitoring/performance_tracker.py`
  - `core/strategy_brain/signal_processors/*`
  - `core/strategy_brain/fusion_engine/signal_fusion.py`
- 研究/舊版路徑（非當前實盤主流程）：
  - `execution/polymarket_client.py`
  - `execution/nautilus_polymarket_integration.py`
  - `data_sources/*`
  - `core/ingestion/*`、`core/nautilus_core/*`（偏框架層，非目前主控）

## 12) 已知設計限制（必須認知）
- expected_net 仍屬 quote-time proxy，不等於 realized PnL。
- live inventory 成本帳僅在本策略內維護，若外部手動交易/跨程序交易，可能失真。
- 尾盤流動性與訂單狀態延遲噪音大，應以保守參數減少最後 30~60 秒操作。
- `both_buy` / `both` 等模式切換會改變資金占用型態，需配合 allowance 與 on-chain 可賣量管理。

## 13) 改版準則
- 先保命再提 edge：
  - 不允許新增邏輯破壞 inventory cap、pending-cancel 保守流程、simulation guard。
- 先可測再上線：
  - 新規則需至少新增 `order_events`/`strategy_events` 記錄點。
- 先減 reject 再談收益：
  - `not enough balance/allowance` 與 cancel 狀態不一致，優先級高於任何 alpha 調參。
- 先穩定成交品質再擴策略：
  - 優先優化 fill rate、reject rate、taker_exit 比例，再做更激進報價。
