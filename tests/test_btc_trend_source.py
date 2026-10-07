from decimal import Decimal

from bot.signal_engine import SignalEngine
from bot.spot_pricer import SpotPricerMixin


class _Strategy(SpotPricerMixin):
    def __init__(self) -> None:
        self._signal_engine = SignalEngine()
        self._binance_ws_price_ts = 0.0
        self.side_signal_btc_trend_primary_stale_sec = 10.0
        self._btc_trend_source = "unavailable"
        self._btc_trend_source_ts = 0.0
        self._btc_trend_source_price = None


def test_raw_chainlink_feeds_trend_when_binance_is_unavailable():
    strategy = _Strategy()

    strategy._update_btc_trend_price(
        Decimal("100.0"), 100.0, source="polymarket_chainlink_ws"
    )

    assert strategy._btc_trend_source == "polymarket_chainlink_ws"
    assert strategy._btc_trend_source_price == Decimal("100.0")
    assert strategy._signal_engine._btc_ema_fast.value == 100.0


def test_raw_chainlink_does_not_override_fresh_binance():
    strategy = _Strategy()
    strategy._binance_ws_price_ts = 1e20  # Fresh relative to the current wall clock.

    strategy._update_btc_trend_price(
        Decimal("100.0"), 100.0, source="polymarket_chainlink_ws"
    )

    assert strategy._btc_trend_source == "unavailable"
    assert strategy._signal_engine._btc_ema_fast.value is None


def test_binance_always_reclaims_btc_trend_source():
    strategy = _Strategy()
    strategy._update_btc_trend_price(
        Decimal("100.0"), 100.0, source="polymarket_chainlink_ws"
    )

    strategy._update_btc_trend_price(Decimal("101.0"), 101.0, source="binance_ws")

    assert strategy._btc_trend_source == "binance_ws"
    assert strategy._btc_trend_source_price == Decimal("101.0")
