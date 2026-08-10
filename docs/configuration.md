# Configuration Contract

The bot has two configuration surfaces:

- [`config/operator.env.example`](../config/operator.env.example) contains the
  66 day-to-day keys: credentials, standard/reduced position tier, five-layer
  entry thresholds, TWAP safety, cost calibration, and exit authority.
- [`.env.example`](../.env.example) remains the full advanced reference for
  development, diagnostics, timing, retries, and experimental controls.

The local `.env` is deliberately backward compatible. Phase 6 does not delete
its existing advanced overrides because removal could silently switch a live
bot back to code defaults. Inspect it before any manual migration:

```bash
./.venv/bin/python scripts/inspect_env_contract.py --env .env
```

Add `--list` only when you need the advanced/unknown key names. The command
never prints values. Treat an advanced
override as a documented experiment: change one value, run a replay using the
same window and mode, then observe a new run before changing another value.

`TREND_BUY_MIN_NET_USDC` and `TREND_BUY_PENALTY_DISCOUNT` are no longer valid
settings. They were removed in Phase 4; every new BUY now uses the same
`robust_net` economics threshold.
