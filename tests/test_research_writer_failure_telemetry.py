import json
import sqlite3
from types import SimpleNamespace

import pytest

from monitoring.lead_lag_db import LeadLagDB
from monitoring.research_writer_diagnostics import WriterFailureDiagnostics
from monitoring.trade_journal_db import TradeJournalDB


class Journal:
    def __init__(self, path, fail=False):
        self.db_path = str(path)
        self.events = []
        self.fail = fail

    def enqueue_strategy_event(self, run_id, event_type, payload):
        if self.fail:
            raise OSError("wallet=secret diagnostic sink failed")
        self.events.append((run_id, event_type, payload))
        return True


def stopped_writer(tmp_path):
    db = LeadLagDB(str(tmp_path / "research.db"))
    assert db.stop()
    journal = Journal(tmp_path / "journal.db")
    db.set_failure_diagnostics(journal=journal, writer_name="twap_research_db", run_id="run-a",
                               identity=lambda: {"cycle_idx": 3, "wallet": "DO_NOT_LEAK"})
    return db, journal


def decision(secret="DO_NOT_LEAK"):
    return ("decision", ("run-a", "btc-slug", None, 100, {"event_type": secret, "wallet": secret}))


def invoke(db, rows):
    # Simulate previously accepted work at terminal drain, with no race/sleep.
    for row in rows:
        db._queue.put_nowait(row)
    db._writer()


class Connection:
    def __init__(self, fail_stage=None, exc=None):
        self.fail_stage = fail_stage
        self.exc = exc or sqlite3.OperationalError("database is locked")
        self.calls = []

    def execute(self, sql, *args):
        self.calls.append("execute")
        if self.fail_stage == "execute":
            raise self.exc

    def executemany(self, *args):
        self.calls.append("executemany")
        if self.fail_stage == "executemany":
            raise self.exc

    def commit(self):
        self.calls.append("commit")
        if self.fail_stage == "commit":
            raise self.exc

    def close(self):
        self.calls.append("close")
        if self.fail_stage == "close":
            raise self.exc


def test_connect_failure_is_independent_and_drops_without_retry(tmp_path, monkeypatch):
    db, journal = stopped_writer(tmp_path)
    calls = []
    def fail():
        calls.append(True)
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(db, "_connect", fail)
    invoke(db, [decision()])
    p = journal.events[0][2]
    assert p["failure_stage"] == "CONNECT"
    assert p["commit_acknowledged"] == "NO"
    assert p["write_errors_after_increment"] == 1
    assert p["run_id"] == "run-a" and p["cycle_idx"] == 3
    assert p["db_target"] == "research.db"
    assert db._queue.empty() and len(calls) == 1
    assert db.research_health()["decision_rows_written"] == 0
    assert db.stop() is False
    assert "DO_NOT_LEAK" not in json.dumps(p)


@pytest.mark.parametrize("stage, expected, row", [
    ("execute", "EXECUTE", decision()),
    ("executemany", "EXECUTEMANY", ("run-a", "slug", None, 100, {"wallet": "SECRET"})),
    ("commit", "COMMIT", decision()),
    ("close", "CLOSE", decision()),
])
def test_actual_stages_and_commit_ack(tmp_path, monkeypatch, stage, expected, row):
    db, journal = stopped_writer(tmp_path)
    conn = Connection(stage)
    calls = []
    def connect():
        calls.append(True)
        return conn
    monkeypatch.setattr(db, "_connect", connect)
    invoke(db, [row])
    p = journal.events[0][2]
    assert p["failure_stage"] == expected
    assert p["commit_acknowledged"] == ("YES" if stage == "close" else "NO")
    assert p["exception_class"] == "OperationalError"
    assert db.research_health()["write_errors"] == 1
    assert len(calls) == 1 and conn.calls.count("close") == 1
    assert db._queue.empty() and not db.stop()
    if stage == "close":
        assert db.research_health()["decision_rows_written"] == 1
    else:
        assert db.research_health()["decision_rows_written"] == 0


def test_pragma_failure_uses_actual_connect_boundary(tmp_path, monkeypatch):
    db, journal = stopped_writer(tmp_path)
    conn = Connection("execute")
    monkeypatch.setattr("monitoring.lead_lag_db.sqlite3.connect", lambda *a, **k: conn)
    invoke(db, [decision()])
    assert journal.events[0][2]["failure_stage"] == "PRAGMA"
    assert conn.calls == ["execute"]  # Existing _connect failure cleanup unchanged.


def test_serialization_failure_is_safe_non_sqlite_exception(tmp_path, monkeypatch):
    db, journal = stopped_writer(tmp_path)
    conn = Connection()
    monkeypatch.setattr(db, "_connect", lambda: conn)
    invoke(db, [("decision", ("run-a", "slug", None, 1, {"private": object()}))])
    p = journal.events[0][2]
    assert p["failure_stage"] == "SERIALIZE"
    assert p["exception_class"] == "TypeError"
    assert p["sqlite_errorcode"] is None
    assert p["exception_message"] == "Object type is not JSON serializable"
    assert conn.calls == ["close"]


def test_real_sqlite_code_name_and_safe_message(tmp_path, monkeypatch):
    db, journal = stopped_writer(tmp_path)
    with sqlite3.connect(":memory:") as conn:
        try:
            conn.execute("select * from private_wallet_identifier")
        except sqlite3.OperationalError as error:
            exc = error
    monkeypatch.setattr(db, "_connect", lambda: Connection("execute", exc))
    invoke(db, [decision()])
    p = journal.events[0][2]
    assert p["sqlite_errorcode"] == sqlite3.SQLITE_ERROR
    assert p["sqlite_errorname"] == "SQLITE_ERROR"
    assert p["exception_message"] == "no such table"
    assert "private_wallet_identifier" not in json.dumps(p)


@pytest.mark.parametrize("message", ["wallet=" + "s" * 10000, "database is locked " + "s" * 10000])
def test_exception_message_bounded_and_unknown_contents_withheld(tmp_path, monkeypatch, message):
    db, journal = stopped_writer(tmp_path)
    monkeypatch.setattr(db, "_connect", lambda: Connection("commit", ValueError(message)))
    invoke(db, [decision()])
    p = journal.events[0][2]
    assert len(p["exception_message"]) <= WriterFailureDiagnostics.MESSAGE_LIMIT
    assert "ssss" not in json.dumps(p)
    assert p["exception_message_redacted"] is True


def test_batch_counts_only_protocol_kinds_no_rows(tmp_path, monkeypatch):
    db, journal = stopped_writer(tmp_path)
    def fail():
        raise RuntimeError("SQL parameters: PRIVATE_ACCOUNT")
    monkeypatch.setattr(db, "_connect", fail)
    rows = [decision(), decision(), ("run-a", "slug", None, 1, {"secret": "TOKEN"}),
            ("reference", ()), ("latency", ()), ("markout", ())]
    invoke(db, rows)
    p = journal.events[0][2]
    assert p["batch_item_count"] == 6
    assert p["batch_type_counts"] == {"decision": 2, "snapshot": 1, "reference": 1, "latency": 1, "markout": 1}
    assert all(secret not in json.dumps(p) for secret in ["DO_NOT_LEAK", "PRIVATE_ACCOUNT", "TOKEN"])


def test_sink_failure_does_not_kill_writer_or_retry_batches(tmp_path, monkeypatch):
    db, journal = stopped_writer(tmp_path)
    journal.fail = True
    connections = []
    def connect():
        c = Connection("execute") if not connections else Connection()
        connections.append(c)
        return c
    monkeypatch.setattr(db, "_connect", connect)
    # Different invocations force two batches: next successful batch continues.
    invoke(db, [decision()])
    invoke(db, [decision()])
    assert len(connections) == 2
    assert db.research_health()["write_errors"] == 1
    assert db.research_health()["decision_rows_written"] == 1
    assert db._queue.empty()
    assert db.research_health()["writer_failure_diagnostics"]["diagnostic_enqueue_failures"] == 1


def diagnostics(tmp_path):
    clock = SimpleNamespace(now=0.0)
    journal = Journal(tmp_path / "journal.db")
    d = WriterFailureDiagnostics(journal=journal, writer_name="twap_research_db", db_target="twap.db",
                                 run_id="r", monotonic=lambda: clock.now, wall_clock=lambda: 1000 + clock.now)
    def emit(stage="EXECUTE", exc=None):
        d.record(exc=exc or sqlite3.OperationalError("database is locked"), stage=stage,
                 rows=[decision()], queue_depth=0, write_errors=1, queue_drops=0, commit_acknowledged=False)
    return d, clock, journal, emit


def test_repeated_errors_periodic_summary_preserves_suppressed_count(tmp_path):
    d, clock, j, emit = diagnostics(tmp_path)
    emit()
    for _ in range(972):
        emit()
    assert len(j.events) == 1
    assert d.health_snapshot()["pending_suppressed_count"] == 972
    clock.now = 60
    emit()
    assert len(j.events) == 2
    assert j.events[1][2]["suppressed_count"] == 972
    assert j.events[1][2]["suppressed_error_count_total"] == 972
    assert d.health_snapshot()["pending_suppressed_count"] == 0


def test_materially_different_signature_emits_immediately(tmp_path):
    _, _, j, emit = diagnostics(tmp_path)
    emit()
    emit("COMMIT")
    emit("COMMIT", TypeError("Circular reference detected"))
    assert len(j.events) == 3


def test_alternating_and_distinct_error_storm_bounded_state(tmp_path):
    d, clock, j, emit = diagnostics(tmp_path)
    for i in range(1000):
        exc = type("Error" + str(i), (Exception,), {})("secret")
        emit("EXECUTE", exc)
    assert len(j.events) == d.MAX_EMISSIONS_PER_INTERVAL
    assert len(d._signatures) <= d.MAX_SIGNATURES
    assert d.suppressed_total == 1000 - d.MAX_EMISSIONS_PER_INTERVAL
    clock.now = 60
    emit()
    assert j.events[-1][2]["suppressed_count"] == 992


def test_rejected_diagnostic_is_not_retried_and_summary_reports_it(tmp_path):
    d, clock, j, emit = diagnostics(tmp_path)
    j.fail = True
    emit()
    emit()
    assert d.enqueue_failures == 1
    assert d.health_snapshot()["pending_suppressed_count"] == 2
    j.fail = False
    clock.now = 60
    emit()
    assert j.events[-1][2]["suppressed_count"] == 2
    assert j.events[-1][2]["diagnostic_enqueue_failures"] == 1


def test_no_diagnostics_for_success_and_shutdown_unchanged(tmp_path):
    db = LeadLagDB(str(tmp_path / "research.db"))
    j = Journal(tmp_path / "journal.db")
    db.set_failure_diagnostics(journal=j, writer_name="lead_lag_db", run_id="r")
    assert db.enqueue_decision(run_id="r", slug="s", market_id=None, decision_epoch_ns=1, payload={})
    assert db.stop()
    assert db.research_health()["write_errors"] == 0
    assert j.events == []
    assert db.enqueue_decision(run_id="r", slug="s", market_id=None, decision_epoch_ns=2, payload={}) is False


def test_both_writers_persist_to_existing_journal_after_research_failure(tmp_path, monkeypatch):
    journal = TradeJournalDB(str(tmp_path / "journal.db"))
    writers = []
    try:
        for name, target in [("lead_lag_db", "hyperliquid_lead_lag.db"), ("twap_research_db", "twap_forward_shadow.db")]:
            db = LeadLagDB(str(tmp_path / target))
            assert db.stop()
            db.set_failure_diagnostics(journal=journal, writer_name=name, run_id="r",
                                       identity=lambda: {"cycle_idx": 2})
            monkeypatch.setattr(db, "_connect", lambda: Connection("commit"))
            invoke(db, [decision()])
            writers.append(db)
        journal.stop()  # Existing worker drains; no new diagnostic DB/thread.
        with sqlite3.connect(journal.db_path) as conn:
            raw = conn.execute("SELECT payload_json FROM strategy_events WHERE event_type='RESEARCH_WRITER_FAILURE'").fetchall()
        payloads = [json.loads(row[0]) for row in raw]
        assert {p["writer_name"] for p in payloads} == {"lead_lag_db", "twap_research_db"}
        assert {p["db_target"] for p in payloads} == {"hyperliquid_lead_lag.db", "twap_forward_shadow.db"}
        assert all(p["cycle_idx"] == 2 and p["commit_acknowledged"] == "NO" for p in payloads)
        assert all(db._queue.empty() and db.research_health()["write_errors"] == 1 for db in writers)
    finally:
        journal.stop()


def test_same_database_diagnostic_target_rejected(tmp_path):
    db, _ = stopped_writer(tmp_path)
    db._failure_diagnostics = None
    same = Journal(tmp_path / "research.db")
    db.set_failure_diagnostics(journal=same, writer_name="twap_research_db", run_id="r")
    assert db._failure_diagnostics is None


def test_hardlink_same_database_target_rejected(tmp_path):
    import os
    db, _ = stopped_writer(tmp_path)
    db._failure_diagnostics = None
    alias = tmp_path / "alias.db"
    os.link(db.db_path, alias)
    db.set_failure_diagnostics(journal=Journal(alias), writer_name="twap_research_db", run_id="r")
    assert db._failure_diagnostics is None


def test_precommit_and_close_failure_each_increment_without_retry(tmp_path, monkeypatch):
    db, j = stopped_writer(tmp_path)
    class DoubleFailure(Connection):
        def execute(self, *a):
            self.calls.append("execute")
            raise sqlite3.OperationalError("database is locked")
        def close(self):
            self.calls.append("close")
            raise OSError("private close details")
    c = DoubleFailure()
    monkeypatch.setattr(db, "_connect", lambda: c)
    invoke(db, [decision()])
    assert db.research_health()["write_errors"] == 2
    assert [p[2]["failure_stage"] for p in j.events] == ["EXECUTE", "CLOSE"]
    assert [p[2]["commit_acknowledged"] for p in j.events] == ["NO", "NO"]
    assert c.calls == ["execute", "close"]


def test_failing_identity_callback_does_not_change_original_failure(tmp_path, monkeypatch):
    db, j = stopped_writer(tmp_path)
    def bad_identity():
        raise ValueError("private identity")
    db._failure_diagnostics.identity = bad_identity
    monkeypatch.setattr(db, "_connect", lambda: Connection("commit"))
    invoke(db, [decision()])
    assert db.research_health()["write_errors"] == 1
    assert db.research_health()["writer_failure_diagnostics"]["diagnostic_enqueue_failures"] == 1
    assert db._queue.empty() and j.events == []


def test_diagnostic_binding_failure_does_not_break_writer_startup(tmp_path):
    db = LeadLagDB(str(tmp_path / "research.db"))
    class UnavailableJournal:
        @property
        def db_path(self):
            raise OSError("sink path unavailable")
    db.set_failure_diagnostics(journal=UnavailableJournal(), writer_name="lead_lag_db", run_id="r")
    assert db.enqueue_decision(run_id="r", slug="s", market_id=None, decision_epoch_ns=1, payload={})
    assert db.stop() is True


def test_malformed_batch_type_metadata_is_bounded_unknown():
    assert WriterFailureDiagnostics.batch_types([([], {}), (), ("untrusted-kind", {})]) == {"unknown": 3}
