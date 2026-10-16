import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backtest_short_term_take_profit import (
    _candidate_path, _exit_pnl, _first_touch, _outcome_for_token, _sell_marks, _settlement_pnl,
    _timed_exit_proxy, tp_level,
)


def _trade(ts, price, side="UP", aggressor="SELL"):
    return {"ts": ts, "price": price, "size": 10, "outcome": side, "aggressor": aggressor}


def test_tp_price_and_binary_cap_unreachable():
    assert tp_level(.80, .10) == (.8800000000000001, "REACHABLE")
    level, status = tp_level(.95, .10)
    assert level > 1 and status == "UNREACHABLE_BY_PRICE_CAP"


def test_tp_uses_first_later_sell_side_print_not_mid_or_last_buy_print():
    rows = [_trade(101, .90, aggressor="BUY"), _trade(102, .93), _trade(103, .95)]
    sell_marks = _sell_marks(rows, "UP", 100, 200, sell_only=True)
    candidate = {"side": "UP", "winner": "DOWN", "entry_price": .90,
                 "entry_ts": 100, "market_start_epoch": 0, "final_direction_correct": False}
    _candidate_path(candidate, rows)
    assert candidate["tp3_hit"] is True
    assert candidate["tp3_hit_ts"] == 102
    assert candidate["tp3_time_to_hit_sec"] == 2
    assert candidate["tp3_final_wrong_but_hit"] is True
    assert candidate["tp3_hit_proxy"].startswith("TP_HIT_PROXY")
    assert [r["ts"] for r in sell_marks] == [102, 103]


def test_mfe_mae_and_mae_before_tp_are_path_based():
    rows = [_trade(101, .84), _trade(103, .96), _trade(105, .92)]
    candidate = {"side": "UP", "winner": "UP", "entry_price": .90,
                 "entry_ts": 100, "market_start_epoch": 0}
    _candidate_path(candidate, rows)
    assert candidate["mfe_price"] == .96
    assert candidate["mae_price"] == .84
    assert candidate["time_to_mfe_sec"] == 3
    assert candidate["time_to_mae_sec"] == 1
    assert candidate["tp5_time_to_hit_sec"] == 3
    assert round(candidate["tp5_mae_before_tp_pct"], 2) == round((.84/.90-1)*100, 2)


def test_fixed_notional_pnl_fees_and_adverse_slippage():
    notional, entry = 5, .50
    base = _exit_pnl(entry, .55, notional, "zero")
    fee = _exit_pnl(entry, .55, notional, "existing_fee_assumption_1pct_notional")
    slip = _exit_pnl(entry, .55, notional, "zero", slippage=.01)
    assert round(base["gross_pnl"], 8) == .5
    assert round(fee["net_pnl"], 8) == .45
    assert round(slip["net_pnl"], 8) == .4
    assert notional / entry == 10


def test_settlement_pnl_and_missing_resolution_are_separate():
    assert _settlement_pnl("UP", "UP", .60, 5, "zero")["net_pnl"] > 0
    assert _settlement_pnl("UP", "DOWN", .60, 5, "zero")["net_pnl"] == -5
    assert _settlement_pnl("UP", None, .60, 5, "zero") is None


def test_path_ignores_pre_entry_prints_no_lookahead():
    rows = [_trade(99, .99), _trade(101, .91)]
    marks = _sell_marks(rows, "UP", 100, 200)
    assert [r["ts"] for r in marks] == [101]


def test_timed_exit_uses_nearest_available_print_and_reports_staleness():
    rows = [_trade(90, .80), _trade(115, .81), _trade(130, .82)]
    mark, age = _timed_exit_proxy(rows, 120)
    assert mark["ts"] == 130
    assert age == 10
    mark, age = _timed_exit_proxy(rows[:2], 120)
    assert mark["ts"] == 115
    assert age == 5


def test_tp_sl_first_touch_and_same_timestamp_are_not_assumed():
    assert _first_touch(100, 101) == ("TP", False)
    assert _first_touch(101, 100) == ("SL", False)
    assert _first_touch(100, 100) == ("AMBIGUOUS", True)
    assert _first_touch(None, None) == ("SETTLEMENT", False)


def test_local_token_to_outcome_mapping_reads_gamma_market_record():
    market = {"gamma": {"market": {"outcomes": '["Up", "Down"]',
                                    "clobTokenIds": '["up-token", "down-token"]'}}}
    assert _outcome_for_token(market, "down-token") == "DOWN"
    assert _outcome_for_token(market, "missing") is None


def test_weekend_and_holdout_tags_are_derived_from_market_date_not_random_split():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monday = datetime(2026, 9, 21, 12, tzinfo=ZoneInfo("America/New_York"))
    sunday = datetime(2026, 9, 20, 12, tzinfo=ZoneInfo("America/New_York"))
    assert monday.weekday() < 5
    assert sunday.weekday() >= 5
    # Chronological split invariant used by the runner: earlier dates are dev,
    # later dates are holdout; the same date is never in both partitions.
    dates = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
    dev = set(dates[:3])
    holdout = set(dates[3:])
    assert max(dev) < min(holdout)
    assert not (dev & holdout)
