# Polymarket BTC 15-Minute Trading Bot

An experimental, maker-first trading bot for Polymarket BTC 15-minute Up/Down
markets. This repository can submit real orders. Dry-run output is research
evidence, not a guarantee of fills or profitability.

[`project_overview.md`](project_overview.md) is the single authority for the
current implementation, known debt, evidence, and approved change sequence.
This README is the concise operator entry point; it must not be used to infer
that a planned Phase D change has already been deployed.

Traditional Chinese translation: [繁體中文 README](docs/readme_ZH.md).

## Current live contract

For each 15-minute market, the bot makes one directional decision: `UP`,
`DOWN`, or `NONE`. It is not two independent bots quoting both outcomes.

1. **Market and strike safety.** Gamma establishes the market identity and
   configuration. The frontend-compatible Polymarket `crypto-price` request,
   including the market's configured 60-second TWAP parameters, supplies the
   canonical Price To Beat. If that opening value cannot be verified, new BUYs
   fail closed.
2. **Shared fair and direction.** `ForecastState` is the one live fair/sigma
   policy. `SignalEngine` turns the same state, book, trend, and strike
   distance into a signed score: positive for UP, negative for DOWN.
3. **Entry gates.** Fresh market data, time window, direction confidence,
   external conflict checks, position limits, and the common `robust_net`
   economics gate must all pass.
4. **One entry per market.** A successful maker BUY consumes the market's
   entry budget. Partial fills are accepted as the result of that one order;
   the bot never reloads or supplements the entry in that market.
5. **Exit and settlement.** `HOLD_TO_REDEEM` keeps ordinary profitable
   inventory through settlement. When enabled and eligible, the static
   tail-protect TP is a passive GTC sell at `0.97`. A confirmed invalidation
   can take ownership for the recovery/urgent-exit ladder; it is distinct from
   the normal TP. Settlement, redeem, and PnL events are written to the local
   journal.

The 0.97 TP does not require a fresh TWAP. A stale TWAP blocks new BUYs and,
when configured, TWAP-confirmed recovery exits; it does not cancel an existing
static TP by itself.

## Strategy status

- Phases A, B, C, and D.3 (canonical strike provenance) are complete.
- D.4 has deployed **observability only**: fills record 10/30-second markout,
  spot continuation, BBO/depth, volatility, time-left, and UTC
  weekday/weekend features. Live economics still use the conservative 168-hour
  execution-cost calibration until a candidate 12–48-hour window has at least
  30 independent, current-version maker-BUY markets and passes the required
  out-of-sample review.
- D.5 configuration/document/code ownership cleanup has not started. The
  versioned profile currently has 218 assignments; do not treat that as 218
  daily operator knobs or delete a key based only on its name.

## Setup

Use the repository virtual environment directly:

```bash
git clone https://github.com/ericeric0101/Polymarket-15m-BTC-bot.git
cd Polymarket-15m-BTC-bot
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp config/operator.env.example .env
```

Fill `.env` with wallet/CLOB/RPC credentials before live use. Never commit it.

Configuration precedence is:

1. `config/profiles/btc15_twap_v3.env` — versioned, non-secret advanced
   defaults.
2. Local `.env` — credentials, host settings, and the supported operator
   overlay shown in `config/operator.env.example`.
3. Shell/CI environment — highest priority.

The operator example currently lists 55 supported deployment keys. The final
reader inventory and removal of remaining profile-only/legacy settings are D.5
work, not an invitation to copy the 218-key profile into `.env`.

Validate the local configuration without printing values:

```bash
./.venv/bin/python scripts/inspect_env_contract.py --env .env --strict
```

For an old, full `.env`, preview a migration before applying it:

```bash
./.venv/bin/python scripts/migrate_env_to_profile.py --env .env --profile btc15_twap_v3
./.venv/bin/python scripts/migrate_env_to_profile.py --env .env --profile btc15_twap_v3 --apply
```

## Run

Run preflight before a new deployment or configuration change:

```bash
./.venv/bin/python run_bot.py --preflight-only
```

Dry run is the default. It exercises the live decision and local order
lifecycle but never submits wallet orders:

```bash
./.venv/bin/python run_bot.py
```

Live mode requires an explicit command and interactive `yes` confirmation:

```bash
./.venv/bin/python run_bot.py --live
```

Useful variants:

```bash
./.venv/bin/python run_bot.py --live --terminal-dashboard
./.venv/bin/python run_bot.py --test-mode
```

`--test-mode` is for accelerated testing, not a production strategy setting.
Never run a second live launcher for the same wallet on the same host.

## Operations and evidence

```bash
# Check collateral/allowance only.
./.venv/bin/python scripts/check_allowance.py --check-only

# Inspect settled positions; --apply sends chain transactions.
./.venv/bin/python scripts/check_positions_and_redeem.py
./.venv/bin/python scripts/check_positions_and_redeem.py --apply

# Terminal journal dashboard.
DASHBOARD_THEME=light ./.venv/bin/python dashboard.py

# Historical signal replay with current gates.
./.venv/bin/python scripts/replay_journal_signals.py --hours 168

# D.4 markout/regime evidence. It does not change live policy.
./.venv/bin/python scripts/market_regime_report.py --db logs/trade_journal.db --min-samples 30

# Regression suite.
./.venv/bin/python -m pytest -q
```

`logs/trade_journal.db` is the canonical local strategy/order/fill/settlement
record. Only real maker-BUY fills count toward D.4 execution-cost selection;
dry-run shadow fills are useful diagnostics but do not replace live-fill
evidence.

The Telegram controller is optional and requires both `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_OWNER_CHAT_ID`. Notification delivery is asynchronous and serialized
so a Telegram outage cannot block the trading loop. Conditional-token balance
queries also back off per token after an upstream API failure; the bot uses its
safe inventory fallback rather than inventing a balance.

## Repository map

```text
run_bot.py                    live/dry-run CLI entry point and strategy host
bot/                          lifecycle, pricing, signals, quoting, exits, recovery
execution/                    maker economics and Polymarket integration
monitoring/trade_journal_db.py SQLite journal/report access
config/profiles/              versioned non-secret strategy profile
config/operator.env.example   supported local operator overlay
scripts/                      preflight, replay, regime report, allowance, redeem
docs/readme_ZH.md             Traditional Chinese README translation
```

When documentation conflicts with `project_overview.md`, the overview wins.

## Verification and risk

For any intentional strategy change, run the phase-specific evidence defined
in `project_overview.md`, then at minimum:

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/python scripts/inspect_env_contract.py --env .env --strict
git diff --check
```

Binary contracts can lose the full entry cost. Venue availability, order state,
settlement references, fees, and liquidity can change. Monitor live operation
and independently verify wallet/chain activity before moving funds. This code
is not investment advice.
