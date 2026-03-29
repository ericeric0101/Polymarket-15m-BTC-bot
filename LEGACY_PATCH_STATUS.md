# Legacy And Patch Status

This file documents which parts of the repo are on the live trading path, which parts are legacy or sidecar, and which local patches are runtime-critical.

## Live Trading Path

These modules are on the current BTC 15-minute live trading path and should be treated as production code:

- `run_bot.py`
- `bot/`
- `execution/`
- `monitoring/`
- `data_sources/binance/`
- `data_sources/coinbase/`

These areas are the first priority for testing and refactors.

## Legacy / Sidecar Areas

These modules are present in the repo but are not the primary live trading path today:

- `core/ingestion/`
- `core/nautilus_core/`
- `core/strategy_brain/`
- `feedback/learning_engine.py`
- `execution/risk_engine.py`
  Note: the module header already states it is not used by the maker strategy path.

Rules for these areas:

- Do not change them as part of trading hotfixes unless there is a proven runtime dependency.
- Do not assume their `test_*.py` files are valid pytest tests.
- Prefer isolating or documenting them before attempting refactors.

## Runtime Patch Scripts

`run_bot.py` currently auto-applies local compatibility patches on startup via `auto_apply_local_patches()`.

The following scripts patch dependencies inside `venv/site-packages` and are therefore runtime-critical in the current setup:

- `scripts/patch_nautilus_polymarket_drop_log.py`
- `scripts/patch_nautilus_polymarket_ticksize_log.py`
- `scripts/patch_nautilus_polymarket_trade_log.py`
- `scripts/patch_nautilus_polymarket_execution.py`
- `scripts/patch_py_clob_http_helpers.py`

Implications:

- A fresh environment may not behave the same way unless these patches are applied.
- Upgrading Nautilus / py-clob-client can silently invalidate these patches.
- Any cleanup touching these scripts must preserve current runtime behavior first.

## Safe Cleanup Order

1. Isolate broken legacy test entrypoints from pytest collection.
2. Add focused tests for the live trading path only.
3. Inventory every runtime patch and document the exact behavior it changes.
4. Replace runtime patching with one of:
   - vendored shim code inside the repo, or
   - pinned fork / patched dependency package.
5. Only then refactor or delete legacy `core/` components.

## What Not To Touch Blindly

- Anything under `venv/site-packages/nautilus_trader/...`
- `run_bot.py` startup patch bootstrap
- Polymarket execution adapter behavior
- order event / strategy event payload contracts used by DB reports

These need explicit verification against live behavior before modification.
