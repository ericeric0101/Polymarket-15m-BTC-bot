"""Journal diagnostic retention: archive -> verify -> delete, fail-safe and idempotent.

Hermetic: temporary SQLite journals only; no production DB, no network.
"""
import hashlib
import json
import lzma
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from monitoring import journal_retention as jr

NOW = datetime(2026, 10, 20, 5, 0, tzinfo=timezone.utc)  # cutoff day = 2026-10-06

SCHEMA = """
CREATE TABLE order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, run_id TEXT NOT NULL, event_type TEXT NOT NULL,
    client_order_id TEXT, venue_order_id TEXT, side TEXT, price REAL, qty REAL, status TEXT, reason TEXT,
    instrument_id TEXT, token_id TEXT, fee_rate_bps INTEGER, expected_net_usdc REAL, commission_usdc REAL,
    payload_json TEXT);
CREATE TABLE strategy_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, run_id TEXT NOT NULL, event_type TEXT NOT NULL,
    payload_json TEXT);
CREATE INDEX idx_strategy_events_type_id ON strategy_events(event_type, id);
"""


def _ts(day, sec=0):
    return f"{day}T00:00:{sec:02d}.000000+00:00"


def make_journal(path: Path, days=("2026-10-01", "2026-10-02", "2026-10-15", "2026-10-20")):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    for day in days:
        for i in range(3):
            con.execute("INSERT INTO strategy_events(ts, run_id, event_type, payload_json) VALUES (?,?,?,?)",
                        (_ts(day, i), "r1", "ENTRY_DECISION_TRACE", json.dumps({"day": day, "i": i, "x": "é"})))
            con.execute("INSERT INTO strategy_events(ts, run_id, event_type, payload_json) VALUES (?,?,?,?)",
                        (_ts(day, i), "r1", "MARKET_CYCLE_PNL", json.dumps({"pnl": 1.5})))
            con.execute(
                "INSERT INTO order_events(ts, run_id, event_type, client_order_id, side, price, qty, status, "
                "fee_rate_bps, expected_net_usdc, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (_ts(day, i), "r1", "ENTRY_EDGE_OBSERVATION", None, "BUY", 0.1 + 0.2, float("nan") if i == 2 else 1e-17,
                 "SHADOW", 0, -0.0, None))
            con.execute("INSERT INTO order_events(ts, run_id, event_type, side, price, qty) VALUES (?,?,?,?,?,?)",
                        (_ts(day, i), "r1", "ORDER_FILLED", "BUY", 0.7, 10.0))
            con.execute("INSERT INTO order_events(ts, run_id, event_type) VALUES (?,?,?)",
                        (_ts(day, i), "r1", "ORDER_SKIP_DIRECTIONAL_ENTRY_GATE"))
    con.commit()
    con.close()
    return path


def counts(path):
    con = sqlite3.connect(path)
    try:
        return dict(con.execute(
            "SELECT event_type || '@' || substr(ts,1,10), COUNT(*) FROM ("
            "SELECT event_type, ts FROM order_events UNION ALL SELECT event_type, ts FROM strategy_events) GROUP BY 1"
        ).fetchall())
    finally:
        con.close()


@pytest.fixture
def env(tmp_path):
    (tmp_path / "logs").mkdir()
    journal = make_journal(tmp_path / "logs" / "trade_journal.db")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup = backup_dir / "trade_journal.db"
    src, dst = sqlite3.connect(journal), sqlite3.connect(backup)
    src.backup(dst)
    src.close(), dst.close()
    return {"journal": journal, "backup": backup, "archive": tmp_path / "data" / "journal_archive", "tmp": tmp_path}


def run(env, **kw):
    params = dict(journal_path=env["journal"], archive_dir=env["archive"], backup_path=env["backup"],
                  now=NOW, apply=True, journal_busy_fn=lambda _p: False)
    params.update(kw)
    return jr.run_retention(**params)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --- policy -------------------------------------------------------------------------------

def test_diagnostic_policy_is_an_explicit_allow_list():
    assert jr.HOT_RETENTION_DAYS == 14
    assert "ENTRY_DECISION_TRACE" in jr.DIAGNOSTIC_EVENT_TYPES["strategy_events"]
    assert "ENTRY_EDGE_OBSERVATION" in jr.DIAGNOSTIC_EVENT_TYPES["order_events"]
    for core in ("ORDER_FILLED", "ORDER_SUBMIT", "ORDER_MAKER_INTENT", "MARKET_CYCLE_PNL", "MARKET_SETTLEMENT",
                 "FILL_MARKOUT", "ORDER_SKIP_DIRECTIONAL_ENTRY_GATE", "SOME_FUTURE_EVENT"):
        assert not jr.is_diagnostic("order_events", core) and not jr.is_diagnostic("strategy_events", core)


def test_only_completed_days_older_than_hot_window_are_eligible(env):
    con = sqlite3.connect(env["journal"])
    try:
        assert jr.eligible_days(con, now=NOW) == ["2026-10-01", "2026-10-02"]
    finally:
        con.close()
    assert jr.cutoff_day(NOW) == "2026-10-06"


# --- happy path ---------------------------------------------------------------------------

def test_archive_verify_delete_keeps_core_and_recent_rows(env):
    before = counts(env["journal"])
    report = run(env)
    after = counts(env["journal"])
    assert report["deleted_rows"] == 12 and report["status"] == "ok"
    for day in ("2026-10-01", "2026-10-02"):
        assert f"ENTRY_DECISION_TRACE@{day}" not in after and f"ENTRY_EDGE_OBSERVATION@{day}" not in after
        assert after[f"MARKET_CYCLE_PNL@{day}"] == 3 and after[f"ORDER_FILLED@{day}"] == 3
        assert after[f"ORDER_SKIP_DIRECTIONAL_ENTRY_GATE@{day}"] == 3
    for day in ("2026-10-15", "2026-10-20"):
        assert after[f"ENTRY_DECISION_TRACE@{day}"] == before[f"ENTRY_DECISION_TRACE@{day}"] == 3
    manifest = json.loads((env["archive"] / "2026-10" / "journal_diag_2026-10-01.manifest.json").read_text())
    assert manifest["status"] == "deleted" and manifest["hot_retention_days"] == 14
    assert manifest["tables"]["strategy_events"]["rows"] == 3
    assert manifest["tables"]["order_events"]["per_event_type"] == {"ENTRY_EDGE_OBSERVATION": 3}


def test_archive_round_trips_rows_exactly_including_nan_none_and_unicode(env):
    con = sqlite3.connect(env["journal"])
    original = con.execute("SELECT * FROM order_events WHERE event_type='ENTRY_EDGE_OBSERVATION' "
                           "AND ts < '2026-10-02' ORDER BY id").fetchall()
    con.close()
    run(env)
    restored = list(jr.iter_archived_rows(env["archive"], "order_events", start_day="2026-10-01", end_day="2026-10-01"))
    assert len(restored) == len(original)
    assert json.dumps(restored) == json.dumps([list(r) for r in original])  # NaN/None/-0.0/float bits identical
    traces = list(jr.iter_archived_rows(env["archive"], "strategy_events", event_types={"ENTRY_DECISION_TRACE"}))
    assert len(traces) == 6 and json.loads(traces[0][4])["x"] == "é"


def test_rerun_is_idempotent(env):
    run(env)
    files = {p: sha(p) for p in env["archive"].rglob("*") if p.is_file()}
    report = run(env)
    assert report["deleted_rows"] == 0 and report["status"] == "ok"
    assert {p: sha(p) for p in env["archive"].rglob("*") if p.is_file()} == files


def test_dry_run_archives_but_never_deletes(env):
    before = counts(env["journal"])
    report = run(env, apply=False)
    assert counts(env["journal"]) == before and report["deleted_rows"] == 0
    assert report["archived_days"] == ["2026-10-01", "2026-10-02"]
    run(env)  # a later apply reuses the verified archive
    assert "ENTRY_DECISION_TRACE@2026-10-01" not in counts(env["journal"])


# --- fail-safe ----------------------------------------------------------------------------

def test_export_failure_deletes_nothing(env, monkeypatch):
    before = counts(env["journal"])

    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(jr, "_write_archive_file", boom)
    report = run(env)
    assert counts(env["journal"]) == before and report["status"] == "failed"


def test_readback_mismatch_deletes_nothing(env, monkeypatch):
    before = counts(env["journal"])
    real = jr._write_archive_file

    def corrupt(path, lines):
        result = real(path, lines[:-1])  # drop a row: readback count/hash must not match
        return result
    monkeypatch.setattr(jr, "_write_archive_file", corrupt)
    report = run(env)
    assert counts(env["journal"]) == before and report["status"] == "failed"


def test_tampered_archive_blocks_delete_and_reader(env):
    run(env, apply=False)
    target = next(env["archive"].rglob("journal_diag_2026-10-01_order_events.jsonl.xz"))
    os.chmod(target, 0o644)
    target.write_bytes(lzma.compress(b"[]\n"))
    before = counts(env["journal"])
    report = run(env)
    assert counts(env["journal"])["ENTRY_EDGE_OBSERVATION@2026-10-01"] == before["ENTRY_EDGE_OBSERVATION@2026-10-01"]
    assert report["status"] == "failed"
    with pytest.raises(jr.ArchiveIntegrityError):
        list(jr.iter_archived_rows(env["archive"], "order_events"))


def test_source_change_after_archive_rolls_back_delete(env):
    run(env, apply=False)
    con = sqlite3.connect(env["journal"])
    con.execute("INSERT INTO strategy_events(ts, run_id, event_type, payload_json) VALUES (?,?,?,?)",
                (_ts("2026-10-01", 30), "late", "ENTRY_DECISION_TRACE", "{}"))
    con.commit()
    con.close()
    report = run(env)
    after = counts(env["journal"])
    assert after["ENTRY_DECISION_TRACE@2026-10-01"] == 4  # nothing deleted for the changed day
    assert "ENTRY_DECISION_TRACE@2026-10-02" not in after  # the unchanged day proceeds
    assert report["status"] == "failed"


def test_busy_journal_runs_nothing_destructive(env):
    before = counts(env["journal"])
    report = run(env, journal_busy_fn=lambda _p: True)
    assert counts(env["journal"]) == before and report["status"] == "skipped_journal_busy"


def test_open_handle_check_fails_closed_when_lsof_is_unusable(env, monkeypatch):
    monkeypatch.setattr(jr, "LSOF", "/nonexistent/lsof")
    assert jr.journal_in_use(env["journal"]) is True


def test_missing_or_stale_backup_blocks_delete(env):
    env["backup"].unlink()
    before = counts(env["journal"])
    report = run(env)
    assert counts(env["journal"]) == before and report["status"] == "failed"
    stale = sqlite3.connect(env["backup"])
    stale.executescript(SCHEMA)
    stale.close()
    assert run(env)["status"] == "failed" and counts(env["journal"]) == before


# --- mirror -------------------------------------------------------------------------------

def test_missing_mirror_never_blocks_local_archive(env):
    report = run(env, mirror_dir=env["tmp"] / "not_mounted")
    assert report["status"] == "ok" and report["mirror"]["status"] == "unavailable"


def test_mirror_copies_are_hash_verified_and_failures_keep_local(env, monkeypatch):
    mirror = env["tmp"] / "external"
    mirror.mkdir()
    report = run(env, mirror_dir=mirror)
    assert report["mirror"]["status"] == "ok" and report["mirror"]["copied"] >= 4
    for local in env["archive"].rglob("*.xz"):
        remote = mirror / local.relative_to(env["archive"])
        assert sha(remote) == sha(local)

    def broken_copy(*_a, **_k):
        raise OSError("device removed")
    for remote in mirror.rglob("*.xz"):
        os.chmod(remote, 0o644)
        remote.write_bytes(b"corrupt")
    monkeypatch.setattr(jr, "_copy_file", broken_copy)
    local_files = {p: sha(p) for p in env["archive"].rglob("*") if p.is_file()}
    report = run(env, mirror_dir=mirror)
    assert report["mirror"]["status"] == "failed"
    assert {p: sha(p) for p in env["archive"].rglob("*") if p.is_file()} == local_files


# --- restore for research -----------------------------------------------------------------

def test_restore_materializes_archive_as_queryable_sqlite(env):
    run(env)
    out = env["tmp"] / "restored.db"
    jr.restore_to_sqlite(env["archive"], out)
    con = sqlite3.connect(out)
    try:
        assert con.execute("SELECT COUNT(*) FROM strategy_events WHERE event_type='ENTRY_DECISION_TRACE'").fetchone()[0] == 6
        assert con.execute("SELECT COUNT(*) FROM order_events").fetchone()[0] == 6
    finally:
        con.close()


# --- one-time VACUUM ----------------------------------------------------------------------

def test_vacuum_once_requires_preconditions_and_runs_only_once(env):
    params = dict(journal_path=env["journal"], archive_dir=env["archive"], backup_path=env["backup"],
                  journal_busy_fn=lambda _p: False)
    with pytest.raises(jr.RetentionRefused):
        jr.vacuum_once(**params)  # no successful archive+delete yet
    run(env)
    with pytest.raises(jr.RetentionRefused):
        jr.vacuum_once(**{**params, "journal_busy_fn": lambda _p: True})
    result = jr.vacuum_once(**params)
    assert result["integrity_check"] == "ok" and result["bytes_after"] <= result["bytes_before"]
    with pytest.raises(jr.RetentionRefused):
        jr.vacuum_once(**params)  # never a recurring operation


# --- exit hook ----------------------------------------------------------------------------

def test_exit_hook_runs_only_on_graceful_final_exit(tmp_path):
    from bot import launcher

    spawned = []

    class Child:
        def wait(self, timeout=None):
            return 0
    assert launcher.run_journal_retention_on_exit(tmp_path, "crash", popen=lambda *a, **k: spawned.append(a)) \
        == "skipped_not_graceful"
    assert spawned == []
    assert launcher.run_journal_retention_on_exit(
        tmp_path, launcher.EXIT_OPERATOR_STOP, popen=lambda cmd, **k: spawned.append(cmd) or Child()
    ) == "completed"
    assert "--apply" in spawned[0] and "journal_retention.py" in spawned[0][1]


def test_real_lsof_detects_another_process_holding_the_journal(env):
    import subprocess
    import sys
    import time as _time

    if not Path(jr.LSOF).exists():
        pytest.skip("lsof not available")
    assert jr.journal_in_use(env["journal"]) is False
    holder = subprocess.Popen([sys.executable, "-c",
                               "import sys,time; f=open(sys.argv[1],'rb'); print('ok', flush=True); time.sleep(30)",
                               str(env["journal"])], stdout=subprocess.PIPE)
    try:
        assert holder.stdout.readline().strip() == b"ok"
        _time.sleep(0.2)
        assert jr.journal_in_use(env["journal"]) is True
        before = counts(env["journal"])
        report = jr.run_retention(journal_path=env["journal"], archive_dir=env["archive"],
                                  backup_path=env["backup"], now=NOW, apply=True)
        assert report["status"] == "skipped_journal_busy" and counts(env["journal"]) == before
    finally:
        holder.kill()
        holder.wait()


def test_current_day_rows_are_never_eligible_even_with_zero_hot_days(env):
    today = datetime(2026, 10, 20, 23, 59, tzinfo=timezone.utc)
    report = run(env, now=today, hot_days=0)
    after = counts(env["journal"])
    assert after["ENTRY_DECISION_TRACE@2026-10-20"] == 3
    assert "2026-10-20" not in report["archived_days"]
