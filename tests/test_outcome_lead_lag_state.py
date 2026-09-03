import time

from bot.outcome_lead_lag_runtime import OutcomeLeadLagRuntime
from bot.outcome_lead_lag_state import OutcomeLeadLagState, OutcomeLeadLagStateConfig
from bot.outcome_lead_lag_types import ReferenceTick
from bot.outcome_lead_lag_exit_handoff import handoff_confirmed_candidate


def tick(source, price, ms):
    return ReferenceTick(source=source, price_cents=price, received_epoch_ns=ms * 1_000_000, received_monotonic_ns=ms * 1_000_000)


def test_state_requires_fresh_twap_then_confirms_untranslated_outcome_shock():
    state = OutcomeLeadLagState(OutcomeLeadLagStateConfig(shock_cents=500, residual_cents=300, debounce_ticks=2))
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
    runtime = OutcomeLeadLagRuntime(config=OutcomeLeadLagStateConfig(shock_cents=1, residual_cents=1, debounce_ticks=1), db=db, candidate_handler=candidates.append)
    runtime.start()
    runtime.publish(tick("polymarket_twap", 1_000, 0))
    runtime.publish(tick("outcome_btc_mark", 1_000, 0))
    runtime.publish(tick("outcome_btc_mark", 1_010, 1_000))
    time.sleep(0.05)
    runtime.stop()
    assert candidates
    assert any(kind == "decision" for kind, _ in db.rows)
    assert handoff_confirmed_candidate(object()) is False
