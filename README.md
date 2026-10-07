# Polymarket BTC 15 分鐘交易 Bot

這是一個以 maker 為優先、用於 Polymarket BTC 15 分鐘 Up/Down 市場的實驗性交易 bot。此 repository 可以送出真實訂單；dry-run 輸出僅供研究，不能保證成交或獲利。

[`project_overview.md`](project_overview.md) 是目前實作、已知技術債、研究證據與核准變更順序的唯一權威；本 README 是精簡操作入口。README 不應被用來推論尚未完成的 Phase D 已經部署。

## 目前 live 行為

每個 15 分鐘市場只做一個方向決策：`UP`、`DOWN` 或 `NONE`，不是兩個獨立 bot 同時交易兩個 outcome。

1. **市場與 strike 安全性：** Gamma 確認市場身分與設定；與前端相容的 Polymarket `crypto-price` 請求（包含市場設定的 60 秒 TWAP 參數）提供唯一的 Price To Beat。若無法驗證開盤值，新的 BUY 會 fail closed。
2. **共用 fair 與方向：** `ForecastState` 是唯一的 live fair／sigma policy。`SignalEngine` 使用同一狀態、order book、trend 與 strike distance 產生帶正負號的 score：正值代表 UP，負值代表 DOWN。
3. **進場閘門：** 平日的新鮮市場資料、時間窗、方向信心、外部衝突檢查、倉位限制、深度、餘額與風控條件都必須通過。一般 maker BUY 仍受報價預期淨利與費用條件限制；經驗 markout／`robust_net` execution penalty 只作觀測，不再 veto maker BUY。Outcome fast-follow 使用獨立的 forecast、taker fee 與 adverse-markout economics 檢查。
4. **交易時段：** 台北時間週一至週五全天允許新的 live BUY；週六、週日只觀察，不開新倉。週末仍收集 shadow 資料；SELL、止損、緊急出場與既有持倉管理保持運作。
5. **每市場只進場一次：** 成功的 maker BUY 會消耗該市場進場額度。部分成交是同一張單的正常結果；bot 不會在同一市場 reload 或補單。
6. **出場與結算：** `HOLD_TO_REDEEM` 讓一般符合條件的盈利庫存持有至結算。若啟用且符合條件，static tail-protect TP 會以 `0.97` 掛出被動 GTC SELL。確認的 invalidation 可接管 recovery／urgent-exit ladder；這與一般 TP 是不同路徑。結算、redeem 與 PnL 事件會寫入本機 journal。

`0.97` TP 不要求新鮮 TWAP。TWAP stale 會阻止新的 BUY，並在設定要求時阻止需 TWAP 確認的 recovery exit；TWAP stale 本身不會取消已存在的 static TP。

## 研究與策略狀態

- Phase A、B、C 與 D.3（canonical strike provenance）已完成。
- D.4 已部署觀測資料，包括 fill 後 10／30 秒 markout、spot continuation、BBO／depth、波動、time-left 與 UTC weekday/weekend 特徵。168 小時 markout 校準仍可供分析，但 execution penalty 不再是一般 maker BUY 的 live veto；12–48 小時候選窗口的研究選擇仍待足夠樣本與樣本外審查。
- D.5 的設定、程式與文件 ownership 收尾尚未開始。版本化 profile 目前約有 218 個 assignments；不可把它視為 218 個日常操作旋鈕，也不可只依名稱刪除設定。
- 前瞻 early-entry shadow 已實作，但尚待 bot 實際收集資料。`scripts/forward_shadow_report.py` 可輸出候選、配對、BBO、MFE／MAE、退出政策與恢復分析；樣本不足時不得據此變更 live policy。
- T+60／120／180 秒、0／2／5 bps 的六組 trend-entry shadow 也已實作；它與前瞻退出政策實驗是不同資料集，兩者都只觀察、不控制訂單。

## 安裝

直接使用 repository 的虛擬環境：

```bash
git clone https://github.com/ericeric0101/Polymarket-15m-BTC-bot.git
cd Polymarket-15m-BTC-bot
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp config/operator.env.example .env
```

Live 使用前，請在 `.env` 填入 wallet／CLOB／RPC 憑證；絕不可 commit。

設定優先順序：

1. `config/profiles/btc15_twap_v3.env`：可版控、非機密的進階預設。
2. 本機 `.env`：憑證、主機設定，以及 `config/operator.env.example` 列出的操作員設定。
3. Shell／CI 環境變數：最高優先權。

操作員範例目前列出 55 個支援的部署設定。最終 reader inventory 與剩餘 profile-only／legacy 設定的清理屬於 D.5；不要因此把 218-key profile 複製到 `.env`。

不輸出設定值地驗證本機設定：

```bash
./.venv/bin/python scripts/inspect_env_contract.py --env .env --strict
```

若有舊版完整 `.env`，先預覽遷移結果再決定是否套用：

```bash
./.venv/bin/python scripts/migrate_env_to_profile.py --env .env --profile btc15_twap_v3
./.venv/bin/python scripts/migrate_env_to_profile.py --env .env --profile btc15_twap_v3 --apply
```

## 執行

新部署或變更設定前先執行 preflight：

```bash
./.venv/bin/python run_bot.py --preflight-only
```

未指定 `--live` 時預設為 dry run。它會執行 live 決策與本機 order lifecycle，但不會送出 wallet 訂單：

```bash
./.venv/bin/python run_bot.py
```

Live mode 必須明確指定指令，並在互動提示輸入 `yes`：

```bash
./.venv/bin/python run_bot.py --live
```

其他常用方式：

```bash
./.venv/bin/python run_bot.py --live --terminal-dashboard
./.venv/bin/python run_bot.py --test-mode
```

`--test-mode` 用於加速測試，不是 production strategy setting。同一個 wallet、同一台主機絕不可啟動第二個 live launcher。

## 操作與研究證據

```bash
# 僅檢查 collateral／allowance。
./.venv/bin/python scripts/check_allowance.py --check-only

# 檢查已結算部位；--apply 會送出鏈上交易。
./.venv/bin/python scripts/check_positions_and_redeem.py
./.venv/bin/python scripts/check_positions_and_redeem.py --apply

# 終端 journal dashboard。
DASHBOARD_THEME=light ./.venv/bin/python dashboard.py

# 以目前 gates 回放歷史訊號。
./.venv/bin/python scripts/replay_journal_signals.py --hours 168

# D.4 markout／regime 證據，不會變更 live policy。
./.venv/bin/python scripts/market_regime_report.py --db data/trading/trade_journal.db --min-samples 30

# 前瞻 shadow 收集狀態與報表。
./.venv/bin/python scripts/forward_shadow_report.py --status
./.venv/bin/python scripts/forward_shadow_report.py --db data/research/twap_forward_shadow.db --trade-db data/trading/trade_journal.db

# 完整測試套件。
./.venv/bin/python -m pytest -q
```

`data/trading/trade_journal.db` 是策略／訂單／fill／結算的 canonical 本機紀錄；`data/backups/trade_journal.db` 是原子快照。journal 缺失、空白、無法讀取或 schema 不相容時，bot 會以 SELL-only 狀態啟動。只有真實 maker-BUY fills 可用於 D.4 execution-cost 分析；dry-run shadow fills 是診斷資料，不能取代 live fill 證據。

Outcome lead/lag 目前設定為 `OUTCOME_LEAD_LAG_MODE=live_entry_only`。Fast-follow 新進場要求 journal 健康、重連後資料新鮮，並通過專屬 execution-penalty economics 檢查；預設不繞過該檢查。週末 session gate 仍會阻擋新的 fast-follow BUY。

Telegram controller 為選用功能，需要 `TELEGRAM_BOT_TOKEN` 與 `TELEGRAM_OWNER_CHAT_ID`。通知傳送採非同步序列化，不會因 Telegram 故障阻塞交易迴圈。Conditional-token 餘額查詢遇上游 API 故障時會依 token 退避；bot 使用安全的 inventory fallback，不虛構餘額。

## Repository 地圖

```text
run_bot.py                    live／dry-run CLI 入口與策略主體
bot/                          lifecycle、pricing、signals、quoting、exits、recovery
execution/                    maker economics 與 Polymarket 整合
monitoring/trade_journal_db.py SQLite journal 與報表存取
config/profiles/              版本化、非機密策略 profile
config/operator.env.example   支援的本機操作員設定
scripts/                      preflight、replay、研究報表、allowance、redeem
```

若本 README 與 `project_overview.md` 不一致，以 `project_overview.md` 為準。

## 驗證與風險

任何有意的策略變更，都要先跑 `project_overview.md` 定義的 phase-specific evidence，至少再執行：

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/python scripts/inspect_env_contract.py --env .env --strict
git diff --check
```

二元合約可能損失全部進場成本。場館可用性、訂單狀態、settlement reference、費用與流動性都可能改變。Live 運行時請持續監控；移動資金前應獨立驗證 wallet／chain activity。本程式不是投資建議。
