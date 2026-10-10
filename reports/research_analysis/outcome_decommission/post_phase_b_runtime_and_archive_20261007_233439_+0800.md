# POST PHASE B RUNTIME AND ARCHIVE

觀察起點：2026-10-07T22:56:31.613665+08:00；T30 實測：2026-10-07T23:27:18.973073+08:00。觀察超過30分鐘，未縮短。T10/T20由5分鐘heartbeat採樣，實際時間列於下表，未冒充精確10/20分鐘。

|採樣|實際時間|Free GiB|Journal GiB|TWAP GiB|Old DB handle|
|---|---|---:|---:|---:|---:|
|T0|2026-10-07T22:57:31.122358+08:00|19.9277|2.9431|1.1385|0|
|T10|2026-10-07T23:08:55.075719+08:00|18.5194|2.9476|1.1467|0|
|T20|2026-10-07T23:19:22.740358+08:00|14.9195|2.9504|1.1542|0|
|T30|2026-10-07T23:27:18.973073+08:00|14.7997|2.9542|1.1606|0|

## Runtime proof

Durable manifest verified correct full HEAD, test_dry_run=true, outcome_subsystem_retired=true, PID56787, cycle1. Scheduled rollover10800s; this window does not test3h rollover. Journal 2543 strategy events; research 12207 decision rows; prediction 1673; DECISION_POINT_L2 401; 392 maker traces contain positive nearby ask depth. ORDER_DRY_RUN_SUBMITTED=7, no real venue submission. One missing_l2 trace rejects fail-closed. Neutral authority bot/pricing_runtime.py:_get_native_l2_depth uses l2_update_ts_by_inst and existing2s age gate, bot/market_runtime.py records it. No retry_at stderr/log error. Queue last=6/20000, drops=0, errors=0. No Outcome/FF current run event/ownership/embedded order evidence. Source imports/runtime construction removed; manifest retired. TCP IP snapshot alone cannot prove TLS hostname; no Outcome client or socket-opening path exists in current runtime.

## Five moved producer paths

|Producer|Destination / count|Result|Evidence / limitation|
|---|---|---|---|
|TrendEntryShadow|TWAP decisions: 24 candidates + 12 settlements; markouts 78|ACTIVE|Persisted rows prove acceptance; writer drops/errors0|
|ForwardShadowExperiment|TWAP 9 candidates, 5716 position marks, 3036 material changes|ACTIVE|Same shared writer, no guard-wide suppression|
|POST_ENTRY_SMART_MONEY_SNAPSHOT|TWAP0|NOT_OBSERVED_IN_WINDOW|Requires real strategy inventory>0; dry-run shadow fills do not create such inventory. No trigger seen; cannot claim dynamic acceptance tested.|
|order_handoff|latency_spans0|NOT_OBSERVED_IN_WINDOW|Dry-run returns before submit_order / enqueue_latency; no actual handoff trigger.|
|cancel_request_to_ack|latency_spans0|NOT_OBSERVED_IN_WINDOW|Dry cancellation is not venue cancel acknowledgement; no trigger.|

## TWAP guard impact

Existing db_size_cap500MiB was triggered at1165.515625MiB. Guard is scoped to TwapForwardShadow._persist, suppressing MARKET_OPENING_TWAP_SAMPLE, TWAP_STRIKE_CROSS, TWAP_PROJECTED_SIDE_CHANGE, TMINUS_CHECKPOINT, SETTLEMENT_PATH_THRESHOLD_CROSS and optional material traffic. MARKET_TWAP_SUMMARY remains allowed (2 observed). Prediction, decisionL2, trend and forward continue directly via shared LeadLagDB. Required research overall PARTIALLY_SUPPRESSED; five moved paths show0 guard-suppressed producers,3 untriggered (not verified dynamically). No cap raised.

## Freshness

- btc-updown-15m-1791384300: 190/205 joint fresh=92.68%; clock versions=[2].
- btc-updown-15m-1791385200: 778/785 joint fresh=99.11%; clock versions=[2].
- btc-updown-15m-1791386100: 677/683 joint fresh=99.12%; clock versions=[2].

22:45 start market is partial. Only23:00 market completed fully during the window:778/785=99.11%.23:15 market remains incomplete when stopped. High freshness is not proof of all synchronized quality gates.

## Backup / storage

- 2026-10-07 23:10:10.265 | INFO     | monitoring.trade_journal_db:flush_backup:413 - TradeJournal backup completed: reason=periodic interval_sec=900.0 image_bytes=3165347840 free_bytes_before=19790180352 last_success_ts=1791385810.264666
- 2026-10-07 23:25:16.913 | INFO     | monitoring.trade_journal_db:flush_backup:413 - TradeJournal backup completed: reason=periodic interval_sec=900.0 image_bytes=3170918400 free_bytes_before=15903248384 last_success_ts=1791386716.913858

Periodic publication interval906.649s (900s plus completion time), no30s loop. No leftover temp at checkpoints; transient backup temp during active operation not continuously sampled. Final forced_boundary backup and node.run returned shown below. Filesystem fell20.0063->14.7997GiB during observation, while journal+TWAP logical growth only~0.034GiB. This unresolved filesystem discrepancy does not establish APFS or backup causality;30min cannot justify long-term slope. Runtime core passed, but SAFE_TO_BEGIN_CLEAN_POST_PHASE_B_COLLECTION=NO until known guard/research omissions and rapid disk pressure are reviewed. No live config changes.

## Graceful stop

- 2026-10-07 23:27:44.305 | INFO     | bot.market_runtime:handle_stop:1209 - Strategy shutdown stage started: stage=background_threads
- 2026-10-07 23:27:46.328 | INFO     | bot.market_runtime:log_shutdown_stage:1199 - Strategy shutdown stage completed: stage=background_threads elapsed_sec=2.023 total_sec=2.023
- 2026-10-07 23:27:46.328 | INFO     | bot.market_runtime:handle_stop:1242 - Strategy shutdown stage started: stage=orders_and_final_journal_events
- 2026-10-07 23:27:46.333 | INFO     | bot.market_runtime:log_shutdown_stage:1199 - Strategy shutdown stage completed: stage=orders_and_final_journal_events elapsed_sec=0.005 total_sec=2.028
- 2026-10-07 23:27:46.780 | INFO     | bot.market_runtime:log_shutdown_stage:1199 - Strategy shutdown stage completed: stage=research_writers elapsed_sec=0.447 total_sec=2.475
- 2026-10-07 23:27:46.780 | INFO     | bot.market_runtime:handle_stop:1290 - Strategy shutdown stage started: stage=trade_journal_final_backup
- 2026-10-07 23:27:52.938 | INFO     | monitoring.trade_journal_db:flush_backup:413 - TradeJournal backup completed: reason=forced_boundary interval_sec=900.0 image_bytes=3172315136 free_bytes_before=15888797696 last_success_ts=1791386872.93745
- 2026-10-07 23:27:52.940 | INFO     | bot.market_runtime:log_shutdown_stage:1199 - Strategy shutdown stage completed: stage=trade_journal_final_backup elapsed_sec=6.160 total_sec=8.635
- 2026-10-07 23:28:03.120 | INFO     | bot.launcher:run_integrated_bot:1005 - Bot cycle 1 stopped elapsed_sec=1986.664 node_run_returned=True rollover=False
- 2026-10-07 23:28:03.121 | ERROR    | bot.launcher:run_integrated_bot:1026 - Node returned without an explicit rollover request; stopping instead of silently rebuilding.

Exit0, no remaining run_bot process. Launcher ERROR after manual CtrlC states no rollover requested; it is expected refusal to silently rebuild, not failed shutdown.

## Cold archive / deletion

Archive: /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_archives/legacy_active_hyperliquid_lead_lag_phase_b_20261007_233042_+0800.tar.zst
External manifest: /Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_archives/legacy_active_hyperliquid_lead_lag_phase_b_20261007_233042_+0800.tar.zst.manifest.json
Embedded ARCHIVE_MANIFEST.json records source identity, SQLite schema/metadata and DB member SHA. Archive itself hash is external, avoiding impossible self-reference. Deterministic USTAR member timestamps0, uid/gid0, mode444; streaming zstd, no raw tar duplicate. zstd integrity passed, both members restored, DB SHA exact, restored quick_check ok/pagecount1143903/pagesize4096/tables match. Archive/external manifest chmod444. Unique staging removed after verification. Source exact path removed only after final identity/handle checks, no empty replacement/no WAL existed.

Repo logical bytes before=18109461779; after=14050839165. Free before archive=14.7951, after archive=14.2029, after deletion=19.2826GiB. Logical reclaim=3.7799GiB, defined raw minus archive only. External manifest/report bytes negligible additional overhead, not included in mandated formula. No causal claim about filesystem fluctuations. Historical Outcome CLIs require --db, readers mode=ro, compact tool rejects absent input; no tool silently creates missing old path. Bot remains STOPPED.

## Summary

```text
POST_PHASE_B_RUNTIME_ARCHIVE

HEAD=33896ce9ba123708999c896bd7c2fd4023412d5f
BOT_PID=56787
RUN_ID=run_1791384905_563432fa
OBSERVATION_MINUTES=30
DRY_RUN=YES
POST_PHASE_B_RUNTIME=PASS_WITH_TWAP_GUARD_WARNING
OUTCOME_NETWORK_ACTIVITY=0
OUTCOME_OBSERVER_ACTIVITY=0
FAST_FOLLOW_RUNTIME_EVENTS=0
FAST_FOLLOW_ORDER_EVENTS=0
MAKER_ACTIVE=YES
MAKER_DEPENDS_ON_OUTCOME=NO
NEUTRAL_L2_ACTIVE=YES
RETRY_AT_KEYERROR_COUNT=0
PREDICTION_WRITING=YES
PREDICTION_DROPS=0
JOURNAL_WRITING=YES
NEW_SQLITE_ERRORS=0
TWAP_DB_GIB_START=1.1381988525390625
TWAP_DB_GIB_END=1.1606025695800781
TWAP_STORAGE_GUARD=CRITICAL: existing db_size_cap=500MiB
TWAP_REQUIRED_RESEARCH_PERSISTENCE=PARTIALLY_SUPPRESSED
MOVED_RESEARCH_PRODUCERS_ACTIVE=2/5
MOVED_RESEARCH_PRODUCERS_SUPPRESSED=0
MOVED_RESEARCH_PRODUCERS_NOT_TRIGGERED=3
BACKUP_INTERVAL_SEC=900
OLD_30S_BACKUP_BEHAVIOR_PRESENT=NO
OLD_HYPERLIQUID_DB_PATH=/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/research/hyperliquid_lead_lag.db
OLD_DB_OPEN_HANDLE_COUNT=0
OLD_DB_SIZE_CHANGED=NO
OLD_DB_MTIME_CHANGED=NO
OLD_DB_SHA256_CHANGED=NO
OLD_DB_WAL_CREATED_BY_RUNTIME=NO
OLD_DB_QUICK_CHECK=OK
ARCHIVE_CREATED=YES
ARCHIVE_PATH=/Users/cheng-kaihuang/Polymarket-BTC-15-Minute-Trading-Bot-main/data/analysis_archives/legacy_active_hyperliquid_lead_lag_phase_b_20261007_233042_+0800.tar.zst
ARCHIVE_GIB=0.5837544677779078
ARCHIVE_SHA256=45109aa605df59679da63e82d1be10bd9a20ddf12033f9777d919e30bc20beb9
ARCHIVE_ZSTD_TEST=PASS
ARCHIVE_RESTORE_HASH_MATCH=YES
ARCHIVE_RESTORED_QUICK_CHECK=OK
OLD_RAW_DB_DELETED=YES
OLD_RAW_DB_GIB=4.363643646240234
NET_GIB_RECLAIMED=3.7798891784623265
FILESYSTEM_FREE_GIB_PRESTART=20.006298065185547
FILESYSTEM_FREE_GIB_PRE_ARCHIVE=14.795120239257812
FILESYSTEM_FREE_GIB_POST_DELETE=19.2825927734375
BOT_GRACEFULLY_STOPPED=YES
BOT_RESTARTED_AFTER_ARCHIVE=NO
CODE_CHANGED=NO
COMMIT=NONE
PUSHED=NO
SAFE_TO_BEGIN_CLEAN_POST_PHASE_B_COLLECTION=NO
```
