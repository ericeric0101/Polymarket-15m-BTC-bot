"""Collection telemetry never supplies trading or rollover authority."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from bot import launcher
from bot.ops import collection_lifecycle
from bot.prediction_research_snapshot import PredictionResearchSnapshotter
from bot.research.provenance import process_identity
from monitoring.trade_journal_db import TradeJournalDB


def strategy(journal, cycle=1):
    return SimpleNamespace(trade_db=journal, run_id=f"run-{cycle}",
        current_market_slug="market", collection_identity={**process_identity(), "cycle_idx": cycle})


def test_process_identity_stays_fixed_across_cycles_and_run_is_not_process():
    events = []
    journal = SimpleNamespace(enqueue_strategy_event=lambda *args: events.append(args) or True)
    for cycle in (1, 2):
        collection_lifecycle(strategy(journal, cycle), "new_node_built")
    a, b = [event[2] for event in events]
    assert a["process_instance_id"] == b["process_instance_id"]
    assert a["pid"] == b["pid"]
    assert a["process_started_at"] == b["process_started_at"]
    assert a["cycle_idx"] != b["cycle_idx"]
    assert a["run_id"] != b["run_id"]


@pytest.mark.parametrize("source,context", [
    ("scheduled_auto_rollover", {}),
    ("strategy_requested", {"lifecycle_reason": "stale_instrument_lifecycle", "waiting_miss_count": 3}),
    ("quote_watchdog_recovery", {"watchdog_trigger": "stale"}),
])
def test_stop_cause_persisted_before_node_stop_once(source, context):
    events = []
    journal = SimpleNamespace(enqueue_strategy_event=lambda *args: events.append(args[2]) or True)
    host = strategy(journal)
    host._collection_stop_context = {"stop_request_source": source, **context}
    stops = []
    node = SimpleNamespace(trader=SimpleNamespace(strategies=lambda: [host]),
                           stop=lambda: stops.append(len(events)))
    callback = launcher.threadsafe_node_stop_callback(node)
    callback()
    callback()
    assert stops == [1]
    assert len(events) == 1
    assert events[0]["stop_request_source"] == source
    assert events[0]["stop_requested_at"] > 0
    for key, value in context.items():
        assert events[0][key] == value


def test_disposal_timeline_survives_cleared_strategy_actors(monkeypatch):
    events = []
    journal = SimpleNamespace(log_strategy_event=lambda *args: events.append(args[2]) or True)
    host = strategy(journal)
    node = SimpleNamespace(_collection_strategy_refs=[host], dispose=lambda: None)
    monkeypatch.setattr(launcher, "wait_for_client_disconnect_tasks", lambda *a, **k: (True, []))
    monkeypatch.setattr(launcher, "wait_for_node_engines_disconnected", lambda *a, **k: (True, []))
    assert launcher.dispose_node_and_wait_for_engines_disconnected(node) == (True, [])
    assert [x["transition"] for x in events] == ["client_disconnect_drain_start",
        "client_disconnect_drain_complete", "node_dispose_start", "node_dispose_complete", "engine_disconnect_complete"]
    assert [x["monotonic_ts"] for x in events] == sorted(x["monotonic_ts"] for x in events)
    launcher._collection_transition(node, "node_run_return", stop_request_source="unexpected_node_exit")
    assert events[-1]["stop_request_source"] == "unexpected_node_exit"


def test_async_journal_uses_existing_worker_and_drains_at_stop(tmp_path):
    journal = TradeJournalDB(str(tmp_path / "journal.db"))
    host = strategy(journal)
    assert collection_lifecycle(host, "stop_request", stop_request_source="operator_stop")
    journal.stop()
    with sqlite3.connect(journal.db_path) as conn:
        rows = conn.execute("select event_type,payload_json from strategy_events").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "COLLECTION_LIFECYCLE"
    assert json.loads(rows[0][1])["stop_request_source"] == "operator_stop"
    assert not collection_lifecycle(host, "late_event")


def test_health_is_low_frequency_nonfatal_and_marks_unavailable():
    rows = []
    class Writer:
        def research_health(self):
            return {"queue_depth": 2, "queue_capacity": 20000}
        def enqueue_decision(self, **row):
            rows.append(row["payload"])
            return True
    snapshotter = PredictionResearchSnapshotter(db=Writer(), run_id="run")
    for now in range(100, 222):
        snapshotter._emit_metrics(now)
    assert [r["timestamp"] for r in rows] == [100, 160, 220]
    assert all(r["last_persist_ts"] is None for r in rows)
    assert all(r["oldest_queue_age_sec"] is None for r in rows)
    assert snapshotter.interval_sec == 1
    snapshotter.db.enqueue_decision = lambda **k: (_ for _ in ()).throw(OSError())
    snapshotter._emit_metrics(280)
    assert snapshotter._counters["errors"] == 0


def test_lifecycle_telemetry_failure_does_not_prevent_stop():
    def fail(*args):
        raise OSError("optional journal unavailable")
    host = strategy(SimpleNamespace(enqueue_strategy_event=fail))
    stops = []
    node = SimpleNamespace(trader=SimpleNamespace(strategies=lambda: [host]), stop=lambda: stops.append(True))
    launcher.threadsafe_node_stop_callback(node)()
    assert stops == [True]


def test_health_cadence_stays_in_observational_range():
    assert PredictionResearchSnapshotter(db=None, run_id="r", metrics_interval_sec=1).metrics_interval_sec == 30
    assert PredictionResearchSnapshotter(db=None, run_id="r", metrics_interval_sec=999).metrics_interval_sec == 60


def test_enqueue_does_not_call_sqlite_in_caller(tmp_path, monkeypatch):
    journal = TradeJournalDB(str(tmp_path / "journal.db"))
    # Hold the existing worker asleep, then ensure enqueue itself cannot open SQLite.
    import threading
    caller_thread = threading.get_ident()
    connect = journal._connect
    def check_worker():
        assert threading.get_ident() != caller_thread
        return connect()
    monkeypatch.setattr(journal, "_connect", check_worker)
    assert journal.enqueue_strategy_event("r", "COLLECTION_LIFECYCLE", {"transition": "test"})
    # Stop's existing final backup is intentionally outside the callback hot path.
    journal._backup_stop.set()
    journal._backup_wakeup.set()
    journal._backup_thread.join(timeout=2)
    monkeypatch.setattr(journal, "_connect", connect)
    journal.stop()


def test_first_marker_retry_preserves_first_accepted_snapshot_per_run():
    markers = []
    attempts = []
    def enqueue(*args):
        attempts.append(args)
        if len(attempts) == 1:
            return False
        markers.append(args[2])
        return True
    writer = SimpleNamespace(enqueue_decision=lambda **k: True, research_health=lambda: {})
    for cycle in (1, 2):
        host = strategy(SimpleNamespace(enqueue_strategy_event=enqueue), cycle)
        host._research_market_quote_instruments = lambda **k: None
        snapshotter = PredictionResearchSnapshotter(db=writer, run_id=host.run_id)
        for now in (100, 101, 102):
            assert snapshotter.capture(host, now_ts=now) is not None
    assert len(markers) == 2
    assert [m["cycle_idx"] for m in markers] == [1, 2]
    assert [m["snapshot_ts"] for m in markers] == [100, 100]


def test_idle_capture_still_emits_health_without_snapshot_or_new_cadence():
    rows = []
    writer = SimpleNamespace(enqueue_decision=lambda **k: rows.append(k) or True, research_health=lambda: {})
    snapshotter = PredictionResearchSnapshotter(db=writer, run_id="r")
    host = strategy(None)
    host.current_market_slug = ""
    for now in range(100, 161):
        assert snapshotter.capture(host, now_ts=now) is None
    assert len(rows) == 2
    assert all(r["payload"]["event_type"] == "PREDICTION_RESEARCH_HEALTH" for r in rows)
    assert snapshotter._counters["eligible"] == 0
