# BOT 運行規格

## 核心定位

- 策略類型：`7-phase` 市場生命週期 bot
- 交易風格：`只做 UP`、`maker-first`、`risk-first`
- 主要目標：用低於 fair value 的價格買入 `UP`，用高於成本的價格賣出 `UP` 庫存；只有在信心足夠高時才持有到結算

## Bot 交易什麼

- 只交易當前 BTC 15 分鐘市場的 `UP` outcome
- `MAKER_QUOTE_SIDES=both` 的意思是：
  - 掛 `UP` 的 `BUY`
  - 對已有 `UP` 庫存掛 `SELL`
- 不直接交易 `DOWN`
- `DOWN` 壓力只會間接反映在：
  - fair price 惡化
  - directional edge gate
  - regime guard
  - cooldown / reduce-only 行為

## 市場生命週期

1. `WAITING`
   - 沒有有效的當前市場
   - 搜尋下一個 BTC 15m 市場
2. `ACTIVE`
   - 正常 maker 報價
   - 可以在 `UP` 上掛 `BUY` 與 `SELL`
3. `REDUCE_ONLY`
   - 接近市場結束
   - 不再開新的 `BUY` 風險
   - 優先減倉
4. `SETTLING`
   - 市場已結束
   - 取消 maker 掛單
   - 計算結算 PnL
   - 等 grace period 後再搜尋下一個市場

## 進場規則

- 預設動作是對 `UP` 掛 `BUY`
- 在以下任一情況下，bot 會拒絕進場：
  - 預期淨收益太小
  - directional edge gate 不達標
  - post-fill buy cooldown 生效中
  - momentum filter 判定市場下跌太快
  - regime guard 生效中
  - reduce-only 模式生效中
  - 預估成交後庫存會超過上限

## 出場規則

- 第一優先：用 maker `SELL` 出掉 `UP` 庫存
- 若利潤已經足夠，允許用 taker exit 快速落袋
- taker exit 也可用於受控 stop-loss / max-hold / near-close 處理
- 在以下情況下，bot 可能暫停賣出：
  - hold-to-settlement gate 判定值得持有到結算
  - high-cost exit cooldown 生效中
  - 賣價低於保護成本門檻

## 持有到結算

- 只有在以下條件都成立時才允許持有：
  - 庫存夠小
  - 平均成本夠高，值得走 redeem 路徑
  - 剩餘時間足夠
- 持有到結算不是預設行為
- 它是針對小倉位、高信心 `UP` 庫存的例外覆寫

## 冷卻規則

- Post-fill buy cooldown：
  - `BUY` 成交後，暫停新的 `BUY` 掛單一段時間
- Consecutive loss cooldown：
  - 連續實現虧損後，暫停全部報價
- Regime guard cooldown：
  - 連續多個市場表現很差時，進入保守或暫停模式
- High-cost fill cooldown：
  - 昂貴的 `BUY` 成交後，避免在成本以下積極出場
- Taker reject cooldown：
  - taker exit 被拒後，延後下一次 taker exit 嘗試

## 風控核心

- `UP` 庫存上限
- 下單前 projected inventory guard
- 賣出前 conditional token balance guard
- cancel/requote 節流
- quote health watchdog
- orderbook 缺失暫停
- balance / allowance 暫停
- reduce-only tail guard
- rollover 後自動重置 per-market 狀態

## Fair price 模型

- 建議模式：`digital`
- 輸入包含：
  - 外部 BTC 現貨價格
  - 解析出的 strike
  - 距離結算剩餘時間
  - 短週期估算 sigma
- 若 strike 暫時不可得，bot 會先 fallback，直到 opening strike 被鎖定

## 建議運行姿勢

- 每次改參數後，先用 `test_mode` / dry run 跑
- 保持較小的 inventory cap
- 保持 directional edge gate 開啟
- 把 hold-to-settlement 的 inventory cap 維持很小
- 在 live 穩定前，先不要啟用 auto-redeem

## 當前預設值的意圖

- 少交易，但只挑更乾淨的 `UP` 機會
- 避免在弱 `UP` 上不停攤平
- 在已經贏到足夠 edge 時，允許快速 taker 落袋
- hold-to-settlement 是例外，不是主要出場方式
