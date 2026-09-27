import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backtest_profit_lock import (  # noqa: E402
    _active_ladder_floor, _entry_excursion, _partition_rows, _replay_exit, _split_development_holdout,
    _timed_exit_marks,
)


def mark(ts, price):
    return {"ts": ts, "price": price, "aggressor": "SELL", "outcome": "UP", "size": 10}


def test_loose_ladder_activation_and_floor_never_moves_backward():
    assert _active_ladder_floor("LOCK_A_LOOSE", 0.04, 0) is None
    assert _active_ladder_floor("LOCK_A_LOOSE", 0.05) == -0.05
    assert _active_ladder_floor("LOCK_A_LOOSE", 0.05, 0) == 0
    assert _active_ladder_floor("LOCK_A_LOOSE", 0.10, 0) == 0
    assert _active_ladder_floor("LOCK_A_LOOSE", 0.31, 0) == 0.15


def test_floor_breach_exits_once_and_uses_sell_print_path():
    path = [mark(101, .95), mark(102, .83), mark(103, .70)]
    result = _replay_exit("LOCK_A_LOOSE", .80, 100, 1000, path, "UP", "UP")
    assert result["exit_kind"] == "PROFIT_LOCK"
    assert result["exit_ts"] == 102
    assert result["exit_price"] == .83


def test_trailing_is_percentage_points_from_entry_not_fraction_of_peak():
    path = [mark(101, .96), mark(102, .88)]  # +20%, then +10%
    result = _replay_exit("TRAIL_10", .80, 100, 1000, path, "UP", "UP")
    assert result["exit_kind"] == "TRAILING_STOP"
    assert result["exit_ts"] == 102


def test_partial_tp_accounts_for_two_exit_legs_and_remaining_shares():
    path = [mark(101, .90), mark(102, .95)]
    result = _replay_exit("PARTIAL_A", .80, 100, 1000, path, "UP", "UP")
    assert result["partial_exit"] is True
    assert result["partial_fraction"] == .5
    assert result["exit_kind"] == "SETTLEMENT"
    assert result["gross_pnl"] > 0


def test_time_lock_activates_at_configured_remaining_time():
    path = [mark(101, .90), mark(102, .82)]
    result = _replay_exit("TIMELOCK_B", .80, 100, 200, path, "UP", "UP")
    assert result["exit_kind"] == "TIME_LOCK"
    assert result["exit_ts"] == 102


def test_same_timestamp_floor_ambiguity_can_be_excluded():
    path = [mark(100.5, .83), mark(101, .79), mark(101, .84)]
    result = _replay_exit("LOCK_C_TIGHT", .80, 100, 1000, path, "UP", "UP",
                          ambiguity_policy="exclude_ambiguous")
    assert result["ambiguous"] is True
    assert result["net_pnl"] is None


def test_sparse_print_path_marks_execution_uncertainty():
    path = [mark(101, .96), mark(160, .92)]
    result = _replay_exit("TRAIL_10", .80, 100, 1000, path, "UP", "UP", max_gap_sec=30)
    assert result["trailing_execution_uncertain"] is True
    assert result["max_gap_after_entry_sec"] == 840


def test_replay_has_no_future_lookahead_and_settles_if_no_exit():
    path = [mark(99, .99), mark(101, .82)]
    result = _replay_exit("HOLD_TO_SETTLEMENT", .80, 100, 1000, path, "UP", "UP")
    assert result["exit_kind"] == "SETTLEMENT"
    assert result["gross_pnl"] > 0


def test_mfe_mae_usdc_are_based_on_fixed_five_dollar_notional():
    result = _entry_excursion([mark(101, .88), mark(102, .72)], .80, 100)
    assert round(result["mfe_pct"], 8) == 10.0
    assert round(result["mfe_usdc"], 8) == .5
    assert round(result["mae_usdc"], 8) == -.5


def test_development_holdout_split_is_chronological_by_date():
    dev, holdout = _split_development_holdout(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"])
    assert max(dev) < min(holdout)
    assert not set(dev) & set(holdout)


def test_weekday_weekend_partition_is_explicit():
    rows = [{"weekday_weekend": "weekday"}, {"weekday_weekend": "weekend"}]
    assert len(_partition_rows(rows, "weekday")) == 1
    assert len(_partition_rows(rows, "weekend")) == 1
    assert len(_partition_rows(rows, "all")) == 2


def test_timed_exit_audit_covers_four_horizons_and_marks_reused_print():
    rows = _timed_exit_marks([mark(950, .9)], 1000, "slug", "120/0")
    assert [r["requested_exit_time_label"] for r in rows] == ["T-5m", "T-3m", "T-2m", "T-1m"]
    assert all("SAME_PROXY_PRINT_REUSED" in r["fallback_reason"] for r in rows)


def test_partial_b_does_not_activate_lock_floor_before_fifteen_percent_peak():
    path = [mark(101, .90), mark(102, .92), mark(103, .90)]
    result = _replay_exit("PARTIAL_B", .80, 100, 1000, path, "UP", "UP")
    assert result["partial_exit"] is True
    assert result["exit_kind"] == "SETTLEMENT"
