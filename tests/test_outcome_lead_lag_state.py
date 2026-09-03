import time
from types import SimpleNamespace

from bot.outcome_lead_lag_runtime import OutcomeLeadLagRuntime
from bot.outcome_lead_lag_state import OutcomeLeadLagState, OutcomeLeadLagStateConfig
from bot.outcome_lead_lag_types import ReferenceTick
from bot.outcome_lead_lag_exit_handoff import handoff_confirmed_candidate
from bot.outcome_lead_lag_shadow import OutcomeLeadLagShadow
from bot.outcome_lead_lag_types import LeadLagCandidate, LeadLagDecision


def tick(source, price, ms):
    return ReferenceTick(source=source, price_cents=price, received_epoch_ns=ms * 1_000_000, received_monotonic_ns=ms * 1_000_000)


def test_state_requires_fresh_twap_then_confirms_untranslated_outcome_shock():
    state = OutcomeLeadLagState(OutcomeLeadLagStateConfig(
        shock_cents=500, residual_cents=300, debounce_ticks=2,
        baseline_warmup_samples=1,
    ))
    assert state.apply(tick("polymarket_twap", 7_700_000, 0)).state == "unavailable"
    state.apply(tick("outcome_btc_mark", 7_700_000, 0))
    state.apply(tick("polymarket_twap", 7_700_000, 100))
    first = state.apply(tick("outcome_btc_mark", 7_700_600, 1_000))
    second = state.apply(tick("outcome_btc_mark", 7_701_200, 1_100))
    assert first.state == "adverse_candidate"
    assert second.state == "adverse_confirmed"
    assert second.direction == 1


def test_state_fails_closed_for_out_of_order_and_stale_sources():
    state = OutcomeLeadLagState()
    state.apply(tick("outcome_btc_mark", 1_000, 1_000))
    assert state.apply(tick("outcome_btc_mark", 1_001, 999)).reason == "out_of_order"
    state.apply(tick("outcome_btc_mark", 1_100, 5_000))
    assert state.apply(tick("polymarket_twap", 1_100, 1_000)).reason == "stale_reference"


def test_runtime_records_only_shadow_candidates_and_handoff_is_disabled():
    class DB:
        def __init__(self): self.rows = []
        def enqueue_reference_1s(self, **kwargs): self.rows.append(("ref", kwargs))
        def enqueue_decision(self, **kwargs): self.rows.append(("decision", kwargs))
    db, candidates = DB(), []
    runtime = OutcomeLeadLagRuntime(config=OutcomeLeadLagStateConfig(
        shock_cents=1, residual_cents=1, debounce_ticks=1, baseline_warmup_samples=1,
    ), db=db, candidate_handler=candidates.append)
    runtime.start()
    runtime.publish(tick("polymarket_twap", 1_000, 0))
    runtime.publish(tick("outcome_btc_mark", 1_000, 0))
    runtime.publish(tick("outcome_btc_mark", 1_010, 1_000))
    time.sleep(0.05)
    runtime.stop()
    assert candidates
    assert any(kind == "decision" for kind, _ in db.rows)
    assert handoff_confirmed_candidate(object()) is False


def test_state_calibrates_static_cross_venue_basis_before_scoring_shock():
    state = OutcomeLeadLagState(OutcomeLeadLagStateConfig(
        shock_cents=500, residual_cents=300, debounce_ticks=1,
        baseline_window_samples=8, baseline_warmup_samples=2,
    ))
    # Outcome can be $50 below TWAP without that static venue basis itself
    # becoming an adverse signal.
    state.apply(tick("outcome_btc_mark", 7_695_000, 0))
    state.apply(tick("polymarket_twap", 7_700_000, 100))
    state.apply(tick("outcome_btc_mark", 7_695_000, 1_000))
    state.apply(tick("polymarket_twap", 7_700_000, 1_100))
    settled = state.apply(tick("outcome_btc_mark", 7_695_000, 2_000))
    assert settled.reason == "no_untranslated_shock"
    state.apply(tick("polymarket_twap", 7_700_000, 2_100))
    moved = state.apply(tick("outcome_btc_mark", 7_695_600, 3_000))
    assert moved.state == "adverse_confirmed"
    assert moved.raw_residual_cents == -4_400
    assert moved.baseline_cents == -5_000
    assert moved.residual_cents == 600


def test_runtime_emits_one_candidate_until_signal_rearms():
    class DB:
        def enqueue_reference_1s(self, **_kwargs): pass
        def enqueue_decision(self, **_kwargs): pass

    candidates = []
    runtime = OutcomeLeadLagRuntime(
        config=OutcomeLeadLagStateConfig(
            shock_cents=1, residual_cents=1, debounce_ticks=1, baseline_warmup_samples=1,
        ), db=DB(), candidate_handler=candidates.append,
    )
    runtime.start()
    for ms, price in ((0, 1_000), (1_000, 1_010), (2_000, 1_020), (3_000, 1_030)):
        runtime.publish(tick("polymarket_twap", 1_000, ms))
        runtime.publish(tick("outcome_btc_mark", price, ms + 100))
    time.sleep(0.05)
    runtime.stop()
    assert len(candidates) == 1


def test_shadow_records_actual_markout_timing_and_late_quality_flag():
    class DB:
        def __init__(self): self.markouts = []
        def enqueue_decision(self, **_kwargs): pass
        def enqueue_markout(self, **kwargs): self.markouts.append(kwargs)

    db = DB()
    strategy = SimpleNamespace(lead_lag_db=db, active_side="UP", _polymarket_chainlink_twap_price=0)
    shadow = OutcomeLeadLagShadow(strategy, max_markout_delay_ms=100)
    decision = LeadLagDecision(
        "adverse_confirmed", -1, -500, -500, 1, "v", 0, "test",
        follower_price_cents=7_700_000,
    )
    candidate = LeadLagCandidate(decision, "r", "s", None, 1_000_000_000)
    shadow.record_candidate(candidate)
    shadow.on_tick(tick("polymarket_twap", 7_699_900, 1_400), None)
    row = next(item for item in db.markouts if item["horizon_ms"] == 250)
    assert row["payload"]["observed_elapsed_ms"] == 400
    assert row["payload"]["observation_delay_ms"] == 150
    assert row["payload"]["timely"] is False
