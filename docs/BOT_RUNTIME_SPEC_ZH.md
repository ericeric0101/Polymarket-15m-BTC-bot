# BTC 15 分鐘 Runtime 規格

本文件是目前實際執行路徑的規格。早期 V1 或初期 V2 設計文件僅供歷史參考，不能當作操作手冊。

## 範圍

- 標的：Polymarket `btc-updown-15m-*` 二元市場。
- 方向：策略可選 `UP`、`DOWN` 或 `NONE`；`NONE` 時不建立新倉位。
- 模式：maker-first 進場、受限制的 recovery exit，及可選擇的 hold-to-redeem。
- 定價：優先使用 Chainlink 60 秒 TWAP；僅依設定的 degraded-feed 規則使用新鮮外部現貨備援。

## 市場生命週期

`WAITING -> ACTIVE -> REDUCE_ONLY -> SETTLING -> WAITING`。`REDUCE_ONLY` 不可建立新風險；`SETTLING` 取消 maker 單、記錄結果並尋找下一個市場。

## 固定五層進場鏈

1. **硬安全：** 報價與市場資料新鮮、TWAP/reference spot 可用、外部價格與 book 不衝突、生命週期允許。
2. **方向：** 唯一的 `UP` / `DOWN` / `NONE` 判定。每市場第一筆使用 `max(FIRST_ENTRY_SCORE_MIN, ENTRY_SCORE_MIN)`；後續買入使用 `ENTRY_SCORE_MIN`。
3. **模型一致性：** strike/spot 合理、校準後 fair probability、與高價單的風報比限制。
4. **經濟性：** 唯一的 `robust_net = expected value - empirical execution cost - fees`。負 fair-edge 分層僅供 shadow 研究，不能藉例外規則直接進 live。
5. **執行：** 被動掛單、TTL/requote、庫存上限。一般目標 10 shares；價格超過高價門檻時為 5 shares；每市場預估總曝險不得超過 10 shares。

每個被拒絕候選都會寫入單一 final reason，以便 replay 區分安全、方向、模型、經濟性或執行層阻擋。

## 出場與贖回

- `HOLD_TO_REDEEM=1` 時，符合條件的贏家倉位可持有至結算後贖回。
- recovery exit 受 TWAP confirmation、最短持有時間及剩餘時間限制。
- 一般 maker SELL 只出售已確認庫存；重啟恢復的庫存，在完成對帳前強制 sell-only。
- 自動 redeem 與下單獨立，仍需 Polygon allowance 與 gas 正常。

## 啟動模式

- `./.venv/bin/python run_bot.py`：dry run，執行 live 同等決策與模擬訂單生命週期，但不送出錢包交易。
- `./.venv/bin/python run_bot.py --live`：確認後送出真實訂單。
- `./.venv/bin/python run_bot.py --preflight-only`：只檢查啟動條件。

設定優先順序請見 [configuration.md](configuration.md)，完整繁中操作請見 [readme_ZH.md](readme_ZH.md)，策略規則請見 [STRATEGY_RULES.md](STRATEGY_RULES.md)。
