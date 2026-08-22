# Polymarket BTC 15 分鐘交易 Bot

這是一個針對 Polymarket BTC 15 分鐘 Up/Down 市場、以 maker 為優先的實驗性交易 bot。本 repository 可以送出真實訂單；dry run 的輸出僅是研究證據，不能保證成交或獲利。

[`project_overview.md`](../project_overview.md) 是目前實作、已知技術債、證據與已核准變更順序的唯一權威。本 README 是精簡的操作入口；不可因它而將尚未完成的 Phase D 變更視為已部署。

英文版：[English README](../README.md)。

## 目前 live 合約

每個 15 分鐘市場，bot 只做一個方向決策：`UP`、`DOWN` 或 `NONE`；不是兩個獨立 bot 同時報兩個 outcome。

1. **市場與 strike 安全性。** Gamma 用來確認市場身分與設定；與前端相容的 Polymarket `crypto-price` 請求（包含市場設定的 60 秒 TWAP 參數）提供唯一的 Price To Beat。若無法驗證該開盤值，新的 BUY 會 fail closed。
2. **共用 fair 與方向。** `ForecastState` 是唯一的 live fair/sigma policy。`SignalEngine` 使用同一狀態、order book、trend 與 strike distance 產生帶正負號的 score：正值代表 UP、負值代表 DOWN。
3. **進場閘門。** 新鮮市場資料、時間窗、方向信心、外部衝突檢查、倉位上限與共同的 `robust_net` economics gate 都必須通過。
4. **每市場只進場一次。** 一筆成功的 maker BUY 會消耗該市場的進場額度。部分成交是這張單的正常結果；bot 不會在同一市場 reload 或補單。
5. **出場與結算。** `HOLD_TO_REDEEM` 讓一般符合條件的盈利庫存持有至結算。若啟用且符合條件，static tail-protect TP 會以 `0.97` 掛出被動 GTC SELL。確認的 invalidation 可以接管為 recovery/urgent-exit ladder；它與一般 TP 是不同路徑。結算、redeem 與 PnL 事件都會寫入本機 journal。

0.97 TP 不需要新鮮的 TWAP。TWAP stale 會阻止新 BUY，並在設定要求時阻止需要 TWAP 確認的 recovery exit；它本身不會取消已存在的 static TP。

## 策略狀態

- Phase A、B、C 與 D.3（canonical strike provenance）已完成。
- D.4 目前只部署**觀測資料**：fill 會記錄 10/30 秒 markout、spot continuation、BBO/depth、volatility、time-left 及 UTC weekday/weekend 特徵。live economics 仍使用保守的 168 小時 execution-cost calibration；必須有至少 30 個獨立、current-version maker-BUY 市場，且完成必要的 out-of-sample review，才可選擇 12–48 小時候選窗口。
- D.5 的設定／程式／文件 ownership 收尾尚未開始。versioned profile 目前有 218 個 assignment；不可因此把它視為 218 個日常 operator knobs，也不可只根據名稱刪除 key。

## 安裝

直接使用 repository 的 virtual environment：

```bash
git clone https://github.com/ericeric0101/Polymarket-15m-BTC-bot.git
cd Polymarket-15m-BTC-bot
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp config/operator.env.example .env
```

在 live 使用前，請於 `.env` 填入 wallet/CLOB/RPC credentials。絕不可 commit。

設定優先順序：

1. `config/profiles/btc15_twap_v3.env`：可版控、非機密的進階預設。
2. 本機 `.env`：credentials、host settings，以及 `config/operator.env.example` 所列的 supported operator overlay。
3. Shell/CI 環境變數：最高優先權。

operator 範例目前列出 55 個 supported deployment keys。最終 reader inventory 與剩餘 profile-only/legacy setting 的移除屬於 D.5 工作；不要因此把 218-key profile 複製到 `.env`。

不印出 value 地驗證本機設定：

```bash
./.venv/bin/python scripts/inspect_env_contract.py --env .env --strict
```

若有舊的完整 `.env`，先 preview migration，再決定是否套用：

```bash
./.venv/bin/python scripts/migrate_env_to_profile.py --env .env --profile btc15_twap_v3
./.venv/bin/python scripts/migrate_env_to_profile.py --env .env --profile btc15_twap_v3 --apply
```

## 執行

在新部署或設定變更前先執行 preflight：

```bash
./.venv/bin/python run_bot.py --preflight-only
```

沒有 `--live` 時預設為 dry run。它執行 live 的決策與本機 order lifecycle，但絕不送出 wallet order：

```bash
./.venv/bin/python run_bot.py
```

Live mode 必須明確指定指令，並在互動提示輸入 `yes`：

```bash
./.venv/bin/python run_bot.py --live
```

常用變體：

```bash
./.venv/bin/python run_bot.py --live --terminal-dashboard
./.venv/bin/python run_bot.py --test-mode
```

`--test-mode` 用於加速測試，不是 production strategy setting。同一 wallet、同一 host 絕不可啟動第二個 live launcher。

## 操作與證據

```bash
# 僅檢查 collateral/allowance。
./.venv/bin/python scripts/check_allowance.py --check-only

# 檢查已結算部位；--apply 會送出鏈上交易。
./.venv/bin/python scripts/check_positions_and_redeem.py
./.venv/bin/python scripts/check_positions_and_redeem.py --apply

# 終端 journal dashboard。
DASHBOARD_THEME=light ./.venv/bin/python dashboard.py

# 以目前 gate 回放歷史訊號。
./.venv/bin/python scripts/replay_journal_signals.py --hours 168

# D.4 markout/regime 證據；不改變 live policy。
./.venv/bin/python scripts/market_regime_report.py --db logs/trade_journal.db --min-samples 30

# Regression suite。
./.venv/bin/python -m pytest -q
```

`logs/trade_journal.db` 是策略／訂單／fill／結算的唯一本機紀錄。只有真實 maker-BUY fill 才能計入 D.4 execution-cost selection；dry-run shadow fill 是有用診斷，但不能取代 live-fill evidence。

Telegram controller 為選用功能，且需要 `TELEGRAM_BOT_TOKEN` 與 `TELEGRAM_OWNER_CHAT_ID`。通知傳送是 asynchronous 且 serialized，因此 Telegram outage 不會阻塞交易迴圈。conditional-token balance query 在上游 API 失敗後也會依 token back off；bot 會使用安全的 inventory fallback，而不會虛構餘額。

## Repository 地圖

```text
run_bot.py                    live/dry-run CLI entry point 與 strategy host
bot/                          lifecycle、pricing、signals、quoting、exits、recovery
execution/                    maker economics 與 Polymarket integration
monitoring/trade_journal_db.py SQLite journal/report access
config/profiles/              versioned non-secret strategy profile
config/operator.env.example   supported local operator overlay
scripts/                      preflight、replay、regime report、allowance、redeem
docs/readme_ZH.md             本繁體中文 README 翻譯
```

若其他文件與 `project_overview.md` 衝突，以 overview 為準。

## 驗證與風險

任何有意的策略變更，都要先跑 `project_overview.md` 定義的 phase-specific evidence，至少再執行：

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/python scripts/inspect_env_contract.py --env .env --strict
git diff --check
```

二元合約可能損失全部進場成本。場館可用性、訂單狀態、settlement reference、費用與流動性都可能改變。live 運行時必須監控，移動資金前必須獨立驗證 wallet/chain activity。本程式不是投資建議。
