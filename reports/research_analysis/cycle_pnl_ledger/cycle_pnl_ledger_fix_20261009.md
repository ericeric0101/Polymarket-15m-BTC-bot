# MARKET_CYCLE_PNL 帳本會計缺陷：根因與修正（2026-10-09）

## 背景

2026-10-09 稽核以成交 + Polymarket 官方結算重建 LIVE PnL（2026-09-10..09-30，125 筆部位）得到 **−$9.28**，
但 `logs/trade_journal.db` 中 `MARKET_CYCLE_PNL` 合計為 **+$15.37**。結算標籤不是原因（108 個有交易的市場 0 筆翻轉），
差異來自帳本會計。本報告找出兩個獨立根因並修正。機器人全程未啟動，交易門檻未變更。

## 兩個範例市場：日誌 vs 重建

| 市場 | 實際成交 | 官方結算 | 日誌 fill_realized | 日誌 settlement | 日誌 combined | 重建 PnL | 修正後重播 |
|---|---|---|---|---|---|---|---|
| btc-updown-15m-1789745400 | BUY 5.839507 DOWN @0.81（手續費 0.0647 股）→ SELL 5.774801 @0.97 | DOWN | +0.9240 | **+5.8395** | **+6.7635** | +0.94 | +0.9240（settlement 0） |
| btc-updown-15m-1790568000 | BUY 10 DOWN @0.66，之後重啟，未賣出 | UP | 0.00 | **0.00** | **0.00** | −6.60 | **−6.60** |

「修正後重播」以實際程式碼（`FillLedgerMixin`、`PricingRuntimeMixin`、`StrategyRecoveryMixin`、`StrategyLifecycleMixin`）
重播相同的成交與餘額序列。在修正前版本（`a9b3f70`）上同一重播得到 +6.7635 與 0.00，與日誌逐位相符，證明重現的就是線上缺陷。

1789745400 的 +0.924 與重建 +0.94 之差 0.016：重建腳本把 0.0647 股的「手續費股」視為留倉並以 $1 結算，
而機器人帳本把淨股數以 0.81 計成本（手續費股成本未計入 avg_entry）。真實現金流為 5.6016 − 4.7300 = **+0.8716**。
見下方「後續事項」。

## 根因 1：全部賣出後，落後的鏈上餘額被當成「幽靈庫存」以零成本補回（症狀：殘留庫存重複計算）

時間軸（1789745400）：

1. 15:41:08.106 SELL 5.774801 成交，帳本 qty → 0，實現 +0.924。
2. `FillLedgerMixin._update_live_inventory_cost_from_fill` 在 qty 歸零時 **刪除** `recent_sell_fill_ts_by_inst[inst]`。
3. 15:41:10.792（2.7 秒後）`_get_effective_sellable_qty` 讀到尚未同步的條件代幣餘額 5.839507。
   原本有 8 秒「賣出後餘額同步寬限」與強制刷新邏輯，但兩者都以 `recent_sell_fill_ts_by_inst` 為依據——已被刪除，所以全部跳過。
4. 進入 `_reconcile_ghost_inventory`：把 5.839507 股以 `avg_entry_price=0` 寫回帳本（`GHOST_INVENTORY_RECONCILED`）。
5. 結算時 `compute_settlement_summary`：redeem 5.8395 × $1 − 成本 0 = **+5.8395** 幻影利潤，加上已實現 +0.924 → +6.7635。

影響範圍：全日誌 13 筆 `GHOST_INVENTORY_RECONCILED` 中，**7 筆發生在 SELL 成交後 1.4–3.0 秒**，全部屬於此缺陷：

| 市場 | 結算時幽靈股數 | 幽靈成本 | 計入的 settlement PnL |
|---|---|---|---|
| 1789052400 | 5.5 | 4.40 | +1.10 |
| 1789054200 | 5.625 | 5.0625 | +0.5625 |
| 1789141500 | 5.5 | 4.95 | +0.55 |
| 1789387200 | 5.634147 | 0 | +5.634147 |
| 1789745400 | 5.839507 | 0 | +5.839507 |
| 1789753500 | 5.569621 | 0 | +5.569621 |
| 1790253000 | 5.5 | 0 | +5.5 |
| **合計** | | | **+24.76** |

（這 7 筆的結算方向剛好都與幽靈側一致；若方向相反，則會以 −成本 計為幻影虧損。）其餘 6 筆 ghost 事件前無 SELL，
屬於重啟後的真實庫存補回。

**修正**（`bot/fill_ledger.py`）：全部出場時不再刪除 `recent_sell_fill_ts_by_inst`。寬限期內回傳帳本數量；
寬限期後若餘額仍高於帳本，會先 `force_refresh` 再判斷，只有鏈上確實仍持有（賣單鏈上回滾）才補回。
`recent_sell_fill_ts_by_inst` 只被這段 sellable-qty 邏輯使用，且每個市場週期會重設，不影響其他決策。

## 根因 2：市場中途重啟時啟動回補從未生效（症狀：結算時庫存遺失）

時間軸（1790568000）：

1. 04:03:11 run `2e32a818` BUY 10 DOWN @0.66（$6.60）。
2. 04:07:18 重啟為 run `8ceadbc8`。
3. `on_start` → `_rehydrate_inventory_state_on_startup()` 只以 Nautilus `cache.positions_open()` 判斷持倉；
   此時框架快取沒有任何部位 → 回傳 0 → 什麼都沒有回補。**整個日誌中 `STARTUP_INVENTORY_REHYDRATED` 出現 0 次**，代表此路徑從未在線上生效過。
4. 帳本為空，之後沒有賣出嘗試，ghost 補回路徑也未觸發。
5. 04:15 結算走「無庫存」分支 → `MARKET_CYCLE_PNL` = 0.00；真實為 0 − 10 × 0.66 = **−6.60**。

**修正**（`bot/recovery.py`）：快取為 0 時，改以錢包條件代幣餘額（`force_refresh=True`）作為持倉數量權威；
成本仍由既有的 `ORDER_FILLED` 重播（`_rebuild_inventory_state_from_db`）恢復（本例 avg_entry 0.66）。
保護措施：僅在可實際下單的 LIVE 模式（`real_order_submission_allowed`）啟用，dry-run 絕不接收錢包真實持倉；
低於 1 股的手續費殘渣不回補（與 ghost 補回同一門檻）；`STARTUP_INVENTORY_REHYDRATED` 新增 `qty_source` 欄位。
回補後既有的 `_startup_rehydrated_inventory_force_sell_only` 行為不變。

## 回歸測試

`tests/test_cycle_pnl_ledger_regressions.py`（5 項，使用真實 mixin，I/O 在記憶體）：

| 測試 | 修正前 `a9b3f70` | 修正後 |
|---|---|---|
| `test_full_sell_exit_then_lagging_balance_is_not_settled_as_free_inventory`（1789745400 重播） | **FAIL**（sellable 5.7227，幽靈補回） | PASS（combined +0.924） |
| `test_mid_market_restart_rehydrates_held_inventory_from_onchain_balance`（1790568000 重播） | **FAIL**（無回補） | PASS（combined −6.60） |
| `test_balance_still_above_ledger_after_grace_is_force_refreshed_before_ghost_recovery` | PASS（防護） | PASS |
| `test_dry_run_restart_never_adopts_wallet_balance` | PASS（防護） | PASS |
| `test_restart_ignores_sub_share_fee_dust` | PASS（防護） | PASS |

修正前驗證方式：`git archive a9b3f70 | tar -x -C /tmp/prefix_a9b3f70`，複製測試檔後執行（未在 repo 內 checkout/stash）。
完整測試套件：**1279 passed**。

## 對 −9.28 vs +15.37 差距的解釋程度

日誌 − 重建 = 24.65。根因 1 的 7 筆幻影結算合計 +24.76，量級上幾乎等於整個差距；根因 2 的 1790568000 方向相反（日誌少記 6.60）。
兩者相加無法逐筆精確對上 24.65，表示仍有其他較小的逐市場差異（例如手續費股處理、重建腳本以毛股數計留倉、真實 ghost 補回的成本）
互相抵銷。**本報告未做 125 筆逐市場對帳**，建議以修正後的語意重跑一次逐市場比對。

## 後續事項（未在本次修改）

- 手續費以股數收取時（taker BUY），`InventoryLedger.update_from_fill` 的 avg_entry 只以淨股數 × 成交價計成本，
  手續費股的成本（本例 0.0647 × 0.81 = $0.052）不會進入已實現 PnL。修改會影響停損/停利使用的 avg_entry，屬交易行為變更，故未動。
- `compute_settlement_summary` 的 `inventory_cost` 加總所有 `live_inventory_cost` 項目，但 redeem 只計可辨識方向的股數，兩者口徑不一致。
- 重建腳本 `live_trades()` 以毛 BUY 股數計留倉，會把手續費股當成可結算股數（本例 +0.065）。
- 已寫入的歷史 `MARKET_CYCLE_PNL` 列未改寫；研究分析應以官方結算重建值為準。
