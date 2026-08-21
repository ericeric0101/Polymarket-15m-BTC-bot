# Legacy And Patch Status

This file documents which parts of the repo are on the live trading path, which parts are legacy or sidecar, and how compatibility overrides are controlled.

## Live Trading Path

These modules are on the current BTC 15-minute live trading path and should be treated as production code:

- `run_bot.py`
- `bot/`
- `execution/`
- `monitoring/`

These areas are the first priority for testing and refactors.

## Removed Sidecars

The obsolete Grafana exporter and the execution/risk/core chain that existed
only to feed it were removed in Phase B. The live maker strategy continues to
use Nautilus `LiveRiskEngineConfig`; it never routed orders through the removed
sidecar engine.

## Runtime Patch Scripts

`bot.launcher` currently installs compatibility overrides through
`bot.compat_patches.apply_compatibility_patches()`. These are process-local
Python overrides; they do **not** modify `venv/site-packages`.

The active implementation is [`bot/adapter_overrides.py`](../bot/adapter_overrides.py).
It contains narrowly scoped behavior for Polymarket data/execution parsing and
HTTP transport retry. Startup supports three explicit modes:

- `NAUTILUS_COMPAT_PATCH_MODE=runtime`: install the overrides.
- `NAUTILUS_COMPAT_PATCH_MODE=verify`: verify expected upstream targets only.
- `NAUTILUS_COMPAT_PATCH_MODE=off`: do not install or verify overrides.

Implications:

- A dependency upgrade can still invalidate an override, so upgrades require a
  dry-run startup and `pytest -q` before live use.
- `requirements.txt` pins the tested Nautilus and py-clob-client versions;
  do not relax those pins without updating the compatibility test.
- Legacy site-package rewrite scripts have been removed from the repository;
  do not reintroduce them.
- Compatibility behavior is covered by focused live-path tests; do not add
  unreviewed broad monkeypatches.

## Safe Cleanup Order

1. Isolate broken legacy test entrypoints from pytest collection.
2. Add focused tests for the live trading path only.
3. Keep compatibility behavior in `bot/adapter_overrides.py`, with focused tests.
4. If an override grows beyond a small adapter shim, move it to a pinned fork
   with a versioned integration test before replacing it.
5. Only then refactor or delete legacy `core/` components.

## What Not To Touch Blindly

- Anything under `venv/site-packages/nautilus_trader/...`
- `bot/adapter_overrides.py`
- Polymarket execution adapter behavior
- order event / strategy event payload contracts used by DB reports

These need explicit verification against live behavior before modification.
