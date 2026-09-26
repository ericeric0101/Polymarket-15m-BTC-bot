from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from bot.live_entry_research import (
    StrikeCrossingTracker,
    build_safety_sigma,
    build_shadow_labels,
    classify_time_et,
)
from scripts.live_entry_quality_report import generate_report, load_candidates
from run_bot import IntegratedBTCStrategy


def test_strike_distance_and_leader_crossings_use_only_canonical_ticks():
    tracker = StrikeCrossingTracker()
    assert tracker.observe(slug="m", spot=101, strike=100, observed_ts=10, canonical=True)["current_leader"] == "UP"
    tracker.observe(slug="m", spot=99, strike=100, observed_ts=20, canonical=True)
    tracker.observe(slug="m", spot=98, strike=100, observed_ts=30, canonical=True)
    tracker.observe(slug="m", spot=101, strike=100, observed_ts=40, canonical=True)
    result = tracker.snapshot("m", 41)
    assert result["crossings_total"] == 2
    assert result["crossings_last_30s"] == 2
    assert result["crossings_last_60s"] == 2
    assert result["current_leader"] == "UP"

    tracker.observe(slug="m", spot=1, strike=100, observed_ts=42, canonical=False)
    assert tracker.snapshot("m", 42)["current_leader"] == "UP"
    ignored = tracker.observe(slug="m", spot=99, strike=100, observed_ts=39, canonical=True)
    assert ignored["stale_or_duplicate_tick_ignored"] is True
    assert tracker.snapshot("m", 42)["current_leader"] == "UP"


def test_crossing_windows_expire_old_crossings_without_fabricating_ticks():
    tracker = StrikeCrossingTracker()
    tracker.observe(slug="m", spot=101, strike=100, observed_ts=0, canonical=True)
    tracker.observe(slug="m", spot=99, strike=100, observed_ts=1, canonical=True)
    snapshot = tracker.snapshot("m", 302)
    assert snapshot["crossings_total"] == 1
    assert snapshot["crossings_last_300s"] == 0
    assert snapshot["last_canonical_observation_age_sec"] == 301


def test_weekend_and_et_time_block_labels_are_dst_aware():
    zone = ZoneInfo("America/New_York")
    saturday = datetime(2026, 9, 26, 7, 30, tzinfo=zone).timestamp()
    tuesday = datetime(2026, 9, 22, 13, 15, tzinfo=zone).timestamp()
    assert classify_time_et(saturday) == ("weekend", "06-12")
    assert classify_time_et(tuesday) == ("weekday", "12-18")
    assert classify_time_et(None) == (None, None)


def test_safety_sigma_uses_current_forecast_components_and_missing_is_null():
    data = build_safety_sigma(spot=100, strike=99, sigma_annual=0.5,
                              time_left_sec=365.25 * 24 * 3600,
                              sigma_source="current_forecast")
    assert data["safety_sigma"] == 0.02
    assert data["estimated_remaining_volatility_usd"] == 50
    assert data["safety_sigma_source"] == "current_forecast"
    missing = build_safety_sigma(spot=100, strike=None, sigma_annual=0.5,
                                 time_left_sec=60, sigma_source="current_forecast")
    assert missing["safety_sigma"] is None
    assert missing["safety_sigma_source"] is None
    assert missing["safety_sigma_unavailable_reason"]


def test_shadow_verdicts_are_annotations_only_and_do_not_mutate_live_decision():
    live_decision = {"should_quote": True, "size_multiplier": 0.8, "quantity": 10}
    before = dict(live_decision)
    labels = build_shadow_labels(edge_ps=-0.01, robust_net_usdc=-0.2, fair=0.7,
                                 entry_price=0.8, abs_distance_bps=0.5,
                                 safety_sigma=0.5, crossings_last_120s=2,
                                 weekend=True, depth_adequate=False)
    assert labels["shadow_reject"] is True
    assert labels["shadow_size_multiplier"] < 1
    assert labels["shadow_has_order_authority"] is False
    assert "should_quote" not in labels and "quantity" not in labels
    assert live_decision == before


def test_live_snapshot_uses_canonical_strike_and_keeps_missing_fields_null():
    strategy = IntegratedBTCStrategy.__new__(IntegratedBTCStrategy)
    strategy.current_market_slug = "btc-updown-15m-1790172000"
    strategy.current_market_end_timestamp = 1790172900
    strategy.market_strike_cache_by_slug = {strategy.current_market_slug: 100.0}
    strategy.market_strike_status_by_slug = {strategy.current_market_slug: "verified"}
    strategy.market_strike_source_by_slug = {strategy.current_market_slug: "polymarket_crypto_price_twap_open"}
    strategy.latest_external_spot = 100.01
    strategy.latest_external_spot_source = "polymarket_chainlink_twap_60s_ws"
    strategy.latest_external_spot_source_ts = 1000.0
    strategy._market_strike_is_entry_eligible = lambda slug: True
    strategy._side_for_instrument_id = lambda inst: SimpleNamespace(value="UP")
    strategy._live_strike_crossing_tracker = StrikeCrossingTracker()
    strategy._live_strike_crossing_tracker.observe(slug=strategy.current_market_slug, spot=100.01,
                                                   strike=100, observed_ts=1000, canonical=True)
    snapshot = strategy._build_live_entry_research_snapshot(
        now_ts=1001.0, inst_id="up-token", side="buy", fair=0.72, entry_price=0.70,
        robust_net_usdc=0.1,
        candidate_context={"time_left_sec": 200, "desired_entry": {"planned_quantity": 5,
                           "fee_ps": 0.01, "size_multiplier": 1.0}},
        candidate_id="candidate-1",
    )
    assert snapshot["official_strike"] == 100
    assert snapshot["current_reference_spot"] == 100.01
    assert snapshot["signed_distance_usd"] == pytest.approx(0.01)
    assert snapshot["current_leader"] == "UP"
    assert snapshot["outcome_side"] == "UP"
    assert snapshot["top_ask_size"] is None
    assert snapshot["safety_sigma"] is None

    strategy.latest_external_spot_source = "binance_ws"
    unavailable = strategy._build_live_entry_research_snapshot(
        now_ts=1001.0, inst_id="up-token", side="buy", fair=None, entry_price=None,
        robust_net_usdc=None, candidate_context={"time_left_sec": 200}, candidate_id="candidate-2",
    )
    assert unavailable["current_reference_spot"] is None
    assert unavailable["official_strike"] == 100
    assert unavailable["abs_distance_bps"] is None


def _make_journal(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE strategy_events(id INTEGER PRIMARY KEY, ts REAL, event_type TEXT, payload_json TEXT);
        CREATE TABLE order_events(id INTEGER PRIMARY KEY, ts REAL, event_type TEXT,
            client_order_id TEXT, side TEXT, price REAL, qty REAL, status TEXT,
            expected_net_usdc REAL, payload_json TEXT);
        """)
        slug = "btc-updown-15m-1790172000"
        candidate_id = f"{slug}|token|12345"
        snap = {"candidate_id": candidate_id, "slug": slug, "entry_source": "outcome_fast_follow",
                "outcome_side": "UP", "side": "BUY", "entry_price": 0.82,
                "planned_quantity": 5, "directional_edge_ps": -0.01,
                "shadow_reject": True, "shadow_reject_reason": "negative_robust_net",
                "shadow_size_multiplier": 0.5, "shadow_entry_risk_level": "HIGH"}
        conn.execute("INSERT INTO strategy_events VALUES(1, 1, 'FAST_FOLLOW_QUOTE_HANDOFF', ?)",
                     (json.dumps({"research_snapshot": snap, "research_candidate_id": candidate_id}),))
        conn.execute("INSERT INTO strategy_events VALUES(2, 2, 'MARKET_SETTLEMENT', ?)",
                     (json.dumps({"slug": slug, "outcome": "UP", "settlement_pnl_usdc": 1.0}),))
        conn.execute("INSERT INTO order_events VALUES(1, 1.1, 'ORDER_FAST_FOLLOW_SUBMIT', 'coid', 'BUY', .82, 5, 'SUBMITTED', .2, ?)",
                     (json.dumps({"research_candidate_id": candidate_id}),))
        conn.execute("INSERT INTO order_events VALUES(2, 1.2, 'ORDER_FILLED', 'coid', 'BUY', .82, 5, 'FILLED', .2, '{}')")
        conn.execute("INSERT INTO order_events VALUES(3, 1.3, 'FILL_MARKOUT', 'coid', 'BUY', .82, 5, 'RECORDED', NULL, ?)",
                     (json.dumps({"fill_id": "coid", "horizon_sec": 5, "signed_markout_ps": -0.02}),))


def test_report_joins_candidate_submit_fill_markout_and_settlement(tmp_path):
    db = tmp_path / "journal.db"
    _make_journal(db)
    rows = load_candidates(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["submitted"] is True and row["filled"] is True
    assert row["markout_5s"] == -0.02
    assert row["settlement_correct_if_taken"] is True
    assert row["settlement_pnl_usdc"] == 1.0
    assert row["shadow_size_counterfactual_pnl_usdc"] == 0.5

    output = tmp_path / "report"
    assert generate_report(db, output) == 0
    expected = {"summary.md", "candidate_level.csv", "edge_buckets.csv", "price_buckets.csv",
                "strike_distance_buckets.csv", "safety_sigma_buckets.csv", "crossing_buckets.csv",
                "weekday_weekend.csv", "hour_blocks.csv", "shadow_reject_counterfactual.csv",
                "shadow_size_counterfactual.csv"}
    assert expected <= {p.name for p in output.iterdir()}
    assert "`ENTRY_DECISION_TRACE` rows in the selected journal: 0." in (output / "summary.md").read_text(encoding="utf-8")
    with (output / "shadow_reject_counterfactual.csv").open(encoding="utf-8", newline="") as f:
        counterfactual = next(csv.DictReader(f))
    assert int(counterfactual["priced_directional_counterfactual_count"]) == 1
    assert float(counterfactual["gross_resolution_pnl_if_fully_filled_usdc"]) == pytest.approx(0.9)
    assert "fill probability" in counterfactual["counterfactual_basis"]
