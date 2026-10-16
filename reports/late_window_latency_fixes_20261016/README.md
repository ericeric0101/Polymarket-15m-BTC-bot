# P0：尾段策略的延遲修復（2026-10-16）

**分支：** `fix/late-window-latency`，從 DRY-RUN 實際跑的 runtime `4b1375d` 切出。

**目的：** TWAP-z 尾段策略在每個市場的 T−55 到 T−5 秒之間決策。POST-24H 報告（`reports/post24h_entry_v3_analysis_20261016/system_stability.md`）列出的三個 WARN，在這個時間尺度上都可能直接決定能不能成交。

**沒有改動：** 進出場規則、門檻、prereg、`.env`、E5B 凍結檔，以及受保護的決策模組（`run_bot.py`、`order_submission.py` 等）。

## 變更

| # | 問題（POST-24H 實測） | 修改 | 預期效果 |
|---|---|---|---|
| 1 | 決策路徑上的 fee lookup 有 232 次超過 1 秒，最慢約 4.6 秒。原因是快取過期，或新 token 第一次查詢時，會同步呼叫 CLOB `/fee-rate`，而且每次都重建 HTTPS 連線 | `bot/pricing_runtime.py`：快取新鮮就直接回傳；過期但未超過 4 個間隔就先回傳舊值，背景只發一個 refresh；完全沒看過的 token，會加入已在進行的預抓，沒有預抓才同步查詢。`bot/market_runtime.py`：訂閱市場時（目前這對加上預熱的下一對），同時預抓 fee | 正常情況下，決策路徑不再等待網路 |
| 2 | 每 3 小時 rollover，event loop 卡 11–15 秒，時間點和市場邊界無關 | `bot/launcher.py`：排定的 rollover 只能在市場開盤 +30 秒到收盤前 120 秒之間開始，否則延後到下一個允許時點。持倉保護的延後邏輯照舊 | strike 擷取和最後 60 秒結算窗口，永遠由執行中的 node 覆蓋。最多延後約 2.5 分鐘 |
| 3 | RTDS TWAP WS 一天斷線 27 次，呼叫端無法得知 Chainlink 目前是否可用 | `bot/price_streams.py` 新增 `chainlink_feed_health()`；`bot/spot_pricer.py` 新增 `_chainlink_feed_health()` 和連線旗標。以本機接收時間判斷 spot／TWAP 是否超過 3 秒 | **只提供旗標，還沒接到任何進場規則。** P1 執行器會在旗標為 stale 時 fail-closed |
| 4 | 研究用的 BTC 報酬緩衝是 `deque(maxlen=2400)`，成交量大時涵蓋不到 60 秒；另外，跨執行緒迭代 deque 可能拋例外 | 新增 `bot/research_return_history.py`：保留最近 90 秒、加鎖、上限 50,000 點，並記錄超過上限而丟棄的筆數 | btc_ret_60s 不再因成交量缺值。只影響研究遙測 |

## 測試

- 新增 22 個測試：`tests/test_fee_rate_off_decision_path.py`、`tests/test_chainlink_feed_health.py`、`tests/test_research_return_history.py`，以及 `tests/test_scheduled_rollover_interval.py` 的新增案例。
- 全套測試：修改前 2086 passed / 1 failed，修改後 2108 passed / 1 failed。
- 唯一的失敗是 `test_s7_to_s11_production_modules_untouched_by_e5b`，在基線 `4b1375d` 就已經失敗，與本次修改無關。

## 需要注意

- **policy fingerprint：** fingerprint 包含 git revision，所以用這個分支跑，會自動得到新的 fingerprint，不會和 `4b1375d` 的資料混在一起。`PLUMBING_REVISION` 沒有升版；如果要把修改 4 視為量測變更，需要你決定是否升到 `dryrun_plumbing_v2`。
- **非排定的 rollover：** 由報價 watchdog 觸發的恢復性 rollover 不受修改 2 限制，因為那是故障恢復。
- **fee 的過期上限：** fee rate 在市場期間幾乎不變。過期值最多沿用 4 個間隔，預設 300 秒 × 4 = 20 分鐘，超過後會回到同步查詢。
