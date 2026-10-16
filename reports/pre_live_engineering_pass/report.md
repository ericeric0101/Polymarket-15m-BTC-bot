# Pre-12-Market Engineering Pass — final report

Bot stopped throughout (0 processes). No run_bot / --live, no network, no push. Historical DBs read with mode=ro / immutable=1. Pre-fix comparisons via `git archive a9b3f70` into the session scratchpad.

## UNRESOLVED（operator decisions）
1. Protective evaluation is driven only by QuoteTick. A quote-stream stall stops all protective evaluation (55.1 s on 2026-10-10). A timer-driven evaluator during stalls = new trading behaviour / stale-feed liquidation policy. Now observable via PROTECTIVE_EVAL_GAP; not changed.
2. The high-cost BUY cooldown (`taker_exit.py`, 45 s after a fill ≥0.75 while bid < entry) `continue`s before the hard breakers. It cannot mask them at entry TTE ≈580 s; for entries with TTE ≲ 340 s it can overlap the catastrophic breaker's adaptive min hold (floor 30 s). Policy choice; not changed.
3. The quote-watchdog node rebuild defers only for a live protective SELL, not for inventory alone (scheduled rollover defers for both).
4. Unchanged open items: kill-switch semantics for open positions; stale-feed liquidation; client-order-id idempotency / startup orphan-cancel never exercised in LIVE (UNVERIFIED); <5-share positions unsellable; disk ≈12 GiB vs 10 GiB guard.

## Verdicts
| Item | Verdict | Evidence |
|---|---|---|
| Run provenance 2026-10-10 | VERIFIED: f1a134e + dirty patch b8ae4cdf (byte-identical to manifest hash); weekend unlock active. Now reproducible via commit d379f56 + `ENTRY_ALLOW_TAIPEI_WEEKEND_BUYS=1` | S0_S1A_findings.md |
| ~83 s gap | Explained: ≈27.6 s high-cost cooldown early return (INFERRED) + 55.1 s quote-stream stall (VERIFIED). Position exposed (held, unevaluated); nothing bad happened (net ≈ −0.73, no breaker eligible) | S0_S1A_findings.md |
| instrument_id mismatch | Journal-identity bug (ORDER_FILLED defaulted to active-side instrument). In-memory inventory / pending-exit / PnL / oversell correct (tests pass on unmodified code). Startup replay keyed by instrument_id was affected (qty clamped to on-chain). Fixed cc1cd49. 20 historical SELL rows | tests/test_fill_instrument_identity.py |
| MARKET_CYCLE_PNL double count | RISK_P1 historically (session-guard rebuild, regime-guard bootstrap, live settlement delta). Fixed at source by b0c383f; exact +6.47/+0.84 shape pinned a8ff2bc (fails on a9b3f70, passes HEAD). Replay: one session (09-29) and 3 regime triggers differ | guard replay in summary |
| Telemetry gaps | E1/E3/E4 added (8b417f0); E2 verified | tests/test_stop_timing_telemetry_inputs.py |

## Commits
233fd3e chore(obs) · cc1cd49 fix(ledger) · a8ff2bc test(pnl) · d379f56 feat(config) · 8b417f0 research · 3fec933 docs(project)

## Tests
Baseline 1502 passed (dirty tree) → final 1539 passed; 37 new tests (8 eval-gap, 5 fill identity, 1 TP-exit shape, 15 weekend switch, 8 telemetry inputs). Each new test failed on unmodified code first (D's shape test failed on pre-fix a9b3f70 since the source fix pre-dated this pass).

## Coverage ledger
| Item | Status |
|---|---|
| S0 baseline + provenance | DONE |
| S1-A protective gap | DONE (cause explained; mechanism remains → UNRESOLVED policy) |
| S1-B weekend semantics | DONE |
| S1-C instrument_id | DONE (late post-rollover fill slug attribution documented, not fixed) |
| S1-D PnL double count | DONE (already fixed b0c383f; replay quantified) |
| F-A | DONE (observability only) |
| F-B | DONE |
| F-C | DONE |
| F-D | DONE (test-only commit; source fix pre-existing) |
| S3 telemetry | DONE |
| S4 verification/docs/rollover note | DONE |

READY_FOR_12_MARKET_RUN = CONDITIONAL (see final response).
