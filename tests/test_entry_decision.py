import pytest

from bot.entry_decision import EntryDecision, classify_entry_decision_layer


def test_entry_decision_observation_maps_existing_reason_without_evaluating_it():
    decision = EntryDecision.observe(
        slug="btc-updown-15m-test",
        instrument_id="inst-up",
        side="buy",
        should_quote=False,
        reason="econ_gate robust_net=-0.03",
        source_event_type="ORDER_OBSERVE_BUY_BLOCKED",
        fair=0.61,
        entry_price=0.60,
    )

    assert decision.state == "REJECT"
    assert decision.layer == "economics"
    assert decision.fair_minus_entry == pytest.approx(0.01)
    assert decision.to_payload()["final_reason"] == "econ_gate robust_net=-0.03"


def test_entry_decision_marks_fair_edge_research_as_shadow():
    decision = EntryDecision.observe(
        slug="btc-updown-15m-test",
        instrument_id="inst-up",
        side="buy",
        should_quote=True,
        reason="fair_edge_bucket:neg_0_02_to_0",
        shadow_only=True,
    )

    assert decision.state == "SHADOW"
    assert decision.layer == "model_consistency"
    assert classify_entry_decision_layer(
        reason="twap_reference_degraded",
        event_type="ORDER_SKIP_TWAP_REFERENCE_DEGRADED",
        shadow_only=False,
    ) == "hard_safety"
