import json
import sqlite3

from bot.research.evidence import MarketEvidence
from bot.research.metrics import bounded_capacity, capital_efficiency
from bot.research.metrics import entry_timing_bin
from bot.research.indexing import apply_index_plan, index_status
from bot.research.store import ResearchStore


def _db(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE lead_lag_decisions (run_id TEXT, slug TEXT, market_id INTEGER, decision_epoch_ns INTEGER, payload_json TEXT)")
        payload = {"event_type": "PREDICTION_RESEARCH_SNAPSHOT", "freshness_clock_semantics_version": 2, "market_slug": "btc-updown-15m-1", "snapshot_ts": 10}
        conn.execute("INSERT INTO lead_lag_decisions VALUES (?, ?, ?, ?, ?)", ("r1", "btc-updown-15m-1", None, 10_000_000_000, json.dumps(payload)))
        conn.execute("INSERT INTO lead_lag_decisions VALUES (?, ?, ?, ?, ?)", ("r1", "btc-updown-15m-1", None, 10_000_000_001, json.dumps(payload)))


def test_store_is_read_only_and_deduplicates_snapshot_keys(tmp_path):
    path = tmp_path / "research.db"
    _db(path)
    store = ResearchStore(path)
    rows = store.get_prediction_snapshots()
    assert len(rows) == 1
    assert store.integrity()["quick_check"] == "ok"


def test_market_evidence_preserves_captured_values_without_recalculation():
    evidence = MarketEvidence.from_snapshot({"market_slug": "m", "snapshot_ts": 1, "p_up_ex_market": .7, "freshness_clock_semantics_version": 2},
                                            run_id="r", market_start_taipei="2026-10-03T00:00:00+08:00",
                                            session_regime="WEEKEND")
    assert evidence.get("p_up_ex_market") == .7
    assert evidence.session_regime == "WEEKEND"


def test_capital_and_bounded_capacity_metrics_are_descriptive():
    result = capital_efficiency(capital_committed_usdc=10, entry_ts=0, exit_ts=60, gross_pnl=2)
    assert result["capital_minutes"] == 10
    assert result["profit_per_dollar_minute"] == .2
    capacity = bounded_capacity([(0.60, 5), (0.61, 10), (0.63, 9)], side="BUY", reference_price=.60, max_slippage=.01)
    assert capacity["depth_available"] == 15
    assert capacity["capital_usdc"] == 9.1
    assert entry_timing_bin(600) == "480–600s"


def test_index_plan_is_idempotent(tmp_path):
    path = tmp_path / "index.db"
    _db(path)
    with sqlite3.connect(path) as conn:
        assert index_status(conn) is False
        assert apply_index_plan(conn) is True
        assert apply_index_plan(conn) is False
