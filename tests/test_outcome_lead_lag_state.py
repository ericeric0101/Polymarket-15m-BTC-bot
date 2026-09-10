import time
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bot.outcome_lead_lag_runtime import OutcomeLeadLagRuntime
from bot.outcome_lead_lag_state import OutcomeLeadLagState, OutcomeLeadLagStateConfig
from bot.outcome_lead_lag_types import ReferenceTick
from bot.outcome_lead_lag_exit_handoff import (
    FastFollowLiveConfig,
    OutcomeFastFollowLive,
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
        follower_confirm_window_ms=5_000,
    ))
    state.apply(tick("polymarket_twap", 7_700_000, 0))
    state.apply(tick("outcome_btc_mark", 7_700_000, 100))
    # Establish the one-sample basis before the first shock.
    state.apply(tick("polymarket_twap", 7_700_000, 4_900))
    first = state.apply(tick("outcome_btc_mark", 7_700_600, 5_000))
    assert first.state == "adverse_candidate"
    # These are individually too far from the last Outcome tick to form a
    # new pair.  They must remain fail-closed but retain the debounce state.
    assert state.apply(tick("polymarket_twap", 7_700_000, 6_200)).reason == "waiting_for_outcome_refresh"
    assert state.apply(tick("polymarket_twap", 7_700_000, 9_800)).reason == "waiting_for_outcome_refresh"
    second = state.apply(tick("outcome_btc_mark", 7_701_200, 10_000))
    assert second.state == "adverse_confirmed"
    # The post-arm TWAP move is allowed to use the frozen, already verified
    # follower baseline for the short confirmation window.
    confirmed = state.apply(tick("polymarket_twap", 7_700_100, 10_300))
    assert confirmed.state == "follower_confirmed"


def test_state_auxiliary_references_cannot_clear_outcome_debounce_or_arm():
    state = OutcomeLeadLagState(OutcomeLeadLagStateConfig(
        shock_cents=500, residual_cents=300, debounce_ticks=2,
        baseline_warmup_samples=1, follower_confirm_cents=100,
    ))
    state.apply(tick("polymarket_twap", 7_700_000, 0))
    state.apply(tick("outcome_btc_mark", 7_700_000, 100))
    state.apply(tick("polymarket_twap", 7_700_000, 4_900))
    assert state.apply(tick("outcome_btc_mark", 7_700_600, 5_000)).state == "adverse_candidate"
    # These sources remain durable research observations but are not valid
    # settlement followers and must have no execution-state authority.
    assert state.apply(tick("binance", 7_700_000, 5_200)).reason == "non_signal_reference"
    assert state.apply(tick("polymarket_bbo", 7_700_000, 5_300)).reason == "non_signal_reference"
    state.apply(tick("polymarket_twap", 7_700_000, 9_800))
    assert state.apply(tick("outcome_btc_mark", 7_701_200, 10_000)).state == "adverse_confirmed"
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


def test_state_fails_closed_for_out_of_order_and_stale_sources():
    state = OutcomeLeadLagState()
    state.apply(tick("outcome_btc_mark", 1_000, 1_000))
    assert state.apply(tick("outcome_btc_mark", 1_001, 999)).reason == "out_of_order"
    state.apply(tick("outcome_btc_mark", 1_100, 5_000))
    # A stale TWAP is ignored fail-closed without destroying valid Outcome
    # persistence; the next Outcome tick will require a fresh pair.
    assert state.apply(tick("polymarket_twap", 1_100, 1_000)).reason == "waiting_for_outcome_refresh"


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


def _live_harness(*, ask: Decimal):
    now_ts = datetime(2026, 9, 8, 21, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    submitted, order_kwargs, events = [], [], []

    class Factory:
        def limit(self, **kwargs):
            order_kwargs.append(kwargs)
            return SimpleNamespace(client_order_id=kwargs["client_order_id"])

    instrument = SimpleNamespace(size_precision=1, price_precision=2, price_increment=Decimal("0.01"))
    strategy = SimpleNamespace(
        current_market_slug="s", current_market_end_timestamp=now_ts + 600,
        maker_min_minutes_to_close=1, bi_side_min_time_left_sec=60,
        first_entry_max_time_left_sec=720, market_buy_count_total_by_slug={},
        live_inventory_cost={}, active_side=SimpleNamespace(value="NONE"), active_side_locked=False,
        maker_fixed_shares=Decimal("10"), maker_exchange_min_shares=Decimal("5"),
        maker_high_entry_price_size_adjust_threshold=Decimal("0.70"),
        maker_high_entry_price_size_adjust_multiplier=Decimal("0.55"),
        _cached_usdc_balance=Decimal("100"), active_maker_orders={}, order_factory=Factory(),
        _twap_reference_degraded=False,
        _market_strike_is_entry_eligible=lambda _slug: True,
        cache=SimpleNamespace(instrument=lambda _inst: instrument),
        _side_for_instrument_id=lambda _inst: SimpleNamespace(value="UP"),
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


def test_live_fast_follow_terminal_fok_failure_releases_reservation_without_counting_fill():
    owner, _submitted, kwargs, _events = _live_harness(ask=Decimal("0.60"))
    coid = str(kwargs[0]["client_order_id"])
    night = "2026-09-08"
    assert owner._night_filled_entries[night] == 0
    assert coid in owner._night_pending_entry_ids[night]

    owner.on_order_terminal(coid)

    assert owner._night_filled_entries[night] == 0
    assert coid not in owner._night_pending_entry_ids[night]


def test_live_fast_follow_buy_fill_consumes_exactly_one_nightly_slot():
    owner, _submitted, kwargs, _events = _live_harness(ask=Decimal("0.60"))
    coid = str(kwargs[0]["client_order_id"])
    night = "2026-09-08"

    owner.on_fill(client_order_id=coid, side="buy", instrument_id="UP.INST")
    # Duplicate callbacks must not double-count a single FOK order.
    owner.on_fill(client_order_id=coid, side="buy", instrument_id="UP.INST")

    assert owner._night_filled_entries[night] == 1
    assert not owner._night_pending_entry_ids[night]


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
