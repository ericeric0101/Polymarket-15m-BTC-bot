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
    ResearchCandidateLifecycle,
    StrikeCrossingTracker,
    build_safety_sigma,
    build_shadow_labels,
    classify_time_et,
    edge_semantics,
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
    assert {"SHADOW_STRIKE_NEAR_1BPS", "SHADOW_LOW_SAFETY_SIGMA", "SHADOW_RECENT_CROSSING",
            "SHADOW_HIGH_CROSSING_COUNT", "SHADOW_WEEKEND", "SHADOW_WEAK_DEPTH"} <= set(labels["shadow_labels"])
    assert "should_quote" not in labels and "quantity" not in labels
    assert live_decision == before


def test_candidate_episode_identity_survives_minor_updates_and_closes_on_submit():
    lifecycle = ResearchCandidateLifecycle()
    candidate_id, episode, _ = lifecycle.candidate(slug="m1", instrument_id="up", intended_side="UP", now_ts=100)
    snapshot = {"entry_price": .70, "gross_edge_bucket": "2-5c", "net_edge_bucket": "unavailable",
                "strike_distance_bucket": "5-10bps", "shadow_entry_risk_level": "LOW",
                "current_leader": "UP", "crossings_last_120s": 0, "actual_size_multiplier": 1.0}
    assert lifecycle.should_emit(candidate_id, snapshot, now_ts=100, should_quote=False)[0] is True
    second_id, _, _ = lifecycle.candidate(slug="m1", instrument_id="up", intended_side="UP", now_ts=100.1)
    assert second_id == candidate_id
    small_move = {**snapshot, "entry_price": .701}
    assert lifecycle.should_emit(candidate_id, small_move, now_ts=100.1, should_quote=False)[0] is False
    tick_move = {**snapshot, "entry_price": .71}
    assert lifecycle.should_emit(candidate_id, tick_move, now_ts=100.7, should_quote=False, tick_size=.01)[0] is True
    assert lifecycle.should_emit(candidate_id, tick_move, now_ts=108, should_quote=False)[0] is True
    assert lifecycle.should_emit(candidate_id, tick_move, now_ts=108.1, should_quote=True)[0] is True
    emitted, invalidated = lifecycle.should_emit(candidate_id, tick_move, now_ts=108.2, should_quote=False)
    assert emitted is True
    assert invalidated["status"] == "candidate_invalidated"
    lifecycle.record_write_result(123, success=False)
    assert lifecycle.counters["research_snapshot_write_failures"] == 1
    assert lifecycle.complete(candidate_id, "candidate_submitted")["status"] == "candidate_invalidated"
    next_id, _, _ = lifecycle.candidate(slug="m1", instrument_id="up", intended_side="UP", now_ts=109)
    assert next_id != candidate_id


def test_candidate_side_flip_rollover_and_expiry_start_new_episodes():
    lifecycle = ResearchCandidateLifecycle()
    first, _, _ = lifecycle.candidate(slug="m1", instrument_id="up", intended_side="UP", now_ts=10)
    flipped, _, terminal = lifecycle.candidate(slug="m1", instrument_id="down", intended_side="DOWN", now_ts=11)
    assert flipped != first
    assert any(cid == first and state["status"] == "candidate_invalidated" for cid, state in terminal)
    rolled, _, terminal = lifecycle.candidate(slug="m2", instrument_id="new-up", intended_side="UP", now_ts=12)
    assert rolled != flipped
    assert any(cid == flipped and state["status"] == "market_rolled" for cid, state in terminal)
    expired, _, _ = lifecycle.candidate(slug="m2", instrument_id="new-up", intended_side="UP", now_ts=12 + lifecycle.TTL_SEC + 1)
    assert expired != rolled


@pytest.mark.parametrize("missing", ["fee", "penalty"])
def test_net_edge_stays_unavailable_when_either_cost_is_missing(missing):
    kwargs = {"fee_per_share": .01, "execution_penalty_per_share": .02}
    kwargs["fee_per_share" if missing == "fee" else "execution_penalty_per_share"] = None
    result = edge_semantics(probability=.8, entry_price=.7, method="fast_follow_resolution_ev_minus_fee_minus_markout", **kwargs)
    assert result["gross_probability_edge_ps"] == pytest.approx(.1)
    assert result["net_directional_edge_ps"] is None
    assert result["edge_cost_complete"] is False
    labels = build_shadow_labels(edge_ps=None, robust_net_usdc=None, fair=.8, entry_price=.7,
        abs_distance_bps=10, safety_sigma=2, crossings_last_120s=0, weekend=False,
        depth_adequate=True, gross_edge_ps=.1, edge_cost_complete=False)
    assert labels["shadow_edge_verdict"] == "SHADOW_EDGE_UNAVAILABLE"
    assert "SHADOW_GROSS_EDGE_POSITIVE" in labels["shadow_labels"]


def test_complete_net_edge_requires_both_costs_and_maker_uses_its_own_method():
    result = edge_semantics(probability=.8, entry_price=.7, fee_per_share=.01,
        execution_penalty_per_share=.02, method="fast_follow_resolution_ev_minus_fee_minus_markout")
    assert result["net_directional_edge_ps"] == pytest.approx(.07)
    assert result["edge_cost_complete"] is True
    maker = edge_semantics(probability=.8, entry_price=.7, fee_per_share=.0,
        execution_penalty_per_share=.0, method="maker_quote_economics")
    assert maker["gross_probability_edge_ps"] == pytest.approx(.1)
    assert maker["net_directional_edge_ps"] is None
    assert maker["edge_cost_complete"] is False


def test_research_write_failure_does_not_change_candidate_identity_or_live_choice():
    strategy = IntegratedBTCStrategy.__new__(IntegratedBTCStrategy)
    strategy.trade_db = object()
    strategy.current_market_slug = "m-100"
    strategy.side_decision_score = 0.2
    strategy._side_for_instrument_id = lambda _inst: SimpleNamespace(value="UP")
    strategy._db_strategy_event = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("journal unavailable"))
    strategy._build_live_entry_research_snapshot = lambda **kwargs: {
        "candidate_id": kwargs["candidate_id"], "entry_price": .7,
        "gross_edge_bucket": "positive", "net_edge_bucket": "unavailable",
        "strike_distance_bucket": "5-10bps", "shadow_entry_risk_level": "LOW",
        "current_leader": "UP", "crossings_last_120s": 0, "actual_size_multiplier": 1.0,
    }
    live_should_quote = True
    strategy._record_entry_decision_trace(
        now_ts=100.0, inst_id="up", side="buy", should_quote=live_should_quote,
        fair=.8, entry_price=.7, candidate_context={},
    )
    lifecycle = strategy._live_entry_research_lifecycle
    assert live_should_quote is True
    assert lifecycle.counters["research_snapshot_write_failures"] == 1
    candidate_id, _, _ = lifecycle.candidate(slug="m-100", instrument_id="up", intended_side="UP", now_ts=100.1)
    assert candidate_id in {state["candidate_id"] for state in lifecycle._active.values()}


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


def test_research_snapshot_weekend_and_et_labels_are_observational_only():
    strategy = IntegratedBTCStrategy.__new__(IntegratedBTCStrategy)
    strategy.current_market_slug = "btc-updown-15m-1790172000"
    strategy.current_market_end_timestamp = 1790172900
    strategy.market_strike_cache_by_slug = {strategy.current_market_slug: 100.0}
    strategy.market_strike_status_by_slug = {strategy.current_market_slug: "verified"}
    strategy.market_strike_source_by_slug = {strategy.current_market_slug: "canonical"}
    strategy.latest_external_spot = 100.001
    strategy.latest_external_spot_source = "polymarket_chainlink_twap_60s_ws"
    strategy._market_strike_is_entry_eligible = lambda _slug: True
    strategy._side_for_instrument_id = lambda _inst: SimpleNamespace(value="UP")
    now = datetime(2026, 9, 26, 7, 30, tzinfo=ZoneInfo("America/New_York")).timestamp()
    strategy.latest_external_spot_source_ts = now
    desired = {"should_quote": True, "size_multiplier": 0.8, "price": .7}
    before = dict(desired)
    snapshot = strategy._build_live_entry_research_snapshot(
        now_ts=now, inst_id="up", side="buy", fair=.8, entry_price=.7,
        robust_net_usdc=None,
        candidate_context={"entry_source": "normal_maker", "desired_entry": desired,
                          "time_left_sec": 120},
        candidate_id="candidate",
    )
    assert "SHADOW_WEEKEND" in snapshot["shadow_labels"]
    assert "SHADOW_ET_06_12" in snapshot["shadow_labels"]
    assert snapshot["shadow_only"] is True
    assert snapshot["shadow_has_order_authority"] is False
    assert desired == before


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
                "estimated_probability": 0.85,
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
    assert row["gross_probability_edge_ps"] == pytest.approx(.03)
    assert row["net_directional_edge_ps"] is None
    assert row["edge_cost_complete"] is False
    assert row["shadow_edge_verdict"] == "SHADOW_EDGE_UNAVAILABLE"
    rows_with_metrics, metrics = load_candidates(db, return_metrics=True)
    assert len(rows_with_metrics) == 1
    assert metrics["submit_to_candidate_join_rate"] == 1.0
    assert metrics["fill_to_candidate_join_rate"] == 1.0
    assert metrics["orphan_submit_count"] == 0
    assert metrics["orphan_fill_count"] == 0

    output = tmp_path / "report"
    assert generate_report(db, output) == 0
    expected = {"summary.md", "candidate_level.csv", "edge_buckets.csv", "gross_edge_buckets.csv", "candidate_join_quality.csv", "telemetry_health.csv", "price_buckets.csv",
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


def test_report_counts_orphan_submit_and_fill_rows(tmp_path):
    db = tmp_path / "journal.db"
    _make_journal(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO order_events VALUES(4, 2, 'ORDER_SUBMIT', 'orphan', 'BUY', .5, 2, 'SUBMITTED', NULL, '{}')")
        conn.execute("INSERT INTO order_events VALUES(5, 3, 'ORDER_FILLED', 'orphan', 'BUY', .5, 2, 'FILLED', NULL, '{}')")
    _, metrics = load_candidates(db, return_metrics=True)
    assert metrics["orphan_submit_count"] == 1
    assert metrics["orphan_fill_count"] == 1
    assert metrics["submit_to_candidate_join_rate"] == pytest.approx(.5)
    assert metrics["fill_to_candidate_join_rate"] == pytest.approx(.5)
