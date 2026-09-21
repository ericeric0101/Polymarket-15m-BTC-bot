from decimal import Decimal
from types import SimpleNamespace

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


class _FastFollowForecastHost(SpotPricerMixin):
    def __init__(self, observed_ts: float) -> None:
        self._polymarket_chainlink_twap_price = Decimal("100.0")
        self._polymarket_chainlink_twap_observation_ts = observed_ts
        self._polymarket_chainlink_twap_window_sec = 60
        self.current_market_slug = "btc-updown-test"
        self.market_strike_cache_by_slug = {"btc-updown-test": Decimal("99.0")}
        self.current_market_end_timestamp = 1_600.0
        self.cache = SimpleNamespace(instrument=lambda _inst: SimpleNamespace())
        self.inputs = None

    def _market_strike_is_entry_eligible(self, _slug):
        return True

    def _normalize_instrument_id(self, instrument_id):
        return instrument_id

    def _extract_outcome_from_instrument(self, _instrument):
        return "up"

    def _build_forecast_state(self, **kwargs):
        self.inputs = kwargs
        return SimpleNamespace(created_ts=1_000.0)


def test_fast_follow_forecast_rejects_stale_underlying_twap(monkeypatch):
    monkeypatch.setattr("bot.spot_pricer.time.time", lambda: 1_000.0)
    strategy = _FastFollowForecastHost(observed_ts=994.0)

    assert strategy._build_fast_follow_forecast_state(
        instrument_id="UP.INST", market_mid=Decimal("0.60"), max_source_age_sec=5.0,
    ) is None


def test_fast_follow_forecast_accepts_fresh_underlying_twap_and_exposes_source_age(monkeypatch):
    monkeypatch.setattr("bot.spot_pricer.time.time", lambda: 1_000.0)
    strategy = _FastFollowForecastHost(observed_ts=997.0)

    assert strategy._build_fast_follow_forecast_state(
        instrument_id="UP.INST", market_mid=Decimal("0.60"), max_source_age_sec=5.0,
    ) is not None
    assert strategy.inputs["source_observed_ts"] == 997.0
    assert strategy.inputs["source_age_sec"] == 3.0
