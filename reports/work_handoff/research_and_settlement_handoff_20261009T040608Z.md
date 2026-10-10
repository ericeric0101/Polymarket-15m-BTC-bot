# Research & settlement handoff (single working source of truth)

## State
- Branch `codex/db-resilience-and-stoploss-priority`, HEAD `5f40b14`. Bot STOPPED; must stay stopped. No push. No live.
- Untracked: `scripts/final_research_analysis.py` (prior analysis, not yet reviewed/committed); reports/* untracked by policy.

## Already fixed (committed)
- d24b576/b9b3f8b: single dry-run submit choke (ExecutionSafetyMixin), hard breakers independent of STOP_LOSS, per-cycle invalidation, freshness-v2 reader gate, unique client order ids, orphan-order cancel on live start, unresolved BUY intents in buy guard, BBO-repair BUY block, provenance diff archive, diagnostic budget.
- 14245c4: open-market strike lookup at wall-clock open; strike-pending snapshots; ENTRY_DECISION_TRACE not sampled; threshold hysteresis; forward-shadow storage pressure.
- 0d2c62c / c9310db / 8d1fc02 / 5f40b14: tiers A/B/P exports (+iCloud mirror), tier C verified partitions, gated retention (manual, bot stopped), startup maintenance pass.

## Unresolved
- SETTLEMENT AUTHORITY DEFECT (Stage 1): `bot/lifecycle_runtime.py::_record_market_settlement` derives
  MARKET_SETTLEMENT.outcome / cycle PnL / shadow settle from `latest_external_spot >= strike` while a canonical
  TWAP label (`_canonical_twap_shadow_label`) is computed in the same function. Prior research: journal outcome vs
  Polymarket official 68/895 (7.6%) mismatches; canonical TWAP vs official 2/437. (To be re-verified independently.)
- Analysis script reproducibility; candidate-entry/stop shadow; retention-on-exit (Stage 7, default OFF).

## Current research conclusions (prior, unvalidated, in-sample)
- |score|>=0.30, entry price >=0.75, first-entry TTE<=480 looked better on LIVE (9/10-9/30, 111 positions, 11 days) and
  DRY-RUN shadow sims (10/1-10/8, 266 markets, 8 days). Adaptive stop: market approx calibrated; keep OFF; keep hard breakers.
  legacy required_move_sigma monotone but not calibrated; z_diffusion only ~1 day of data.

## Known evidence limitations
- Few days (3 native-v2, <=11 LIVE). Thresholds chosen on same data. DRY-RUN fills optimistic. LIVE PnL not reconciled to
  on-chain redeems. Crossing events suppressed 10/4 18:25Z-10/7 in old cap period. Live research DB rows for 9/28-10/8 were
  moved into tier C partitions (10/2-10/8 still present; 9/28-10/1 partitions in macOS Trash); tier P keeps per-second paths.

## Tasks (in order; each its own commit; CHECKPOINT after Stage 1)
1 settlement authority fix + tests (+pre-fix proof) -> commit `fix(settlement): use canonical settlement authority` -> CHECKPOINT
2 outcome-provenance dataset rebuild (official > canonical TWAP > journal diagnostic; versioned official cache w/ hash)
3 entry analyses (SCORE_030, ENTRY_PRICE_075, TTE_480, combined IN_SAMPLE_ONLY)
4 flip/reversal + diffusion-z audit
5 stop-loss shadow + CANDIDATE_ENTRY_POLICY_V1 shadow fields (research only, policy_version) -> commit
6 analysis reproducibility -> commit `research: make final strategy analysis reproducible`
7 RESEARCH_RETENTION_ON_EXIT (default OFF) -> commit `feat(storage): run verified research retention on graceful exit`
Final report: reports/final_research_iteration/research_iteration_<ts>.md (+ machine summary).

## Files / functions likely involved
- bot/lifecycle_runtime.py (_canonical_twap_shadow_label, _record_market_settlement), bot/post_trade.py (redeem/settlement PnL),
  bot/shadow_simulation.py (_settle_shadow_simulation), bot/twap_forward_shadow.py (finalize_market / canonical side),
  bot/db_runtime.py (MARKET_CYCLE_PNL, session guard), bot/session_pnl_guard.py, monitoring/trade_journal_db.py
  (startup cycle reconcile ~L1500-1610, session_pnl_state), monitoring/pnl_attribution.py.
- Research: scripts/final_research_analysis.py, bot/research/*, bot/launcher.py (exit path), bot/research/retention.py.

## Tests that must keep passing
- Full suite (last: 1215 passed). Focused: tests/test_second_opinion_remediation.py, test_open_findings_remediation.py,
  test_runtime_validation_followups.py, test_research_*.py, test_session_pnl_guard*.py, test_shadow_simulation.py,
  test_trade_journal_recovery.py, test_live_path_regressions.py.

## FROZEN DECISION RULES (Step 0B; fixed before computing any result)
- PRIMARY entry metric: mean PnL per trade; secondary: market-relative edge; win rate secondary only.
- Unit: market. Uncertainty: day-blocked.
- Labels for SCORE_030 / ENTRY_PRICE_075 / TTE_480:
  STRONG_CANDIDATE / INDEPENDENT_SIGNAL = same direction on EVERY available day AND on LIVE and DRY-RUN separately AND
  survives controls (where controls apply). WEAK_CANDIDATE = same direction on a majority of days.
  PROXY_FOR_OTHER_FEATURES = effect disappears/shrinks to ~0 after controls. NOT_SUPPORTED = no consistent direction.
  UNRESOLVED = too little data.
- Minimum sample: <3 independent days in a cell -> descriptive only (N markets, N days, means), no p-values/CIs, best label
  WEAK_CANDIDATE or UNRESOLVED. >=3 days -> day-blocked intervals, labelled exploratory.
- Multivariable: <=3-4 predictors; report predictor correlation matrix first.
- DRY-RUN reported separately; DRY-RUN alone never upgrades a label.
- 0.30 / 0.75 / 480 are FROZEN candidates; no new threshold search.
- Production thresholds, adaptive stop, hard-loss thresholds, maker economics, L2 thresholds: NOT to be changed.

## ADDENDUM (written after Stage 2, BEFORE any Stage 3 result was computed) — operationalization of frozen rules
- Outcome labels: provenance CSV market_outcomes_5356cf95f81e.csv (official). PnL: LIVE = fills + official payoff; DRY-RUN = shadow sim qty*(1[won]-price).
- "Direction" of a candidate = sign of (mean PnL/trade in passing group - mean PnL/trade in failing group).
  Per-day direction is counted only on days where both groups have >=1 market; days with one group are reported, not counted.
- "Survives controls" (3B/3C only): OLS of PnL/trade on [candidate indicator + <=3 controls]; survives if the adjusted
  coefficient has the same sign as the raw difference AND |adjusted| >= 50% of |raw|. PROXY_FOR_OTHER_FEATURES if |adjusted|
  < 25% of |raw| or the sign flips. 25-50% -> cannot exceed WEAK_CANDIDATE / UNRESOLVED.
  Controls: LIVE: |score|, TTE (or price for 3C), distance (entry signed spot distance); DRY-RUN: |score|, TTE (or price), legacy
  required_move_sigma at fill from tier P (nearest snapshot <=5 s before fill). Correlation matrix reported first.
- Intervals: day-blocked bootstrap (2000 resamples, seed 11) only for cells with >=3 days; else descriptive.
- STRONG_CANDIDATE / INDEPENDENT_SIGNAL additionally needs the direction to hold on LIVE and DRY-RUN separately; DRY-RUN alone
  can never upgrade a label. Max drawdown = largest peak-to-trough of cumulative PnL in chronological order.
