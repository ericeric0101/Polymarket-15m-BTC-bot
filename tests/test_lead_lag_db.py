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
        payload={
            "hyperliquid_outcome_available": True, "hyperliquid_outcome_mids_age_sec": 0.1,
            "twap_age_sec": 0.2, "hyperliquid_outcome_btc_mark": 100.0, "twap_price": 99.0,
            "binance_price": 101.0, "binance_age_sec": 0.2,
        },
    )
    db.enqueue_snapshot(
        run_id="run-a", polymarket_slug="btc-a", hyperliquid_market_id=1313,
        observed_ts=105.0,
        payload={
            "hyperliquid_outcome_available": False, "hyperliquid_outcome_mids_age_sec": 0.1,
            "twap_age_sec": 0.2, "hyperliquid_outcome_btc_mark": 101.0, "twap_price": 100.0,
        },
    )
    db.stop()

    assert load_snapshots(db_path) == [{
        "run_id": "run-a", "slug": "btc-a", "market_id": 1313,
        "ts": 100.0, "outcome_btc_mark": 100.0, "polymarket_twap": 99.0, "binance_price": 101.0,
    }]


def test_lead_lag_db_persists_compact_reference_decision_and_latency(tmp_path):
    db_path = tmp_path / "lead_lag.db"
    db = LeadLagDB(str(db_path))
    db.enqueue_reference_1s(run_id="r", slug="s", market_id=1, bucket_epoch_ms=1_000, source="outcome_btc_mark", price_cents=7_700_000, received_epoch_ns=1_000_000_000)
    db.enqueue_decision(run_id="r", slug="s", market_id=1, decision_epoch_ns=2_000_000_000, payload={"state": "observe"})
    db.enqueue_latency(run_id="r", client_order_id="", name="tick_to_decision", started_monotonic_ns=100, ended_monotonic_ns=250, created_epoch_ns=3_000_000_000)
    db.stop()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM reference_1s").fetchone()[0] == 1
        assert conn.execute("SELECT payload_json FROM lead_lag_decisions").fetchone()[0] == '{"state": "observe"}'
        assert conn.execute("SELECT elapsed_ns FROM latency_spans").fetchone()[0] == 150
