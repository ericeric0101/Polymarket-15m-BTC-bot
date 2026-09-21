import json
import sqlite3
import time

from monitoring.lead_lag_db import LeadLagDB
from scripts.archive_lead_lag_research import apply_retention
from scripts.hyperliquid_outcome_lead_lag_report import load_snapshots
from scripts.outcome_lead_lag_event_report import load_quality_gated_markouts, summarize


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


def test_reference_compaction_uses_global_market_sentinel_for_null_market_id(tmp_path):
    db_path = tmp_path / "lead_lag.db"
    db = LeadLagDB(str(db_path))
    for price in (7_700_000, 7_700_100):
        db.enqueue_reference_1s(
            run_id="r", slug="s", market_id=None, bucket_epoch_ms=1_000,
            source="binance", price_cents=price, received_epoch_ns=1_000_000_000,
        )
    db.stop()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT market_id, price_cents FROM reference_1s").fetchall()
    assert rows == [(LeadLagDB.GLOBAL_MARKET_ID, 7_700_100)]


def test_writer_explicitly_closes_each_batch_connection(tmp_path, monkeypatch):
    db = LeadLagDB(str(tmp_path / "lead_lag.db"))
    db.stop()
    closed = []

    class Connection:
        def execute(self, *_args):
            return None

        def executemany(self, *_args):
            return None

        def commit(self):
            return None

        def close(self):
            closed.append(True)

    monkeypatch.setattr(db, "_connect", Connection)
    db.enqueue_reference_1s(
        run_id="r", slug="s", market_id=None, bucket_epoch_ms=1_000,
        source="hyperliquid_btc_bbo", price_cents=7_700_000, received_epoch_ns=1_000_000_000,
    )
    db._writer()

    assert closed == [True]


def test_event_report_excludes_late_markouts_and_deduplicates_candidate_second(tmp_path):
    db_path = tmp_path / "lead_lag.db"
    db = LeadLagDB(str(db_path))
    payload = {"timely": True, "twap_change_cents": 100, "observed_elapsed_ms": 300, "decision": {"direction": 1}}
    db.enqueue_markout(run_id="r", slug="s", market_id=None, candidate_epoch_ns=1_000_000_000, horizon_ms=250, observed_epoch_ns=1_300_000_000, payload=payload)
    # A historical duplicate in the same direction/second must not become a
    # second independent observation in the report.
    db.enqueue_markout(run_id="r", slug="s", market_id=None, candidate_epoch_ns=1_100_000_000, horizon_ms=250, observed_epoch_ns=1_400_000_000, payload=payload)
    db.enqueue_markout(run_id="r", slug="s", market_id=None, candidate_epoch_ns=2_000_000_000, horizon_ms=250, observed_epoch_ns=2_400_000_000, payload={**payload, "timely": False})
    db.stop()
    rows = load_quality_gated_markouts(str(db_path))
    assert len(rows) == 1
    assert summarize(rows)["250"]["direction_hit_rate"] == 1.0


def test_manual_retention_archives_only_expired_raw_partitions_and_keeps_markouts(tmp_path):
    db_path = tmp_path / "lead_lag.db"
    archive_dir = tmp_path / "archive"
    db = LeadLagDB(str(db_path))
    old_ns = int((time.time() - 10 * 86_400) * 1_000_000_000)
    fresh_ns = int(time.time() * 1_000_000_000)
    db.enqueue_reference_1s(
        run_id="r", slug="old", market_id=1, bucket_epoch_ms=old_ns // 1_000_000,
        source="outcome_btc_mark", price_cents=7_700_000, received_epoch_ns=old_ns,
    )
    db.enqueue_reference_1s(
        run_id="r", slug="new", market_id=1, bucket_epoch_ms=fresh_ns // 1_000_000,
        source="outcome_btc_mark", price_cents=7_700_000, received_epoch_ns=fresh_ns,
    )
    db.enqueue_markout(
        run_id="r", slug="old", market_id=1, candidate_epoch_ns=old_ns,
        horizon_ms=60_000, observed_epoch_ns=old_ns + 60_000_000_000, payload={"timely": True},
    )
    db.stop()

    archived = apply_retention(str(db_path), str(archive_dir), raw_retention_days=7)

    assert archived and archived[0][0] == "reference_1s"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM reference_1s").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM lead_lag_markouts").fetchone()[0] == 1
        manifest = conn.execute("SELECT archive_path, row_count FROM lead_lag_archive_manifest").fetchone()
    assert manifest[1] == 1
    assert (archive_dir / f"reference_1s-{time.strftime('%Y-%m-%d', time.gmtime(old_ns / 1_000_000_000))}.jsonl.gz").exists()
