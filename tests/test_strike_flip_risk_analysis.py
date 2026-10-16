from scripts.strike_flip_risk_analysis import (
    adjacent_pairs,
    classify_weekend,
    crossing_count,
    distance_metrics,
    distance_bucket,
    _evidence_verdicts,
    leader_for,
    leader_persistence,
    near_strike_duration,
    reconstruct_adjacent_strike,
    safety_sigma,
)


def candle(ts, close):
    return {"ts": ts, "close_ts": ts + 59.999, "open": close, "high": close, "low": close, "close": close, "volume": 1}


def test_strike_distance_usd_bps_and_leader():
    row = distance_metrics(101.0, 100.0)
    assert row["signed_distance_usd"] == 1.0
    assert row["abs_distance_usd"] == 1.0
    assert row["distance_bps"] == 100.0
    assert row["abs_distance_bps"] == 100.0
    assert leader_for(101.0, 100.0) == "UP"
    assert leader_for(99.0, 100.0) == "DOWN"
    assert leader_for(100.0, 100.0) == "TIE"


def test_crossing_count_is_lower_bound_and_ignores_ties_between_directions():
    rows = [candle(0, 101), candle(60, 99), candle(120, 100), candle(180, 98), candle(240, 102)]
    assert crossing_count(rows, 100.0) == 2


def test_leader_persistence_and_flip_after_time_use_final_winner():
    assert leader_persistence("UP", "UP") == (True, 0.0)
    assert leader_persistence("DOWN", "UP") == (False, 1.0)
    assert leader_persistence("TIE", "UP") == (None, None)


def test_distance_buckets_are_half_open_and_cover_all_ranges():
    assert distance_bucket(0.5) == "<1bps"
    assert distance_bucket(1.0) == "1-2bps"
    assert distance_bucket(2.0) == "2-5bps"
    assert distance_bucket(5.0) == "5-10bps"
    assert distance_bucket(10.0) == "5-10bps"
    assert distance_bucket(10.01) == ">10bps"


def test_near_strike_duration_counts_minute_observations_and_reentries():
    rows = [candle(i * 60, px) for i, px in enumerate([100.01, 100.01, 100.2, 100.01, 100.2])]
    stats = near_strike_duration(rows, 100.0, start_ts=0, end_ts=300, thresholds_bps=(2,))
    assert stats[2]["minutes"] == 3
    assert stats[2]["episodes"] == 2
    assert stats[2]["reentries"] == 1


def test_adjacent_pairing_requires_exact_15m_spacing_and_reconstructs_boundary_close():
    markets = [{"slug": "btc-updown-15m-900", "start": 900}, {"slug": "btc-updown-15m-1800", "start": 1800}, {"slug": "btc-updown-15m-3600", "start": 3600}]
    assert [(a["slug"], b["slug"]) for a, b in adjacent_pairs(markets)] == [(markets[0]["slug"], markets[1]["slug"])]
    rows = [candle(780, 100.0), candle(840, 101.0)]
    value, source, confidence = reconstruct_adjacent_strike(rows, 900)
    assert value == 101.0
    assert source == "RECONSTRUCTED_ADJACENT_MARKET_BINANCE_PROXY"
    assert confidence == "LOW_PROXY"


def test_safety_sigma_only_uses_candles_closed_by_observation_time():
    rows = [candle(i * 60, 100 + i) for i in range(70)]
    first = safety_sigma(rows, spot=169.0, strike=160.0, observation_ts=rows[60]["close_ts"], time_left_sec=300)
    altered_future = [*rows[:61], *[candle(r["ts"], r["close"] * 10) for r in rows[61:]]]
    second = safety_sigma(altered_future, spot=169.0, strike=160.0, observation_ts=rows[60]["close_ts"], time_left_sec=300)
    assert first["safety_sigma"] == second["safety_sigma"]
    assert first["volatility_source"] == "VOLATILITY_PROXY_PRIOR_60M"


def test_weekend_classification_uses_market_start_epoch_in_et():
    # Saturday noon UTC is Saturday morning in New York.
    assert classify_weekend(1785672000, "America/New_York")[0] is True
    assert classify_weekend(1785758400, "America/New_York")[0] is False


def test_evidence_verdicts_do_not_promote_proxy_only_results_to_canonical_claims():
    rows = _evidence_verdicts(distance_comparisons=[], crossing_tests=[],
                              persistence_tests=[], model_rows=[], strategy_group=[])
    verdicts = {r["hypothesis"]: r["verdict"] for r in rows}
    assert verdicts["Canonical weekend strike-proximity effect"] == "Cannot assess with current strike coverage"
    assert verdicts["H1 weekend closer to strike"] == "Suggestive (proxy only)"
