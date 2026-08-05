from decimal import Decimal

from bot.signal_engine import SignalEngine, SignalEngineConfig


def test_missing_btc_and_strike_renormalizes_market_weight_without_double_penalty():
    engine = SignalEngine(SignalEngineConfig())
    engine.update_market_mid(Decimal("0.75"), 100.0)

    signal = engine.compute(
        spot=None,
        strike=None,
        sigma=None,
        time_left_sec=450.0,
        market_mid=Decimal("0.75"),
    )

    assert signal.w_market == 1.0
    assert signal.w_btc == 0.0
    assert signal.w_strike == 0.0
    assert signal.composite_score > 0.0
    assert signal.confidence == signal.composite_score


def test_missing_market_renormalizes_external_layers():
    engine = SignalEngine(SignalEngineConfig())
    engine.update_btc_price(Decimal("100.0"), 100.0)
    engine.update_btc_price(Decimal("101.0"), 110.0)

    signal = engine.compute(
        spot=Decimal("101.0"),
        strike=Decimal("100.0"),
        sigma=Decimal("0.8"),
        time_left_sec=450.0,
        market_mid=None,
    )

    assert signal.w_market == 0.0
    assert signal.w_btc > 0.0
    assert signal.w_strike > 0.0
    assert abs((signal.w_btc + signal.w_strike) - 1.0) < 1e-9
    assert signal.confidence > 0.0


def test_no_available_signal_layer_has_zero_confidence():
    signal = SignalEngine().compute(
        spot=None,
        strike=None,
        sigma=None,
        time_left_sec=450.0,
        market_mid=None,
    )

    assert signal.w_market == 0.0
    assert signal.w_btc == 0.0
    assert signal.w_strike == 0.0
    assert signal.composite_score == 0.0
    assert signal.confidence == 0.0
