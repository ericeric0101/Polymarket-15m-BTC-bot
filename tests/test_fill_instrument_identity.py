"""ORDER_FILLED instrument identity (pre-12-market engineering pass, F-C).

Twenty LIVE SELL fills (mostly taker-exit stops after a side invalidation)
were journaled under the paired token's instrument_id: the ORDER_FILLED call
did not pass the filled instrument, so the journal defaulted to the strategy's
current active-side instrument.  The in-memory ledger already used the filled
(held) instrument; startup rehydration, however, replays ORDER_FILLED by
instrument_id and could not see those SELLs.
"""
from decimal import Decimal
from types import SimpleNamespace

from bot.enums import ActiveSide
from run_bot import IntegratedBTCStrategy
from test_live_path_regressions import DummyStrategyForFill

HELD = "0xcond-held.POLYMARKET"
PAIRED = "0xcond-paired.POLYMARKET"


def _flipped_strategy(qty="5.5"):
    strategy = DummyStrategyForFill()
    strategy.recent_sell_fill_ts_by_inst = {}
    strategy.inventory_metric_count = 0
    strategy._side_for_instrument_id = lambda inst: ActiveSide.UP if str(inst) == HELD else ActiveSide.DOWN
    strategy.market_stop_loss_max_per_market = 1
    strategy._clear_profit_run_state = lambda _inst: None
    strategy.active_maker_orders = {}
    strategy.instrument_id = PAIRED  # active side flipped to the other token after invalidation
    strategy.live_inventory_cost = {
        HELD: {"qty": Decimal(qty), "avg_entry_price": Decimal("0.82"),
               "entry_fee_remaining": Decimal("0"), "opened_ts": 1.0},
    }
    strategy.taker_exit_reason_by_client_order_id = {"BTC-15M-TAKER-EXIT-1": "stop_loss"}
    strategy.cleared_pending = []
    strategy._clear_pending_taker_exit_for_order = strategy.cleared_pending.append
    return strategy


def _sell(qty, px="0.25", coid="BTC-15M-TAKER-EXIT-1"):
    return SimpleNamespace(client_order_id=coid, last_px=float(px), last_qty=float(qty), commission=0.0,
                           liquidity_side="TAKER", order_side="SELL", instrument_id=HELD, venue_order_id=None)


def _fill_events(strategy):
    return [e for e in strategy.order_events if e.get("event_type") == "ORDER_FILLED"]


def test_taker_exit_fill_is_journaled_under_the_held_token():
    strategy = _flipped_strategy()
    IntegratedBTCStrategy.on_order_filled(strategy, _sell("5.4945"))
    (event,) = _fill_events(strategy)
    assert event["instrument_id"] == HELD
    assert event["payload"]["instrument_id"] == HELD


def test_sell_reduces_held_inventory_without_phantom_paired_inventory():
    strategy = _flipped_strategy()
    IntegratedBTCStrategy.on_order_filled(strategy, _sell("5.4945"))
    assert strategy.live_inventory_cost[HELD]["qty"] == Decimal("0.0055")
    assert PAIRED not in strategy.live_inventory_cost
    assert strategy.cleared_pending == ["BTC-15M-TAKER-EXIT-1"]


def test_partial_sell_fills_accumulate_on_the_held_token():
    strategy = _flipped_strategy()
    IntegratedBTCStrategy.on_order_filled(strategy, _sell("3.0"))
    IntegratedBTCStrategy.on_order_filled(strategy, _sell("2.4945"))
    assert strategy.live_inventory_cost[HELD]["qty"] == Decimal("0.0055")
    assert [e["instrument_id"] for e in _fill_events(strategy)] == [HELD, HELD]


def test_sell_larger_than_held_never_goes_negative():
    strategy = _flipped_strategy(qty="5.5")
    IntegratedBTCStrategy.on_order_filled(strategy, _sell("6.0"))
    assert strategy.live_inventory_cost[HELD]["qty"] == Decimal("0")
    assert PAIRED not in strategy.live_inventory_cost


def test_maker_fill_keeps_the_maker_order_instrument():
    strategy = DummyStrategyForFill()  # resting maker BUY on inst-1, active instrument inst-1
    strategy.instrument_id = "inst-other"
    fill = SimpleNamespace(client_order_id="BUY-1", last_px=0.46, last_qty=5.2, commission=0.0,
                           liquidity_side="MAKER", order_side="BUY", instrument_id="inst-1", venue_order_id=None)
    IntegratedBTCStrategy.on_order_filled(strategy, fill)
    (event,) = _fill_events(strategy)
    assert event["instrument_id"] == "inst-1"
