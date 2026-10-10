# S0 + S1-A findings (partial, written during the pass)

## S0 baseline
- HEAD f1a134e999edd3f2e03fff80f22a67b8a13242ec, branch codex/db-resilience-and-stoploss-priority.
- Uncommitted: bot/entry_session_policy.py, tests/test_entry_session_policy.py, tests/test_quoting.py (weekend unlock). Saved to dirty_patch_20261010T100440.diff (sha256 b8ae4cdf…7be5).
- Bot processes: 0. Free disk ≈12 GiB. Baseline full suite (dirty tree): 1502 passed.

## Run provenance (2026-10-10 LIVE, process_1791585579_54482)
- VERIFIED: run manifest git_commit=f1a134e, git_dirty=True, dirty_diff_hash=b8ae4cdf…7be5, archive logs/run_diffs/<hash>.patch.
- VERIFIED: saved working-tree diff sha256 == manifest dirty_diff_hash (byte-identical) → the run executed f1a134e + exactly this patch.
- VERIFIED: weekend unlock active: runs 1–2 (clean HEAD, 01:03–06:35 +08) emitted ENTRY_SESSION_BUY_BLOCKED; cycles run_1791585596/run_1791589295 emitted none and filled BUYs on Taipei Saturday.
- Reproducible from a commit: NO until the patch is committed (reproducible from f1a134e + archived patch: YES).

## S1-A protective-evaluation gaps while held (2026-10-10)
| Position | Gap | Window (UTC) | Cause | Evidence |
|---|---|---|---|---|
| 1791587700 UP | 82.8 s | 23:20:17→23:21:39.9 | (a) ≈27.6 s high-cost BUY cooldown `continue` before evaluate (INFERRED); (b) 55.1 s quote-stream stall → no QuoteTick → no quote cycle → no protective cycle (VERIFIED) | taker_exit.py:463-482; order_events.py:410-421 (fill 0.82 ≥ maker_high_cost_fill_threshold 0.75, cooldown 45 s; bid 0.70–0.77 < avg 0.82); journal silent 23:20:44.6→23:21:39.9 in both tables; research snapshot (captured in `_quote_maker_orders`) gap 55.1 s; QUOTE_WATCHDOG_TRIGGERED stale_for 39.5 s at 23:21:24; TWAP WS silent stall + disconnects 23:20:59–23:21:43 |
| 1791587700 UP | 22.4 s | 23:28:00→23:28:22 | FOK protective SELL rejected → `taker_exit_reject_cooldown_sec=20` `continue` (by design) | taker_exit.py:196-198; ORDER_REJECTED 23:28:02 |
| 1791586800, 1791589500 | none >10 s | — | — | snapshot cadence + telemetry obs_interval |

Exposure: the position was held with no protective evaluation for 82.8 s (VERIFIED). Nothing bad happened in the window: bid 0.82→0.70, net ≈ −0.73 at resumption, below every breaker; absolute min_hold 60 s and catastrophic effective min hold ≈77 s at TTE ~580 → no breaker could have been eligible (INFERRED).

Flow at HEAD (VERIFIED by code reading): QuoteTick → market_runtime.start_maker_worker → run_bot._quote_maker_orders (3655) → quote_runtime._prepare_quote_cycle → kill-switch branch (169) / entry-pause branch (182) / normal (278) → protective_exit._run_protective_exit_cycle → _protective_exit_blocked_instruments → taker_exit._maybe_taker_exit_positions → exit_engine.evaluate → _submit_taker_exit_order → ExecutionSafetyMixin. The earlier P1 fix is effective: pauses no longer skip protection. BUT the only driver is QuoteTick; the watchdog timer (run_bot.py:4396-4401) only reports availability. A quote stall therefore stops all protective evaluation (stale-feed policy item; adding a timer-driven evaluator is new trading behaviour → UNRESOLVED for CK).

Latent policy item (UNRESOLVED): the high-cost cooldown `continue` precedes the hard breakers. It cannot mask them at entry TTE ≈580 s, but for entries with TTE ≲ 340 s the catastrophic breaker's adaptive min hold (floor 30 s) can fall inside the 45 s cooldown.
