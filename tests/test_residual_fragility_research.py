from scripts.residual_fragility_research import (
    _bin, _cluster_mean_ci, _fit_metrics, _future_match, _residual_bins,
    RESIDUAL_BINS,
)


def test_residual_bins_use_fixed_edges_without_optimization():
    assert _bin(-0.12, RESIDUAL_BINS) == "<=-0.10"
    assert _bin(-0.08, RESIDUAL_BINS) == "-0.10_to_-0.05"
    assert _bin(0.0, RESIDUAL_BINS) == "-0.02_to_+0.02"
    assert _bin(0.12, RESIDUAL_BINS) == ">=+0.10"


def test_future_match_uses_only_forward_fresh_candidates_and_narrow_tolerance():
    current = 100.0
    rows = [
        {"observed_ts": 104.0, "fresh": True, "market_slug": "m"},
        {"observed_ts": 105.7, "fresh": False, "market_slug": "m"},
        {"observed_ts": 106.1, "fresh": True, "market_slug": "m"},
    ]
    result = _future_match(rows, 105.0, current, tolerance=1.5)
    assert result["observed_ts"] == 104.0
    assert _future_match(rows, 105.0, current, tolerance=0.5) is None


def test_market_cluster_bootstrap_counts_markets_not_rows_as_clusters():
    rows = [
        {"market_slug": "a", "x": 1.0},
        {"market_slug": "a", "x": 3.0},
        {"market_slug": "b", "x": 5.0},
    ]
    mean, low, high, n_markets = _cluster_mean_ci(rows, "x", reps=100, seed=1)
    assert mean == 3.0
    assert n_markets == 2
    assert low is not None and high is not None and low <= high


def test_model_comparison_zero_baseline_and_mid_only_are_descriptive():
    rows = [
        {"market_slug": "a", "observed_ts": 1, "market_mid_up": .5, "residual_up": .1,
         "btc_return_10s_bps": 1.0, "future_mid_change": .01},
        {"market_slug": "b", "observed_ts": 2, "market_mid_up": .5, "residual_up": -.1,
         "btc_return_10s_bps": -1.0, "future_mid_change": -.01},
        {"market_slug": "c", "observed_ts": 3, "market_mid_up": .5, "residual_up": .2,
         "btc_return_10s_bps": 2.0, "future_mid_change": .02},
    ]
    zero = _fit_metrics(rows, [])
    residual = _fit_metrics(rows, ["residual_up"])
    assert zero["n_observations"] == 3
    assert zero["mae"] > 0
    assert residual["n_markets"] == 3
    assert residual["mae"] < zero["mae"]


def test_residual_bin_summary_keeps_horizons_separate_and_clusters_by_market():
    rows = [
        {"market_slug": "m1", "residual_up": .06, "mid_change_5s": .01, "mid_change_10s": -.01,
         "mid_change_30s": .02, "mid_change_60s": .03},
        {"market_slug": "m2", "residual_up": .08, "mid_change_5s": .02, "mid_change_10s": .01,
         "mid_change_30s": .03, "mid_change_60s": .04},
    ]
    summary = _residual_bins(rows)
    five = next(row for row in summary if row["residual_bin"] == "+0.05_to_+0.10" and row["horizon_sec"] == 5)
    ten = next(row for row in summary if row["residual_bin"] == "+0.05_to_+0.10" and row["horizon_sec"] == 10)
    assert five["n_unique_markets"] == 2
    assert five["mean_future_mid_change"] == .015
    assert ten["mean_future_mid_change"] == 0
