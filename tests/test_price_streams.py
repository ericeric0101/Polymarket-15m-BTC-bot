from decimal import Decimal

from bot.price_streams import BINANCE_AGGTRADE_WS_URL, extract_binance_aggtrade_tick


def test_binance_btc_trend_feed_uses_spot_aggtrade_endpoint():
    assert BINANCE_AGGTRADE_WS_URL == "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"


def test_binance_spot_aggtrade_payload_is_accepted():
    tick = extract_binance_aggtrade_tick(
        '{"e":"aggTrade","E":1786282626546,"p":"65130.00000000"}'
    )

    assert tick is not None
    assert tick.price == Decimal("65130.00000000")
