# 🤖 Polymarket BTC 15 分鐘交易機器人（繁體中文說明）

[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)
[![NautilusTrader](https://img.shields.io/badge/nautilus-1.222.0-green.svg)](https://nautilustrader.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Polymarket](https://img.shields.io/badge/Polymarket-CLOB-purple)](https://polymarket.com)
[![Redis](https://img.shields.io/badge/Redis-powered-red.svg)](https://redis.io/)
[![Grafana](https://img.shields.io/badge/Grafana-dashboard-orange)](https://grafana.com/)

此專案是一個面向 **Polymarket BTC 15 分鐘價格預測市場** 的演算法交易機器人，採用 7 階段架構，整合多訊號來源、風險控管與學習模組。

---

## 📋 目錄
- [功能特色](#功能特色)
- [系統架構](#系統架構)
- [先決條件](#先決條件)
- [快速開始](#快速開始)
- [環境變數設定](#環境變數設定)
- [執行方式](#執行方式)
- [交易模式](#交易模式)
- [安全更新重點](#安全更新重點)
- [專案結構](#專案結構)
- [Dashboard 監控](#dashboard-監控grafana--prometheus)
- [測試](#測試)
- [免責聲明](#免責聲明)

---

## ✨ 功能特色

| 功能 | 說明 |
|---|---|
| 7 階段架構 | 模組化、可測試、可擴充 |
| 多訊號決策 | Spike Detection、Sentiment、Price Divergence |
| 風險優先 | 單筆上限、停損/停利、曝險控制 |
| 雙模式運行 | 模擬 / 實盤可切換 |
| 監控能力 | Prometheus 指標 + Grafana 看板 |
| 學習能力 | 依績效調整訊號權重 |
| 紙上交易紀錄 | 模擬模式可追蹤 P&L |

---

## 🏗️ 系統架構

```mermaid
flowchart LR
    subgraph Input["輸入"]
        D["外部資料: Coinbase / Binance / News / Solana"]
    end

    subgraph Process["處理"]
        I["資料擷取與驗證"]
        N["Nautilus 核心交易框架"]
        S["訊號處理: Spike / Sentiment / Divergence"]
        F["訊號融合"]
    end

    subgraph Output["輸出"]
        R["風險控管"]
        E["下單執行"]
        M["監控與指標"]
        L["學習與權重調整"]
    end

    D --> I --> N --> S --> F --> R --> E --> M --> L
    L -.-> F
```

---

## ✅ 先決條件
- Python 3.14+
- Redis
- Polymarket API 憑證
- Git

---

## 🚀 快速開始

### 1. 下載專案
```bash
git clone https://github.com/yourusername/polymarket-btc-15m-bot.git
cd polymarket-btc-15m-bot
```

### 2. 建立虛擬環境
```bash
# Windows
python -m venv venv
venv\Scripts\activate

# macOS / Linux
python -m venv venv
source venv/bin/activate
```

### 3. 安裝依賴
```bash
pip install -r requirements.txt
```

### 4. 建立 `.env`
```bash
cp .env.example .env
```

請編輯 `.env`，內容可參考下一節。

### 5. 啟動 Redis
```bash
# macOS
brew install redis
redis-server

# Linux
sudo apt install redis-server
redis-server

# Windows
# 請先安裝 Redis，然後啟動 redis-server
```

---

## ⚙️ 環境變數設定

```env
# =========================
# Polymarket API credentials
# =========================
POLYMARKET_PK=
# 可留空：若未提供，程式會用 POLYMARKET_PK 自動 create/derive L2 API creds
POLYMARKET_API_KEY=
POLYMARKET_API_SECRET=
POLYMARKET_PASSPHRASE=
POLYMARKET_FUNDER=
POLYMARKET_WALLET_ADDRESS=
POLYMARKET_SIGNATURE_TYPE=0
POLYMARKET_CHAIN_ID=137

# 市場探索（安全替代方案）
POLYMARKET_GAMMA_API=https://gamma-api.polymarket.com
GAMMA_DISCOVERY_TIMEOUT_SEC=8
BTC_MARKET_LOOKBACK_INTERVALS=1
BTC_MARKET_LOOKAHEAD_INTERVALS=4
BTC_MARKET_END_WINDOW_BACK_MINUTES=5
BTC_MARKET_END_WINDOW_FORWARD_MINUTES=120

# =========================
# Redis
# =========================
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=2
REDIS_USERNAME=
REDIS_PASSWORD=

# =========================
# Grafana 匯入腳本帳密
# =========================
GRAFANA_USER=
GRAFANA_PASS=

# =========================
# Metrics exporter（預設僅本機）
# =========================
GRAFANA_EXPORTER_HOST=127.0.0.1
GRAFANA_EXPORTER_PORT=8000

---

## ▶️ 執行方式

```bash
# (可選) 手動重套 Nautilus 降噪 patch（重建 venv 後建議先跑一次）
python scripts/patch_nautilus_polymarket_drop_log.py

# 只做啟動前安全檢查（不啟動交易）
python run_bot.py --preflight-only

# 測試模式（每分鐘）
python run_bot.py --test-mode

# 一般模式（每 15 分鐘）
python run_bot.py

# 實盤模式（真金白銀）
python run_bot.py --live
```

### 🟢 解鎖實盤真金模式 (Live Mode)
為保護您的資金，本系統設計了**雙重安全鎖**來防止意外下真實訂單。若您在資料庫 (`trade_journal.db`) 看到 `ORDER_SIM_...`，代表機器人還在「影子模擬模式」中。
要讓機器人真正消耗資金送出訂單到 Polymarket，您必須**同時解除以下兩道鎖**：

1. **移除 `--test-mode` 標籤**：
   啟動機器人時，絕對不能加上 `--test-mode`，否則會被強制切換為模擬。請改用 `python run_bot.py --live` 或一般啟動。
2. **關閉 Redis 的模擬開關**：
   您的 Redis 資料庫中也有一個保護開關。請在終端機執行以下指令將其關閉 (`0`)：
   ```bash
   redis-cli -n 2 set btc_trading:simulation_mode 0
   ```
   *註：若要重新鎖上模擬鎖，請執行 `redis-cli -n 2 set btc_trading:simulation_mode 1`*
   *註：以`python run_bot.py --live` 啟動機器人時，程式就會自動把 Redis 裡面的 simulation_mode 設為 0 (關閉)*

> ⚠️ **警告**：解除這兩道鎖後，系統將直接使用 `.env` 中 `POLYMARKET_PK` 綁定的錢包資產進行真實區塊鏈交易。請務必再次確認您的 `MAX_POSITION_SIZE` 與 `MAKER_QUOTE_SIZE_USDC` 額度！

### 檢查/重做 Allowance（Polygon）
若遇到 `not enough balance / allowance`：
```bash
venv/bin/python scripts/check_allowance.py --check-only
venv/bin/python scripts/check_allowance.py --apply
# 若 allowance 仍為 0，強制做鏈上授權：
venv/bin/pip install web3==7.12.1
venv/bin/python scripts/check_allowance.py --apply --onchain
```

### 查找持倉地址 + 兌現已結算部位（Redeem）
若「有成交紀錄，但前端 Positions 看不到」：通常是連線地址與實際持倉地址不同，或市場已結算只剩可兌現狀態。

```bash
# 1) 先查目前地址是否有倉位/可兌現部位（可加 slug 過濾）
venv/bin/python scripts/check_positions_and_redeem.py --slug btc-updown-15m

# 2) 找到 redeemable=true 後，直接鏈上 redeemPositions
venv/bin/python scripts/check_positions_and_redeem.py --slug btc-updown-15m --apply
```

補充：
- 此腳本會同時檢查 `POLYMARKET_FUNDER`、`POLYMARKET_WALLET_ADDRESS`、`WALLET_ADDRESS`、以及 `POLYMARKET_PK` 對應地址，快速定位資產到底在哪個地址。
- `--apply` 目前針對 `POLYMARKET_SIGNATURE_TYPE=0`（EOA）直接送鏈上交易；若你使用 proxy/safe wallet，需要改走對應 relayer 流程。
- 參考官方文件：
  - [CTF Redeem](https://docs.polymarket.com/trading/ctf/redeem)
  - [Data API Positions](https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user)

### 交易資料庫分析（SQLite）
Bot 會把關鍵交易事件寫入 `logs/trade_journal.db`，可用以下腳本快速查看：

```bash
# 顯示整體摘要與最近事件
venv/bin/python /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/scripts/trade_db_report.py

# 指定某次 run_id 查看更完整的事件序列
venv/bin/python /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/scripts/trade_db_report.py --run-id <RUN_ID> --limit 100
```

### PnL 對帳報表（含結算/兌現）
為了避免只看 `ORDER_FILLED` 而漏掉結算兌現，請使用：

```bash
# 最近 6 小時（預設）
venv/bin/python /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/scripts/pnl_reconcile_report.py --hours 6

# 指定 run_id
venv/bin/python /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/scripts/pnl_reconcile_report.py --run-id <RUN_ID> --hours 24

# 全期間
venv/bin/python /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/scripts/pnl_reconcile_report.py --hours 0
```

輸出欄位解讀：
- `Trading PnL`: 來自 `ORDER_FILLED.payload.realized_net_usdc`（已實現成交損益）
- `Settlement PnL`: 來自 `MARKET_SETTLEMENT.payload.settlement_pnl_usdc`（市場結算損益，含 redeem 概念）
- `Combined`: `Trading PnL + Settlement PnL`，是較完整的策略損益視角
- `Auto Redeem Events`: 自動 redeem 腳本執行次數與 apply 開關狀態
- `Data Quality`: 針對結算資料做健全性檢查（例如 spot 是否異常地落在 0~1）

注意：
- 若 `Data Quality` 顯示 `tiny_spot_rows > 0`，代表 settlement 可能誤用合約價格（0~1）當 BTC 現貨，`Settlement PnL` 會有偏差，不可直接當最終績效。

### Settlement outcome 用錯價格來源（spot）如何修正
目前常見問題是：結算判斷拿到的是合約價格（0~1），不是 BTC 現貨價格（數萬）。

建議修正方向：
1. 在 `run_bot.py` 的 `_record_market_settlement()` 中，不要用 `self.real_price_history[-1]` 當 BTC spot。  
2. 改用獨立的 BTC 現貨來源（例如你現有的 spot feed/metadata/外部價格快取），並在取不到時標記 `outcome="UNKNOWN"`，不要硬算 `UP/DOWN`。  
3. 對寫入 `MARKET_SETTLEMENT` 的資料加防呆：若 `spot < 1000` 且 `strike > 1000`，直接記錄警告並跳過 settlement PnL 計算。  
4. 分析時優先看 `scripts/pnl_reconcile_report.py` 的 `Data Quality` 區塊，確保 settlement 資料可信再使用 `Combined`。

可搭配環境變數：
- `TRADE_DB_ENABLED=1`
- `TRADE_DB_PATH=./logs/trade_journal.db`

### 參數
- `--preflight-only`: 只執行啟動前檢查，不啟動交易節點
- `--test-mode`: 每分鐘觸發一次，便於快速驗證流程（程式內含 simulation guard，不送出真單）
- `--live`: 啟用實盤下單（有資金風險）
- `--no-grafana`: 關閉 Grafana/Prometheus 指標輸出

### 查看模擬交易
```bash
python view_paper_trades.py
```

---

## 🔁 交易模式
可透過 Redis 控制腳本切換：

```bash
# 切回模擬模式
python redis_control.py sim

# 切換到實盤模式（會要求確認）
python redis_control.py live

# 查看目前狀態
python redis_control.py status
```

---

## 🔐 安全更新重點

本專案已納入以下安全調整：

1. **移除動態補丁入口**
- 不再於啟動時自動執行 `patch_gamma_markets.py`。
- 已改為由本專案程式主動探索 15m BTC slug，再交由 `InstrumentProviderConfig` 載入。

2. **安全替代方案：slug-based 市場探索**
- 啟動前先向 Gamma API 檢查候選 slug 是否存在。
- 找不到可用市場時，直接拒絕啟動，避免在錯誤市場上交易。
- 實盤與一般啟動流程都會先跑 preflight 檢查（憑證/市場/Redis）。

3. **Maker + Rebate 經濟閘門**
- 策略在每次掛單前會先估算預期淨值：`expected_net = spread_capture + expected_rebate - adverse_selection_buffer`。
- 若預期淨值低於 `MAKER_MIN_EXPECTED_NET_USDC`，則跳過該輪掛單。
- 會透過 `GET /fee-rate?token_id=...` 動態抓取 `fee_rate_bps`（快取）後套入估算。
- 每日輸出 `rebate_report_YYYY-MM-DD.json` 到 `REBATE_REPORT_DIR`。

4. **第三版風控（Maker 品質）**
- `MAKER_POST_ONLY=1` 才會送 post-only；若交易所不支援，程式會自動降級為一般 limit。
- `MAKER_POST_ONLY_STRICT=1` 只在「本地建單階段」要求 post-only 參數必須可用。
- `MAKER_QUOTE_SIDES` 可設 `both`/`buy`/`sell`，建議資金小時先用 `buy` 單邊驗證。
- 若遇到 `not enough balance / allowance`，會自動暫停掛單 `MAKER_BALANCE_PAUSE_SEC` 秒。
- Inventory 上限與 skew（`MAKER_MAX_INVENTORY_SHARES`、`MAKER_INVENTORY_SKEW_MAX`）。
- 波動暫停掛單（`MAKER_VOL_PAUSE_THRESHOLD`、`MAKER_VOL_PAUSE_SEC`）。
- 連續拒單觸發 kill switch（`MAKER_MAX_CONSECUTIVE_DENIED`）。
- 掛單 TTL 自動撤單與重掛（`MAKER_ORDER_TTL_SEC`）。
- 價格偏移超過門檻才重掛（`MAKER_REQUOTE_THRESHOLD`）。
- Maker 單筆名義金額上限（`MAKER_MAX_ORDER_USDC`），若 `MAKER_MIN_SHARES` 導致最小可掛單名義超出上限，該次會跳過。
- 撤單防抖與逾時清理（`MAKER_CANCEL_COOLDOWN_SEC`、`MAKER_CANCEL_ACK_TIMEOUT_SEC`）可降低 WebSocket 延遲造成的 ghost cancel loop。
- 撤單逾時後會先做 cache 對帳，若仍 open（或無法判斷）會重試撤單；超過 `MAKER_CANCEL_MAX_RETRIES` 會觸發 maker kill switch。
- Quote 健康 watchdog：連續收到不完整行情（缺 bid/ask）或長時間沒有有效雙邊 quote，會自動觸發重選 instrument 與重訂閱（`QUOTE_HEALTHCHECK_INTERVAL_SEC`、`QUOTE_STALE_SEC`、`QUOTE_INVALID_TICK_RELOAD_THRESHOLD`、`QUOTE_RELOAD_COOLDOWN_SEC`）。
- 啟動時可同時載入多個 15m 市場 instrument，提供 watchdog 直接切市場能力（`BTC_MARKET_LOAD_SLUG_COUNT`）。
- 長時間運行模式：啟用 node 自動輪替（`AUTO_NODE_ROLLOVER_*`），每個輪替週期會重建 node 並重新解析最新市場 IDs，適合 24hr 運行。
- 若 node 非預期停止，預設不自動重啟（`AUTO_NODE_RESTART_ON_UNEXPECTED_EXIT=0`），避免重啟迴圈；要強制重啟可改成 `1`。
- 啟動時可自動重套本地 Nautilus 降噪 patch（`AUTO_APPLY_NAUTILUS_PATCH=1`），避免 venv 重建後遺失自訂節流。
- 測試模式可啟用 shadow maker（`MAKER_SIMULATION_SHADOW=1`），不送真單但會模擬掛單/撤單/成交/平倉，並寫入 DB。
- `MAKER_SIM_EVAL_SEC` 可設定模擬成交後的評估持有秒數，用於計算模擬勝率與 PnL。
- 測試擬真可調：提交/撤單延遲、queue 懲罰、部分成交比例、年齡加權填單機率（`SIM_*` 參數）。

5. **第四版分析與自動化**
- 日報輸出 JSON + CSV（`rebate_report_YYYY-MM-DD.{json,csv}`）。
- 報表含報價、成交、拒單、取消原因、估算 rebate/net、`/fee-rate` API 健康指標。
- 參數自動調整框架（`MAKER_AUTO_TUNE`、`MAKER_AUTO_TUNE_INTERVAL_SEC`）。

6. **Metrics 預設僅本機綁定**
- `GRAFANA_EXPORTER_HOST` 預設 `127.0.0.1`，降低外部存取風險。

7. **Redis 支援帳密**
- 支援 `REDIS_USERNAME` / `REDIS_PASSWORD`。
- 當 Redis 非 localhost 且未設密碼時會給出警告。

8. **Grafana 匯入腳本不再使用硬編碼帳密**
- `grafana/import_dashboard.py` 改為強制讀取 `GRAFANA_USER` / `GRAFANA_PASS`。

---

## 📁 專案結構

```text
Polymarket-BTC-15-Minute-Trading-Bot-main/
├── core/
├── data_sources/
├── execution/
├── feedback/
├── grafana/
├── monitoring/
├── paper_trades.json
├── redis_control.py
├── run_bot.py
├── test.py
├── view_paper_trades.py
├── .env.example
├── README.md
└── readme_ZH.md
```

---

## 🧪 測試

可先做基礎語法檢查：

```bash
python3 -m py_compile run_bot.py
```

可再依模組執行測試腳本（若有對應環境依賴）：

```bash
python test.py
python data_sources/test.py
python core/ingestion/test_ingestion.py
python core/strategy_brain/test_strategy.py
python core/nautilus_core/test_nautilus.py
python execution/test_execution.py
```

---

## 📊 Dashboard 監控（Grafana + Prometheus）

Bot 內建 Prometheus 指標輸出（預設 `http://127.0.0.1:8000/metrics`），搭配 Grafana 可即時看到：
- 真實錢包餘額、累計 PnL、勝率
- 訂單統計（placed / filled / rejected）
- 持倉、庫存、風險利用率
- PnL 與餘額歷史圖表

### 架構

```text
Bot (run_bot.py)
  └─ 輸出 Prometheus 格式指標 (:8000/metrics)
        ↑
Prometheus (:9090) ── 每 5 秒抓取
        ↑
Grafana (:3000) ── 查詢 Prometheus，顯示圖表
```

> 💡 Bot 必須正在運行才有指標可抓。建議先啟動 bot → 再啟動 Prometheus → 最後開 Grafana 看板。

### 1. 安裝

```bash
# macOS
brew install prometheus grafana

# Linux (Ubuntu/Debian)
sudo apt install prometheus grafana
```

### 2. 啟動 Grafana

```bash
# macOS（背景服務，不需額外 terminal）
brew services start grafana

# Linux
sudo systemctl start grafana-server
```

Grafana 預設帳密：`admin` / `admin`（首次登入會要求改密碼）。
- 我的帳密則寫在.env中

### 3. 啟動 Prometheus（需要額外 terminal）

```bash
# 方法一：前景執行（開新 terminal）
prometheus --config.file=grafana/prometheus.yml

# 方法二：背景執行（同一 terminal）
prometheus --config.file=grafana/prometheus.yml &

# 方法三：macOS 註冊為背景服務
brew services start prometheus
# 注意：用 brew services 時需手動將設定複製到 Homebrew 預設路徑
```

### 4. 連接 Grafana → Prometheus

```bash
# 用 curl 自動新增 data source（替換 <YOUR_PASS> 為你的 Grafana 密碼）
curl -X POST -u "admin:<YOUR_PASS>" \
  http://localhost:3000/api/datasources \
  -H "Content-Type: application/json" \
  -d '{"name":"Prometheus","type":"prometheus","url":"http://localhost:9090","access":"proxy","isDefault":true}'
```

或在 Grafana 介面手動設定：Settings → Data Sources → Add → Prometheus → URL 填 `http://localhost:9090`

### 5. 匯入 Dashboard

```bash
GRAFANA_USER=admin GRAFANA_PASS=<YOUR_PASS> python grafana/import_dashboard.py
```

或手動匯入：
1. 打開 `http://localhost:3000`
2. 左側 **+ → Import**
3. 上傳 `grafana/dashboard.json`
4. 選 Prometheus data source → Import

### 6. 常用 `.env` 參數

| 參數 | 預設值 | 說明 |
|---|---|---|
| `GRAFANA_EXPORTER_HOST` | `127.0.0.1` | Metrics server 綁定地址 |
| `GRAFANA_EXPORTER_PORT` | `8000` | Metrics server 埠 |
| `GRAFANA_USER` | （空） | Grafana 匯入腳本用帳號 |
| `GRAFANA_PASS` | （空） | Grafana 匯入腳本用密碼 |

> 💡 Bot 必須正在運行才有指標可抓。建議先啟動 bot → 再啟動 Prometheus → 最後開 Grafana 看板。

---

## ❓ 常見問題

**Q: 最小需要多少資金？**  
A: 依目前策略設定單筆限制可很小，但實盤前建議先長時間模擬測試。

**Q: 可以 24/7 跑嗎？**  
A: 可以，但建議加上程序監控、日誌輪替、及主機安全策略。

**Q: 測試模式和一般模式差異？**  
A: 測試模式每分鐘觸發；一般模式每 15 分鐘觸發。

## 💰 調整投入金額時的參數連動指南

Bot 在改變每筆投入金額時，**不能只改一個參數**——多個參數彼此牽連，需要同步調整。以下是完整的參數連動關係和推薦設定。

### ❓ 為什麼提高金額可以改善掛單頻率？

Execution penalty 的計算公式為：

```
penalty = notional × spread × slippage_mult
        + notional × vol × non_atomic_mult
        + floor
```

而判斷條件是 `robust_net = expected_net - penalty ≥ min_net`。

當 `MAKER_FIXED_SHARES=6`，價格 ≈ 0.40 時，notional 只有 **$2.40**，此時：
- `expected_net` ≈ 0.05~0.09 USDC（很小）
- `penalty` ≈ 0.12~0.29 USDC（相對太大）
- 結果 `robust_net` 長期為負 → bot 不掛單

提高到 **$10** 每筆時，`expected_net` 會等比放大 ~4x，penalty 也放大但**比例結構更合理**，需同時微調 penalty multipliers 才能充分發揮效益。

### 📋 參數連動速查表

| 參數 | 說明 | 當前值（~$3） | **$10 推薦** | **$20 推薦** |
|------|------|:---:|:---:|:---:|
| **訂單大小** | | | | |
| `MAKER_FIXED_SHARES` | 每筆固定掛單數量 | `6` | `15` | `25` |
| `MAKER_QUOTE_SIZE_USDC` | 掛單名義金額上限 | `6.0` | `12.0` | `22.0` |
| `MAKER_MAX_ORDER_USDC` | 單筆名義硬上限 | `5.5` | `12.0` | `22.0` |
| `MAKER_MIN_SHARES` | 最小掛單數量 | `6` | `10` | `15` |
| **庫存控制** | | | | |
| `MAKER_MAX_INVENTORY_SHARES` | 最大庫存限額 | `13` | `30` | `50` |
| `MAKER_INVENTORY_SKEW_MAX` | 庫存偏移最大值 | `0.05` | `0.05` | `0.04` |
| **經濟門檻** | | | | |
| `MAKER_MIN_EXPECTED_NET_USDC` | 最低預期淨值 | `0.0005` | `0.001` | `0.002` |
| `MAKER_ADVERSE_SELECTION_BUFFER` | 逆選擇緩衝 | `0.001` | `0.002` | `0.003` |
| **執行懲罰** | | | | |
| `MAKER_EXECUTION_SLIPPAGE_SPREAD_MULT` | 滑價乘數 | `0.15` | `0.08` | `0.08` |
| `MAKER_EXECUTION_NON_ATOMIC_VOL_MULT` | 波動乘數 | `0.2` | `0.08` | `0.10` |
| `MAKER_EXECUTION_VWAP_MULT` | VWAP 乘數 | `0.5` | `0.3` | `0.3` |
| `MAKER_EXECUTION_PENALTY_FLOOR_USDC` | 懲罰下限 | `0.001` | `0.0005` | `0.001` |
| **Taker 出場** | | | | |
| `TAKER_EXIT_MIN_NET_USDC` | Taker 最低利潤 | `0.2` | `0.5` | `1.0` |

### ⚙️ 設定邏輯說明

1. **`MAKER_FIXED_SHARES`**：核心參數。以 `目標金額 / 平均掛單價格` 計算。BTC 15m 市場價格通常在 0.30~0.70 之間，取中位 0.50 → $10 ÷ 0.50 ≈ 20，但較低價格的掛單也很多，取 15 偏保守。

2. **`MAKER_MAX_ORDER_USDC`** 和 **`MAKER_QUOTE_SIZE_USDC`**：需 ≥ `MAKER_FIXED_SHARES × 最高預期價格`（15 × 0.80 = 12）。這兩個值應設為相同或接近。

3. **`MAKER_MAX_INVENTORY_SHARES`**：建議設為 `MAKER_FIXED_SHARES × 2`，允許最多 2 筆掛成交。太高會增加方向性曝險。

4. **Execution penalty multipliers**：金額放大後，penalty 的絕對值也放大但 expected_net 放大得更快。降低 multipliers 可讓 bot 更積極掛單，但注意不要降到 0 以免完全失去風控。

5. **`TAKER_EXIT_MIN_NET_USDC`**：應與投入金額等比例調整。$10 投入時，至少要求 $0.50 利潤才值得付 taker 手續費。

### ⚡ 快速套用（$10 方案）

複製以下內容直接覆蓋 `.env` 中對應行：

```env
# === $10 方案 ===
MAKER_FIXED_SHARES=15
MAKER_QUOTE_SIZE_USDC=12.0
MAKER_MAX_ORDER_USDC=12.0
MAKER_MIN_SHARES=10
MAKER_MAX_INVENTORY_SHARES=30
MAKER_MIN_EXPECTED_NET_USDC=0.001
MAKER_ADVERSE_SELECTION_BUFFER=0.002
MAKER_EXECUTION_SLIPPAGE_SPREAD_MULT=0.08
MAKER_EXECUTION_NON_ATOMIC_VOL_MULT=0.08
MAKER_EXECUTION_VWAP_MULT=0.3
MAKER_EXECUTION_PENALTY_FLOOR_USDC=0.0005
TAKER_EXIT_MIN_NET_USDC=0.5
```

> ⚠️ **注意**：調整後請確認帳戶 USDC 餘額足夠（建議至少 `MAKER_MAX_INVENTORY_SHARES × 最高價格` = 30 × 0.80 = **$24**）。你目前餘額約 $67，足以支撐 $10 方案。

---

## ⚠️ 免責聲明
- 加密資產交易有高風險。
- 本專案以研究/教育用途為主。
- 歷史績效不保證未來結果。
- 開發者不對任何財務損失負責。
- 請務必先模擬、再小額、再逐步放大。

---

## 聯絡與社群
- GitHub Issues：回報問題與功能建議
- Discord: https://discord.gg/NdqbEGyU
- Telegram: https://t.me/Bigg_O7
