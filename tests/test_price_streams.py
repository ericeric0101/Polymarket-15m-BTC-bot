from decimal import Decimal

from bot.price_streams import (
    BINANCE_AGGTRADE_WS_URL,
    extract_binance_aggtrade_tick,
    rtds_silent_stall_due,
)


def test_binance_btc_trend_feed_uses_spot_aggtrade_endpoint():
    assert BINANCE_AGGTRADE_WS_URL == "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"


def test_binance_spot_aggtrade_payload_is_accepted():
    tick = extract_binance_aggtrade_tick(
        '{"e":"aggTrade","E":1786282626546,"p":"65130.00000000"}'
    )

    assert tick is not None
    assert tick.price == Decimal("65130.00000000")


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
