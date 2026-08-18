# Phase 2: Volatility and Fair-Model Audit

Date: 2026-08-15

This is an inventory and investigation report only. It does not change live
entry, exit, sizing, volatility, fair-price, or environment values.

## Data API Compatibility

The repository already uses the documented Polymarket Data API routes that
benefit from the serving-layer update:

| Repository path | Route | Purpose | Action |
| --- | --- | --- | --- |
| `scripts/check_positions_and_redeem.py` | `GET /positions` | Current and redeemable positions | Keep. Faster positions and resolved-market fixes apply server-side. |
| `scripts/backfill_redeem_activity.py` | `GET /activity` | Redeem reconciliation into the local journal | Keep. Faster activity/PnL serving applies server-side. |
| `bot/smart_money.py` | Data API trade/position polling | Shadow smart-money telemetry | Keep current documented client. |

The strategy does not call a Data API price-history route. CLOB market-data
clients and WebSocket feeds provide its price and order-book inputs. The new
`/v1/approvals` endpoint is not yet present in the public API documentation
available during this audit, so it must not replace the existing CLOB/on-chain
allowance checks until its schema, authentication requirements, and semantics
are verified. It is an optional operational read, not a fair-price input.

## Scope

Phase 2 concerns the forecast sigma and fair-probability path only. The
following separate system is deliberately out of scope and belongs to the
execution-lifecycle family:

- `MAKER_VOL_*` values used by `pricing_runtime._compute_recent_volatility()`
  to select quote-spread/size regimes from Polymarket quote history.

Those values are not the sigma passed to the digital BTC settlement model.

## Current Forecast Path

1. `spot_pricer._compute_fair_probability()` obtains a fresh Chainlink 60s
   TWAP reference whenever available. It records external observations.
2. `estimate_external_spot_sigma_annualized()` calculates annualized standard
   deviation from recent log returns, using the configured window and minimum
   points.
3. The quote pricer applies: default or realized sigma -> scale -> floor/cap
   -> optional time decay -> optional implied-sigma guardrail.
4. For a native TWAP source, the quote pricer replaces the ordinary digital
   probability with `twap_settlement_up_probability()`.
5. `side_decision._compute_new_signal_side_decision()` independently derives
   sigma, but currently applies only default/realized -> scale -> floor/cap.
   It then uses `digital_up_probability()` even when the reference source is
   native TWAP.

The two consumers therefore do not share one forecast state.

## Core Parameter Inventory

| Key | Current profile value | Reader | Classification | Phase-2 treatment |
| --- | ---: | --- | --- | --- |
| `MAKER_DIGITAL_VOL_WINDOW` | 120 | `AppConfig`, external sigma estimator | Estimator input | Replace with calibration-selected model input after out-of-sample validation. |
| `MAKER_DIGITAL_VOL_MIN_POINTS` | 20 | `AppConfig`, external sigma estimator | Estimator readiness policy | Retain temporarily; later derive readiness from sampling cadence and confidence. |
| `MAKER_DIGITAL_SIGMA_DEFAULT` | 0.60 | Quote pricer and side decision | Fallback model prior | Calibrate by source/regime; do not tune from a small recent sample. |
| `MAKER_DIGITAL_SIGMA_FLOOR` | 0.20 | Quote pricer and side decision | Numerical safety boundary | Keep as an internal safety bound until calibration defines valid range. |
| `MAKER_DIGITAL_SIGMA_CEILING` | 1.60 | Quote pricer and side decision | Numerical safety boundary | Keep as an internal safety bound until calibration defines valid range. |
| `MAKER_DIGITAL_VOL_SCALE` | 1.00 | Quote pricer and side decision | Duplicate manual calibration proxy | Replace with a data-calibrated forecast transformation. |
| `MAKER_DIGITAL_SIGMA_TIME_DECAY` | 1 | Quote pricer only | Model transform | Must be centralized or removed; it currently creates consumer disagreement. |
| `MAKER_DIGITAL_SIGMA_TIME_DECAY_REF_SEC` | 600 | Quote pricer only | Manual model shape | Evaluate from calibration curves, not subjective tuning. |
| `MAKER_DIGITAL_SIGMA_TIME_DECAY_MIN` | 0.30 | Quote pricer only | Manual model shape | Evaluate from calibration curves, not subjective tuning. |
| `MAKER_DIGITAL_IMPLIED_SIGMA_ENABLED` | 1 | Quote pricer only | Market-derived diagnostic guardrail | Keep observational until native-TWAP calibration separates information from circularity. |
| `MAKER_DIGITAL_IMPLIED_SIGMA_WEIGHT` | inherited/default 0.50 | `AppConfig` and `settings` only | Dead setting | Remove when the compatibility transition permits: no pricing path reads it. |
| hard-coded implied floor `0.6 * implied_sigma` | n/a | `spot_pricer` | Hidden manual model parameter | Surface as telemetry first; replace/remove only after calibration. |
| hard-coded fallback floor `max(0.20, sigma)` | n/a | `MakerEngine.calculate_fair_price()` | Hidden fallback policy | Align with the centralized forecast bound during implementation. |

Market-data policy values such as `POLYMARKET_CHAINLINK_TWAP_WINDOW_SEC`,
`REQUIRE_TWAP_REFERENCE_SPOT`, and `TWAP_DEGRADED_BLOCK_NEW_ENTRIES` are
hard-safety/source policies, not calibration knobs. They stay outside the
forecast parameter reduction.

## Evidence Available Now

The journal contains 22,149 `LIVE_SIGNAL_COMPARE` events overall; 12,434 are
within the latest 168 hours. Their recorded sigma range is 0.20 to 1.60, with
mean 0.3462. The implied-sigma guardrail was recorded as applied 1,566 times
in the 168-hour window. There are also 451 `TWAP_REFERENCE_DEGRADED` events
in that window.

This is enough to prove the current transforms are materially active. It is
not enough to calibrate them by source: `LIVE_SIGNAL_COMPARE` currently omits
the final `spot_source` and `settlement_model`, so the 12,434 rows cannot be
separated into native-TWAP and degraded-fallback forecast cohorts.

## Confirmed Design Gaps

1. **Two sigma paths:** quote fair and side decision derive sigma separately.
   The quote fair path has time decay and an implied-sigma floor; side
   decision has neither.
2. **Two settlement probabilities:** native-TWAP quote fair uses the TWAP
   average-settlement approximation, while side decision still records the
   instantaneous digital probability.
3. **One dead key and two hidden numbers:**
   `MAKER_DIGITAL_IMPLIED_SIGMA_WEIGHT` has no behavioral consumer; `0.6` and
   `0.20` remain hidden policy inputs in code paths.
4. **Incomplete calibration telemetry:** stored sigma is present, but source,
   final settlement model, raw realized sigma, every transform, and ultimate
   settlement outcome are not one joinable forecast record.
5. **Documentation drift:** `docs/pure_strategy.md` says sigma ceiling is
   1.20, while the active profile is 1.60. It must be corrected together with
   the implementation, not independently guessed now.

## Implementation Sequence After This Audit

Each step is isolated and must pass the full test suite plus a same-window
replay before the next step.

1. **P2.1 telemetry only:** record one forecast snapshot with source, raw
   realized sigma, final sigma, each transformation, ordinary probability,
   TWAP-average probability, and later outcome. Do not change decisions.
2. **P2.2 one forecast-state builder:** make quote pricing and side decision
   consume the same immutable forecast state while preserving the current
   quote-pricer values. This removes disagreement without choosing new
   numeric values.
3. **P2.3 out-of-sample calibration report:** split native TWAP and degraded
   fallback cohorts; report Brier score, calibration curve, and settlement
   PnL by time-to-close and volatility regime. No threshold is chosen before
   the report has adequate settled observations.
4. **P2.4 remove manual proxies one at a time:** first remove the dead implied
   weight, then replace `VOL_SCALE` and time-decay constants only if P2.3
   shows a stable calibration improvement. Preserve the floor/cap as internal
   safety invariants until sufficient evidence supports a replacement.

## P2.1-P2.3 Implementation Status (2026-08-18)

P2.1 telemetry and P2.2's shared forecast builder are complete.  Quote pricing
and side decision now use the same `ForecastState` construction: the same raw
realized sigma, bounds, time-decay, implied-sigma guardrail, reference source,
and native-TWAP average-settlement probability.  No entry, exit, sizing, or
cost threshold was changed while making this convergence.

`scripts/twap_fair_calibration_report.py` now reads the P2.1 forecast fields
from `LIVE_SIGNAL_COMPARE`, chooses one earliest complete forecast per settled
market, separates source/model cohorts, and prints a chronological 30% holdout
instead of treating repeated quote ticks as independent samples.

The first report over the available 120-hour history produced 207 joinable
settled markets.  The native-TWAP cohort (200 markets) had model Brier score
`0.44247`, versus `0.24659` for the contemporaneous market midpoint.  This is
not evidence to tune sigma by hand: all sampled forecasts were at 10-15 minutes
to close and the extreme probability bins are strongly miscalibrated.  It is
evidence that P2.4 must be data-driven and evaluated per forecast timing and
source, not by adding another volatility multiplier.

`MAKER_DIGITAL_IMPLIED_SIGMA_WEIGHT` was removed because it had no behavioral
consumer.  The remaining sigma values stay unchanged until a later
out-of-sample calibration identifies a single replacement policy.

---

# Phase 3: Execution-Lifecycle Audit

Date: 2026-08-15

This section is an inventory only. It changes no TTL, requote, cancel,
watchdog, inventory, rollover, or order-submission behavior.

## Scope and Counting Rule

The lifecycle boundary contains **82 raw environment keys**. The count includes
the supported local canonical names and their legacy internal readers where
both can currently be supplied. It also includes sizing and recovery keys that
participate in order lifecycle, but labels them as belonging to Phases 5 or 7
so they are not removed by the wrong migration.

Every value enters through `bot/app_config.py` (or `bot/launcher.py` for node
rollover), is copied by `bot/settings.py`, and is consumed by the paths listed
below. `bot/runtime_env.py` maps the small local `.env` surface onto legacy
readers; it does not eliminate those readers yet.

## Inventory: Quote, Requote, and Cancellation (23)

| Keys | Read / consumer path | Relationship | Candidate consolidation |
| --- | --- | --- | --- |
| `MAKER_QUOTE_REFRESH_SEC`, `MAX_REQUOTE_PER_SEC` | `AppConfig` -> `settings` -> `quote_runtime` | Evaluation cadence versus rate cap; complementary, not duplicates. | `QuoteSchedulePolicy`; retain both semantic inputs. |
| `MAKER_BUY_PLANNED_QUOTE_MAX_AGE_SEC` | `quote_runtime._should_skip_buy_submit_for_quote_drift` | Submit-time snapshot validity, separate from transport freshness. | Keep under `SubmitFreshnessPolicy`. |
| `MAKER_POST_ONLY`, `ORDER_POST_ONLY` | `AppConfig` -> `quote_service.build_limit_order` | Same boolean through canonical/legacy names. | **Resolved 2026-08-18:** runtime reads only `ORDER_POST_ONLY`; legacy name is migration-only. |
| `MAKER_POST_ONLY_STRICT` | `quote_service.build_limit_order` | Controls failure behavior if Nautilus cannot construct post-only. | Move to adapter/venue capability policy; not a strategy knob. |
| `MAKER_ORDER_TTL_SEC`, `ORDER_TTL_SEC` | `AppConfig` -> `order_runtime._is_order_ttl_expired`, `quote_runtime` | Same normal maker-order TTL. | **Resolved 2026-08-18:** runtime reads only `ORDER_TTL_SEC`; legacy name is migration-only. |
| `MAKER_POST_FILL_BUY_COOLDOWN_SEC` | `fill_ledger` / `quote_runtime` | Prevents immediate buy re-entry after a fill. | `PostFillPolicy`; independent of order TTL. |
| `MAKER_EARLY_SELL_ONLY_SEC`, `MAKER_REDUCE_ONLY_NO_NEW_SELL_LAST_SEC` | `quote_runtime`, `quoting` | Both constrain tail behavior but have different phase semantics. | Replace later with one named market-phase policy, after replay equivalence. |
| `MAKER_GATE_BLOCK_GRACE_SEC` | `quote_service.reconcile_unwanted_quotes` | Delay before canceling an existing quote after a gate blocks it. | `CancelPolicy.gate_block_grace`; not duplicate with cancel cooldown. |
| `MAKER_REQUOTE_MIN_AGE_SEC`, `ORDER_REQUOTE_MIN_AGE_SEC` | `AppConfig` -> `quote_service.should_requote_existing_order` | Same BUY/default requote age. | **Resolved 2026-08-18:** runtime reads only canonical operator input. |
| `MAKER_REQUOTE_MIN_AGE_SEC_SELL` | `quote_service.should_requote_existing_order` | SELL-specific override. | Retain only if replay proves BUY/SELL need distinct ages; otherwise inherit default. |
| `REQUOTE_HYSTERESIS_TICKS`, `ORDER_REQUOTE_HYSTERESIS_TICKS` | `AppConfig` -> `quote_service.compute_requote_target_version` | Same price-move threshold. | **Resolved 2026-08-18:** runtime reads only canonical operator input. |
| `MAKER_CANCEL_COOLDOWN_SEC`, `MAKER_CANCEL_ACK_TIMEOUT_SEC`, `MAKER_CANCEL_MAX_RETRIES`, `MAKER_CANCEL_ACK_DEDUPE_WINDOW_SEC` | `order_runtime`, `execution_events` | Four stages of one cancel state machine: duplicate suppression, wait, retry, event de-dupe. | Introduce internal `CancelPolicy`; do not merge numeric meanings. |
| `MAKER_ERROR_PAUSE_SEC`, `MAKER_MAX_CONSECUTIVE_DENIED` | `order_runtime`, order-event rejection handling | Failure backoff and escalation threshold. | `SubmissionFailurePolicy`; separate from cancel retries. |

## Phase 3.1: Confirmed Alias Removal (2026-08-18)

This first lifecycle change removes only names proven to be duplicate operator
inputs. It does not change TTL, requote cadence, cancellation, watchdog, or
inventory semantics.

`AppConfig` now reads these canonical keys directly:

- `ORDER_POST_ONLY`
- `ORDER_TTL_SEC`
- `ORDER_REQUOTE_MIN_AGE_SEC`
- `ORDER_REQUOTE_HYSTERESIS_TICKS`
- `MARKET_MAX_POSITION_SHARES`

The latter is the sole external cap and supplies both existing internal
inventory-cap consumers. `runtime_env` no longer projects it into
`MAKER_MAX_INVENTORY_SHARES` or `MAX_LOCKED_SIDE_POSITION`; it no longer
aliases the four `ORDER_*` keys either. The legacy names remain accepted only
by `scripts/migrate_env_to_profile.py`, which converts old files to canonical
operator keys rather than preserving them in a profile.

The versioned `btc15_twap_v3.env` already contained none of these six legacy
keys, so this removes six runtime readers rather than profile lines. Its
physical key count will only fall when a later phase removes a setting that is
actually present and whose behavior has independently been validated.

## Inventory: Quote Transport and Watchdog (11)

| Keys | Read / consumer path | Relationship | Candidate consolidation |
| --- | --- | --- | --- |
| `QUOTE_HEALTHCHECK_INTERVAL_SEC`, `QUOTE_STALE_SEC` | `run_bot._start_quote_watchdog_timer`, `ops.should_run_quote_watchdog` | Poll interval and stale threshold. | `QuoteHealthPolicy`; retain separately. |
| `QUOTE_INVALID_TICK_RELOAD_THRESHOLD`, `QUOTE_RELOAD_COOLDOWN_SEC` | `run_bot._maybe_run_quote_watchdog`, `ops.handle_quote_watchdog_recovery` | Invalid-data escalation and reload suppression. | Same policy object, distinct failure controls. |
| `QUOTE_RESUBSCRIBE_GRACE_SEC` | `run_bot._start_quote_watchdog_timer` | Time before resubscribe failure escalates to node rollover. | `QuoteHealthPolicy.resubscribe_grace`. |
| `QUOTE_EVENT_CLOCK_SKEW_TOLERANCE_SEC` | quote-age telemetry | Event-time validation tolerance; not quote staleness. | Keep as data-integrity invariant. |
| `STALE_QUOTE_SYNTH_MAX_AGE_SEC` | `settings`, quote synthesis | Synthetic-book age ceiling. | Keep separate from watchdog stale threshold. |
| `POLYMARKET_QUOTE_HEARTBEAT_SEC` | adapter transport heartbeat | Quiet-book liveness heartbeat. | Adapter transport policy, not strategy configuration. |
| `NO_QUOTE_DIAG_INTERVAL_SEC` | diagnostics throttle | Observability only. | Move to Phase 6 operational defaults. |
| `AUTO_NODE_ROLLOVER_ENABLED`, `AUTO_NODE_RESTART_ON_UNEXPECTED_EXIT` | `launcher.run_integrated_bot` | Scheduled rebuild versus unexpected-exit recovery. | `NodeRecoveryPolicy`; keep two explicit booleans. |

## Inventory: Node and Market Lifecycle (12)

| Keys | Read / consumer path | Relationship | Candidate consolidation |
| --- | --- | --- | --- |
| `AUTO_NODE_ROLLOVER_SEC`, `AUTO_NODE_ROLLOVER_COOLDOWN_SEC`, `AUTO_NODE_ROLLOVER_MAX_FAILURES` | `launcher.run_integrated_bot` | Scheduled node lifecycle, restart pacing, and stop condition. | Phase 6 `NodeRecoveryPolicy`; no strategy decision impact. |
| `MAKER_KILL_SWITCH_RESET_ON_ROLLOVER` | market-switch state reset | Decides whether an emergency guard survives a new market. | Keep as a risk-policy invariant; review with Phase 5. |
| `MARKET_SETTLING_GRACE_SEC`, `MARKET_NEXT_POLL_SEC`, `MARKET_WAITING_MAX_MISSES` | `lifecycle_runtime`, `ops.handle_waiting_phase_search` | Settlement wait, discovery polling, stale-market escalation. | `MarketLifecyclePolicy`; three distinct stages. |
| `BTC_MARKET_LOAD_SLUG_COUNT`, `BTC_MARKET_LOOKAHEAD_INTERVALS`, `BTC_MARKET_LOOKBACK_INTERVALS` | market discovery/bootstrap | Candidate market-cache scope. | Phase 6 discovery policy. |
| `BTC_MARKET_END_WINDOW_BACK_MINUTES`, `BTC_MARKET_END_WINDOW_FORWARD_MINUTES` | market discovery query window | API query bounds, not strategy behavior. | Phase 6 discovery policy. |

## Inventory: Inventory and Venue-Balance Synchronization (17)

| Keys | Read / consumer path | Relationship | Candidate consolidation |
| --- | --- | --- | --- |
| `MARKET_MAX_POSITION_SHARES`, `MAKER_MAX_INVENTORY_SHARES`, `MAX_LOCKED_SIDE_POSITION` | `AppConfig` -> `quote_service` inventory gates | One intended market cap supplied two legacy consumers. | **Resolved 2026-08-18:** one canonical external cap supplies both internal consumers. |
| `INVENTORY_FULL_BEHAVIOR` | `quote_service.build_desired_quote_entry` | Defines STOP_BUY versus spread widening at cap. | Inventory-policy enum; retain. |
| `MAKER_INVENTORY_SKEW_MAX` | `MakerEngine.apply_inventory_skew` | Price skew, not a hard cap. | Move to quote pricing group; do not merge with position cap. |
| `MAKER_STALE_INVENTORY_SEC`, `MAKER_STALE_INVENTORY_MULTIPLIER` | inventory-aware quoting | Stale internal inventory protection. | One `InventoryFreshnessPolicy`; distinct threshold and multiplier. |
| `MAKER_RELOAD_INVENTORY_THRESHOLD_SHARES` | legacy reload-entry guard | Previously governed a second buy after a partial fill. | New entries are limited to one successful BUY per market; retire this reader during Phase 7 sizing. |
| `SELLABLE_FALLBACK_AFTER_BUY_SEC`, `SELLABLE_AFTER_BUY_BUFFER_SHARES` | `pricing_runtime._get_effective_sellable_qty` | Temporary conservative fallback before venue token balance is visible. | `VenueBalanceSyncPolicy`; retain separately. |
| `SELL_DELAY_AFTER_BUY_SEC`, `SELL_BALANCE_RETRY_PAUSE_SEC`, `SELL_RECOVERY_QTY_BUFFER_SHARES` | `order_submission`, `order_events` | Wait/retry/quantity buffer for venue balance synchronization. | Same `VenueBalanceSyncPolicy`; do not collapse until P4/P5 exit evidence is complete. |
| `CONDITIONAL_BALANCE_CHECK_INTERVAL_SEC`, `CONDITIONAL_BALANCE_SAFETY_BUFFER_PCT`, `MAKER_BALANCE_CHECK_INTERVAL_SEC`, `MAKER_BALANCE_PAUSE_SEC` | wallet/conditional-token polling and insufficient-balance pause | Different balance sources plus backoff. | Split into `CollateralBalancePolicy` and `ConditionalTokenBalancePolicy`; no numeric merge. |

## Inventory: Sizing and Capacity Inputs Participating in Lifecycle (9)

These nine keys are counted because they determine whether a planned order can
be submitted, replenished, or sold. They remain **Phase 7**, not a Phase-3
edit.

| Keys | Read / consumer path | Relationship | Candidate consolidation |
| --- | --- | --- | --- |
| `MARKET_TARGET_SHARES`, `MAKER_FIXED_SHARES` | canonical alias -> `MakerEngine._compute_maker_order_qty` | Same standard target through canonical/legacy names. | Keep only `MARKET_TARGET_SHARES` externally. |
| `HIGH_PRICE_TARGET_SHARES` | `runtime_env` derives legacy size multiplier | Derived from normal target and high-price target. | Keep external absolute target; never expose both target and multiplier. |
| `MAKER_MIN_SHARES`, `MAKER_EXCHANGE_MIN_SHARES` | order quantity and sellability guards | Desired minimum versus venue minimum. | Keep venue minimum internal; operator sets target only. |
| `MAKER_QUOTE_SIZE_USDC`, `MAKER_MAX_ORDER_USDC` | `MakerEngine._compute_maker_order_qty` | Legacy notional sizing and cap; bypassed when fixed shares is active. | Remove from active profile after Phase-7 fixed-share equivalence test. |
| one-successful-BUY invariant | fill ledger/risk guard | A market may establish only one directional position. | Hard-coded risk policy; no operator override. |

## Inventory: Shadow Lifecycle and Recovery Exit Execution (10)

| Keys | Read / consumer path | Relationship | Candidate consolidation |
| --- | --- | --- | --- |
| `SHADOW_SIMULATION_ENABLED`, `SHADOW_SIMULATION_FILL_TIMEOUT_SEC`, `SHADOW_SIMULATION_MAX_QUOTE_AGE_SEC`, `SHADOW_SIMULATION_AGED_QUOTE_MAX_AGE_SEC` | `shadow_simulation.py`, dry-run lifecycle | Simulation-only TTL/fill-age policy. | Keep separate from live TTL until parity evidence is complete. |
| `RECOVERY_EXIT_LADDER_ENABLED`, `RECOVERY_EXIT_PASSIVE_TTL_SEC`, `RECOVERY_EXIT_PASSIVE_MIN_TIME_LEFT_SEC` | `taker_exit.py` recovery ladder | Passive recovery stage enablement, TTL, and time eligibility. | Phase 5 `RecoveryExitPolicy`; no Phase-3 change. |
| `MAKER_URGENT_EXIT_ENABLED`, `MAKER_URGENT_EXIT_COOLDOWN_SEC`, `MAKER_URGENT_EXIT_TTL_SEC` | `taker_exit.py` urgent stage | Separate emergency exit state machine. | Phase 5; retain separate from ordinary maker TTL. |

## Confirmed Duplicates and Non-Duplicates

The following are actual duplication candidates, not merely similarly named
settings:

1. **Resolved 2026-08-18:** canonical-to-legacy aliases: `ORDER_POST_ONLY`/`MAKER_POST_ONLY`,
   `ORDER_TTL_SEC`/`MAKER_ORDER_TTL_SEC`,
   `ORDER_REQUOTE_MIN_AGE_SEC`/`MAKER_REQUOTE_MIN_AGE_SEC`,
   `ORDER_REQUOTE_HYSTERESIS_TICKS`/`REQUOTE_HYSTERESIS_TICKS`,
   `MARKET_TARGET_SHARES`/`MAKER_FIXED_SHARES`, and
   historical `MARKET_MAX_BUY_EVENTS`/`MARKET_MAX_BUY_EVENTS_PER_MARKET` aliases
   (replaced by the one-successful-BUY invariant).
2. **Resolved 2026-08-18:** one canonical position cap previously wrote both
   `MAKER_MAX_INVENTORY_SHARES` and `MAX_LOCKED_SIDE_POSITION`.

The following must **not** be prematurely merged: order TTL versus recovery or
urgent-exit TTL; stale quote versus planned-quote age; cancel cooldown versus
cancel acknowledgement timeout; and normal inventory cap versus sellable
venue-balance buffer. They are separate states with different failure modes.

## Remaining Phase-3 Work

Phase 3.1 removed the six confirmed duplicate readers without changing any
numeric lifecycle policy. The remaining settings are not aliases: they govern
distinct cancellation, watchdog, venue-balance, recovery, and tail-order
states. Their consolidation must therefore wait for a separate behavior-
equivalence change, rather than deleting values merely because their names are
similar.
