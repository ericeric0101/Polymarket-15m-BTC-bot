# Outcome Fast Follow retirement — Phase A

Historical Phase A checkpoint. Runtime retention described below is superseded by
[Phase B](outcome_decommission_phase_b.md); the observer and Fast Follow runtime
are now removed. This document preserves the earlier engineering rationale.

Outcome / Hyperliquid ingestion, observational lead-lag research, shared LeadLagDB
writers and historical databases remain. This change requires a subsequent manual
runtime restart to take effect; it does not alter an already running process.

## Authority map

Previously `initialize_strategy_settings` created `OutcomeFastFollowLive` for
`live_entry_only`. OutcomeLeadLagRuntime delivered confirmed candidates to that
owner and the shadow observer. `handle_quote_tick` delivered quotes to `on_quote`,
which checked signal confirmation, eligibility, L2/FOK depth, execution economics
and session-specific limits, persisted reservations/intents and submitted FOK BUY.
`blocks_normal_buy` blocked the maker path after a durable entry intent; order
callbacks released reservations or updated filled-entry/PnL accounting.

Now settings maps legacy `live_entry_only` to effective `shadow` and never creates
an execution owner. Confirmed candidates and strategy ticks remain observational.
QuoteTick execution dispatch and the maker Fast Follow ownership gate are removed.
`FAST_FOLLOW_EXECUTION_ENABLED = False` additionally guards the retained compatibility
class, including candidates, order/fill callbacks, reservations and quote submission.
There is no environment/config switch to enable it. Legacy execution code and tests
remain for Phase B; only historical tests explicitly override the constant.

## Shared maker depth authority

`handle_order_book_deltas` updates the sole `l2_update_ts_by_inst` map. Maker pricing
reads this map for native book freshness. Instrument keys, wall-clock timestamp,
`max(0.1, quote_max_delivery_delay_sec)` threshold and stale-book rejection are
unchanged. The retained Fast Follow compatibility reader uses the same neutral map.
No duplicate timestamps or subscriptions are introduced.

## Diagnostics and scope

STATUS reports `fast_follow_exec=disabled`. The durable run manifest records
`fast_follow_execution_enabled=false` and `effective_outcome_lead_lag_mode`, while
preserving requested configuration provenance. Outcome telemetry remains.

`_record_blocked` uses `block_reason` as its argument name, retaining the canonical
reason and moving any economics payload `reason` to `reason_detail`. The real
historical economics-rejection path and the disabled path are covered by tests.

The intentional behavior change is removal of Fast Follow entry authority and its
maker ownership veto. Maker pricing, thresholds, sizing, independent risk/PnL
checks, flip/reversal invalidation, exits, prediction capture, rollover, freshness
semantics and storage schemas/policies are unchanged. No historical DB is removed.
