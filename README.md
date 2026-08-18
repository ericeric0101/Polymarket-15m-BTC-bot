# Polymarket BTC 15-Minute Trading Bot

An experimental, maker-first trading bot for Polymarket BTC 15-minute Up/Down
markets. The current live path is `run_bot.py` plus `bot/`, `execution/`, and
`monitoring/`. It is a directional binary-market strategy: for each market it
selects `UP`, `DOWN`, or `NONE`; it does not run independent bots on both
outcomes.

This repository can submit real orders. Treat dry-run results as research, not
as a guarantee of live fills or profitability.

## Current Strategy

New BUY decisions follow five ordered layers:

1. **Hard safety**: valid/fresh quote, settlement-aligned TWAP reference,
   valid market data, and no strong external/book conflict.
2. **Direction**: one locked `UP`/`DOWN`/`NONE` decision. The first entry uses
   `FIRST_ENTRY_SCORE_MIN`, which is never lower than `ENTRY_SCORE_MIN`.
3. **Model consistency**: strike/spot/fair-price validation and high-price
   risk/reward controls.
4. **Economics**: one common `robust_net` gate. Negative fair-edge buckets are
   shadow research only and do not relax live economics.
5. **Execution**: passive maker submission, TTL/requote controls, 10-share
   market cap, and a 5-share high-price or weak-probability tier.

See [Strategy Rules](docs/STRATEGY_RULES.md) for the operational rules and
[the Traditional Chinese guide](docs/readme_ZH.md) for the complete runbook.

## Setup

Use the repository virtual environment directly; this avoids shell activation
issues:

```bash
git clone https://github.com/ericeric0101/Polymarket-15m-BTC-bot.git
cd Polymarket-15m-BTC-bot
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Fill the local `.env` with wallet/CLOB/RPC credentials before any live use.
Do not commit it.

## Configuration Profiles

The bot loads configuration in this order:

1. `config/profiles/btc15_twap_v3.env`: version-controlled advanced strategy
   baseline.
2. Local `.env`: 55 supported day-to-day operator keys plus credentials and
   host-only values.
3. Shell or CI environment variables: highest priority.

The profile is not a second secrets file. It contains reviewed, non-secret
advanced defaults such as timing, retry, diagnostics, and experimental
controls. Local `.env` contains credentials and the small set of settings an
operator changes deliberately.

Validate the split without printing values:

```bash
./.venv/bin/python scripts/inspect_env_contract.py --env .env --strict
```

For a legacy full `.env`, preview then apply a migration:

```bash
./.venv/bin/python scripts/migrate_env_to_profile.py --env .env --profile btc15_twap_v3
./.venv/bin/python scripts/migrate_env_to_profile.py --env .env --profile btc15_twap_v3 --apply
```

## Run

Always run a preflight before a new deployment or configuration change:

```bash
./.venv/bin/python run_bot.py --preflight-only
```

Dry run, the default without `--live`, exercises the live decision and order
lifecycle but does not submit wallet orders:

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
./.venv/bin/python run_bot.py --no-grafana
./.venv/bin/python run_bot.py --test-mode
```

`--test-mode` is for accelerated testing and is not a production strategy
setting. Never run a second live launcher for the same wallet on the same host.

## Operations

```bash
# Inspect balance and allowance only.
./.venv/bin/python scripts/check_allowance.py --check-only

# Inspect/redeem settled positions. --apply sends transactions.
./.venv/bin/python scripts/check_positions_and_redeem.py
./.venv/bin/python scripts/check_positions_and_redeem.py --apply

# Terminal database dashboard.
DASHBOARD_THEME=light ./.venv/bin/python dashboard.py

# Replay historical journal signals with current live gates.
./.venv/bin/python scripts/replay_journal_signals.py --hours 168

# Run the regression suite.
./.venv/bin/python -m pytest -q
```

`logs/trade_journal.db` is the canonical local record for strategy, order, fill,
and settlement analysis. It does not replace independent wallet or chain
verification before moving funds.

## Repository Map

```text
run_bot.py                    launcher and live/dry-run entry point
bot/                          lifecycle, safety, direction, quoting, recovery
execution/                    maker economics and Polymarket integration
monitoring/trade_journal_db.py SQLite journal
config/profiles/              versioned non-secret strategy profiles
scripts/                      preflight, replay, reports, allowance, redeem
docs/                         current runbooks and archived design notes
core/                         legacy/sidecar code; not the active live path
```

Read [Documentation Index](docs/INDEX.md) before relying on a design note.

## Verification

Run this sequence for every intentional strategy change:

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/python scripts/replay_journal_signals.py --hours 168
./.venv/bin/python scripts/inspect_env_contract.py --env .env --strict
```

Replay reports are settlement-only historical comparisons. They exclude future
fill uncertainty, live fees, and live exits unless the report explicitly says
otherwise.

## Risk

Binary contracts can lose their full entry cost. Polymarket/CLOB availability,
order state, settlement references, fees, and liquidity can change. This code
is not investment advice and must be monitored while running live.
