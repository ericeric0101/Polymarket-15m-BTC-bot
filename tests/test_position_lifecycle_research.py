from __future__ import annotations

import csv
import json
import sqlite3

from bot.research.lifecycle import adverse_btc, first_crossings, position_lifecycle_id
from scripts.research_analysis import _price_bucket, _sigma_bucket, position_lifecycle_analysis


def test_lifecycle_identity_is_deterministic_and_not_timestamp_derived():
    assert position_lifecycle_id(
        market_slug="btc-updown-15m-1", instrument_id="up-token", entry_client_order_id="buy-1",
    ) == "btc-updown-15m-1|up-token|buy-1"


def test_snapshot_crossings_use_fresh_probability_and_held_side_sign():
    rows = [
        {"snapshot_ts": 10, "required_move_sigma": 2.1, "p_ex_fresh": True,
         "p_up_ex_market": .92, "p_down_ex_market": .08, "btc_return_5s_bps": .1,
         "btc_return_10s_bps": -.2, "settlement_state_side": "UP"},
        {"snapshot_ts": 20, "required_move_sigma": 1.4, "p_ex_fresh": False,
         "p_up_ex_market": .70, "p_down_ex_market": .30, "btc_return_5s_bps": -.3,
         "btc_return_10s_bps": -.4, "settlement_state_side": "DOWN"},
        {"snapshot_ts": 30, "required_move_sigma": .4, "p_ex_fresh": True,
         "p_up_ex_market": .80, "p_down_ex_market": .20, "btc_return_30s_bps": -.5,
         "settlement_state_side": "DOWN"},
    ]
    result = first_crossings(rows, side="UP")
    assert result["sigma_cross_1_5_ts"] == 20
    assert result["sigma_cross_0_5_ts"] == 30
    # The stale .70 row cannot generate a fake 15% analytic flip crossing.
    assert result["flip_p_15_ts"] == 30
    assert result["first_adverse_btc5_ts"] == 20
    assert result["first_adverse_btc10_ts"] == 10
    assert result["first_adverse_btc30_ts"] == 30
    assert result["first_settlement_state_flip_ts"] == 20


def test_adverse_btc_sign_is_opposite_for_down_position():
    assert adverse_btc({"btc_return_10s_bps": -.5}, side="UP", horizon_sec=10) is True
    assert adverse_btc({"btc_return_10s_bps": -.5}, side="DOWN", horizon_sec=10) is False
    assert adverse_btc({}, side="DOWN", horizon_sec=10) is None


def test_same_price_and_sigma_buckets_have_fixed_boundaries():
    assert _price_bucket(.60) == "0.60–0.65"
    assert _price_bucket(.90) is None
    assert _sigma_bucket(.49) == "<0.5σ"
    assert _sigma_bucket(.5) == "0.5–1σ"
    assert _sigma_bucket(2.0) == ">2σ"


def test_offline_lifecycle_joins_entry_snapshots_stop_and_settlement(tmp_path):
    research, journal, output = tmp_path / "research.db", tmp_path / "journal.db", tmp_path / "out"
    slug, run, instrument = "btc-updown-15m-1791072000", "r1", "up-token"
    with sqlite3.connect(research) as conn:
        conn.execute("CREATE TABLE lead_lag_decisions (id INTEGER PRIMARY KEY, run_id TEXT, slug TEXT, market_id INTEGER, decision_epoch_ns INTEGER, payload_json TEXT)")
        def event(ts, payload):
            conn.execute("INSERT INTO lead_lag_decisions VALUES (NULL, ?, ?, NULL, ?, ?)", (run, slug, int(ts * 1e9), json.dumps(payload)))
        for ts, sigma, p_up, mid, leader in ((100, 2.1, .95, .80, "UP"), (110, 1.4, .80, .74, "UP"), (120, .4, .70, .70, "DOWN")):
            event(ts, {"event_type":"PREDICTION_RESEARCH_SNAPSHOT", "freshness_clock_semantics_version": 2, "market_slug":slug, "snapshot_ts":ts,
                       "joint_fresh":True, "p_ex_fresh":True, "p_up_ex_market":p_up, "p_down_ex_market":1-p_up,
                       "required_move_sigma":sigma, "required_move_bps":sigma, "market_mid_up":mid,
                       "best_ask_up":mid+.01, "market_mid_up_fresh":True, "up_mid":mid,
                       "settlement_state_side":leader, "time_left_sec":900-ts, "up_instrument_id":instrument})
        event(200, {"event_type":"MARKET_TWAP_SUMMARY", "market_slug":slug, "canonical_settlement_side":"DOWN"})
        lifecycle = position_lifecycle_id(market_slug=slug, instrument_id=instrument, entry_client_order_id="buy-1")
        event(130, {"event_type":"STOP_SHADOW_ACTUAL_STOP", "market_slug":slug, "position_lifecycle_id":lifecycle,
                    "actual_stop_ts":130, "actual_stop_price":.65, "actual_stop_pnl":-1.5, "reason":"stop_loss"})
    with sqlite3.connect(journal) as conn:
        conn.execute("""CREATE TABLE order_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT,
                       client_order_id TEXT, side TEXT, price REAL, qty REAL, instrument_id TEXT, payload_json TEXT)""")
        conn.execute("INSERT INTO order_events VALUES (1, ?, ?, 'ORDER_FILLED', 'buy-1', 'BUY', .82, 5, ?, ?)",
                     ("1970-01-01T00:01:40+00:00", run, instrument,
                      json.dumps({"slug":slug, "market_slug":slug, "research_candidate_id":f"{slug}|{instrument}|UP|99"})))
    result = position_lifecycle_analysis(research, journal, output)
    assert result["positions"] == 1
    with (output / "position_lifecycle.csv").open() as handle:
        row = next(csv.DictReader(handle))
    assert row["entry_anchor_status"] == "JOINED"
    assert row["first_adverse_btc5_ts"] == ""  # no BTC value was fabricated
    assert row["sigma_cross_1_5_ts"] == "110.0"
    assert row["first_market_adverse_5c_ts"] == "120.0"
    assert row["stop_join_status"] == "EXACT_IDENTITY"
    assert row["entry_won_final_settlement"] == "False"
