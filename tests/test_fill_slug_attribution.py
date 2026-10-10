"""Late-fill market attribution (pre-12-market pass follow-up, S1-C).

A resting TP SELL of market N can fill after the bot has rolled over to market
N+1 (e.g. 2026-09-28 12:33 for btc-updown-15m-1790597700).  ORDER_FILLED used
to inherit the *current* market slug, so the fill was booked to N+1.  Fills
are now attributed through the instrument->slug map recorded at each market
switch; an unmapped instrument falls back to the current market and says so.
"""
from decimal import Decimal
from types import SimpleNamespace

from bot.instrument_slug_map import (
    MAX_TRACKED_INSTRUMENTS, register_market_instruments, slug_for_instrument,
)
from run_bot import IntegratedBTCStrategy
from test_fill_instrument_identity import HELD, _flipped_strategy, _sell

PREV = "btc-updown-15m-1790597700"
NEXT = "btc-updown-15m-1790598600"


def test_register_and_lookup_is_bounded():
    host = SimpleNamespace()
    register_market_instruments(host, PREV, ["a.POLYMARKET", "b.POLYMARKET"])
    register_market_instruments(host, NEXT, ["c.POLYMARKET", None])
    assert slug_for_instrument(host, "a.POLYMARKET") == PREV
    assert slug_for_instrument(host, "c.POLYMARKET") == NEXT
    assert slug_for_instrument(host, "zzz") is None
    for i in range(MAX_TRACKED_INSTRUMENTS + 10):
        register_market_instruments(host, f"btc-updown-15m-{i}", [f"x{i}.POLYMARKET"])
    assert len(host.instrument_slug_by_key) <= MAX_TRACKED_INSTRUMENTS
    assert slug_for_instrument(host, "a.POLYMARKET") is None  # oldest evicted


def test_lookup_never_raises_on_missing_state():
    assert slug_for_instrument(SimpleNamespace(), "a") is None
    assert slug_for_instrument(SimpleNamespace(instrument_slug_by_key="broken"), "a") is None


def _late_fill_strategy():
    strategy = _flipped_strategy()
    strategy.current_market_slug = NEXT  # bot already rolled over
    register_market_instruments(strategy, PREV, [HELD])
    return strategy


def test_late_fill_is_booked_to_its_own_market():
    strategy = _late_fill_strategy()
    IntegratedBTCStrategy.on_order_filled(strategy, _sell("5.4945", px="0.97"))
    (event,) = [e for e in strategy.order_events if e.get("event_type") == "ORDER_FILLED"]
    payload = event["payload"]
    assert payload["slug"] == PREV and payload["market_slug"] == PREV
    assert payload["slug_attribution"] == "instrument_map"
    assert payload["journal_current_market_slug"] == NEXT
    assert PREV in str(payload.get("position_lifecycle_id") or PREV)


def test_unmapped_instrument_falls_back_to_current_market_and_says_so():
    strategy = _flipped_strategy()
    strategy.current_market_slug = NEXT
    IntegratedBTCStrategy.on_order_filled(strategy, _sell("5.4945"))
    (event,) = [e for e in strategy.order_events if e.get("event_type") == "ORDER_FILLED"]
    assert event["payload"]["slug"] == NEXT
    assert event["payload"]["slug_attribution"] == "current_market_fallback"


def test_same_market_fill_is_unchanged():
    strategy = _flipped_strategy()
    strategy.current_market_slug = PREV
    register_market_instruments(strategy, PREV, [HELD])
    IntegratedBTCStrategy.on_order_filled(strategy, _sell("5.4945"))
    (event,) = [e for e in strategy.order_events if e.get("event_type") == "ORDER_FILLED"]
    assert event["payload"]["slug"] == PREV
    assert event["payload"]["slug_attribution"] == "instrument_map"
    assert "journal_current_market_slug" not in event["payload"]
    assert strategy.live_inventory_cost[HELD]["qty"] == Decimal("0.0055")
