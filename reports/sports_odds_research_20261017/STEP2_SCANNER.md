# S1 第 2 步：只讀的每日掃描器

**日期：** 2026-10-17
**程式：** `bot/research/sports_odds.py`（純邏輯）、`scripts/sports_odds_scan.py`（I/O）、`tests/test_sports_odds.py`（26 個測試）
**不下單。** 只讀 Polymarket Gamma／CLOB 和 The Odds API。

## 第 1 步的結果（The Odds API 免費方案）

- 每月 500 點額度。查運動清單（`/v4/sports`）不扣點；抓一個運動的勝負盤（h2h）每次扣 1 點，最多可同時要 10 家博彩公司。
- 免費方案有 Pinnacle 的即時賠率：EPL 20/20、NHL 12/14、MMA 24/72。
- 歷史賠率要付費方案，API 回 `401 HISTORICAL_UNAVAILABLE_ON_FREE_USAGE_PLAN`。所以第 4 步的驗證只能靠自己存 forward 快照。

## polymm 審查（commit `f598cf8a`，2026-08-16）

三個模組都逐行讀過，只當純文字讀，沒有執行任何程式。它們沒有網路、檔案或系統呼叫，**但最後決定不複製程式碼**：

- `vig_removal.py`：只有比例法去抽水（作者註明 Shin 方法從來沒實作過）。邏輯只有幾行，我們自己重寫了同樣的方法。
- `match_id.py` 的 `TEAM_ALIASES` 是一張不分運動的全域別名表。用 AST 把它當純資料解析後發現：**48 個名字對應到不只一隊**，以最後寫入的那筆為準；另外有 2 個重複的 key。實際的錯誤例子：
  - `rangers`：NHL 的 NY Rangers 和蘇格蘭的 Rangers 共用一個 key；
  - `miamiredhawks` 被對到 `miamihurricanes`，兩隊是不同學校；
  - `georgiastpanthers` 被對到 `georgiatechyellowjackets`，也是不同學校；
  - `t1academy` 被對到 `t1`，二軍被併進一軍，作者自己在 vitality 的註解裡說過這是 bug；
  - `barcelona`、`bayern`、`panathinaikos` 的足球隊和籃球隊共用 key。
- `team_matcher.py` 在完全比對失敗時，會改用「子字串包含」來比對（`a in b or b in a`）。另外，問題裡只要出現 `draw` 這個字，就會被當成和局市場。
- 結論：與其帶進一張有地雷的表，不如利用 Polymarket 自己的結構化欄位來比對。

## 比對方式

Polymarket 的體育 event 有 `sport` 代碼、`series_id`、`startTime`（開賽時間）和 `teams[]`（名稱、別名、主客場）。moneyline 市場有 `sportsMarketType=moneyline`，而且每個市場都帶自己的 `feeSchedule`。

一場 Polymarket 比賽只有同時符合下面三點才算比對成功：
1. 同一個聯賽（`LEAGUES` 對照表）；
2. 開賽時間差在 20 分鐘以內（UFC 放寬到 12 小時，因為 Polymarket 整張賽卡只寫一個時間）；
3. **兩隊**都能對上，而且分別對到不同隊。比對方式有兩種：名稱完全相同；或者用整個詞比對，較短名稱的每個詞都依序出現在較長的名稱裡（例如 Devils ~ New Jersey Devils），而且較短的名稱不能只由 City、United 這類通用詞組成。

候選比賽不只一場時，判為「模糊」，不比對。沒比對到的會寫進報告，之後人工補別名。別名表依聯賽分開，另有一張足球共用的表（歐冠會有各國聯賽的球隊）。

## 公允價和優勢

- 公允價：Pinnacle h2h 用比例法去抽水（足球三方、其他兩方）。抽水大於 10% 的不用。原始賠率每次都存下來，之後可以改用其他去抽水方法重算。
- taker 優勢 = 公允價 − ask − 手續費(ask)，以每股計。
- **手續費公式還沒確認：** 掃描器用 `rate × p(1−p)`，費率讀每個市場自己的 `feeSchedule`：足球、cricket 是 0.03，NHL、NBA、UFC 是 0.05。`execution/rebate_model.py` 用的是 `C × p × rate × p(1−p)`，比我們的小了 p 倍。目前用比較大、比較保守的版本，交易前要確認實際收費。
- maker 優勢 = 公允價 − 目前的最佳 bid（掛在 bid 被成交的情況，不考慮逆選擇）。

## 第一次掃描（2026-10-17 10:23Z，11 個聯賽，花 11 點）

用 `--replay` 套用補過的別名表重算後：

| 聯賽 | 開賽前的比賽 | 比對成功 | 說明 |
|---|---|---|---|
| EPL／La Liga／Bundesliga／Serie A／Ligue 1／UCL／UFC | 81 | 81（100%） | |
| MLS／NFL | 30 | 28 | 各有 1 場 Pinnacle 沒報價 |
| NHL | 47 | 12 | 33 場博彩公司還沒開盤（Polymarket 提前一週就上架） |
| NBA | 28 | 12 | 10 場同一時段博彩公司那邊沒有這場；6 場 Pinnacle 沒報價 |

- 598 個 token。|公允價 − Polymarket 中間價| 的中位數是 **0.6¢**，p90 是 1.3¢，最大 4.4¢。沒有差距大於 20¢ 的 token（大於 20¢ 通常代表配錯）。
- 扣掉手續費後最大的 taker 優勢都集中在**歐冠、離開賽 80–100 小時**、而且深度只有幾百美元的市場（例如 Roma 對 Slovan Bratislava 的和局 YES：公允 0.169，ask 0.13，可成交約 $200）。主流聯賽在開賽前一天內幾乎都是 ≤0.5¢。
- **注意：** 同一場比賽的幾個 token 互相相關（例如「和局 YES」和「Roma NO」押的是同一個方向），不能當成獨立機會相加。離開賽很遠時，Pinnacle 的限額低、價格也沒那麼準，所以這時的「公允價」本身就比較不可靠。

## 怎麼跑

```bash
.venv/bin/python scripts/sports_odds_scan.py                     # 預設 11 個聯賽，花 11 點
.venv/bin/python scripts/sports_odds_scan.py --leagues epl,ucl   # 只跑指定聯賽
.venv/bin/python scripts/sports_odds_scan.py --replay data/sports_odds/raw/2026-10-17/102317Z  # 不花點數
```

輸出寫在 `data/sports_odds/`（已 gitignore）：`raw/` 存原始回應（gzip），`scans/` 存 CSV 和報告，`scan_log.jsonl` 每跑一次記一行。剩餘點數小於等於 `--credit-reserve`（預設 50）時就停止，不再呼叫要扣點的 API。

## 下一步（第 3 步）

- 每天固定時間掃描。11 點／天 × 30 天 = 330 點，在 500 點以內。
- 同一場比賽要在不同的「距離開賽時間」各看一次，才能知道差距是只在很早的時候出現，還是會一直持續到開賽。這需要一天掃好幾次，免費方案的點數不夠，要縮減聯賽或升級方案。
