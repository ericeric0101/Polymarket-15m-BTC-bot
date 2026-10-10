# Protective-exit availability — P1 remediation report

HEAD_BEFORE=7640a44  HEAD_AFTER=14ec69f (fix(risk): keep protective exits active during pauses; local, not pushed)
Bot stopped (ps=0), no network, no push, no secrets.

## Tests
- Baseline full suite (HEAD 7640a44): 1350 passed
- Red-first: tests/test_protective_exit_availability.py on fresh `git archive 7640a44`: 13 failed / 8 passed
- Post-fix targeted: 21 passed; relevant runtime/safety suites (11 files): 397+ passed
- Post-fix full suite: 1371 passed (PATH incl. /usr/sbin); git diff --check PASS

## Changed files
bot/protective_exit.py (new), bot/quote_runtime.py, bot/taker_exit.py, bot/order_events.py, bot/exit_engine.py (label only), run_bot.py, tests/test_protective_exit_availability.py (new), tests/test_live_path_regressions.py (fixture), project_overview.md

## Per-rule input freshness (INFERRED from source; stale-book and stale-TWAP behaviour VERIFIED by tests)
| Rule | Freshness required | Polymarket only | Reference only | Reference stale | Order book stale |
|---|---|---|---|---|---|
| Conditional absolute breaker | book <= QUOTE_STALE_SEC; trend from signal / confirmed invalidation / TWAP <= 5 s | NO (trend cannot be confirmed unless invalidation already confirmed) | NO (needs bid) | PARTIAL (TWAP vote unavailable, never adverse) | NO -> DEGRADED |
| Catastrophic breaker | same + thesis weakening | NO | NO | PARTIAL | NO -> DEGRADED |
| Endgame TWAP exit (off: STOP_LOSS=0) | TWAP <= 5 s + verified strike + bid | NO | NO | NO | NO |
| Resting 0.97 tail TP | none (resting venue order) | n/a | n/a | stays resting | stays resting |

## Gate
PROTECTIVE_EXIT_NORMAL=ACTIVE
PROTECTIVE_EXIT_TELEGRAM_PAUSE=ACTIVE (fixed; resting BUYs cancelled, SELLs kept)
PROTECTIVE_EXIT_ERROR_PAUSE=ACTIVE for class (i); DEGRADED for class (ii) unknown-SELL instrument
PROTECTIVE_EXIT_KILL_SWITCH=UNCHANGED (no orders) + DEGRADED reporting; policy UNRESOLVED
PROTECTIVE_EXIT_QUOTE_OUTAGE=DEGRADED (no evaluation from stale data; watchdog reports)
PROTECTIVE_EXIT_REFERENCE_FEED_OUTAGE=PARTIAL (evaluates with fresh inputs; stale votes unavailable; DEGRADED reported)
STALE_DATA_BEHAVIOR=fail-closed + rate-limited PROTECTIVE_EXIT_DEGRADED; no liquidation policy (UNRESOLVED)
ABSOLUTE_BREAKER_SEMANTICS=conditional_absolute_loss_breaker; $2.00 net, hold >= 60 s, confirmed adverse trend, (<= 120 s left OR 15 s + 2 votes); bypasses spread guard and hold band; NOT a guaranteed $2 cap (10 sh @ 0.70: about $2.0 to $7.00)
CATASTROPHIC_BREAKER_SEMANTICS=$0.40, thesis weakened/strong opposite, 2 confirmations, after hold band, off in last 45 s, 3% spread guard
STOP_LOSS_ENABLED=0  ADAPTIVE_STOP_ENABLED=NO
REAL_SUBMIT_CHOKEPOINT=bot/execution_safety.py ExecutionSafetyMixin.submit_order (unchanged)
DRY_RUN_REAL_SUBMIT_BLOCKED=YES (test)
DUPLICATE_SELL_PROTECTION=in-flight lock per instrument + 8 s interval + 20 s reject cooldown + qty <= verified sellable + UNSELLABLE once (tests)
TINY_LIVE_EXIT_PATH_VALIDATION_REQUIRED=YES (real FOK/IOC fill/reject/expire, in-flight clearing on real events, kill switch, orphan cancel)
UNRESOLVED=kill-switch exit policy; stale-feed liquidation policy
