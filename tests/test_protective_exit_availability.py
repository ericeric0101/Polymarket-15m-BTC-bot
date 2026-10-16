"""Protective-exit availability during pauses and stale data (P1 remediation).

Hermetic: fake venue base class, no network, no production DB. Every real
protective SELL in these harnesses must pass ExecutionSafetyMixin.submit_order.
"""
import asyncio
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bot.enums import ActiveSide, MarketPhase
from bot.execution_safety import ExecutionSafetyMixin
from bot.quote_runtime import QuoteRuntimeMixin
from bot.taker_exit import TakerExitMixin
from run_bot import IntegratedBTCStrategy
from test_absolute_max_loss_breaker import _BreakerExitHost


class _VenueBase:
    """Stands in for the Nautilus Strategy: records orders that reach the venue."""

    def submit_order(self, order, *args, **kwargs):
        self.venue_submissions.append(order)
        return True


class _ProtectiveHost(ExecutionSafetyMixin, QuoteRuntimeMixin, _BreakerExitHost, _VenueBase):
    """Real quote-cycle gate + real taker-exit path + real chokepoint."""

    _submit_taker_exit_order = TakerExitMixin._submit_taker_exit_order

    def __init__(self, *, qty="10", sellable="10"):
        super().__init__()
        now = time.time()
        self.venue_submissions = []
        self.events = []
        self.cancelled = []
        self.test_mode = False
        self.maker_kill_switch = False
        self.quote_pause_until_ts = 0.0
        self.dashboard_state = SimpleNamespace(bot_paused=False)
        self._last_dashboard_pause_log_ts = 0.0
        self.prediction_research_snapshotter = None
        self.bi_side_enabled = True
        self.active_side = ActiveSide.DOWN  # strong opposite to the held UP position
        self.side_decision_score = Decimal("-0.30")
        self.inventory_delta_shares = Decimal(qty)
        self.regime_guard_enabled = False
        self._startup_rehydrated_inventory_force_sell_only = False
        self.maker_max_inventory_shares = Decimal("10")
        self.trade_db_buy_ready = True
        self.last_quote_update_ts = now + 3600  # stop the cycle right after protective exits
        self.quote_refresh_sec = 3
        self.quote_stale_sec = 30
        self.taker_exit_skip_log_interval_sec = 20
        self.taker_exit_reason_by_client_order_id = {}
        self.terminal_dashboard = None
        self.current_market_end_timestamp = now + 300
        self._stop_adverse_since_by_slug = {"breaker-test": now - 15.0}
        self.twap_forward_shadow = SimpleNamespace(latest=lambda _slug: {
            "official_current_twap": 99.0,
            "projected_settlement_side_trend": "DOWN",
            "source_ts": time.time() - 0.1,
        })
        self.live_inventory_cost = {
            "up": {"qty": Decimal(qty), "avg_entry_price": Decimal("0.69"), "opened_ts": now - 90}
        }
        self._sellable = Decimal(sellable)
        self.active_maker_orders = {
            "buy:down": {"side": "buy", "instrument_id": "down", "created_ts": now - 1, "pending_cancel": False},
            "sell:up": {"side": "sell", "instrument_id": "up", "created_ts": now - 60, "pending_cancel": False},
        }
        self.last_quote_update_ts_by_inst = {"up": now, "down": now}
        self.cache = SimpleNamespace(instrument=lambda _i: SimpleNamespace(size_precision=2, price_precision=2))
        self.order_factory = SimpleNamespace(
            market=lambda **kw: SimpleNamespace(**kw),
            limit=lambda **kw: SimpleNamespace(**kw),
        )

    # quote-cycle collaborators
    def _update_market_phase(self): return MarketPhase.ACTIVE
    async def _maybe_finalize_side_decision(self, _now, _phase): return None
    def _new_buy_session_decision_fn(self, now_ts):
        return SimpleNamespace(allowed=True, reason="test", local_time=SimpleNamespace(isoformat=lambda: "t"))
    def session_buy_guard_decision(self, _now): return None
    def _is_dry_run_mode(self): return bool(self.test_mode)
    def _refresh_balance_cache(self): return Decimal("0")  # no collateral at all
    def _telegram_cycle_tick(self): return None
    def _cancel_active_maker_orders(self):
        for key in list(self.active_maker_orders):
            self._cancel_maker_order_side(key, reason="all")
    def _cancel_maker_order_side(self, order_key, reason="", instrument_id=None):
        side = order_key if order_key in ("buy", "sell") else str(order_key).split(":", 1)[0]
        for key in list(self.active_maker_orders):
            if key.startswith(f"{side}:") and (instrument_id is None or key.endswith(f":{instrument_id}")):
                self.cancelled.append(key)
                self.active_maker_orders.pop(key, None)
    async def _maybe_maker_urgent_exit(self, _now): return None
    def _get_effective_sellable_qty(self, **_kwargs): return self._sellable
    def _db_order_event(self, **kwargs): self.events.append(("order", kwargs.get("event_type"), kwargs))
    def _db_strategy_event(self, name, payload): self.events.append(("strategy", name, payload))

    def event_names(self, name):
        return [e for e in self.events if e[1] == name]


def _cycle(host):
    return asyncio.run(host._prepare_quote_cycle())


def _quote_cycle(host):
    asyncio.run(IntegratedBTCStrategy._quote_maker_orders(host, Decimal("0.2"), Decimal("0.25")))


# 1, 9, 16, 14
def test_normal_state_breaker_reaches_chokepoint_and_venue():
    host = _ProtectiveHost()
    _cycle(host)
    assert len(host.venue_submissions) == 1
    assert host.venue_submissions[0].order_side.name == "SELL"


# 2, 7, 8, 13, 14
def test_telegram_pause_keeps_protective_exit_and_cancels_only_buys():
    host = _ProtectiveHost()
    host.dashboard_state.bot_paused = True
    _quote_cycle(host)
    assert len(host.venue_submissions) == 1, "protective SELL must not be blocked by Telegram pause"
    assert "buy:down" in host.cancelled
    assert all(e[1] != "ORDER_SUBMIT" for e in host.events)  # no new maker BUY/SELL quote


def test_telegram_pause_does_not_cancel_resting_protective_sell_without_breaker():
    host = _ProtectiveHost()
    host.active_side = ActiveSide.UP  # no adverse trend -> breaker must not fire
    host.side_decision_score = Decimal("0.30")
    host.twap_forward_shadow = SimpleNamespace(latest=lambda _slug: None)
    host._stop_adverse_since_by_slug = {}
    host._stop_adverse_votes_by_slug = {}
    host.dashboard_state.bot_paused = True
    _quote_cycle(host)
    assert host.venue_submissions == []
    assert "sell:up" in host.active_maker_orders
    assert "buy:down" not in host.active_maker_orders


# 3, 7
def test_error_pause_class_i_keeps_protective_exit_and_blocks_buys():
    host = _ProtectiveHost()
    host.quote_pause_until_ts = time.time() + 30
    assert _cycle(host) is None
    assert len(host.venue_submissions) == 1
    assert "buy:down" in host.cancelled


# 4
def test_error_pause_class_ii_unknown_sell_state_degrades_instead_of_selling():
    host = _ProtectiveHost()
    host.quote_pause_until_ts = time.time() + 30
    host.active_maker_orders["sell:up"].update(pending_cancel=True, reconcile_unknown_retries=1)
    _cycle(host)
    assert host.venue_submissions == []
    degraded = host.event_names("PROTECTIVE_EXIT_DEGRADED")
    assert degraded and degraded[0][2]["reason"] == "inventory_unreliable_sell_order_state_unknown"


# 5 (kill switch semantics unchanged: UNRESOLVED for user decision)
def test_kill_switch_keeps_existing_no_order_semantics_but_reports_degraded():
    host = _ProtectiveHost()
    host.maker_kill_switch = True
    assert _cycle(host) is None
    assert host.venue_submissions == []
    degraded = host.event_names("PROTECTIVE_EXIT_DEGRADED")
    assert degraded and degraded[0][2]["reason"] == "kill_switch_active_exit_policy_unresolved"


# 6
def test_pause_without_inventory_does_no_protective_work():
    host = _ProtectiveHost()
    host.live_inventory_cost = {}
    host.inventory_delta_shares = Decimal("0")
    host.dashboard_state.bot_paused = True
    _quote_cycle(host)
    assert host.venue_submissions == []
    assert host.event_names("PROTECTIVE_EXIT_DEGRADED") == []


# 10
def test_dry_run_never_reaches_venue_even_when_breaker_condition_holds():
    host = _ProtectiveHost()
    host.test_mode = True
    _cycle(host)
    asyncio.run(host._maybe_taker_exit_positions(time.time(), is_simulation=False))  # force the path
    assert host.venue_submissions == []


# 11
def test_stale_reference_feed_does_not_fabricate_adverse_trend():
    host = _ProtectiveHost()
    host.active_side = ActiveSide.NONE  # no live signal
    host.side_decision_score = Decimal("0")
    host.twap_forward_shadow = SimpleNamespace(latest=lambda _slug: {
        "official_current_twap": 99.0, "projected_settlement_side_trend": "DOWN",
        "source_ts": time.time() - 120,  # stale
    })
    host._stop_adverse_since_by_slug = {}
    host._stop_adverse_votes_by_slug = {}
    _cycle(host)
    assert host.venue_submissions == []


# 12
def test_stale_orderbook_emits_rate_limited_degraded_and_no_sell():
    host = _ProtectiveHost()
    host.last_quote_update_ts_by_inst = {"up": time.time() - 120}
    for _ in range(5):
        _cycle(host)
    assert host.venue_submissions == []
    degraded = [e for e in host.event_names("PROTECTIVE_EXIT_DEGRADED") if e[2]["reason"] == "orderbook_stale"]
    assert len(degraded) == 1


def test_watchdog_reports_degraded_during_full_quote_outage():
    host = _ProtectiveHost()
    host.last_quote_update_ts_by_inst = {"up": time.time() - 120}
    host._report_protective_exit_availability(time.time())
    host._report_protective_exit_availability(time.time())
    degraded = host.event_names("PROTECTIVE_EXIT_DEGRADED")
    assert len(degraded) == 1 and degraded[0][2]["reason"] == "orderbook_stale"
    assert host.venue_submissions == []


# 17
def test_repeated_ticks_keep_one_in_flight_sell_per_position():
    host = _ProtectiveHost()
    host.dashboard_state.bot_paused = True
    for _ in range(4):
        _quote_cycle(host)
    assert len(host.venue_submissions) == 1


# 18
def test_pause_to_resume_does_not_double_submit():
    host = _ProtectiveHost()
    host.dashboard_state.bot_paused = True
    _quote_cycle(host)
    host.dashboard_state.bot_paused = False
    _quote_cycle(host)
    assert len(host.venue_submissions) == 1


# 19
def test_failed_sell_retries_are_rate_limited():
    host = _ProtectiveHost()
    host.taker_exit_reject_cooldown_sec = 20
    _cycle(host)
    coid = str(host.venue_submissions[0].client_order_id)
    # Venue rejection handling (bot/order_events.py:725-743): clear in-flight, start cooldown.
    host._clear_pending_taker_exit_for_order(coid)
    host.taker_exit_reject_cooldown_until_by_inst["up"] = time.time() + 20
    for _ in range(5):
        _cycle(host)
    assert len(host.venue_submissions) == 1
    host.taker_exit_reject_cooldown_until_by_inst["up"] = time.time() - 1
    _cycle(host)
    assert len(host.venue_submissions) == 2


# 20
def test_sell_quantity_never_exceeds_verified_inventory():
    host = _ProtectiveHost(qty="10", sellable="7.25")
    _cycle(host)
    assert host.venue_submissions[0].quantity.as_decimal() == Decimal("7.25")


# 21
def test_sub_minimum_position_is_marked_unsellable_once_and_not_retried():
    host = _ProtectiveHost(qty="4", sellable="4")
    for _ in range(3):
        _cycle(host)
    assert host.venue_submissions == []
    unsellable = host.event_names("PROTECTIVE_EXIT_UNSELLABLE")
    assert len(unsellable) == 1 and unsellable[0][2]["qty"] == 4.0


# 15
def test_stop_loss_disabled_keeps_hard_breaker_active_during_pause():
    host = _ProtectiveHost()
    host.stop_loss_enabled = False
    host.endgame_twap_exit_enabled = True
    host.dashboard_state.bot_paused = True
    _quote_cycle(host)
    assert len(host.venue_submissions) == 1
    submit = host.event_names("ORDER_TAKER_EXIT_SUBMIT")[0][2]
    assert submit["payload"]["decision_reason"] == "absolute_max_loss_breaker"


# 13
def test_fresh_reference_while_paused_still_evaluates_without_degraded_reference():
    host = _ProtectiveHost()
    host.latest_external_spot_source_ts = time.time()
    host.dashboard_state.bot_paused = True
    _quote_cycle(host)
    assert len(host.venue_submissions) == 1
    assert host.event_names("PROTECTIVE_EXIT_DEGRADED") == []


def test_absolute_breaker_decision_is_labelled_conditional():
    from bot.exit_engine import ExitPolicyEngine
    from test_absolute_max_loss_breaker import _make_config, _position, _signal, _snapshot

    engine = ExitPolicyEngine(_make_config(stop_loss_enabled=False))
    decision = engine.evaluate(_snapshot("0.20"), _position("0.69"),
                               _signal(score=Decimal("-.30"), matches=False, active_side="DOWN"))
    assert decision.reason == "absolute_max_loss_breaker"
    assert decision.metadata["breaker_label"] == "conditional_absolute_loss_breaker"


# error-pause class (i): BUY collateral reject keeps the protective SELL
@pytest.mark.parametrize("side,expected", [("BUY", [("buy",)]), (None, ["all"])])
def test_buy_balance_reject_cancels_only_buys_unknown_side_stays_fail_closed(side, expected):
    from test_live_path_regressions import DummyRejectRecoveryStrategy

    strategy = DummyRejectRecoveryStrategy()
    strategy.maker_error_pause_sec = 30
    strategy.quote_pause_until_ts = 0.0
    strategy.maker_max_consecutive_denied = 5
    calls = []
    strategy._cancel_maker_order_side = lambda *args, **kwargs: calls.append(tuple(args[:1]))
    strategy._cancel_active_maker_orders = lambda: calls.append("all")
    event = SimpleNamespace(
        client_order_id="BTC-15M-MAKER-BUY-1",
        reason="PolyApiException[status_code=400, error_message={'error': 'not enough balance / allowance'}]",
        instrument_id="cond-123-456.POLYMARKET", order_side=side, venue_order_id=None,
    )
    IntegratedBTCStrategy._handle_order_rejection_like_event(strategy, event, title="ORDER REJECTED")
    assert calls == expected
    assert strategy.quote_pause_until_ts > time.time()
