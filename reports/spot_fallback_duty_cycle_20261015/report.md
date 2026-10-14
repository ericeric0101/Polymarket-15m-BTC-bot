# Live 參考價格切換到 Binance 備援的頻率與影響 — 2026-10-15

資料來源：主 checkout 的 `logs/trade_journal.db`，以唯讀方式開啟，範圍 2026-09-07 到 10-12。這不是 forward process 正在寫入的資料庫。沒有修改程式或設定。

## 結論

**到 10-12 為止，Binance 備援沒有影響任何實際的進場、選方向或出場。** 但價差從約 2 bps 擴大到約 10 bps，萬一備援剛好發生在關鍵時刻，影響會比以前大。

## 各決策路徑

| 路徑 | 樣本 | 使用非 TWAP 價格 | 實際影響 |
|---|---:|---:|---|
| 送單前進場決策（`ENTRY_DECISION_PRE_SUBMIT`） | 38,611 | **0** | 無；備援時一律記為 `ORDER_SKIP_TWAP_REFERENCE_DEGRADED`（250 筆），已經 fail closed |
| 方向決策（`SIDE_DECISION`） | 38,108 | 54（0.14%）：LIVE 28、DRY 26 | 54 筆全部 `active_side = NONE`；都發生在開盤後約 2 分鐘、進場時間窗之前，只持續約 10 秒 |
| 進場確認觀測（每隔一段時間記錄，可當作時間佔比的近似） | 50,190 | **0** | — |
| 出場決策（`EXIT_POLICY_DECISION`，LIVE） | 2,259 | 落在備援警告後 30 秒內的有 12 筆 | 12 筆全部是 `hold_to_redeem_enabled`，沒有任何出場動作 |

備援事件數只是下限，因為警告每 30 秒才記一次：`TWAP_REFERENCE_DEGRADED` 390 次（LIVE 312），`POLYMARKET_TWAP_SILENT_STALL` 204 次，分布在 24–25 天。

出場事件沒有記錄價格來源欄位，所以只能用時間鄰近來推斷，無法直接證明。

## 範例：價差可能讓方向反轉

`btc-updown-15m-1791654300`（10-10 17:47Z）：

- TWAP 83,044.21，strike 83,058.73，TWAP 低於 strike，偏 DOWN。
- 切換到 Binance 後 spot = 83,132，比 strike 高約 +8.8 bps，composite score 由負轉正（+0.127）。
- 當時還沒鎖定方向，所以沒有造成實際影響。

## Binance − TWAP 價差（`SIDE_DECISION` 中參考價為 TWAP 的列，含 TWAP 本身的落後）

| 期間 | 每日中位數（bps） |
|---|---:|
| 09-27～10-07 | 1.6–3.5 |
| 10-08 | 6.2 |
| 10-09～10-12 | 9.2–11.3 |

## 對第 3 步的意涵

- 進場路徑已經 fail closed，不需要再修。
- 方向決策：建議在備援期間直接不更新方向（fail closed），改動很小。
- 出場和停損：到目前為止沒有實際觸發。若要修，因為價差會漂移，必須用滾動估計扣除，而且在 Oct 9 之後，價差約 10 bps 已超過停損門檻（±8 bps）。
- 出場事件應補記錄參考價格來源，以後才能直接量測。

## 重現

```sh
.venv/bin/python reports/spot_fallback_duty_cycle_20261015/analyze.py
```
