from scripts.empirical_probability_research import (
    empirical_cdf_probability,
    horizon_seconds,
    market_cluster_bootstrap,
    rolling_history,
    estimate_row,
    MIN_EMPIRICAL_SAMPLES,
    _metric_rows,
)


def test_empirical_cdf_signed_required_move():
    returns = [-10, -5, 0, 5, 10]
    assert empirical_cdf_probability(returns, 5, "DOWN") == 2 / 5
    assert empirical_cdf_probability(returns, -5, "UP") == 3 / 5


def test_empirical_cdf_zero_move_is_current_side():
    assert empirical_cdf_probability([0, 1], 0, "UP") == 1
    assert empirical_cdf_probability([0, 1], 0, "DOWN") == 0
    assert empirical_cdf_probability([], 5, "UP") is None


def test_checkpoint_horizons_are_resolution_honest():
    assert horizon_seconds(120) == 120
    assert horizon_seconds(60) == 60
    assert horizon_seconds(30) is None
    assert horizon_seconds(15) is None
    assert horizon_seconds(10) is None
    assert horizon_seconds(5) is None


def test_rolling_history_excludes_evaluation_time_and_future():
    samples = [{"ts": 100, "return_bps": 1}, {"ts": 200, "return_bps": 2},
               {"ts": 201, "return_bps": 3}]
    assert rolling_history(samples, 201, days=1) == samples[:2]


def test_market_bootstrap_resamples_market_clusters():
    rows = [{"market_slug": "a", "delta": 1.0}, {"market_slug": "a", "delta": 3.0},
            {"market_slug": "b", "delta": -1.0}]
    result = market_cluster_bootstrap(rows, reps=100, seed=1)
    assert result["n_markets"] == 2
    assert result["mean_delta"] == .5


def test_vol_conditioning_falls_back_when_bucket_sparse_and_insufficient_is_null():
    base = {
        "market_slug": "m", "checkpoint_sec": 120, "time_left_sec": 119,
        "required_move_mode": "PRE_FINAL_STRIKE_PROXY", "required_move_bps": 3,
        "settlement_state_side": "DOWN", "observed_ts": 10_000_000,
        "sigma_ex_market": .5,
    }
    summary = {"settlement_reference_is_canonical": True, "settlement_side": "UP"}
    sparse_samples = {120: [{"ts": 9_999_000 - i, "return_bps": float(i % 7), "vol_proxy": .5}
                            for i in range(MIN_EMPIRICAL_SAMPLES - 1)],
                      60: []}
    result = estimate_row(base, summary, [], sparse_samples)
    assert result["p_up_empirical"] is None
    assert result["empirical_resolution_note"] == "insufficient_historical_sample_count"


def test_sparse_vol_bucket_falls_back_to_unconditional_empirical():
    base = {
        "market_slug": "m", "checkpoint_sec": 120, "time_left_sec": 119,
        "required_move_mode": "PRE_FINAL_STRIKE_PROXY", "required_move_bps": 3,
        "settlement_state_side": "DOWN", "observed_ts": 10_000_000,
        "sigma_ex_market": .5,
    }
    samples = [{"ts": 9_999_000 - i, "return_bps": float(i % 9), "vol_proxy": float(i % 3)}
               for i in range(150)]
    result = estimate_row(base, {"settlement_reference_is_canonical": True, "settlement_side": "UP"},
                          [], {120: samples, 60: []})
    assert result["p_up_empirical"] is not None
    assert result["p_up_empirical_vol_conditioned"] == result["p_up_empirical"]
    assert result["vol_conditioning_fallback"] is True


def test_exact_final_boundary_is_not_faked_from_minute_close_returns():
    row = {"market_slug": "m", "checkpoint_sec": 30, "time_left_sec": 29,
           "required_move_mode": "EXACT_FINAL_WINDOW_BOUNDARY", "required_move_bps": 10,
           "settlement_state_side": "DOWN", "observed_ts": 10_000_000}
    result = estimate_row(row, {"settlement_reference_is_canonical": True, "settlement_side": "UP"}, [], {})
    assert result["p_up_empirical"] is None
    assert "cannot_reconstruct_remaining_average" in result["empirical_resolution_note"]


def test_empirical_report_excludes_stale_and_legacy_market_mid_quotes():
    rows = [
        {"market_slug": "stale", "checkpoint_sec": 120,
         "required_move_mode_group": "PRE_FINAL_PROXY", "settlement_side": "UP",
         "analytic_p_up": .5, "p_up_empirical": .8,
         "market_mid_probability_up": .7, "market_bbo_up_source_age_sec": 30,
         "market_bbo_up_received_age_sec": .1, "market_bbo_max_age_sec": 2},
        {"market_slug": "fresh", "checkpoint_sec": 120,
         "required_move_mode_group": "PRE_FINAL_PROXY", "settlement_side": "DOWN",
         "analytic_p_up": .5, "p_up_empirical": .2,
         "market_mid_probability_up": .3, "market_bbo_up_source_age_sec": .5,
         "market_bbo_up_received_age_sec": .2, "market_bbo_max_age_sec": 2},
        {"market_slug": "legacy", "checkpoint_sec": 120,
         "required_move_mode_group": "PRE_FINAL_PROXY", "settlement_side": "UP",
         "analytic_p_up": .5, "p_up_empirical": .8,
         "market_mid_probability_up": .7},
    ]

    metrics, _, paired = _metric_rows(rows)
    market = next(row for row in metrics if row["checkpoint_sec"] == 120
                  and row["required_move_mode_group"] == "PRE_FINAL_PROXY"
                  and row["source"] == "market_mid_probability_up")
    assert market["n_markets"] == 1
    assert market["n_observations"] == 1
    market_pairs = [row for row in paired if row["checkpoint_sec"] == 120
                    and row["empirical_minus_baseline"] == "market_mid_probability_up"]
    assert market_pairs and market_pairs[0]["n_paired_markets"] == 1
