# Configuration Contract

The bot has two configuration layers:

- [`.env.example`](../.env.example) and
  [`config/operator.env.example`](../config/operator.env.example) define the
  55-key day-to-day deployment surface. Local `.env` also retains any secret
  or host-only settings such as Redis credentials and process lock paths.
- [`config/profiles/btc15_twap_v3.env`](../config/profiles/btc15_twap_v3.env)
  is the versioned advanced strategy profile. It contains the reviewed legacy
  timing, retry, diagnostics, and experimental defaults.

At startup, the loader applies the profile first, then local `.env`, and keeps
shell/CI variables as the highest priority. Canonical operator names map to
the current AppConfig names so this Phase 8 migration does not change strategy
output. For example, `ENTRY_SCORE_MIN` maps to
`DIRECTIONAL_ENTRY_MIN_SCORE_ABS_NEW`, and `MARKET_TARGET_SHARES` maps to
`MAKER_FIXED_SHARES`.

To inspect the local split without revealing values:

```bash
./.venv/bin/python scripts/inspect_env_contract.py --env .env --strict
```

The expected result is 55 operator keys, zero advanced/unknown keys, plus any
listed `local_only_keys` required for credentials or host integration.

`FIRST_ENTRY_SCORE_MIN` is the effective threshold for the first filled entry
in a market. The runtime enforces `max(FIRST_ENTRY_SCORE_MIN,
ENTRY_SCORE_MIN)`: a first entry can be stricter than a later entry, but never
weaker. For the supplied profile, this is `0.22` for the first entry and
`0.20` thereafter.

To migrate another legacy machine-local environment:

```bash
./.venv/bin/python scripts/migrate_env_to_profile.py --env .env --profile btc15_twap_v3
./.venv/bin/python scripts/migrate_env_to_profile.py --env .env --profile btc15_twap_v3 --apply
```

The first command is a dry run. The migration never writes `POLYMARKET_PK`,
CLOB credentials, Telegram credentials, RPC URLs, or Redis credentials into a
versioned profile.

`TREND_BUY_MIN_NET_USDC` and `TREND_BUY_PENALTY_DISCOUNT` are no longer valid
settings. Every new BUY uses the same `robust_net` economics threshold.
