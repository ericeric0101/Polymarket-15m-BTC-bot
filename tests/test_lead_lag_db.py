import json
import sqlite3

from monitoring.lead_lag_db import LeadLagDB
from scripts.hyperliquid_outcome_lead_lag_report import load_snapshots


def test_lead_lag_db_batches_raw_snapshots_and_flushes_on_stop(tmp_path):
    db_path = tmp_path / "hyperliquid_lead_lag.db"
    db = LeadLagDB(str(db_path))
    db.enqueue_snapshot(
        run_id="run-a", polymarket_slug="btc-a", hyperliquid_market_id=1313,
        observed_ts=100.25, payload={"up_mid": 0.5},
    )
    db.enqueue_snapshot(
        run_id="run-a", polymarket_slug="btc-a", hyperliquid_market_id=1313,
        observed_ts=105.25, payload={"up_mid": 0.51},
    )
    db.stop()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT run_id, polymarket_slug, hyperliquid_market_id, observed_ts_ms, payload_json FROM snapshots ORDER BY id"
        ).fetchall()
    assert [(row[0], row[1], row[2], row[3]) for row in rows] == [
        ("run-a", "btc-a", 1313, 100250), ("run-a", "btc-a", 1313, 105250),
    ]
    assert json.loads(rows[0][4]) == {"up_mid": 0.5}


def test_report_loads_only_quality_gated_rows_from_dedicated_db(tmp_path):
    db_path = tmp_path / "hyperliquid_lead_lag.db"
    db = LeadLagDB(str(db_path))
    db.enqueue_snapshot(
        run_id="run-a", polymarket_slug="btc-a", hyperliquid_market_id=1313,
        observed_ts=100.0,
        payload={"hyperliquid_outcome_analysis_available": True, "hyperliquid_outcome_side0_bbo_mid": 0.5, "up_mid": 0.4},
    )
    db.enqueue_snapshot(
        run_id="run-a", polymarket_slug="btc-a", hyperliquid_market_id=1313,
        observed_ts=105.0,
        payload={"hyperliquid_outcome_analysis_available": False, "hyperliquid_outcome_side0_bbo_mid": 0.51, "up_mid": 0.41},
    )
    db.stop()

    assert load_snapshots(db_path) == [{
        "run_id": "run-a", "slug": "btc-a", "market_id": 1313,
        "ts": 100.0, "outcome_side0": 0.5, "polymarket_up": 0.4,
    }]
