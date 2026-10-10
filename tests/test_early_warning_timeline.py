"""Read-only early-warning timeline reconstruction (research/early_warning_timeline.py).

Exact Decimal arithmetic, >= for deterioration, censored (never interpolated)
observation gaps, PRIMARY vs pre-registered HYSTERESIS recovery, and the
Binance-relative / Chainlink-spot / TWAP split.  Nothing here can trade.
"""
from decimal import Decimal

import pytest

from research.early_warning_timeline import (
    HYSTERESIS_HOLD_SEC,
    HYSTERESIS_TICKS,
    MAX_OBS_GAP_SEC,
    binance_move_events,
    deterioration_episodes,
    normalize_row,
    position_timeline,
    spot_cross,
    token_episode_summary,
)


def _obs(path, *, start=0.0, step=1.0, state="FRESH_BID"):
    return [{"t": start + i * step, "bid": None if b is None else Decimal(str(b)), "bid_state": state}
            for i, b in enumerate(path)]


def test_preregistered_constants():
    assert HYSTERESIS_TICKS == 2 and HYSTERESIS_HOLD_SEC == 5.0 and MAX_OBS_GAP_SEC == 3.0


# --------------------------------------------------------------- D: episodes
PATH_D = [0.75] + [0.58] * 3 + [0.63] * 7 + [0.74] * 3 + [0.56] * 3   # t = 0..16


def test_first_recovery_second_deterioration_primary():
    result = deterioration_episodes(_obs(PATH_D), Decimal("0.75"), "0.15")
    assert result["status"] == "TRIGGERED"
    assert result["first_trigger_ts"] == 1.0
    assert result["first_recovery_ts"] == 4.0
    assert result["first_episode_duration_sec"] == 3.0
    assert result["second_trigger_ts"] == 14.0
    assert result["time_recovery_to_second_trigger_sec"] == 10.0
    first = result["episodes"][0]
    assert first["max_dd"] == "0.17" and first["start_censored"] is False and first["end_censored"] is False
    assert result["episodes"][1]["censored_at_end"] is True and result["episodes"][1]["end_ts"] is None


def test_first_recovery_second_deterioration_hysteresis():
    result = deterioration_episodes(_obs(PATH_D), Decimal("0.75"), "0.15", hysteresis=True)
    assert result["first_trigger_ts"] == 1.0
    assert result["first_recovery_ts"] == 4.0               # start of the >= 5 s hold at dd <= 0.13
    assert result["episodes"][0]["recovery_confirmed_ts"] == 9.0
    assert result["second_trigger_ts"] == 14.0


def test_exact_equality_triggers_without_float_error():
    # 0.70 - 0.55 is 0.1499999... in binary float; exact arithmetic says 0.15.
    result = deterioration_episodes(_obs([0.70, 0.55]), Decimal("0.70"), "0.15")
    assert result["first_trigger_ts"] == 1.0
    assert result["episodes"][0]["max_dd"] == "0.15"
    assert deterioration_episodes(_obs([0.70, 0.56]), Decimal("0.70"), "0.15")["status"] == "NOT_TRIGGERED"


def test_flapping_around_threshold_primary_vs_hysteresis():
    path = [0.75, 0.60, 0.61, 0.60, 0.61, 0.60, 0.61, 0.50]
    summary = token_episode_summary(_obs(path), Decimal("0.75"))["0.15"]
    assert summary["primary"]["episode_count"] == 4      # t1, t3, t5 and the final 0.50 at t7
    assert summary["hysteresis"]["episode_count"] == 1   # dd 0.14 never reaches X - 2 ticks
    assert summary["flap_count"] == 3
    assert summary["primary"]["second_trigger_ts"] == 3.0
    assert summary["hysteresis"]["second_trigger_ts"] is None


def test_hysteresis_hold_is_broken_by_a_rebound_inside_the_band():
    # dd 0.12 for 3 s, then 0.14 (inside X-2 ticks .. X), then 0.12 for 6 s.
    path = [0.75, 0.58] + [0.63] * 3 + [0.61] + [0.63] * 6
    result = deterioration_episodes(_obs(path), Decimal("0.75"), "0.15", hysteresis=True)
    assert result["first_recovery_ts"] == 6.0
    assert result["episode_count"] == 1


def test_never_triggered_and_no_observation_are_distinct():
    assert deterioration_episodes(_obs([0.75, 0.74]), Decimal("0.75"), "0.05")["status"] == "NOT_TRIGGERED"
    unknown = deterioration_episodes(_obs([0.75, 0.74], state="STALE"), Decimal("0.75"), "0.05")
    assert unknown["status"] == "UNKNOWN"
    assert deterioration_episodes(_obs([0.75]), None, "0.05")["status"] == "UNKNOWN"


# ------------------------------------------------- B/C: held-side bid states
def test_book_empty_is_exit_unavailable_and_counts_as_deterioration():
    obs = _obs([0.75, 0.74]) + [{"t": 2.0, "bid": Decimal("0.001"), "bid_state": "BOOK_EMPTY"}]
    result = deterioration_episodes(obs, Decimal("0.75"), "0.20")
    assert result["first_trigger_ts"] == 2.0
    episode = result["episodes"][0]
    assert episode["exit_unavailable_seen"] is True and episode["max_dd"] is None


def test_stale_held_side_is_unobserved_not_zero_drawdown():
    obs = _obs([0.75, 0.74]) + [{"t": 2.0 + i, "bid": None, "bid_state": "STALE"} for i in range(3)]
    obs += [{"t": 5.0, "bid": Decimal("0.55"), "bid_state": "FRESH_BID"}]
    result = deterioration_episodes(obs, Decimal("0.75"), "0.15")
    episode = result["episodes"][0]
    assert episode["start_ts"] == 5.0 and episode["start_ts_lower"] == 1.0 and episode["start_censored"] is True
    assert result["coverage"]["observed_points"] == 3 and result["coverage"]["total_points"] == 6


# ------------------------------------------------------------ F: obs gaps
def test_observation_gap_is_censored_not_interpolated():
    obs = _obs([0.75, 0.74]) + [{"t": 20.0, "bid": Decimal("0.55"), "bid_state": "FRESH_BID"},
                                {"t": 21.0, "bid": Decimal("0.70"), "bid_state": "FRESH_BID"}]
    result = deterioration_episodes(obs, Decimal("0.75"), "0.15")
    episode = result["episodes"][0]
    assert episode["start_ts"] == 20.0 and episode["start_ts_lower"] == 1.0
    assert episode["duration_sec"] == 1.0
    assert episode["duration_min_sec"] == 1.0 and episode["duration_max_sec"] == 20.0
    assert result["coverage"]["gaps"] == [{"from_ts": 1.0, "to_ts": 20.0, "duration_sec": 19.0}]


def test_gap_during_hysteresis_hold_restarts_the_hold():
    obs = _obs([0.75, 0.58, 0.63, 0.63]) + _obs([0.63] * 6, start=10.0)
    result = deterioration_episodes(obs, Decimal("0.75"), "0.15", hysteresis=True)
    assert result["first_recovery_ts"] == 10.0  # the 2..3 s hold never reached 5 s before the gap


# ----------------------------------------- A: Binance move vs Chainlink cross
def test_chainlink_tie_settles_up_and_binance_never_crosses_the_strike():
    rows = [{"snapshot_ts": 100.0 + i, "time_left_sec": 500 - i, "strike": 100000.0,
             "chainlink_spot": 100000.0, "chainlink_spot_fresh": True,
             "btc_spot": 100010.0, "btc_fresh": True, "best_bid_up": 0.75, "up_bid_state": "FRESH_BID",
             "official_twap": 100000.0, "twap_fresh": True} for i in range(5)]
    up = position_timeline(rows, held_side="UP", entry_ts=100.0, entry_bid=Decimal("0.75"),
                           entry_binance=Decimal("100010"))
    assert up["timeline"]["T_CHAINLINK_SPOT_CROSS"]["status"] == "NOT_TRIGGERED"
    assert up["timeline"]["T_BINANCE_1BPS"]["status"] == "NOT_TRIGGERED"   # relative to Binance entry
    assert not [key for key in up["timeline"] if "BINANCE" in key and "CROSS" in key]
    assert up["basis"]["binance_minus_chainlink_bps_median"] == pytest.approx(1.0)
    down = position_timeline(rows, held_side="DOWN", entry_ts=100.0, entry_bid=Decimal("0.25"),
                             entry_binance=Decimal("100010"))
    assert down["timeline"]["T_CHAINLINK_SPOT_CROSS"]["status"] == "ADVERSE_AT_ENTRY"
    assert down["timeline"]["T_TWAP_CROSS"]["status"] == "ADVERSE_AT_ENTRY"


@pytest.mark.parametrize("side,prices,status,ts", [
    ("UP", [100005, 100001, 99999], "CROSS", 2.0),
    ("DOWN", [99995, 99999, 100000], "CROSS", 2.0),      # tie is adverse for DOWN
    ("UP", [99999, 100001, 99998], "ADVERSE_AT_ENTRY", 0.0),
    ("UP", [100001, 100002], "NOT_TRIGGERED", None),
])
def test_spot_cross_sign_convention(side, prices, status, ts):
    result = spot_cross([(float(i), Decimal(p)) for i, p in enumerate(prices)], side, Decimal("100000"))
    assert result["status"] == status and result["ts"] == ts


def test_stale_chainlink_is_unknown_and_a_later_adverse_read_is_censored():
    assert spot_cross([(0.0, None), (1.0, None)], "UP", Decimal("100000"))["status"] == "UNKNOWN"
    late = spot_cross([(0.0, None), (1.0, None), (2.0, Decimal("99990"))], "UP", Decimal("100000"))
    assert late["status"] == "ADVERSE_AT_FIRST_KNOWN_OBSERVATION" and late["censored"] is True


def test_binance_relative_move_thresholds_use_exact_bps():
    points = [(0.0, Decimal("100000")), (1.0, Decimal("99970")), (2.0, Decimal("99950"))]
    up = binance_move_events(points, "UP", Decimal("100000"))
    assert up["3"]["ts"] == 1.0 and up["5"]["ts"] == 2.0 and up["10"]["status"] == "NOT_TRIGGERED"
    down = binance_move_events(points, "DOWN", Decimal("100000"))
    assert down["1"]["status"] == "NOT_TRIGGERED"
    assert binance_move_events(points, "UP", None)["1"]["status"] == "UNKNOWN"


# ------------------------------------------------------- E: no midpoint
def test_no_midpoint_substitution_when_bid_is_missing():
    row = {"snapshot_ts": 1.0, "best_bid_up": None, "best_ask_up": 0.62, "market_mid_up": 0.61,
           "market_quote_up_fresh": False}
    obs = normalize_row(row, "UP")
    assert obs["bid"] is None and obs["bid_state"] == "STALE"


# ------------------------------------------- H: old-schema rows and readers
def test_schema_v1_rows_still_reconstruct_with_explicit_limits():
    rows = [{"snapshot_ts": 10.0 + i, "time_left_sec": 400 - i, "strike": 100000.0, "btc_spot": 100000.0 - 40 * i,
             "btc_fresh": True, "best_bid_up": b, "market_quote_up_fresh": True, "market_quote_fresh": False,
             "official_twap": 100002.0, "twap_fresh": True, "required_move_sigma": 0.5,
             "required_move_z_diffusion": 0.4} for i, b in enumerate([0.75, 0.70, 0.60, 0.58])]
    result = position_timeline(rows, held_side="UP", entry_ts=10.0, entry_bid=Decimal("0.75"),
                               entry_binance=Decimal("100000"))
    assert result["schema_versions_seen"] == [1]
    assert result["book_empty_detectable"] is False
    assert result["timeline"]["T_CHAINLINK_SPOT_CROSS"]["status"] == "UNKNOWN"      # not recorded in v1
    assert result["timeline"]["T_TOKEN_015_FIRST"]["ts"] == 12.0
    first = result["timeline"]["T_TOKEN_015_FIRST"]
    assert first["context"] == {"tte": 398, "required_move_sigma": 0.5, "required_move_z_diffusion": 0.4}
    assert result["timeline"]["T_BINANCE_5BPS"]["ts"] == 12.0


def test_journal_events_unknown_vs_not_triggered():
    rows = [{"snapshot_ts": 1.0, "best_bid_up": 0.7, "up_bid_state": "FRESH_BID", "snapshot_schema_version": 2}]
    result = position_timeline(rows, held_side="UP", entry_ts=1.0, entry_bid=Decimal("0.7"),
                               events={"T_EXIT_SUBMIT": None, "T_MINUS2": 1.0})
    assert result["timeline"]["T_EXIT_SUBMIT"]["status"] == "NOT_TRIGGERED"
    assert result["timeline"]["T_EXIT_FILL"]["status"] == "UNKNOWN"
    assert result["timeline"]["T_MINUS2"]["status"] == "TRIGGERED"
    assert result["timeline"]["T_MINUS2"]["context"]["tte"] is None


def test_held_side_v2_fields_take_precedence_over_per_side_legacy():
    row = {"snapshot_ts": 1.0, "snapshot_schema_version": 2, "held_side": "UP", "held_side_bid": 0.001,
           "held_side_bid_state": "BOOK_EMPTY", "best_bid_up": None, "up_bid_state": "STALE"}
    obs = normalize_row(row, "UP")
    assert obs["bid_state"] == "BOOK_EMPTY" and obs["bid"] == Decimal("0.001")


def test_existing_reader_accepts_mixed_v1_and_v2_rows(tmp_path):
    import json
    import sqlite3

    from bot.prediction_research_snapshot import build_prediction_snapshot
    from bot.research.store import ResearchStore
    from test_prediction_research_snapshot import _context

    path = tmp_path / "research.db"
    v2 = build_prediction_snapshot(_context(chainlink_spot=100000.0, chainlink_received_ts=99.8,
                                            market_up_bid_size=10, market_down_bid_size=0, snapshot_ts=101.0))
    v1 = {k: v for k, v in build_prediction_snapshot(_context()).items()   # a pre-v2 row: no new keys
          if not (k.startswith(("chainlink_", "up_bid", "down_bid", "held_")) or k == "snapshot_schema_version")}
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE lead_lag_decisions (run_id TEXT, slug TEXT, market_id INTEGER, "
                     "decision_epoch_ns INTEGER, payload_json TEXT)")
        for row in (v1, v2):
            conn.execute("INSERT INTO lead_lag_decisions VALUES (?, ?, ?, ?, ?)",
                         ("r", "btc-updown-15m-100", None, int(row["snapshot_ts"] * 1e9), json.dumps(row)))
    rows = ResearchStore(path).get_prediction_snapshots()
    assert [r.get("snapshot_schema_version") for r in rows] == [None, 2]
    timeline = position_timeline(rows, held_side="UP", entry_ts=100.0, entry_bid=Decimal("0.60"))
    assert timeline["schema_versions_seen"] == [1, 2] and timeline["snapshot_count"] == 2
