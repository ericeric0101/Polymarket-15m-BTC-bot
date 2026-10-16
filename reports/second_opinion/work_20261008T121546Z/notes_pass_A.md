# Pass A notes (blind code-first) — TS=20261008T121546Z, HEAD=9352e69
## Pass 0
- Bot NOT running at audit start (ps: only unrelated uvicorn, cwd Blogwriter-/server). evidence/00_ps.txt
- df: 228Gi, 12Gi avail (94%). evidence/00_df.txt
- Sizes: logs/trade_journal.db 3,172,360,192 B (mtime Oct 7 23:28 +08); backups/trade_journal.db 3,172,360,192 B (mtime Oct 8 00:08 +08);
  data/research/twap_forward_shadow.db 1,246,535,680 B; data/analysis_snapshots/freshness_audit_.../trade_journal_snapshot.db 2.75GB + twap snapshot 0.96GB;
  data/analysis_archives/*.tar.zst ~1.43GB total. evidence/00_file_sizes.txt
- Working tree dirty only with untracked reports/research_analysis/* (no runtime-relevant code change). evidence/00_git_pass0.txt

## Live/dry-run mode (Phase 12)
- launcher.py:1091 simulation = not --live; test_mode = --test-mode or not --live. _is_dry_run_mode() == test_mode (run_bot.py:1625).
- Exec client is ALWAYS a real authenticated PolymarketExecClient (launcher.py:~806) in both modes; LiveRiskEngine bypass=simulation (launcher.py:828) -> risk engine BYPASSED in dry-run.
- `--live --test-mode` => simulation=False & test_mode=True (dry-run with risk engine on). OK.
- submit_order call sites: order_submission.py:574 (gated by is_dry_run at :454), taker_exit.py:1060, :1269, :1617.
  * :1060/:1269 reached from _maybe_taker_exit_positions which returns if is_simulation (taker_exit.py:119; caller quote_runtime.py:268 passes _is_dry_run_mode()).
  * :1617 (_maybe_maker_urgent_exit, taker_exit.py:1396) has NO dry-run guard; gated only by stop_loss_enabled and maker_urgent_exit_enabled.
- Code defaults: STOP_LOSS_ENABLED default True (app_config.py:1016), MAKER_URGENT_EXIT_ENABLED default True (app_config.py:980).
  Profile config/profiles/btc15_twap_v3.env:4 STOP_LOSS_ENABLED=0, :134 MAKER_URGENT_EXIT_ENABLED=1; .env:25 STOP_LOSS_ENABLED=0.
  Loader runtime_env.py:load_runtime_env: profile then .env, shell env wins. => effective stop_loss_enabled=False unless shell overrides.
- Startup rehydration (recovery.py:288-337) reads Nautilus cache.positions_open (populated by live exec reconciliation, LiveExecEngineConfig default reconciliation) -> real wallet holdings populate live_inventory_cost even in dry-run.
  => FINDING candidate: dry-run + STOP_LOSS_ENABLED=1 + wallet holding current-market tokens => urgent exit can submit a REAL GTC SELL with risk engine bypassed. Currently dormant because STOP_LOSS_ENABLED=0. INFERRED (not executed).

## Run-mode history (DB)
- strategy_runs: LIVE (test_mode=0) 295 runs 2026-09-07..2026-09-30; TEST_DRY_RUN 198 runs 2026-09-30T23:56Z..2026-10-07. => entire Oct 3-7 window is DRY-RUN; no real fills. evidence/01_runs_by_day.txt
- runs/day: Oct3 35, Oct4 35, Oct5 64, Oct6 15, Oct7 6 -> rollover cadence changed around Oct 6.

## Phase 3 entry (code)
- fair := market mid (run_bot.py:2131-2142 "market_mid_canonical"); digital fair only telemetry.
- MakerEngine.generate_quote_plan (execution/maker_engine.py:414-639): passive quote_bid=min(skewed_fair-half_spread, inst_bid); econ via estimate_quote_economics with adverse_selection_buffer=0 (:497-510); BUY should_quote = expected_net >= maker_min_expected_net (:579-586) — empirical markout explicitly NOT gating (:577-584 comment). => With fair=mid and bid<=mid, ex-ante BUY "edge" is half-spread minus fee by construction; adverse selection unmodelled in gate. Real directional edge comes only from side selection/score gates (quote_service.py:515 evaluate_buy_entry_controls: strike verified, TWAP not degraded, locked-side, first-entry gate, score gates).
- Fail-closed: market_strike_unverified (quote_service.py:559), twap_reference_degraded (:574), L2 depth stale -> levels None (pricing_runtime.py:270-277, max age quote_max_delivery_delay_sec default 2.0 s, local receipt clock).
- QUOTE_STALE_SEC default 30 s (app_config.py:1051) — watchdog stale threshold; edge_state uses quote_stale_sec as max_quote_age (run_bot.py:2539).
- Per-loop ORDER_SKIP_* events written to journal on every quote cycle for blocked sides (run_bot.py:2280-2335) -> write amplification candidate (check counts).
- client_order_id = f"BTC-15M-MAKER-{SIDE}-{int(time.time()*1000)}" (order_submission.py:523); TAKER-EXIT and URGENT-EXIT same ms pattern -> not idempotent across restart, collision possible within same ms.
- SELL path writes no durable intent before submit (order_submission.py:548 only for buy).

## Dry-run fill model (bot/shadow_simulation.py:445-515)
- One simulated maker BUY per market; FILLED when later ask <= entry_price; full qty at limit, no partial, no post-only reject/latency. Directionally conservative on queue (requires level to clear), optimistic on size/latency; misses non-touch fills.

## Phase 4 stop/invalidation (code)
- Effective: STOP_LOSS_ENABLED=0 (profile) => _maybe_taker_exit_positions returns (taker_exit.py:121-123), urgent exit returns (:1398), catastrophic requires stop_loss_enabled (exit_engine.py:504-505). Positions held to settlement. And in dry-run taker exits never run at all (is_simulation, :119). => No stop execution evidence exists in Oct window; only stop_forensics_shadow.
- Side invalidation (run_bot.py:1116-1250): invalidated = spot not beyond strike±2bps AND held-token mid <= 1-0.64=0.36; hits counter per slug; confirm at hits>=MAKER_SIDE_INVALIDATION_CONFIRM_CYCLES (profile 3).
  BUG: _resolve_locked_side_runtime_state is called inside `for side in side_plan` loop (run_bot.py:2276 loop, call :2402) and generate_quote_plan always returns both 'buy' and 'sell' keys (maker_engine.py:573-639) -> counter increments twice per quote cycle for the active instrument (unless buy side `continue`s early at :2280-2335). "3 cycles" is effectively 2 cycles (~3-6 s at MAKER_QUOTE_REFRESH_SEC=3), and the effective persistence varies with buy-limit state. Persistence is cycle-count, not time-based. DB: 209 CONFIRMED (all hits=3) since Oct 3. evidence/02_invalidation_counts.txt

## Phase 8 L2 retry (e4e87ee)
- l2_publish_state() helper (adapter_overrides.py:~907-918) is now the ONLY accessor of _btc15m_l2_publish_state (grep: lines 901,902,913 only). Every producer path (stopping, unsubscribed, due-check, snapshot) uses it.
- Pre-fix check: new tests run against `git archive e4e87ee^` + new test file: 3 FAILED (KeyError 'retry_at'), 1 passed; HEAD: 27/27 pass. evidence/03_prefix_e4e87ee.txt, 03_head_backpressure.txt => STRONG_FIX for reported path; tests do cover the failure.
- l2_update_ts_by_inst (market_runtime.py:574-587): local time.time() at DataEngine delivery, keyed str(instrument_id) (token-unique, so no cross-market overwrite); consumer pricing_runtime.py:270-277 fails closed (None levels) when absent/negative/> quote_max_delivery_delay_sec(2.0 s). Stamp = "a delta was delivered", not "book is valid/uncrossed".

## Log window evidence
- logs/bot/terminal_bot.log covers 2026-10-07 07:34:57 -> 23:28:03 (+08 local). Cycles: 3h rollover cycles 10833 s/10831 s/10835 s with ~15 s stop->start gaps (lines 3339/3350, 6583/6594, 13000/13011). Several operator stops (rollover=False) at 16:25, 20:59, 22:30, 23:28.
- Final shutdown 23:27:44: forced_boundary backup image_bytes=3,172,315,136 took 6.16 s, free_bytes_before=15,888,797,696.
- Health during last hour: Storage CRITICAL ('twap_guard:EXISTIN...'), Data DEGRADED quote_transport:EXISTING_QUOTE_FRESHNESS_FAILED, yet STATUS tradable=YES -> health is observational, not an entry gate (verify).
- BTC1S: source_receive_p50_ms=-414.0 (negative: source ts ahead of local receipt by ~0.4 s) -> cross-domain clock subtraction still present in BTC1S telemetry.

## Phase 6/7 sigma & freshness (code)
- Settlement modelled as Chainlink TWAP average over final window (spot_pricer.py ~1155-1270). required_move_mode: PRE_FINAL_STRIKE_PROXY (target=strike) before final window; EXACT_FINAL_WINDOW_BOUNDARY inside.
- required_move_sigma = |S-target| / (S * sigma_after_time_decay * sqrt(T/yr)) (live_entry_research.py:348-359) — linear, not log.
- sigma_after_time_decay = max(floor 0.20, clamp(raw,0.20..1.60)*max(0.30, min(1, TTE/600))) (forecast_state.py:119-127; profile MAKER_DIGITAL_* lines 69-77). => For TTE<600 s sigma is deliberately shrunk with TTE, so required_move_sigma is inflated by up to 1/0.30=3.33x vs a diffusion z-score; NOT the reflection-principle z. Bins over TTE x z are confounded by this transform.
- Final window: uses sqrt(remaining window) for a future AVERAGE; Var(avg of BM over tau) = sigma^2 tau/3 -> z understated by sqrt(3) relative to the average's own sd (partially offsets the decay inflation; net semantics unclear).
- Raw sigma (market_data.py:209-247): last 120 ticks of Chainlink raw spot (not a fixed time window), demeaned, std/sqrt(avg_dt) annualised; irregular dt; past-only (OK).
- path_spot falls back Chainlink -> Binance ws (spot_pricer.py ~975-990) while strike/settlement are Chainlink => reference price can switch mid-market (record path_spot_source).
- freshness_clock_semantics_version=2 written only at prediction_research_snapshot.py:118; NO reader in code/scripts filters on it (grep). Enforcement end-to-end absent.
- build_prediction_snapshot: btc fresh requires local transport age AND same-source value age; market source age only when market_source_reference_ts present else skipped (receive age still required); twap fresh = local receive age <= pex_max_age(10 s). joint_fresh = p_ex & (UP or DOWN mid fresh) & btc & twap — note market_mid_fresh uses OR.

## Phase 9 storage (code + DB)
- TradeJournal backup (monitoring/trade_journal_db.py:315-379): sqlite online backup -> .tmp -> os.replace; free-space preflight required = 2*image + existing tmp (storage_retention.py:56-58) -> refuses when short (OK). DEFAULT_BACKUP_INTERVAL_SEC = 3h at HEAD (:28, commit 9352e69 2026-10-08 01:00 +08) — the running process at 23:27 used 900 s => HEAD cadence has NEVER run in production.
- Dirty flag set by essentially every journal write incl. telemetry -> a full 3.17 GB image every interval in practice.
- Backup lives on same APFS volume (backups/ vs logs/) -> protects against SQLite corruption/operator error only, not disk loss.
- Journal composition Oct 6 (UTC): top bytes ENTRY_DECISION_TRACE 77.4 MB, QUOTE_TRANSPORT_TELEMETRY 54.4 MB, ENTRY_EDGE_OBSERVATION 42.0 MB, EVENT_LOOP_CONSUMER_TIMING 30.9 MB (~22.7 KB/row) ... canonical order/fill events are a small fraction. evidence/09_tj_top_types_oct6.txt
- Journal payload growth: strategy_events 152-227 MB/day + order_events 50-66 MB/day (Oct 3-7). evidence/08_tj_growth.txt. No retention/pruning of journal tables found (to verify in storage_retention).
- TWAP research store data/research/twap_forward_shadow.db 1,246,535,680 B vs max_db_mb 500 -> 2.4x over cap. Schema is the legacy LeadLagDB (snapshots.hyperliquid_market_id column) — Hyperliquid-era schema reused.
- Guard history (evidence/07_guard_first.txt): free_disk_low 2026-09-29 15:27Z (9.49 GB free); db_size_cap from 2026-10-04 18:25Z (500.05 MB).
- Pre-HEAD guard suppressed everything except MARKET_TWAP_SUMMARY (9352e69 diff). DATA: TWAP_STRIKE_CROSS / TMINUS_CHECKPOINT / TWAP_PROJECTED_SIDE_CHANGE / SETTLEMENT_PATH_THRESHOLD_CROSS / MARKET_OPENING_TWAP_SAMPLE present through 2026-10-04 and ABSENT on 2026-10-05, -06, -07 (UTC) in the TWAP store, and absent from trade journal (evidence/05_twap_by_day_type.txt, 06_tj_twap_events.txt (empty)). => canonical crossing/checkpoint evidence lost for the whole native-v2 window; only 1 Hz PREDICTION_RESEARCH_SNAPSHOT (official_twap/strike/required_move gated on freshness) remains to reconstruct crossings.
- Meanwhile PREDICTION_RESEARCH_SNAPSHOT (~38-54k rows/day, 146-209 MB/day) kept writing to the same store (no cap in prediction_research_snapshot.py) -> cap suppressed the small canonical events while the large telemetry continued: inverted priority.
- HEAD 9352e69: REQUIRED_EVENT_TYPES bypass guard (fixes canonical loss going forward) but no hard cap on required events or on prediction snapshots; free_disk_low only labels CRITICAL. => STORAGE_BOUNDEDNESS = UNBOUNDED.
- Side effect noted: opening WAL DB with mode=ro touched data/research/twap_forward_shadow.db-shm and logs/trade_journal.db-shm mtimes (shm/wal files existed/0-byte wal). No content change.
