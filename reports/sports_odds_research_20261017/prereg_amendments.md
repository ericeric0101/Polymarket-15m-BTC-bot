# S1 v1 事前登記修改紀錄

依 `prereg.md` 第 8 節記錄：收集層面的修正、延長規則的決定、規則錯誤的另行登記。每筆都要寫時間（UTC）、內容、影響範圍。

## A1 — 2026-10-17 11:35Z：每日健康檢查通知（只影響收集監控）

- 新增 `com.ck.s1-sports-odds.health`：每天 09:00（台北）執行 `sports_odds_health.py --notify`，有問題就跳出 macOS 通知。
- 健康檢查只計算 T0 之後的快照，避免 T0 前的開發掃描造成誤報。
- 偵測的問題：基準快照少於預期的 90%、6 小時以上沒有任何收盤快照、剩餘點數 < 3,000、log 有錯誤行、足球或 UFC 比對率 < 80%。
- **不影響**第 3 節規則、第 4 節檢驗，也不讀取收盤價或結算值。

## A2 — 2026-10-17 13:45Z：新增研究 S2 的成交紀錄收集（跟 S1 無關）

- 新增 `com.ck.s1-sports-odds.trades`：每 2 小時（台北時間偶數點 :30）執行 `scripts/sports_trades_recorder.py`，記錄 S1 各聯賽 moneyline 市場在 Polymarket 的公開成交紀錄。
- 只寫入 `data/sports_trades/`，**不讀** `data/sports_odds/`。不影響 S1 的規則、檢驗、盲測。
- `sports_odds_cron.sh` 只新增 `trades` 模式，原有模式不變。
