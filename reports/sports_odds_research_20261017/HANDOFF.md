# 研究 S1：體育賠率 vs Polymarket 體育市場（交接文件）

**建立：** 2026-10-17，從 MD1 的 session 交接過來。
**資料夾：** `/Users/cheng-kaihuang/Polymarket-BTC-sports-odds`（git worktree，branch `research/sports-odds-scanner`，從 `main` 開出）。`.venv` 是指向主資料夾的 symlink。

## 目標

用準確博彩公司的賠率（去抽水後）作為公允機率，找出 Polymarket 體育市場的錯誤定價。**只在 Polymarket 交易，不在博彩公司下注。**

方法和 MD1 相同：外部準確價格 → 公允價 → 比較 Polymarket。差別只在公允價的來源。

## 已知事實（2026-10-17 調查）

### Polymarket 體育市場

- 約 1,600 個開放中的市場。常見 tag：cricket、soccer、MLB、UFC／MMA、esports（CS2、LoL）、NFL、F1。
- **費率：** taker `0.03 × p × (1−p)`（crypto 是 0.07），價平時每股約 0.75¢；maker 免費。
- **流動性：** 掛單深度中位數只有約 $50，p90 約 $5,000。冷門市場的容量很小。

### 現成 repo：`kachence/polymm`（MIT 授權）

- 作者 Kamen Dochev，錢包公開（`@b00k13`），2026 年初實際跑了幾個月，淨賺約 $5k：
  - 鎖定套利的部分：+$8,293；
  - 另一邊沒成交、被迫留下的方向性部位：−$3,184，主因是逆選擇。
- **作者的結論：** 真正的優勢在「賠率夠新、撤單夠快」。所以私有的賠率爬蟲沒有公開，只附 The Odds API 的串接。Python 版已停止維護，作者改用 Rust 重寫（未公開）。
- **值得借用：**
  - `src/core/vig_removal.py`（158 行）：兩方和三方賠率去抽水。
  - `src/core/match_id.py`（約 1,600 行）、`src/scanning/team_matcher.py`：隊名、選手名、cricket 隊名後綴的正規化與賽事比對。**這是最大的工作量所在。**
  - `analytics/`：分析任何公開錢包的損益。
- **不借用：** 下單、WS、狀態管理（我們已經有更完整的版本），以及 Supabase／Redis 的依賴。
- **安全檢查（2026-10-17，只當純文字讀，沒有執行）：** 對外連線只有 Polymarket、the-odds-api、Supabase（範例網址）、Telegram 和作者的部落格。私鑰只用來建立 CLOB client。全部約 5 萬行，只抽查了一部分；要借用的模組必須**逐行讀過**再放進來。
- **授權：** 借用時保留 MIT 授權聲明（例如放在 `third_party/polymm/LICENSE`）。
- 作者文章：
  - https://kacho.io/polymarket-arbitrage-real-numbers
  - https://kacho.io/why-my-polymarket-arbitrage-bot-lost-money

### 其他 repo（只能參考思路，都沒有授權條款，不可複製程式碼）

- `ImMike/polymarket-arbitrage`：Polymarket 對 Kalshi，監控上萬個市場的掃描架構。
- `Dylanwu2025/polymarket-sports-arbitrage`：用 The Odds API 比對體育市場的小型實驗。
- **避開**描述裡堆疊關鍵字的「polymarket trading bot」repo，常見的偷私鑰詐騙。**絕不執行外部 repo 的程式碼**，也絕不提供私鑰給它們。

### 賠率資料來源（價格來自廠商或比較網站，使用前要再到官網確認）

- **Pinnacle：** 據報導 2025-07 關閉公開 API，現在要透過第三方取得。
- **OddsPapi：** 據稱免費方案就含 Pinnacle、Betfair 交易所和歷史賠率，需要實測。
- **The Odds API：** 免費／$29／$99 每月；據稱 $99 方案才有 Pinnacle 和歷史賠率（說法有矛盾）。polymm 已經有它的串接。
- **Pinnacle 專門的轉售商：** 約 $0–229 或 €99–599 每月，有賽前歷史資料。
- **Betfair 交易所：** 開發用免費（延遲報價），即時報價一次性 £299，需要開帳戶。
- **API key 由 CK 自行註冊取得**，放在 `.env`，不寫進任何程式碼或 log。

## 計畫（還沒開始）

1. **選資料源：** CK 註冊並取得 API key。先實測它有沒有 Pinnacle 的歷史和即時賠率。
2. **只讀的每日掃描器：** 抓 Polymarket 體育市場和賠率 → 用 polymm 的模組比對 → 去抽水 → 輸出排名：差距扣掉成本後、深度、成交量。**不下單。**
3. **找長期差距：** 掃描並記錄幾天到幾週，找出哪些聯賽、市場類型長期有差距。
4. **驗證：** 用歷史資料（如果資料源有），或 forward 紀錄，比較「Polymarket 和去抽水賠率哪個比較準」。先寫事前登記，方法照 `reports/deribit_vs_polymarket_daily_20261017/prereg.md`（在 MD1 的資料夾裡）。
5. **交易方式之後再決定：** taker 或 maker 都要記住 polymm 的教訓：賠率一變就撤單，資料過期就停止（fail-closed）。

## 同時進行中、**不要碰**的研究

| 研究 | 資料夾 | Branch | 狀態 |
|---|---|---|---|
| MB1（15m 只掛買單的 maker） | `/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main` | `fix/recorder-book-buffer` | 在另一個 session 跑確認性 dry-run |
| MD1（每日 BTC 市場 maker 影子測試） | `/Users/cheng-kaihuang/Polymarket-BTC-deribit-daily` | `research/deribit-vs-polymarket-daily` | 確認性影子測試從 2026-10-17 09:53Z 開始；排程任務在 10/19 18:25 做 48 小時檢查 |
| Entry V3 | `/Users/cheng-kaihuang/Polymarket-BTC-entry-v3-forward` | `integration/entry-v3-forward-shadow` | — |

**規則：**
- 不要在其他研究的資料夾切換 branch。
- 不要停止或重啟 `maker_bid_dryrun.py`、`maker_daily_dryrun.py`、`price_recorder.py`。
- 新的程式寫入資料時用自己的 `data/` 子目錄。
