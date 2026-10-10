# Trade journal growth measurement (read-only, immutable=1; DB/-wal/-shm size+mtime unchanged)

- logs/trade_journal.db: 2.99 GiB pages (strategy_events 1.95 GiB, order_events 0.94 GiB, indexes 0.16 GiB); ~0.57 GiB unused space inside table pages; freelist 0.
- Core accounting/lifecycle event types (orders, fills, cycle PnL, settlement, session, redeem, strike, shadow-sim): ~24 MiB all-time (<1%).
- Diagnostic/telemetry types: ~2.87 GiB (>99%). Top: ENTRY_EDGE_OBSERVATION 577 MiB, ENTRY_DECISION_TRACE 519 MiB, QUOTE_TRANSPORT_TELEMETRY 459 MiB, SIDE_DECISION_OBSERVATION 130, BUY_PATH_DIAGNOSTIC 129, DEPTH_RISK_SHADOW_MARKOUT 128, SIDE_DECISION 112, ENTRY_CONFIRMATION_OBSERVATION 107, SMART_MONEY_OBSERVATION 103, ORDER_OBSERVE_BUY_BLOCKED 84, EVENT_LOOP_CONSUMER_TIMING 77 (4 days).
- Rate before daily diagnostic budget (d24b576): 11-16 MiB per active hour; 24h DRY-RUN days 269-386 MiB.
- Rate after budget (2026-10-08..09, only 6 active hours): ~5.1 MiB/h (ENTRY_DECISION_TRACE ~35%). Projection 24/7: ~120 MiB/day, ~3.6 GiB/month, plus an equal-size backup copy. Small sample -> INFERRED.
- Runtime readers need only core types (monitoring/trade_journal_db.py:702-739,1022-1042,1320,1366,1461-1471) and FILL_MARKOUT 168 h lookback; research scripts and bot/research/decision_export.py read some diagnostic types.
