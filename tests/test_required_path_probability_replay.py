from scripts.required_path_probability_replay import (
    _market_cluster_ci,
    candidate_paths,
    estimate_probability,
    is_flip,
    paired_market_comparison,
    required_average_boundary,
    settlement_side_from_average,
)


def _rows(prices):
    return [{"ts_sec": i, "close": price} for i, price in enumerate(prices)]


def test_exact_required_average_boundary_matches_partial_integral_identity():
    # 60s official average must equal 100 after 30s at observed average 98.
    boundary = required_average_boundary(100, 98, 30, 60)
    assert boundary == 102
    assert settlement_side_from_average((30 * 98 + 30 * boundary) / 60, 100) == "UP"


def test_up_leader_flip_requires_strictly_below_boundary_but_down_tie_flips_up():
    assert is_flip("UP", 99.99, 100)
    assert not is_flip("UP", 100, 100)
    assert not is_flip("DOWN", 99.99, 100)
    assert is_flip("DOWN", 100, 100)


def test_normalized_historical_path_projects_to_current_spot():
    # Historical [100, 90, 80] becomes [200, 180, 160] around current spot 200.
    paths = candidate_paths(_rows([100, 90, 80]), evaluation_ts=3,
                            horizon_sec=3, current_spot=200,
                            boundary=175, current_side="UP")
    assert len(paths) == 1
    assert paths[0]["synthetic_average"] == 180
    assert paths[0]["flip"] is False


def test_path_average_probability_is_distinct_from_endpoint_diagnostic():
    # Mean of [100, 90, 110] is 100 (<105), while endpoint 110 is above 105.
    paths = candidate_paths(_rows([100, 100, 90, 110, 120]), evaluation_ts=5,
                            horizon_sec=3, current_spot=100,
                            boundary=105, current_side="UP")
    first = next(row for row in paths if row["start_sec"] == 1)
    assert first["synthetic_average"] == 100
    assert first["flip"] is True
    assert first["endpoint_flip"] is False


def test_missing_second_is_not_filled_and_coverage_sensitivity_rejects_path():
    rows = [{"ts_sec": 0, "close": 100}, {"ts_sec": 2, "close": 80},
            {"ts_sec": 3, "close": 70}]
    paths = candidate_paths(rows, evaluation_ts=4, horizon_sec=3,
                            current_spot=100, boundary=90, current_side="UP")
    assert len(paths) == 2  # missing start second is counted as a rejected path
    start_zero = next(row for row in paths if row["start_sec"] == 0)
    assert start_zero["coverage"] == 2 / 3
    assert estimate_probability([start_zero], coverage_threshold=.90)["eligible_historical_paths"] == 0
    assert estimate_probability([start_zero], coverage_threshold=.90)["rejected_missing_data_paths"] == 1


def test_no_lookahead_excludes_windows_ending_after_evaluation_time():
    paths = candidate_paths(_rows([100, 101, 102, 103, 104, 105]),
                            evaluation_ts=4.5, horizon_sec=3,
                            current_spot=100, boundary=102, current_side="UP")
    assert all(path["start_sec"] + 3 <= 4.5 for path in paths)
    assert all(path["start_sec"] in {0, 1} for path in paths)


def test_volatility_conditioning_falls_back_when_sample_is_sparse():
    paths = [{"coverage": 1.0, "flip": bool(i % 2), "endpoint_flip": bool(i % 2),
              "trailing_sigma": None} for i in range(20)]
    result = estimate_probability(paths, coverage_threshold=1.0, current_sigma=.5)
    assert result["empirical_vol_conditioned_flip_probability"] is None
    assert result["conditioning_mode"] == "UNCONDITIONAL_FALLBACK"
    assert result["vol_fallback_reason"] == "insufficient_vol_labeled_paths"


def test_market_cluster_bootstrap_uses_market_as_resampling_unit():
    result = _market_cluster_ci([
        {"market_slug": "A", "delta": 1.0},
        {"market_slug": "A", "delta": 3.0},
        {"market_slug": "B", "delta": 5.0},
    ], "delta", reps=100)
    assert result["n_markets"] == 2
    assert result["mean"] == 3.5  # mean of market means (2 and 5), not rows (3)


def test_paired_comparison_excludes_noncanonical_and_clusters_by_market():
    rows = [
        {"market_slug": "A", "checkpoint_sec": 30, "settlement_reference_is_canonical": True,
         "settlement_side": "UP", "analytic_p_up_ex_market": .8, "p_up_empirical_path": .7},
        {"market_slug": "A", "checkpoint_sec": 30, "settlement_reference_is_canonical": True,
         "settlement_side": "UP", "analytic_p_up_ex_market": .8, "p_up_empirical_path": .7},
        {"market_slug": "B", "checkpoint_sec": 30, "settlement_reference_is_canonical": False,
         "settlement_side": "UP", "analytic_p_up_ex_market": .8, "p_up_empirical_path": .7},
    ]
    result = paired_market_comparison(rows)
    assert len(result) == 1
    assert result[0]["paired_observations"] == 2
    assert result[0]["brier_n_markets"] == 1
