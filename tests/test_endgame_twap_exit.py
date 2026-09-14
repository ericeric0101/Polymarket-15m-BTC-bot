from decimal import Decimal
import asyncio
import time

from bot.endgame_twap_exit import evaluate_endgame_twap_exit
from bot.enums import ActiveSide
from bot.taker_exit import TakerExitMixin


class _ExitPolicy:
    def stage(self, _time_left_sec):
        return "unused_by_endgame"


def _decision(**overrides):
    payload = {
        "enabled": True,
        "time_left_sec": 120.0,
        "max_time_left_sec": 120.0,
        "twap_price": Decimal("9990"),
        "twap_source": "polymarket_chainlink_twap_60s_ws",
        "twap_age_sec": 1.0,
        "max_twap_age_sec": 5.0,
        "strike": Decimal("10000"),
        "strike_verified": True,
        "held_side": "UP",
        "min_distance_usd": Decimal("10"),
    }
    payload.update(overrides)
    return evaluate_endgame_twap_exit(**payload)


def test_endgame_twap_exit_triggers_at_exact_120_seconds_and_ten_dollars_adverse():
    decision = _decision()
    assert decision.eligible is True
    assert decision.reason == "adverse_twap_endgame"
    assert decision.twap_minus_strike == Decimal("-10")


def test_endgame_twap_exit_never_exits_a_position_confirmed_by_twap():
    decision = _decision(held_side="DOWN")
    assert decision.eligible is False
    assert decision.reason == "twap_confirms_position"


def test_endgame_twap_exit_fails_closed_for_stale_or_unverified_inputs():
    assert _decision(twap_age_sec=5.01).reason == "twap_stale"
    assert _decision(strike_verified=False).reason == "strike_unverified"
    assert _decision(time_left_sec=120.01).reason == "outside_time_window"
    assert _decision(twap_price=Decimal("9990.01")).reason == "distance_below_minimum"


def test_endgame_twap_exit_uses_up_on_equal_or_above_strike_convention():
    decision = _decision(twap_price=Decimal("10010"), held_side="DOWN")
    assert decision.eligible is True
    assert decision.twap_minus_strike == Decimal("10")


class _EndgameHost(TakerExitMixin):
    def __init__(self):
        now = time.time()
        self.taker_exit_enabled = True
        self.hold_to_redeem_enabled = True
        self.taker_exit_only_after_invalidation = True
        self.taker_exit_cooldown_sec = 0
        self.taker_exit_eval_interval_sec = 999.0
        self.taker_exit_last_eval_ts_by_inst = {"up": now}
        self.taker_exit_reject_cooldown_until_by_inst = {}
        self.taker_exit_tail_attempted_by_inst = {}
        self.pending_taker_exit_by_inst = {}
        self.last_taker_exit_ts_by_inst = {}
        self.current_market_end_timestamp = now + 120.0
        self.exit_policy = _ExitPolicy()
        self.current_market_slug = "btc-updown-15m-test"
        self.market_strike_cache_by_slug = {self.current_market_slug: Decimal("10000")}
        self.maker_reduce_only_no_new_sell_last_sec = 0
        self.taker_exit_disable_stop_loss_last_sec = 45
        self.live_inventory_cost = {"up": {"qty": "10", "avg_entry_price": "0.60"}}
        self.endgame_twap_exit_enabled = True
        self.endgame_twap_exit_max_time_left_sec = 120
        self.endgame_twap_exit_min_distance_usd = Decimal("10")
        self.endgame_twap_exit_max_age_sec = 5.0
        self.latest_external_spot = Decimal("9990")
        self.latest_external_spot_source = "polymarket_chainlink_twap_60s_ws"
        self.latest_external_spot_source_ts = now
        self.maker_exchange_min_shares = Decimal("5")
        self.events = []
        self.submissions = []

    def _maker_quote_instruments(self): return ["up"]
    def _instrument_key(self, instrument_id): return str(instrument_id)
    def _normalize_instrument_id(self, instrument_id): return instrument_id
    def _get_quote_for_instrument(self, instrument_id): return Decimal("0.20"), Decimal("0.21")
    def _side_for_instrument_id(self, instrument_id): return ActiveSide.UP
    def _market_strike_is_entry_eligible(self, slug): return slug == self.current_market_slug
    def _infer_market_fee_rate_default(self): return Decimal("0")
    def _db_strategy_event(self, event_type, payload): self.events.append((event_type, payload))
    def _submit_taker_exit_order(self, **kwargs): self.submissions.append(kwargs); return True


def test_endgame_twap_exit_bypasses_normal_eval_interval_and_submits_taker_sell():
    host = _EndgameHost()
    asyncio.run(host._maybe_taker_exit_positions(time.time(), is_simulation=False))
    asyncio.run(host._maybe_taker_exit_positions(time.time(), is_simulation=False))

    assert len(host.submissions) == 1
    assert host.submissions[0]["reason"] == "endgame_twap_stop_loss"
    assert host.submissions[0]["quantity"] == Decimal("10")
    assert [event for event, _ in host.events].count("ENDGAME_TWAP_EXIT_TRIGGERED") == 1
