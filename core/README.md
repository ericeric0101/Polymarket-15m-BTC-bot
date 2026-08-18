# Core Legacy Status

`core/` is not the primary BTC 15-minute live trading path.

`core/ingestion/` and `core/nautilus_core/` have been removed. Only
`core/strategy_brain/signal_processors/base_processor.py` is retained in this
directory to satisfy the indirect dependency: `monitoring/grafana_exporter.py`
-> `execution/execution_engine.py` -> `SignalDirection`.

See:

- `/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/docs/LEGACY_PATCH_STATUS.md`
- `/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/run_bot.py`
