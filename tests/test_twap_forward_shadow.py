from decimal import Decimal

from bot.twap_forward_shadow import TwapForwardShadow


def sample(model, ts, spot="101", twap="100", strike="100", left=60):
    return model.observe(
        slug="m", now_ts=ts, source_ts=ts, fast_spot=Decimal(spot), official_twap=Decimal(twap),
        strike=Decimal(strike), time_left_sec=left, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"),
    )


def test_primary_features_and_bounded_buffer():
    model = TwapForwardShadow(max_samples=3)
    for ts, twap in enumerate(("100", "100.02", "100.04", "100.06", "100.08")):
        result = sample(model, float(ts), twap=twap)
    assert model.sample_count("m") == 3
    assert result["twap_minus_strike_bps"] == 8.0
    assert result["spot_minus_twap_bps"] > 0


def test_actual_span_slope_and_insufficient_history_are_not_zero():
    model = TwapForwardShadow()
    first = sample(model, 0, twap="100")
    assert first["twap_slope_5s_bps_per_sec"] is None
    sample(model, 5, twap="100.05")
    result = sample(model, 10, twap="100.10")
    assert result["twap_slope_5s_bps_per_sec"] is not None
    assert result["twap_slope_10s_bps_per_sec"] is not None


def test_flat_and_capped_trend_projection_and_crossing_eta():
    model = TwapForwardShadow(trend_cap_bps=10)
    sample(model, 0, spot="99", twap="99", strike="100", left=30)
    sample(model, 10, spot="101", twap="99.5", strike="100", left=20)
    result = sample(model, 20, spot="102", twap="100", strike="100", left=10)
    assert result["projected_settlement_side_flat"] == "UP"
    assert result["projected_settlement_side_trend"] == "UP"
    assert result["projected_crossing_eta_sec"] is None  # already at strike, not a future crossing
    # The trend increment is capped; the absolute level can already be far
    # from strike because the current fast spot itself is far from strike.
    flat = result["projected_settlement_twap_flat"]
    trend = result["projected_settlement_twap_trend"]
    assert abs((trend - flat) / flat * 10_000) <= 10


def test_moving_away_has_no_crossing_eta_and_rollover_summarizes_then_clears():
    model = TwapForwardShadow()
    sample(model, 0, spot="98", twap="99", strike="100", left=30)
    result = sample(model, 10, spot="97", twap="98", strike="100", left=20)
    assert result["projected_crossing_eta_sec"] is None
    summary = model.finalize_market("m", settlement_side="DOWN")
    assert summary["market_slug"] == "m"
    assert model.sample_count("m") == 0
