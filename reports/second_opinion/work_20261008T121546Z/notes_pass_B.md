# Pass B — history, freshness, coupling
Search terms: git log --since=2026-09-25 + grep -iE "fresh|storage|stop|maker|fast.?follow|outcome|hyperliquid|l2|rollover|backup|twap|clock|entry|retire|decommission" (evidence/10_git_log_since_0925.txt).
Phase boundaries (author time, +08 -> UTC): 9f60f63 freshness v2 2026-10-06T16:24Z; 3b50637 Phase A 2026-10-07T02:02Z; 33896ce Phase B 2026-10-07T14:43Z; 9352e69 storage 2026-10-07T17:00Z (after last run stopped 15:28Z); 49aa9ca 3h rollover 2026-10-05T23:49Z; 622e97e stop disabled 2026-09-29T14:55Z.
Runtime phase from run manifests: first POST_A run 2026-10-07T02:36:57Z (commit 3b50637); POST_B run 14:56:31Z-15:27:46Z only (commit 33896ce, git_dirty=true).
Pre-fix checks (evidence/16_prefix_checks.txt, hermetic, parent tree + commit tests):
- e4e87ee: 3/4 fail pre-fix (KeyError retry_at) -> covers failure (behavioral).
- 9f60f63: 22 fail / 39 pass pre-fix, incl. behavioral (receipt vs same-clock freshness, negative same-domain age invalid not clamped).
- b034b4e: 5 fail pre-fix (interval/unchanged-skip/force boundary) — test name 'rpo_is_fifteen_minutes' later changed by 9352e69.
- 1fa2ba0: 8 fail pre-fix (backup failure isolation, destination-open failure, cooldown).
- 49aa9ca: 3 fail pre-fix (single interval authority; defer with exposure).
- 3b50637: collection ImportError pre-fix (observational_outcome_mode) -> proves API change only, NOT behavior coverage.
- 9352e69: collection AttributeError pre-fix (REQUIRED_EVENT_TYPES) -> API only.
- 33896ce: 7/7 fail pre-fix (store path guards, no outcome import/network hooks) -> behavioral.
- HEAD: 140 targeted tests pass (17_head_targeted_tests.txt) — 1 earlier failure was auditor env (TRADE_DB_ENABLED=0), passes without it (7/7).
Fast-follow after Phase A (21_ff_after_phaseA.txt): ORDER_FAST_FOLLOW_INTENT after 02:02Z only from pre-A run (commit 8309152) at 02:35:11Z; under 3b50637 only observational FAST_FOLLOW_* telemetry until 08:01Z (FAST_FOLLOW_EXECUTION_ENABLED=False constant).
Scope review: 622e97e "disable stop-loss" also gates absolute_max_loss_breaker, catastrophic stop, endgame TWAP exit and invalidation recovery behind STOP_LOSS_ENABLED (diff exit_engine.py/taker_exit.py) -> broader than message implies: removes the only per-position loss cap.
Coupling: research store = legacy LeadLagDB schema (snapshots.hyperliquid_market_id); FF compat in recovery.py:243, order_events.py:169-177, trade_journal_db.py:105-108,732-865; .env still holds 1 retired OUTCOME_/HYPERLIQUID_/FAST_FOLLOW_ key (ignored+warned, app_config.py:537). No runtime import/network of Outcome found (18_outcome_refs.txt; test_outcome_decommission passes).
