from decimal import Decimal

from bot.price_streams import (
    BINANCE_AGGTRADE_WS_URL,
    extract_binance_aggtrade_tick,
    rtds_silent_stall_due,
)
from bot.spot_pricer import SpotPricerMixin


def test_binance_btc_trend_feed_uses_spot_aggtrade_endpoint():
    assert BINANCE_AGGTRADE_WS_URL == "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"


def test_binance_spot_aggtrade_payload_is_accepted():
    tick = extract_binance_aggtrade_tick(
        '{"e":"aggTrade","E":1786282626546,"T":1786282626545,"p":"65130.00000000","q":"0.25"}'
    )

    assert tick is not None
    assert tick.price == Decimal("65130.00000000")
    assert tick.updated_at_ms == 1786282626546  # Existing live event-clock semantics stay unchanged.
    assert tick.exchange_trade_ts_ms == 1786282626545
    assert tick.quantity == Decimal("0.25")


def test_rtds_silent_stall_requires_a_real_gap_in_valid_twap_ticks():
    assert not rtds_silent_stall_due(
        now_monotonic=114.9,
        last_valid_twap_monotonic=100.0,
        max_silence_sec=15.0,
    )
    assert rtds_silent_stall_due(
        now_monotonic=115.0,
        last_valid_twap_monotonic=100.0,
        max_silence_sec=15.0,
    )


def test_btc_1s_observer_failure_cannot_escape_into_live_price_callback():
    class BrokenCollector:
        def observe_aggtrade(self, **_kwargs):
            raise OSError("simulated research storage failure")

    class Strategy:
        btc_1s_history_collector = BrokenCollector()

    tick = extract_binance_aggtrade_tick(
        '{"e":"aggTrade","E":1786282626546,"T":1786282626545,"p":"65130","q":"0.25"}'
    )
    SpotPricerMixin._observe_btc_1s_history(Strategy(), tick)
