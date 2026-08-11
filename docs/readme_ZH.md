# Polymarket BTC 15 分鐘 Bot 操作手冊

本文件是目前版本的繁中操作入口。策略規則請讀
[STRATEGY_RULES.md](STRATEGY_RULES.md)，設定載入順序請讀
[configuration.md](configuration.md)。早期 V1、UP-only 與 7-phase 設計文件不能作為現行操作依據。

## Bot 做什麼

- 自動探索 `btc-updown-15m-*` 市場。
- 對 `UP`、`DOWN` 或 `NONE` 做唯一方向判斷。
- 使用 Chainlink 60 秒 TWAP 優先定價；只有符合 degraded-feed 規則時才使用外部現貨備援。
- 使用 maker-first 掛單，並經固定五層決策鏈：硬安全、方向、模型一致性、經濟性、執行。
- 目標倉位為 10 shares；高價進場降為 5 shares；每市場總預估曝險上限為 10 shares。
- 所有策略、訂單與結算事件寫入 `logs/trade_journal.db`。

## 安裝

```bash
cd /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

之後直接使用 `./.venv/bin/python`，不必執行 `.venv/bin/activate`。

## 設定

設定分成兩層：

- `config/profiles/btc15_twap_v3.env`：可版控、不含秘密的進階策略預設。它目前承載 292 個舊設定，目的是維持遷移前行為，不是每日手動編輯入口。
- `.env`：本機私密資料、連線資料與 55 個日常操作 key。`.env` 被 git 忽略，不能 commit。

載入優先順序為：profile -> `.env` -> shell/CI 環境變數。也就是本機 `.env` 可覆蓋 profile，而 command line shell variable 優先權最高。

首次設定 `.env` 時，填入必要的私密與連線值，例如錢包私鑰、funder、CLOB 憑證、Polygon RPC 和 Telegram。不要把它們寫進 profile 或貼到終端紀錄。

檢查 key 結構但不顯示數值：

```bash
./.venv/bin/python scripts/inspect_env_contract.py --env .env --strict
```

## 啟動前檢查

```bash
./.venv/bin/python run_bot.py --preflight-only
./.venv/bin/python scripts/check_allowance.py --check-only
```

第一個檢查市場發現、儀器與策略啟動條件；第二個檢查 collateral/conditional token allowance。若需要贖回已結算倉位：

```bash
./.venv/bin/python scripts/check_positions_and_redeem.py
./.venv/bin/python scripts/check_positions_and_redeem.py --apply
```

`--apply` 會送出鏈上交易，僅在你確認錢包和 gas 正常時使用。

## 啟動 Bot

### Dry run

```bash
./.venv/bin/python run_bot.py
```

沒有 `--live` 就是 dry run。它執行與 live 相同的決策、target/requote、TTL、取消與 submit-time controls，但不送出錢包訂單。用它收集 gate、shadow 和 order lifecycle 資料。

### Live

```bash
./.venv/bin/python run_bot.py --live
```

這會在確認提示後使用真實資金。啟動前請確認只有一個 bot process 和一個 Telegram polling process，避免重複下單或 Telegram `getUpdates` conflict。

### 常用選項

```bash
./.venv/bin/python run_bot.py --test-mode
./.venv/bin/python run_bot.py --no-grafana
./.venv/bin/python run_bot.py --terminal-dashboard
```

`--test-mode` 仍不是 live；`--live` 才允許送出真實訂單。

## Dashboard 與資料分析

```bash
DASHBOARD_THEME=light ./.venv/bin/python dashboard.py
./.venv/bin/python scripts/replay_journal_signals.py --hours 168
./.venv/bin/python -m pytest -q
```

- `dashboard.py` 讀取 journal 顯示目前狀態、倉位與近期交易。
- replay 是歷史診斷工具。只有標示為成本後、可執行的報告才可作為 live economics 判斷；settlement-only PnL 不含成交機率、費用和滑價。
- 測試通過不代表策略一定獲利，只代表已驗證的程式行為未回歸。

## 日常排錯

| 現象 | 先做什麼 |
| --- | --- |
| `ModuleNotFoundError: nautilus_trader` | 使用 `./.venv/bin/python`，並確認 requirements 已安裝到 `.venv`。 |
| `permission denied: .venv/bin/activate` | 不要直接執行它；使用 `source .venv/bin/activate`，或直接使用 `./.venv/bin/python`。 |
| Telegram `Conflict: terminated by other getUpdates request` | 停止另一個使用同一 token 的 Telegram bot process。 |
| quote watchdog / rollover | 查看 `logs/` 與 journal 的 `QUOTE_WATCHDOG_*` 事件，先確認 transport 而不是調整 entry gate。 |
| redeem 成功但資產未見增加 | 檢查 selected collateral、wrap transaction 與鏈上 receipt；Data API 可能延遲更新。 |

## 重要風控

- dry run 不會預測真實成交率；它只能在 live 等價 lifecycle 下提供比較資料。
- 任何參數修改先保存 replay baseline，再一次只改一個因果面向。
- 負 fair-edge 區間目前只記錄 shadow 研究，不是 live 放行規則。
- 不因短期勝率提高就放寬 safety、方向、經濟性與倉位上限。
- 每次 live 前確認 `.env` 的 wallet/funder、allowance、RPC、gas 與 auto-redeem 設定。
