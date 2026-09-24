import time
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from bot.outcome_lead_lag_runtime import OutcomeLeadLagRuntime
from bot.outcome_lead_lag_ingress import record_hyperliquid_btc_probe
from bot.outcome_lead_lag_state import OutcomeLeadLagState, OutcomeLeadLagStateConfig
from bot.outcome_lead_lag_types import ReferenceTick
from bot.outcome_lead_lag_exit_handoff import (
    FastFollowLiveConfig,
    OutcomeFastFollowLive,
    _venue_compatible_fast_follow_quantity,
    fast_follow_l2_precheck,
    handoff_confirmed_candidate,
)
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


def test_state_requires_twap_to_follow_after_outcome_before_live_confirmation():
    state = OutcomeLeadLagState(OutcomeLeadLagStateConfig(
        shock_cents=500, residual_cents=300, debounce_ticks=2,
        baseline_warmup_samples=1, follower_confirm_cents=100,
        follower_confirm_window_ms=5_000,
        max_outcome_return_interval_ms=6_000,
    ))
    state.apply(tick("polymarket_twap", 7_700_000, 0))
    state.apply(tick("outcome_btc_mark", 7_700_000, 0))
    state.apply(tick("polymarket_twap", 7_700_000, 100))
    state.apply(tick("outcome_btc_mark", 7_700_600, 1_000))
    assert state.apply(tick("outcome_btc_mark", 7_701_200, 1_100)).state == "adverse_confirmed"
    assert state.apply(tick("polymarket_twap", 7_700_050, 1_400)).state == "follower_wait"
    confirmed = state.apply(tick("polymarket_twap", 7_700_100, 1_600))
    assert confirmed.state == "follower_confirmed"
    assert confirmed.direction == 1


def test_state_preserves_outcome_debounce_across_frequent_twap_ticks():
    """Outcome is five-second cadence; TWAP ticks between marks must not reset it."""
    state = OutcomeLeadLagState(OutcomeLeadLagStateConfig(
        shock_cents=500, residual_cents=300, debounce_ticks=2,
        baseline_warmup_samples=1, follower_confirm_cents=100,
        max_outcome_return_interval_ms=6_000,
        follower_confirm_window_ms=5_000,
    ))
    state.apply(tick("polymarket_twap", 7_700_000, 0))
    state.apply(tick("outcome_btc_mark", 7_700_000, 100))
    # Establish the one-sample basis before the first shock.
    state.apply(tick("polymarket_twap", 7_700_000, 4_900))
    first = state.apply(tick("outcome_btc_mark", 7_703_000, 5_000))
    assert first.state == "adverse_candidate"
    # These are individually too far from the last Outcome tick to form a
    # new pair.  They must remain fail-closed but retain the debounce state.
    assert state.apply(tick("polymarket_twap", 7_700_000, 6_200)).reason == "waiting_for_outcome_refresh"
    assert state.apply(tick("polymarket_twap", 7_700_000, 9_800)).reason == "waiting_for_outcome_refresh"
    second = state.apply(tick("outcome_btc_mark", 7_706_000, 10_000))
    assert second.state == "adverse_confirmed"
    # The post-arm TWAP move is allowed to use the frozen, already verified
    # follower baseline for the short confirmation window.
    confirmed = state.apply(tick("polymarket_twap", 7_700_100, 10_300))
    assert confirmed.state == "follower_confirmed"


def test_state_auxiliary_references_cannot_clear_outcome_debounce_or_arm():
    state = OutcomeLeadLagState(OutcomeLeadLagStateConfig(
        shock_cents=500, residual_cents=300, debounce_ticks=2,
        baseline_warmup_samples=1, follower_confirm_cents=100,
        max_outcome_return_interval_ms=6_000,
    ))
    state.apply(tick("polymarket_twap", 7_700_000, 0))
    state.apply(tick("outcome_btc_mark", 7_700_000, 100))
    state.apply(tick("polymarket_twap", 7_700_000, 4_900))
    assert state.apply(tick("outcome_btc_mark", 7_703_000, 5_000)).state == "adverse_candidate"
    # These sources remain durable research observations but are not valid
    # settlement followers and must have no execution-state authority.
    assert state.apply(tick("binance", 7_700_000, 5_200)).reason == "non_signal_reference"
    assert state.apply(tick("polymarket_bbo", 7_700_000, 5_300)).reason == "non_signal_reference"
    state.apply(tick("polymarket_twap", 7_700_000, 9_800))
    assert state.apply(tick("outcome_btc_mark", 7_706_000, 10_000)).state == "adverse_confirmed"
    assert state.apply(tick("polymarket_twap", 7_700_100, 10_300)).state == "follower_confirmed"


def test_state_expires_armed_signal_before_late_twap_can_confirm():
    state = OutcomeLeadLagState(OutcomeLeadLagStateConfig(
        shock_cents=500, residual_cents=300, debounce_ticks=1,
        baseline_warmup_samples=1, follower_confirm_cents=100,
        follower_confirm_window_ms=5_000,
    ))
    state.apply(tick("polymarket_twap", 7_700_000, 0))
    state.apply(tick("outcome_btc_mark", 7_700_000, 0))
    state.apply(tick("polymarket_twap", 7_700_000, 900))
    assert state.apply(tick("outcome_btc_mark", 7_700_600, 1_100)).state == "adverse_confirmed"
    late = state.apply(tick("polymarket_twap", 7_700_100, 6_200))
    assert late.state != "follower_confirmed"


def test_state_does_not_arm_fast_follow_when_outcome_return_interval_exceeds_limit():
    state = OutcomeLeadLagState(OutcomeLeadLagStateConfig(
        shock_cents=500, residual_cents=300, debounce_ticks=1,
        baseline_warmup_samples=1, max_outcome_return_interval_ms=2_000,
    ))
    state.apply(tick("polymarket_twap", 7_700_000, 0))
    state.apply(tick("outcome_btc_mark", 7_700_000, 0))
    state.apply(tick("polymarket_twap", 7_700_000, 4_900))

    delayed = state.apply(tick("outcome_btc_mark", 7_700_600, 5_000))

    assert delayed.state == "observe"
    assert delayed.reason == "low_confidence_outcome_interval"
    assert delayed.outcome_interval_ms == 5_000
    assert not state.apply(tick("polymarket_twap", 7_700_100, 5_100)).state == "follower_confirmed"


def test_state_default_interval_limit_accepts_observed_five_second_outcome_cadence():
    state = OutcomeLeadLagState(OutcomeLeadLagStateConfig(
        shock_cents=100, residual_cents=300, debounce_ticks=1,
        baseline_warmup_samples=1,
    ))
    state.apply(tick("polymarket_twap", 7_700_000, 0))
    state.apply(tick("outcome_btc_mark", 7_700_000, 0))
    state.apply(tick("polymarket_twap", 7_700_000, 4_900))

    decision = state.apply(tick("outcome_btc_mark", 7_700_600, 5_000))

    assert decision.state == "adverse_confirmed"
    assert decision.outcome_interval_ms == 5_000


def test_state_fails_closed_for_out_of_order_and_stale_sources():
    state = OutcomeLeadLagState()
    state.apply(tick("outcome_btc_mark", 1_000, 1_000))
    assert state.apply(tick("outcome_btc_mark", 1_001, 999)).reason == "out_of_order"
    state.apply(tick("outcome_btc_mark", 1_100, 5_000))
    # A stale TWAP is ignored fail-closed without destroying valid Outcome
    # persistence; the next Outcome tick will require a fresh pair.
    assert state.apply(tick("polymarket_twap", 1_100, 1_000)).reason == "waiting_for_outcome_refresh"


def test_state_requires_new_warmup_after_outcome_connection_epoch_changes():
    state = OutcomeLeadLagState(OutcomeLeadLagStateConfig(
        shock_cents=100, residual_cents=100, debounce_ticks=1, baseline_warmup_samples=1,
    ))
    state.apply(tick("polymarket_twap", 1_000, 0))
    state.apply(tick("outcome_btc_mark", 1_000, 0))
    state.apply(tick("polymarket_twap", 1_000, 900))
    state.apply(tick("outcome_btc_mark", 1_200, 1_000))

    reconnected = state.apply(ReferenceTick(
        source="outcome_btc_mark", price_cents=1_400,
        received_epoch_ns=1_100_000_000, received_monotonic_ns=1_100_000_000,
        connection_epoch=1,
    ))

    assert reconnected.state == "unavailable"
    assert reconnected.reason == "cross_epoch"


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


def test_hyperliquid_btc_probe_is_persisted_without_publishing_a_signal_tick():
    class DB:
        def __init__(self): self.rows = []
        def enqueue_reference_1s(self, **kwargs): self.rows.append(kwargs)

    strategy = SimpleNamespace(
        lead_lag_db=DB(), run_id="run", current_market_slug="slug",
    )
    record_hyperliquid_btc_probe(
        strategy, source="hyperliquid_btc_bbo", price=Decimal("77500"),
        source_event_ts_ms=123, bid=Decimal("77499"), ask=Decimal("77501"), connection_epoch=2,
    )

    assert strategy.lead_lag_db.rows[0]["source"] == "hyperliquid_btc_bbo"
    assert strategy.lead_lag_db.rows[0]["market_id"] is None
    assert strategy.lead_lag_db.rows[0]["price_cents"] == 7_750_000


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


def test_shadow_also_records_live_follower_confirmation_without_order_authority():
    class DB:
        def __init__(self): self.markouts = []
        def enqueue_decision(self, **_kwargs): pass
        def enqueue_markout(self, **kwargs): self.markouts.append(kwargs)

    db = DB()
    strategy = SimpleNamespace(lead_lag_db=db, active_side="NONE", _polymarket_chainlink_twap_price=0)
    shadow = OutcomeLeadLagShadow(strategy)
    candidate = LeadLagCandidate(
        LeadLagDecision("follower_confirmed", 1, 0, 0, 2, "v", 0, "twap_followed_outcome",
                        follower_price_cents=7_700_000),
        "r", "s", None, 1_000_000_000,
    )

    shadow.record_candidate(candidate)
    shadow.on_tick(tick("polymarket_twap", 7_700_100, 1_400), None)

    assert db.markouts
    assert db.markouts[0]["payload"]["decision"]["state"] == "follower_confirmed"


def _live_harness(*, ask: Decimal, submit_automatically: bool = True):
    now_ts = datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    submitted, order_kwargs, events = [], [], []

    class Factory:
        def limit(self, **kwargs):
            order_kwargs.append(kwargs)
            return SimpleNamespace(client_order_id=kwargs["client_order_id"])

    instrument = SimpleNamespace(size_precision=1, price_precision=2, price_increment=Decimal("0.01"))

    class Level:
        def __init__(self, price, size):
            self.price = price
            self._size = size

        def size(self):
            return self._size

    class Book:
        def asks(self):
            # 20 shares remain visible through the one-tick FOK limit.
            return [Level(ask, Decimal("20"))]

    book = Book()
    strategy = SimpleNamespace(
        current_market_slug="s", current_market_end_timestamp=now_ts + 600,
        current_up_instrument_matched=True, current_down_instrument_matched=True,
        maker_min_minutes_to_close=1, bi_side_min_time_left_sec=60,
        first_entry_max_time_left_sec=720, market_buy_count_total_by_slug={},
        live_inventory_cost={}, active_side=SimpleNamespace(value="NONE"), active_side_locked=False,
        maker_fixed_shares=Decimal("10"), maker_exchange_min_shares=Decimal("5"),
        maker_high_entry_price_size_adjust_threshold=Decimal("0.70"),
        maker_high_entry_price_size_adjust_multiplier=Decimal("0.55"),
        _cached_usdc_balance=Decimal("100"), active_maker_orders={}, order_factory=Factory(),
        trade_db_buy_ready=True,
        trade_db=SimpleNamespace(
            runtime_health=lambda: {"ready": True, "reason": "ready"},
            load_fast_follow_night_risk=lambda _night: {},
        ),
        outcome_bypass_execution_penalty=True,
        _twap_reference_degraded=False,
        _market_strike_is_entry_eligible=lambda _slug: True,
        cache=SimpleNamespace(instrument=lambda _inst: instrument, order_book=lambda _inst: book),
        fast_follow_l2_update_ts_by_inst={"UP.INST": now_ts},
        _instrument_for_side=lambda side: "UP.INST" if getattr(side, "value", str(side)) == "UP" else "DOWN.INST",
        _side_for_instrument_id=lambda inst: SimpleNamespace(
            value="UP" if str(inst) == "UP.INST" else "DOWN" if str(inst) == "DOWN.INST" else "NONE"
        ),
        _instrument_key=lambda inst: str(inst),
        _align_price_to_tick=lambda price, _side, _instrument: price,
        _is_dry_run_mode=lambda: False,
        submit_order=submitted.append,
        _db_strategy_event=lambda event, payload: events.append((event, payload)),
        _db_order_event=lambda **payload: events.append((payload["event_type"], payload)),
        _cancel_maker_order_side=lambda *_args, **_kwargs: None,
    )
    owner = OutcomeFastFollowLive(strategy, FastFollowLiveConfig())
    decision = LeadLagDecision(
        "follower_confirmed", 1, 500, 300, 2, "v3", time.perf_counter_ns(),
        "twap_followed_outcome", follower_price_cents=7_700_100,
    )
    if submit_automatically:
        owner.record_candidate(LeadLagCandidate(decision, "r", "s", 1, time.time_ns()))
        assert owner.on_quote(
            instrument_id="UP.INST", best_bid=ask - Decimal("0.01"),
            best_ask=ask, ask_size=Decimal("100"), now_ts=now_ts,
        )
    return owner, submitted, order_kwargs, events


def test_live_fast_follow_uses_ten_shares_at_or_below_high_price_threshold():
    _owner, submitted, kwargs, _events = _live_harness(ask=Decimal("0.70"))
    assert len(submitted) == 1
    assert float(kwargs[0]["quantity"]) == 10.0
    assert kwargs[0]["time_in_force"].name == "FOK"


def test_live_fast_follow_uses_sellable_five_point_five_shares_above_threshold():
    _owner, submitted, kwargs, events = _live_harness(ask=Decimal("0.71"))
    assert len(submitted) == 1
    assert float(kwargs[0]["quantity"]) == 5.5
    assert any(event == "ORDER_FAST_FOLLOW_SUBMIT" for event, _ in events)


def test_live_fast_follow_persists_explicit_outcome_entry_source():
    owner, _submitted, _kwargs, events = _live_harness(ask=Decimal("0.60"))

    intent_payload = next(
        payload for event, payload in events if event == "ORDER_FAST_FOLLOW_INTENT"
    )
    assert intent_payload["payload"]["entry_source"] == "outcome_fast_follow"
    metadata = next(iter(owner._pending_order_ids.values()))
    assert metadata["entry_source"] == "outcome_fast_follow"


def test_live_fast_follow_records_candidate_to_quote_handoff_once():
    owner, _submitted, _kwargs, events = _live_harness(ask=Decimal("0.60"))
    owner.on_quote(
        instrument_id="UP.INST", best_bid=Decimal("0.59"), best_ask=Decimal("0.60"),
        ask_size=Decimal("100"), now_ts=time.time(),
    )

    handoffs = [payload for event, payload in events if event == "FAST_FOLLOW_QUOTE_HANDOFF"]
    assert len(handoffs) == 1
    assert handoffs[0]["slug"] == "s"


def test_fast_follow_waits_for_signal_side_quote_and_ignores_maker_side_lock():
    owner, submitted, _kwargs, events = _live_harness(ask=Decimal("0.60"), submit_automatically=False)
    owner.strategy.active_side = SimpleNamespace(value="DOWN")
    owner.strategy.active_side_locked = True
    owner.record_candidate(LeadLagCandidate(
        LeadLagDecision("follower_confirmed", 1, 500, 300, 2, "v", time.perf_counter_ns(), "confirmed"),
        "r", "s", 1, time.time_ns(),
    ))
    now_ts = datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()

    # Both tokens are subscribed. A DOWN quote must not consume or veto an UP
    # candidate; wait for the executable quote on the signal's own token.
    assert owner.on_quote(
        instrument_id="DOWN.INST", best_bid=Decimal("0.27"), best_ask=Decimal("0.28"),
        ask_size=Decimal("100"), now_ts=now_ts,
    ) is False
    assert owner._pending is not None
    assert submitted == []
    assert not any(
        event == "FAST_FOLLOW_ENTRY_BLOCKED" and payload["reason"] in {"side_or_price_ineligible", "locked_side_invalidated"}
        for event, payload in events
    )
    assert not any(event == "FAST_FOLLOW_QUOTE_HANDOFF" for event, _payload in events)

    assert owner.on_quote(
        instrument_id="UP.INST", best_bid=Decimal("0.59"), best_ask=Decimal("0.60"),
        ask_size=Decimal("100"), now_ts=now_ts,
    ) is True
    assert len(submitted) == 1
    assert owner._pending is None
    handoff = next(payload for event, payload in events if event == "FAST_FOLLOW_QUOTE_HANDOFF")
    assert handoff["target_side"] == "UP"
    assert handoff["maker_locked_side"] == "DOWN"


def test_fast_follow_side_lock_independence_does_not_override_opposite_inventory_guard():
    owner, submitted, _kwargs, events = _live_harness(ask=Decimal("0.60"), submit_automatically=False)
    owner.strategy.active_side = SimpleNamespace(value="DOWN")
    owner.strategy.active_side_locked = True
    owner.strategy.live_inventory_cost["DOWN.INST"] = {"qty": Decimal("2")}
    owner.record_candidate(LeadLagCandidate(
        LeadLagDecision("follower_confirmed", 1, 500, 300, 2, "v", time.perf_counter_ns(), "confirmed"),
        "r", "s", 1, time.time_ns(),
    ))
    now_ts = datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()

    assert owner.on_quote(
        instrument_id="UP.INST", best_bid=Decimal("0.59"), best_ask=Decimal("0.60"),
        ask_size=Decimal("100"), now_ts=now_ts,
    ) is False
    assert submitted == []
    assert any(
        event == "FAST_FOLLOW_ENTRY_BLOCKED" and payload["reason"] == "conflicting_market_inventory"
        for event, payload in events
    )


def test_fast_follow_requires_explicit_market_token_mapping():
    owner, submitted, _kwargs, events = _live_harness(ask=Decimal("0.60"), submit_automatically=False)
    owner.strategy.current_up_instrument_matched = False
    owner.record_candidate(LeadLagCandidate(
        LeadLagDecision("follower_confirmed", 1, 500, 300, 2, "v", time.perf_counter_ns(), "confirmed"),
        "r", "s", 1, time.time_ns(),
    ))
    now_ts = datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()

    assert owner.on_quote(
        instrument_id="UP.INST", best_bid=Decimal("0.59"), best_ask=Decimal("0.60"),
        ask_size=Decimal("100"), now_ts=now_ts,
    ) is False
    assert submitted == []
    assert any(
        event == "FAST_FOLLOW_ENTRY_BLOCKED" and payload["reason"] == "signal_side_instrument_unavailable"
        for event, payload in events
    )


def test_fast_follow_records_executable_bbo_counterfactual_markouts_for_blocked_signal():
    owner, _submitted, _kwargs, events = _live_harness(ask=Decimal("0.60"), submit_automatically=False)
    owner.strategy.outcome_bypass_execution_penalty = False
    owner.strategy.fast_follow_execution_penalty_allows = lambda **_kwargs: False
    candidate_epoch_ns = time.time_ns()
    owner.record_candidate(LeadLagCandidate(
        LeadLagDecision("follower_confirmed", 1, 500, 300, 2, "v", time.perf_counter_ns(), "confirmed"),
        "r", "s", 1, candidate_epoch_ns,
    ))
    start = datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    for elapsed, bid in ((0.0, "0.59"), (1.1, "0.61"), (5.1, "0.64"), (10.1, "0.55"), (30.1, "0.62")):
        owner.on_quote(
            instrument_id="UP.INST", best_bid=Decimal(bid), best_ask=Decimal("0.65"),
            bid_size=Decimal("80"), ask_size=Decimal("100"), now_ts=start + elapsed,
        )

    marks = [payload for event, payload in events if event == "FAST_FOLLOW_COUNTERFACTUAL_MARKOUT"]
    assert [mark["horizon_sec"] for mark in marks] == [1, 5, 10, 30]
    assert all(mark["entry_ask"] == 0.65 for mark in marks)
    assert [mark["exit_bid"] for mark in marks] == [0.61, 0.64, 0.55, 0.62]
    assert all(mark["execution_basis"] == "top_of_book_gross_no_fees" for mark in marks)
    assert all(mark["matched_top_of_book_quantity"] == 80 for mark in marks)
    assert [mark["gross_top_of_book_pnl_usdc"] for mark in marks] == [-3.2, -0.8, -8.0, -2.4]


def test_fast_follow_l2_precheck_requires_full_fill_and_buffer():
    ok, estimate = fast_follow_l2_precheck(
        asks=[(Decimal("0.60"), Decimal("10"))],
        quantity=Decimal("10"),
        limit_price=Decimal("0.61"),
        depth_buffer=Decimal("1.20"),
    )
    assert not ok
    assert estimate.filled_quantity == Decimal("10")

    ok, estimate = fast_follow_l2_precheck(
        asks=[(Decimal("0.60"), Decimal("12.1"))],
        quantity=Decimal("10"),
        limit_price=Decimal("0.61"),
        depth_buffer=Decimal("1.20"),
    )
    assert ok
    assert estimate.visible_depth == Decimal("12.1")


def test_live_fast_follow_terminal_fok_failure_releases_reservation_without_counting_fill():
    owner, _submitted, kwargs, _events = _live_harness(ask=Decimal("0.60"))
    coid = str(kwargs[0]["client_order_id"])
    night = "2026-09-08"
    assert owner._night_filled_entries[night] == 0
    assert coid in owner._night_pending_entry_ids[night]

    owner.on_order_terminal(coid)

    assert owner._night_filled_entries[night] == 0
    assert coid not in owner._night_pending_entry_ids[night]


def test_fast_follow_signal_pending_does_not_claim_normal_maker_ownership():
    owner, _submitted, _kwargs, _events = _live_harness(ask=Decimal("0.60"), submit_automatically=False)
    owner.record_candidate(LeadLagCandidate(
        LeadLagDecision("follower_confirmed", 1, 500, 300, 2, "v3", time.perf_counter_ns(), "twap_followed_outcome"),
        "r", "s", 1, time.time_ns(),
    ))

    assert owner.blocks_normal_buy("s") is False


def test_fast_follow_submit_exception_rolls_back_local_reservation():
    owner, _submitted, _kwargs, events = _live_harness(ask=Decimal("0.60"), submit_automatically=False)
    owner.strategy.submit_order = lambda _order: (_ for _ in ()).throw(RuntimeError("venue unavailable"))
    decision = LeadLagDecision(
        "follower_confirmed", 1, 500, 300, 2, "v3", time.perf_counter_ns(), "twap_followed_outcome",
    )
    owner.record_candidate(LeadLagCandidate(decision, "r", "s", 1, time.time_ns()))
    now_ts = datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()

    assert owner.on_quote(
        instrument_id="UP.INST", best_bid=Decimal("0.59"), best_ask=Decimal("0.60"),
        ask_size=Decimal("100"), now_ts=now_ts,
    ) is False
    assert owner._pending_order_ids == {}
    assert owner._night_pending_entry_ids["2026-09-08"] == set()
    assert "s" not in owner._attempted_slugs
    assert any(event == "FAST_FOLLOW_ENTRY_BLOCKED" and payload["reason"] == "fast_follow_submit_exception" for event, payload in events)
    assert owner.blocks_normal_buy("s") is False


def test_fast_follow_failures_do_not_leak_across_sequential_maker_markets():
    owner, _submitted, kwargs, _events = _live_harness(ask=Decimal("0.60"), submit_automatically=False)
    maker_submissions = []

    def submit_healthy_maker(slug):
        assert owner.blocks_normal_buy(slug) is False
        maker_submissions.append(slug)

    # Market 1: healthy normal maker path remains available.
    submit_healthy_maker("market-1")

    # Market 2: an Outcome signal is pending but fails before reservation; it
    # must not own the independent maker path.
    owner.strategy.current_market_slug = "market-2"
    decision = LeadLagDecision("follower_confirmed", 1, 500, 300, 2, "v3", time.perf_counter_ns(), "twap_followed_outcome")
    owner.record_candidate(LeadLagCandidate(decision, "r", "market-2", 1, time.time_ns()))
    submit_healthy_maker("market-2")
    owner._pending = None

    # Market 3: no signal state from Market 2 may leak.
    submit_healthy_maker("market-3")

    # Market 4: a real FOK reservation reaches terminal rejection.
    owner.strategy.current_market_slug = "market-4"
    owner.record_candidate(LeadLagCandidate(decision, "r", "market-4", 1, time.time_ns()))
    now_ts = datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    assert owner.on_quote(
        instrument_id="UP.INST", best_bid=Decimal("0.59"), best_ask=Decimal("0.60"),
        ask_size=Decimal("100"), now_ts=now_ts,
    ) is True
    owner.on_order_terminal(str(kwargs[-1]["client_order_id"]))
    assert owner._attempted_slugs == set()
    assert owner._pending_order_ids == {}
    assert owner._night_pending_entry_ids["2026-09-08"] == set()
    assert owner.strategy.recent_buy_submit_by_inst == {}
    assert owner.strategy.market_buy_count_total_by_slug == {}

    # Market 5: terminal FOK cleanup cannot poison a later maker market.
    submit_healthy_maker("market-5")
    assert maker_submissions == ["market-1", "market-2", "market-3", "market-5"]


def test_live_fast_follow_buy_fill_consumes_exactly_one_nightly_slot():
    owner, _submitted, kwargs, _events = _live_harness(ask=Decimal("0.60"))
    coid = str(kwargs[0]["client_order_id"])
    night = "2026-09-08"

    owner.on_fill(client_order_id=coid, side="buy", instrument_id="UP.INST")
    # Duplicate callbacks must not double-count a single FOK order.
    owner.on_fill(client_order_id=coid, side="buy", instrument_id="UP.INST")

    assert owner._night_filled_entries[night] == 1
    assert not owner._night_pending_entry_ids[night]
    assert owner.night_risk_snapshot(
        datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    ) == {
        "night_key": night,
        "filled_entries": 1,
        "pending_entries": 0,
        "max_entries": 15,
        "realized_pnl_usdc": 0.0,
    }


def test_fast_follow_quantity_uses_venue_maker_amount_grid_without_increasing_size():
    # 0.75 * 5.5 = 4.125, which Polymarket rejects because maker amount has
    # three decimals. The nearest safe quantity below the requested size is 5.48.
    quantity, step = _venue_compatible_fast_follow_quantity(Decimal("5.5"), Decimal("0.75"))
    assert step == Decimal("0.04")
    assert quantity == Decimal("5.4800")
    assert quantity * Decimal("0.75") == Decimal("4.110000")


def test_fast_follow_quantity_keeps_valid_quantity_when_maker_amount_is_already_cents_aligned():
    quantity, step = _venue_compatible_fast_follow_quantity(Decimal("5.5"), Decimal("0.72"))
    assert step == Decimal("0.1250")
    assert quantity == Decimal("5.5000")


def test_fast_follow_restores_open_position_ownership_and_credits_sell_after_restart():
    events = []
    strategy = SimpleNamespace(
        trade_db=SimpleNamespace(
            runtime_health=lambda: {"ready": True, "reason": "ready"},
            load_fast_follow_night_risk=lambda _night: {
                "filled_entries": 3,
                "pending_entries": 0,
                "realized_pnl_usdc": Decimal("-0.5"),
                "open_position_instruments": ["UP.INST"],
            },
        ),
        live_inventory_cost={"UP.INST": {"qty": "0"}},
        maker_exchange_min_shares=Decimal("5"),
        _db_strategy_event=lambda event, payload: events.append((event, payload)),
    )
    owner = OutcomeFastFollowLive(strategy, FastFollowLiveConfig())
    owner._ensure_night_loaded("2026-09-08")

    owner.on_fill(
        client_order_id="restored-sell", side="sell", instrument_id="UP.INST",
        realized_net_usdc=Decimal("1.25"),
    )

    snapshot = owner.night_risk_snapshot(
        datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    assert snapshot["filled_entries"] == 3
    assert snapshot["realized_pnl_usdc"] == 0.75
    assert "UP.INST" not in owner._position_instruments
    assert any(event == "FAST_FOLLOW_RISK_STATE" for event, _payload in events)


def test_live_fast_follow_never_buys_when_global_maker_kill_switch_is_on():
    owner, submitted, _kwargs, events = _live_harness(ask=Decimal("0.60"))
    # The first harness signal was submitted; construct a fresh market signal
    # so this assertion tests the global safety gate rather than one-buy-per-market.
    owner.strategy.current_market_slug = "next"
    owner.strategy.maker_kill_switch = True
    decision = LeadLagDecision(
        "follower_confirmed", 1, 500, 300, 2, "v3", time.perf_counter_ns(),
        "twap_followed_outcome", follower_price_cents=7_700_100,
    )
    owner.record_candidate(LeadLagCandidate(decision, "r", "next", 1, time.time_ns()))
    now_ts = datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()

    assert not owner.on_quote(
        instrument_id="UP.INST", best_bid=Decimal("0.59"), best_ask=Decimal("0.60"),
        ask_size=Decimal("100"), now_ts=now_ts,
    )
    assert len(submitted) == 1
    assert any(event == "FAST_FOLLOW_ENTRY_BLOCKED" and payload["reason"] == "maker_kill_switch_on"
               for event, payload in events)


def test_live_fast_follow_never_buys_when_trade_journal_is_not_ready():
    owner, submitted, _kwargs, events = _live_harness(ask=Decimal("0.60"))
    owner.strategy.current_market_slug = "next"
    owner.strategy.trade_db_buy_ready = False
    decision = LeadLagDecision(
        "follower_confirmed", 1, 500, 300, 2, "v3", time.perf_counter_ns(),
        "twap_followed_outcome", follower_price_cents=7_700_100,
    )
    owner.record_candidate(LeadLagCandidate(decision, "r", "next", 1, time.time_ns()))
    now_ts = datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()

    assert not owner.on_quote(
        instrument_id="UP.INST", best_bid=Decimal("0.59"), best_ask=Decimal("0.60"),
        ask_size=Decimal("100"), now_ts=now_ts,
    )
    assert len(submitted) == 1
    assert any(event == "FAST_FOLLOW_ENTRY_BLOCKED" and payload["reason"] == "trade_journal_unhealthy"
               for event, payload in events)


def test_live_fast_follow_cached_night_does_not_bypass_runtime_journal_failure():
    owner, submitted, _kwargs, events = _live_harness(ask=Decimal("0.60"))
    night = "2026-09-08"
    assert night in owner._loaded_nights
    owner.strategy.current_market_slug = "next"
    owner.strategy.trade_db = SimpleNamespace(
        runtime_health=lambda: {"ready": False, "reason": "write_failed"},
        load_fast_follow_night_risk=lambda _night: {},
    )
    decision = LeadLagDecision(
        "follower_confirmed", 1, 500, 300, 2, "v3", time.perf_counter_ns(),
        "twap_followed_outcome", follower_price_cents=7_700_100,
    )
    owner.record_candidate(LeadLagCandidate(decision, "r", "next", 1, time.time_ns()))
    now_ts = datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()

    assert not owner.on_quote(
        instrument_id="UP.INST", best_bid=Decimal("0.59"), best_ask=Decimal("0.60"),
        ask_size=Decimal("100"), now_ts=now_ts,
    )
    assert len(submitted) == 1
    assert owner.strategy.trade_db_buy_ready is True
    assert any(event == "FAST_FOLLOW_ENTRY_BLOCKED" and payload["reason"] == "trade_journal_unhealthy"
               for event, payload in events)


def test_live_fast_follow_aborts_and_rolls_back_when_risk_state_persistence_fails():
    owner, submitted, kwargs, events = _live_harness(ask=Decimal("0.60"))
    owner.on_order_terminal(str(kwargs[0]["client_order_id"]))
    health = {"ready": True, "reason": "ready"}

    def persist_event(event, payload):
        events.append((event, payload))
        if event == "FAST_FOLLOW_RISK_STATE":
            health.update(ready=False, reason="write_failed")

    owner.strategy._db_strategy_event = persist_event
    owner.strategy.trade_db = SimpleNamespace(
        runtime_health=lambda: dict(health),
        load_fast_follow_night_risk=lambda _night: {},
    )
    owner.strategy.current_market_slug = "next"
    decision = LeadLagDecision(
        "follower_confirmed", 1, 500, 300, 2, "v3", time.perf_counter_ns(),
        "twap_followed_outcome", follower_price_cents=7_700_100,
    )
    owner.record_candidate(LeadLagCandidate(decision, "r", "next", 1, time.time_ns()))
    now_ts = datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()

    assert not owner.on_quote(
        instrument_id="UP.INST", best_bid=Decimal("0.59"), best_ask=Decimal("0.60"),
        ask_size=Decimal("100"), now_ts=now_ts,
    )
    assert len(submitted) == 1
    assert not owner._pending_order_ids
    assert not owner._night_pending_entry_ids["2026-09-08"]
    assert "next" not in owner._attempted_slugs
    assert owner._night_filled_entries["2026-09-08"] == 0
    assert any(event == "FAST_FOLLOW_ENTRY_BLOCKED" and payload["reason"] == "risk_state_persist_failed"
               for event, payload in events)


def test_live_fast_follow_intent_persist_failure_aborts_and_rolls_back_before_submit():
    owner, submitted, kwargs, events = _live_harness(ask=Decimal("0.60"))
    owner.on_order_terminal(str(kwargs[0]["client_order_id"]))
    health = {"ready": True, "reason": "ready"}

    def order_event(**payload):
        events.append((payload["event_type"], payload))
        if payload["event_type"] == "ORDER_FAST_FOLLOW_INTENT":
            health.update(ready=False, reason="write_failed")

    owner.strategy._db_order_event = order_event
    owner.strategy.trade_db = SimpleNamespace(
        runtime_health=lambda: dict(health),
        load_fast_follow_night_risk=lambda _night: {},
    )
    owner.strategy.current_market_slug = "next"
    decision = LeadLagDecision(
        "follower_confirmed", 1, 500, 300, 2, "v3", time.perf_counter_ns(),
        "twap_followed_outcome", follower_price_cents=7_700_100,
    )
    owner.record_candidate(LeadLagCandidate(decision, "r", "next", 1, time.time_ns()))
    now_ts = datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()

    assert not owner.on_quote(
        instrument_id="UP.INST", best_bid=Decimal("0.59"), best_ask=Decimal("0.60"),
        ask_size=Decimal("100"), now_ts=now_ts,
    )
    assert len(submitted) == 1
    assert not owner._pending_order_ids
    assert not owner._night_pending_entry_ids["2026-09-08"]
    assert "next" not in owner._attempted_slugs
    assert owner._night_filled_entries["2026-09-08"] == 0
    assert any(event == "FAST_FOLLOW_ENTRY_BLOCKED" and payload["reason"] == "fast_follow_intent_persist_failed"
               for event, payload in events)


def test_live_fast_follow_durably_records_intent_before_venue_submit():
    owner, submitted, kwargs, events = _live_harness(ask=Decimal("0.60"))
    owner.on_order_terminal(str(kwargs[0]["client_order_id"]))
    sequence = []
    owner.strategy._db_strategy_event = lambda event, payload: (sequence.append(event), events.append((event, payload)))
    owner.strategy._db_order_event = lambda **payload: (sequence.append(payload["event_type"]), events.append((payload["event_type"], payload)))
    owner.strategy.submit_order = lambda order: (sequence.append("submit_order"), submitted.append(order))
    owner.strategy.current_market_slug = "next"
    decision = LeadLagDecision(
        "follower_confirmed", 1, 500, 300, 2, "v3", time.perf_counter_ns(),
        "twap_followed_outcome", follower_price_cents=7_700_100,
    )
    owner.record_candidate(LeadLagCandidate(decision, "r", "next", 1, time.time_ns()))
    now_ts = datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()

    assert owner.on_quote(
        instrument_id="UP.INST", best_bid=Decimal("0.59"), best_ask=Decimal("0.60"),
        ask_size=Decimal("100"), now_ts=now_ts,
    )
    assert sequence.index("FAST_FOLLOW_RISK_STATE") < sequence.index("ORDER_FAST_FOLLOW_INTENT")
    assert sequence.index("ORDER_FAST_FOLLOW_INTENT") < sequence.index("submit_order")
    assert sequence.index("submit_order") < sequence.index("ORDER_FAST_FOLLOW_SUBMIT")


@pytest.mark.parametrize("health", [None, {}, [], {"reason": "ready_missing"}])
def test_live_fast_follow_malformed_runtime_health_fails_closed(health):
    strategy = SimpleNamespace(
        trade_db=SimpleNamespace(runtime_health=lambda: health),
        trade_db_buy_ready=True,
    )
    owner = OutcomeFastFollowLive(strategy, FastFollowLiveConfig())

    assert owner._runtime_journal_ready() is False
    assert strategy.trade_db_buy_ready is True
    assert strategy.trade_db_health_reason == "runtime_health_read_failed"


def test_live_fast_follow_accepts_explicit_ready_runtime_health():
    strategy = SimpleNamespace(
        trade_db=SimpleNamespace(runtime_health=lambda: {"ready": True, "reason": "ready"}),
        trade_db_buy_ready=True,
    )
    assert OutcomeFastFollowLive(strategy, FastFollowLiveConfig())._runtime_journal_ready() is True


def test_live_fast_follow_applies_execution_penalty_check_by_default_when_bypass_is_disabled():
    owner, submitted, kwargs, _events = _live_harness(ask=Decimal("0.60"))
    owner.strategy.current_market_slug = "next"
    owner.strategy.outcome_bypass_execution_penalty = False
    checks = []
    owner.strategy.fast_follow_execution_penalty_allows = lambda **payload: checks.append(payload) or True
    decision = LeadLagDecision(
        "follower_confirmed", 1, 500, 300, 2, "v3", time.perf_counter_ns(),
        "twap_followed_outcome", follower_price_cents=7_700_100,
    )
    owner.record_candidate(LeadLagCandidate(decision, "r", "next", 1, time.time_ns()))
    now_ts = datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()

    assert owner.on_quote(
        instrument_id="UP.INST", best_bid=Decimal("0.59"), best_ask=Decimal("0.60"),
        ask_size=Decimal("100"), now_ts=now_ts,
    )
    assert len(submitted) == 2
    assert checks and checks[0]["limit_price"] == Decimal("0.61")
    assert kwargs[-1]["time_in_force"].name == "FOK"
    assert owner.order_metadata(str(kwargs[-1]["client_order_id"]))["execution_penalty_bypassed"] is False


def test_entry_only_fast_follow_never_sells_an_opposite_existing_position():
    now_ts = datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    exits = []
    strategy = SimpleNamespace(
        current_market_slug="s", market_buy_count_total_by_slug={},
        live_inventory_cost={"DOWN.INST": {"qty": "5.5", "avg_entry_price": "0.50"}},
        maker_exchange_min_shares=Decimal("5"), active_maker_orders={},
        taker_exit_stop_loss_max_spread_pct=Decimal("0.03"),
        _side_for_instrument_id=lambda _inst: SimpleNamespace(value="DOWN"),
        _instrument_key=lambda inst: str(inst),
        _get_effective_sellable_qty=lambda **_kwargs: Decimal("5.5"),
        _infer_market_fee_rate_default=lambda: Decimal("0"),
        _submit_taker_exit_order=lambda **kwargs: exits.append(kwargs) or True,
        _cancel_maker_order_side=lambda *_args, **_kwargs: None,
        _db_strategy_event=lambda *_args, **_kwargs: None,
    )
    owner = OutcomeFastFollowLive(strategy, FastFollowLiveConfig())
    decision = LeadLagDecision(
        "follower_confirmed", 1, 500, 300, 2, "v3", time.perf_counter_ns(),
        "twap_followed_outcome", follower_price_cents=7_700_100,
    )
    owner.record_candidate(LeadLagCandidate(decision, "r", "s", 1, time.time_ns()))
    assert not owner.on_quote(
        instrument_id="DOWN.INST", best_bid=Decimal("0.59"),
        best_ask=Decimal("0.60"), ask_size=Decimal("100"), now_ts=now_ts,
    )
    assert not exits


def test_entry_only_fast_follow_has_no_reversal_implementation():
    assert not hasattr(OutcomeFastFollowLive, "_try_reversal_exit")


def test_entry_only_fast_follow_never_cancels_an_existing_order_owner():
    now_ts = datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    submitted, cancelled, events = [], [], []
    instrument = SimpleNamespace(size_precision=1, price_precision=2, price_increment=Decimal("0.01"))
    strategy = SimpleNamespace(
        current_market_slug="s", current_market_end_timestamp=now_ts + 600,
        maker_min_minutes_to_close=1, bi_side_min_time_left_sec=60,
        first_entry_max_time_left_sec=720, market_buy_count_total_by_slug={},
        live_inventory_cost={}, active_side=SimpleNamespace(value="NONE"), active_side_locked=False,
        maker_fixed_shares=Decimal("10"), maker_exchange_min_shares=Decimal("5"),
        maker_high_entry_price_size_adjust_threshold=Decimal("0.70"),
        maker_high_entry_price_size_adjust_multiplier=Decimal("0.55"), _cached_usdc_balance=Decimal("100"),
        active_maker_orders={"normal-buy": {"side": "buy", "instrument_id": "UP.INST"}},
        _twap_reference_degraded=False, _market_strike_is_entry_eligible=lambda _slug: True,
        cache=SimpleNamespace(instrument=lambda _inst: instrument),
        _side_for_instrument_id=lambda _inst: SimpleNamespace(value="UP"),
        _instrument_key=lambda inst: str(inst), _align_price_to_tick=lambda price, *_args: price,
        _is_dry_run_mode=lambda: False, submit_order=submitted.append,
        _cancel_maker_order_side=lambda *args, **kwargs: cancelled.append((args, kwargs)),
        _db_strategy_event=lambda event, payload: events.append((event, payload)),
        _db_order_event=lambda **_kwargs: None,
    )
    decision = LeadLagDecision("follower_confirmed", 1, 500, 300, 2, "v3", time.perf_counter_ns(), "twap_followed_outcome")
    owner = OutcomeFastFollowLive(strategy, FastFollowLiveConfig())
    owner.record_candidate(LeadLagCandidate(decision, "r", "s", 1, time.time_ns()))
    assert not owner.on_quote(
        instrument_id="UP.INST", best_bid=Decimal("0.59"), best_ask=Decimal("0.60"),
        ask_size=Decimal("100"), now_ts=now_ts,
    )
    assert not submitted
    assert not cancelled
    assert any(event == "FAST_FOLLOW_ENTRY_BLOCKED" for event, _ in events)
