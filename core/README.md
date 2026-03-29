# Core Legacy Status

`core/` is not the primary BTC 15-minute live trading path.

Current live trading path is:

- `run_bot.py`
- `bot/`
- `execution/`
- `monitoring/`
- Binance / Coinbase market data adapters used by the current strategy

`core/ingestion/`, `core/nautilus_core/`, and `core/strategy_brain/` remain in the
repo as legacy or sidecar architecture from earlier iterations.

Rules for working in this directory:

- Do not treat changes here as trading hotfixes.
- Do not assume code here is exercised by the current live bot.
- Do not refactor `core/nautilus_core/` casually; the repository still contains
  Nautilus-related experiments and adapters that may be useful for future work,
  but they are not the active maker-strategy runtime.
- Prefer documenting and isolating these modules before deleting or rewriting them.

See:

- `/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/LEGACY_PATCH_STATUS.md`
- `/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/run_bot.py`
