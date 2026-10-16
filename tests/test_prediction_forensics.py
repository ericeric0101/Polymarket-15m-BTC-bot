from scripts.prediction_forensics import (
    _direction_hit,
    _active_side_flips_before,
    _entry_p_ex,
    _lead_lag_rows,
    _metric_summary,
    _parse_side_reason,
    _poisson_binomial_tail,
    _repricing_model_comparison,
    _valid_model,
)


def test_model_probability_requires_fresh_source_and_unit_interval():
    base = {"sigma_ex_market_fresh": True, "p_up_ex_market": 0.72, "source_ts": 100.0}
    assert _valid_model(base)
    assert not _valid_model({**base, "sigma_ex_market_fresh": False})
    assert not _valid_model({**base, "source_ts": None})
    assert not _valid_model({**base, "p_up_ex_market": 1.2})
    assert not _valid_model({**base, "p_up_ex_market": None})


def test_entry_p_ex_requires_both_fresh_sigma_and_fresh_observation():
    row = {"sigma_ex_market_fresh": True, "p_up_ex_market": .8,
           "p_down_ex_market": .2, "feature_age_sec": 1.9}
    assert _entry_p_ex(row, "UP") == .8
    assert _entry_p_ex({**row, "feature_age_sec": 2.01}, "UP") is None
    assert _entry_p_ex({**row, "sigma_ex_market_fresh": False}, "UP") is None
    assert _entry_p_ex({**row, "feature_age_sec": 11}, "UP",
                       max_observation_age_sec=12) == .8


def test_side_reason_component_parser_preserves_logged_values_and_weights():
    parsed = _parse_side_reason("cs=+0.3944 conf=0.394 mkt=+0.550 btc=+0.081 zs=+0.626 w=0.63/0.22/0.15")
    assert parsed["reason_composite"] == 0.3944
    assert parsed["reason_market"] == 0.55
    assert parsed["reason_btc"] == 0.081
    assert parsed["reason_structural"] == 0.626
    assert (parsed["reason_w_market"], parsed["reason_w_btc"], parsed["reason_w_structural"]) == (0.63, 0.22, 0.15)


def test_probability_score_uses_only_present_outcomes():
    result = _metric_summary([{"p": 0.8, "outcome_up": 1}, {"p": None, "outcome_up": 0}], "p")
    assert result["n"] == 1
    assert result["brier"] == pytest.approx(0.04)
    assert result["direction_accuracy"] == 1.0


def test_lead_lag_pairs_same_direction_fresh_crossings_only():
    rows = [
        {"observed_ts": 1, "sigma_ex_market_fresh": True, "p_up_ex_market": .6,
         "source_ts": 1, "market_mid_probability_up": .6, "market_bbo_up_source_age_sec": .2,
         "best_bid_up": .59, "best_ask_up": .61},
        {"observed_ts": 2, "sigma_ex_market_fresh": True, "p_up_ex_market": .4,
         "source_ts": 2, "market_mid_probability_up": .55, "market_bbo_up_source_age_sec": .2,
         "best_bid_up": .54, "best_ask_up": .56},
        {"observed_ts": 3, "sigma_ex_market_fresh": True, "p_up_ex_market": .3,
         "source_ts": 3, "market_mid_probability_up": .4, "market_bbo_up_source_age_sec": .2,
         "best_bid_up": .39, "best_ask_up": .41},
    ]
    paired = [r for r in _lead_lag_rows("m", rows) if r["event"] == "CROSSING_LEAD_LAG"]
    assert len(paired) == 1
    assert paired[0]["transition_to_side"] == "DOWN"
    assert paired[0]["model_to_market_lead_sec"] == 1


def test_model_to_bot_pairs_explicit_active_side_flips_only():
    rows = [
        {"observed_ts": 1, "sigma_ex_market_fresh": True, "p_up_ex_market": .6,
         "source_ts": 1},
        {"observed_ts": 3, "sigma_ex_market_fresh": True, "p_up_ex_market": .4,
         "source_ts": 3},
        {"observed_ts": 5, "sigma_ex_market_fresh": True, "p_up_ex_market": .6,
         "source_ts": 5},
    ]
    side_rows = [
        {"observed_ts": 2, "active_side": "UP"},
        {"observed_ts": 2.5, "active_side": "NONE"},
        {"observed_ts": 3.5, "active_side": "DOWN"},
        {"observed_ts": 4, "active_side": "UP"},
    ]
    pairs = [r for r in _lead_lag_rows("m", rows, side_rows)
             if r["event"] == "MODEL_BOT_LEAD_LAG"]
    assert pairs[0]["status"] == "NOT_MEASURABLE"
    assert pairs[1]["status"] == "MEASURABLE"
    assert pairs[1]["transition_to_side"] == "UP"
    assert pairs[1]["model_to_bot_lead_sec"] == -1


def test_prior_active_side_flips_are_unavailable_without_directional_samples():
    rows = [{"observed_ts": 10, "active_side": "NONE", "_event_type": "SIDE_DECISION"},
            {"observed_ts": 20, "active_side": "NONE", "_event_type": "SIDE_DECISION"}]
    assert _active_side_flips_before(rows, 30, 30) is None
    rows += [{"observed_ts": 21, "active_side": "UP", "_event_type": "SIDE_DECISION"},
             {"observed_ts": 22, "active_side": "DOWN", "_event_type": "SIDE_DECISION"}]
    assert _active_side_flips_before(rows, 30, 30) == 1


def test_direction_hit_ignores_zero_movement():
    assert _direction_hit([0, 1, -1], [1, 1, 1]) == 0.5


def test_poisson_binomial_expected_win_tail():
    assert _poisson_binomial_tail([0.5, 0.5], 2) == pytest.approx(0.25)


def test_repricer_model_comparison_uses_common_market_sample():
    rows = []
    for i in range(8):
        rows.append({"horizon_sec": 5, "slug": str(i),
                     "mean_market_mid_up": .2 + i * .05,
                     "mean_probability_residual": (i % 3) * .01,
                     "mean_btc_return_10s_bps": (-1) ** i * (i + 1),
                     "mean_required_move_sigma": i * i * .1,
                     "mean_future_mid_change": .1 * (.2 + i * .05)})
    result = _repricing_model_comparison(rows)
    baseline = next(r for r in result if r["model"] == "A_mid_only")
    combined = next(r for r in result if r["model"] == "combined")
    assert baseline["n_markets"] == combined["n_markets"] == 8
    assert baseline["r_squared"] == pytest.approx(1.0)


import pytest
