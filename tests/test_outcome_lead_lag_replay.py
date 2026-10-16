from monitoring.outcome_lead_lag_replay import ReplayConfig, replay_rows


def test_replay_rows_scores_existing_reference_history_without_live_orders():
    rows = [
        ("run", "slug", "polymarket_twap", 7_700_000, 0),
        ("run", "slug", "outcome_btc_mark", 7_700_000, 100_000_000),
        ("run", "slug", "polymarket_twap", 7_700_000, 900_000_000),
        ("run", "slug", "outcome_btc_mark", 7_700_600, 1_100_000_000),
        ("run", "slug", "polymarket_twap", 7_700_200, 6_200_000_000),
    ]

    result = replay_rows(
        rows,
        ReplayConfig(shock_cents=500, residual_cents=300, debounce_ticks=1,
                     baseline_warmup_samples=1, horizon_ms=5_000),
    )

    assert result["candidate_count"] == 1
    assert result["observed_markout_count"] == 1
    assert result["direction_hit_rate"] == 1.0
    assert result["mean_signed_twap_move_usd"] == 2.0
