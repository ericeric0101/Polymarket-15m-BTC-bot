from decimal import Decimal

from bot.forecast_state import build_forecast_state


def _state(**overrides):
    values = {
        "spot": Decimal("101"),
        "strike": Decimal("100"),
        "time_left_sec": 300.0,
        "reference_source": "polymarket_chainlink_twap_60s_ws",
        "market_mid": Decimal("0.60"),
        "outcome": "up",
        "sigma_default": Decimal("0.60"),
        "sigma_raw_realized": Decimal("0.10"),
        "sigma_scale": Decimal("1"),
        "sigma_floor": Decimal("0.20"),
        "sigma_ceiling": Decimal("1.60"),
        "time_decay_enabled": True,
        "time_decay_ref_sec": 600.0,
        "time_decay_min": 0.30,
        "implied_sigma_enabled": False,
        "twap_window_sec": 60,
        "observed_twap_average": None,
        "observed_twap_seconds": 0.0,
    }
    values.update(overrides)
    return build_forecast_state(**values)


def test_shared_forecast_applies_existing_floor_and_time_decay_once():
    state = _state()

    assert state.sigma_after_bounds == Decimal("0.20")
    assert state.sigma_time_decay_factor == Decimal("0.5")
    assert state.sigma_after_time_decay == Decimal("0.20")
    assert state.sigma_final == Decimal("0.20")
    assert state.settlement_model == "twap_average_approx"
    assert state.twap_average_up_probability is not None
    assert state.selected_up_probability == state.twap_average_up_probability
    assert state.probability_for_outcome("down") == Decimal("1") - state.selected_up_probability


def test_shared_forecast_uses_instantaneous_model_for_degraded_source():
    state = _state(reference_source="binance_ws")

    assert state.settlement_model == "instantaneous_digital"
    assert state.twap_average_up_probability is None
    assert state.selected_up_probability == state.standard_up_probability
