"""RESEARCH_RETENTION_ON_EXIT: verified research retention on final graceful exit (tmp data only)."""
import ast
import gc
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import launcher
from bot.research import exit_retention, partitions, retention
from test_research_tiers_bc import AFTER, DAY, DAY_START, _exports, _live, _rows

NEXT_DAY = DAY.fromordinal(DAY.toordinal() + 1)


@pytest.fixture
def env(tmp_path):
    live = _live(tmp_path, _rows())
    _exports(tmp_path, offsite=True)
    gc.collect()  # the shared helper's `with sqlite3.connect()` does not close; release it before lsof checks
    return SimpleNamespace(live=live, parts=tmp_path / "parts", export=tmp_path / "export", icloud=tmp_path / "icloud",
                           tmp=tmp_path)


def _run(env, **overrides):
    kw = dict(live_db=env.live, part_dir=env.parts, export_root=env.export, offsite_root=env.icloud,
              deadline=time.monotonic() + 60, now=AFTER)
    kw.update(overrides)
    return exit_retention.run_exit_retention(**kw)


def _count(live, day=DAY):
    lo, hi = partitions._day_ns(day)
    with closing(sqlite3.connect(live)) as conn:
        return conn.execute("SELECT count(*) FROM lead_lag_decisions WHERE decision_epoch_ns >= ? "
                            "AND decision_epoch_ns < ?", (lo, hi)).fetchone()[0]


def _total(live):
    with closing(sqlite3.connect(live)) as conn:
        return conn.execute("SELECT count(*) FROM lead_lag_decisions").fetchone()[0]


# 1 ---------------------------------------------------------------------------------------------
def test_successful_graceful_exit_retention(env):
    report = _run(env)
    assert report["status"] == "COMPLETED" and report["deleted_days"] == [DAY.isoformat()]
    assert report["deleted_rows"] == 50 and _count(env.live) == 0 and _total(env.live) == 2
    steps = [s["step"] for s in report["steps"]]
    assert steps[:4] == ["writers_closed", "no_other_db_holder", "wal_checkpoint", "dry_run_plan"]
    assert steps.index("export_partition") < steps.index("verify_day") < steps.index("delete_live_rows") \
        < steps.index("reclaimed_bytes")
    assert report["bytes"]["logical_payload_bytes_deleted"] > 0
    # the earlier day has no exports yet: kept, not a failure
    assert any(r["reason"].startswith("exports pending") for r in report["refusals"])


def test_child_process_end_to_end(env):
    result = subprocess.run([sys.executable, "scripts/research_exit_retention.py", "--db", str(env.live),
                             "--partitions", str(env.parts), "--export", str(env.export), "--offsite", str(env.icloud),
                             "--timeout-sec", "60"], capture_output=True, text=True, timeout=120)
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert result.returncode == 0 and summary["status"] == "COMPLETED" and summary["deleted_rows"] == 50
    assert list((env.parts / "retention_log").glob("*_exit_retention.json"))


# 2 ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("offsite", ["missing", None])
def test_cloud_verification_unavailable_means_no_delete(env, offsite):
    report = _run(env, offsite_root=(env.tmp / "missing") if offsite else None)
    assert report["status"] == "ABORTED_CLOUD_UNAVAILABLE" and _total(env.live) == 52


# 3 ---------------------------------------------------------------------------------------------
def test_second_process_holding_the_db_means_no_delete(env):
    holder = subprocess.Popen([sys.executable, "-c", f"f=open({str(env.live)!r},'rb'); import time; time.sleep(30)"])
    try:
        time.sleep(0.5)
        report = _run(env)
    finally:
        holder.kill()
    assert report["status"] == "REFUSED_DB_IN_USE" and _total(env.live) == 52


# 4 ---------------------------------------------------------------------------------------------
def test_timeout_means_no_delete(env, monkeypatch):
    assert _run(env, deadline=time.monotonic() - 1)["status"] == "ABORTED"
    # deadline passes after export, before deletion
    real_split = exit_retention.split_day
    clock = {"expired": False}

    def split_then_expire(*a, **k):
        out = real_split(*a, **k)
        clock["expired"] = True
        return out
    monkeypatch.setattr(exit_retention, "split_day", split_then_expire)
    monkeypatch.setattr(exit_retention.time, "monotonic", lambda: 1e12 if clock["expired"] else 0.0)
    report = _run(env, deadline=1e9)
    assert report["status"] == "ABORTED" and "timeout" in report["reason"] and _total(env.live) == 52


def test_launcher_timeout_terminates_child_and_reports(monkeypatch, tmp_path):
    monkeypatch.setenv("RESEARCH_RETENTION_ON_EXIT", "1")
    monkeypatch.setenv("RESEARCH_RETENTION_EXIT_TIMEOUT_SEC", "1")
    calls = []

    class Child:
        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("x", timeout)
            return -15

        def terminate(self):
            calls.append(("terminate",))

        def kill(self):
            calls.append(("kill",))
    assert launcher.run_exit_retention(tmp_path, launcher.EXIT_OPERATOR_STOP, popen=lambda *a, **k: Child()) \
        == "timeout_aborted"
    assert ("terminate",) in calls and calls[0] == ("wait", 11.0)


# 5 ---------------------------------------------------------------------------------------------
def test_already_exported_day_is_idempotent(env):
    partitions.split_day(env.live, DAY, env.parts, now=AFTER, disk_margin_bytes=0)
    manifest = (env.parts / "manifests" / f"C_{DAY}.json").read_text()
    first = _run(env)
    second = _run(env)
    assert first["status"] == "COMPLETED" and second["deleted_rows"] == 0
    assert second["status"] in ("NOTHING_TO_DO", "NOTHING_DELETED")
    assert (env.parts / "manifests" / f"C_{DAY}.json").read_text() == manifest and _total(env.live) == 2


# 6 ---------------------------------------------------------------------------------------------
def test_interrupted_delete_transaction_preserves_source(env, monkeypatch):
    real = retention._row_digest
    seen = {"n": 0}

    def interrupt(digest, row):
        seen["n"] += 1
        if seen["n"] == 10:
            raise KeyboardInterrupt("simulated crash mid-transaction")
        real(digest, row)
    monkeypatch.setattr(retention, "_row_digest", interrupt)  # only the deleting transaction uses this binding
    report = _run(env)
    assert report["status"] == "ABORTED" and _total(env.live) == 52
    monkeypatch.setattr(retention, "_row_digest", real)
    assert _run(env)["status"] == "COMPLETED"  # next graceful exit retries


# 7 ---------------------------------------------------------------------------------------------
def test_node_rollover_does_not_invoke_retention(monkeypatch):
    invoked = []
    monkeypatch.setattr(launcher, "run_exit_retention", lambda *a, **k: invoked.append(a))
    monkeypatch.setattr(launcher, "TelegramNotifier", lambda: None)
    monkeypatch.setattr(launcher, "AlertWatcher", lambda: None)
    monkeypatch.setattr(launcher, "start_telegram_bot_thread", lambda state: None)
    monkeypatch.setattr(launcher, "_install_fresh_main_thread_event_loop", lambda: None)
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    cycles = iter(["rollover", "rollover", "ctrl_c"])

    def discovery():
        if next(cycles) == "ctrl_c":
            raise KeyboardInterrupt
        return []  # no market yet -> the loop rolls over to a new cycle
    monkeypatch.setattr(launcher, "resolve_btc_15m_market_slugs", discovery)
    reason = launcher.run_integrated_bot(simulation=True, test_mode=True, auth={"pk": "test"})
    assert reason == launcher.EXIT_OPERATOR_STOP and invoked == []  # two rollovers, no retention
    # structurally: only main() calls it, after run_integrated_bot() has returned
    tree = ast.parse(Path(launcher.__file__).read_text())
    callers = {fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
               for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "run_exit_retention"}
    assert callers == {"main"}


def test_non_graceful_exit_reasons_skip_retention(monkeypatch, tmp_path):
    monkeypatch.setenv("RESEARCH_RETENTION_ON_EXIT", "1")
    for reason in ("failure_budget_exhausted", "unsafe_engine_shutdown", "auto_rollover_disabled", None):
        assert launcher.run_exit_retention(tmp_path, reason, popen=lambda *a, **k: pytest.fail("spawned")) \
            == "skipped_not_graceful"


# 8 ---------------------------------------------------------------------------------------------
def test_ctrl_c_final_exit_invokes_retention(monkeypatch, tmp_path):
    spawned = []

    class Child:
        def wait(self, timeout=None):
            return 0
    monkeypatch.delenv("RESEARCH_RETENTION_ON_EXIT", raising=False)
    assert launcher.run_exit_retention(tmp_path, launcher.EXIT_OPERATOR_STOP,
                                       popen=lambda *a, **k: pytest.fail("default must be OFF")) is None
    monkeypatch.setenv("RESEARCH_RETENTION_ON_EXIT", "1")
    for reason in (launcher.EXIT_OPERATOR_STOP, launcher.EXIT_CLEAN_RETURN_NO_ROLLOVER):
        result = launcher.run_exit_retention(tmp_path, reason, popen=lambda cmd, **k: spawned.append(cmd) or Child())
        assert result == "completed"
    assert all(cmd[1].endswith("scripts/research_exit_retention.py") and "--timeout-sec" in cmd for cmd in spawned)
    assert len(spawned) == 2


# 9 ---------------------------------------------------------------------------------------------
def test_no_active_db_or_writer_handles_during_deletion(env, monkeypatch):
    real = retention.delete_live_rows
    holders = []

    def checked_delete(live_db, *a, **k):
        out = subprocess.run(["lsof", "-t", "--", str(live_db)], capture_output=True, text=True)
        holders.append(out.stdout.split())  # includes this process: no open handle may exist
        return real(live_db, *a, **k)
    monkeypatch.setattr(retention, "delete_live_rows", checked_delete)
    assert _run(env)["status"] == "COMPLETED"
    assert holders == [[]]


# 10 --------------------------------------------------------------------------------------------
def test_corrupted_archive_means_no_delete(env):
    partitions.split_day(env.live, DAY, env.parts, now=AFTER, disk_margin_bytes=0)
    target = env.parts / f"C_{DAY}.db"
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    data = bytearray(target.read_bytes())
    data[len(data) // 2] ^= 0xFF
    target.write_bytes(bytes(data))
    corrupted = target.read_bytes()
    report = _run(env)
    assert report["status"] == "ABORTED_VERIFICATION_FAILED" and _total(env.live) == 52
    assert target.read_bytes() == corrupted  # never silently rebuilt over: operator review


# 11 --------------------------------------------------------------------------------------------
def test_export_interrupted_halfway_preserves_source(env, monkeypatch):
    real = partitions._row_digest
    seen = {"n": 0}

    def interrupt(digest, row):
        seen["n"] += 1
        if seen["n"] == 20:
            raise KeyboardInterrupt("interrupted export")
        real(digest, row)
    monkeypatch.setattr(partitions, "_row_digest", interrupt)
    report = _run(env)
    assert report["status"] == "ABORTED" and _total(env.live) == 52
    assert not (env.parts / f"C_{DAY}.db").exists()
    monkeypatch.setattr(partitions, "_row_digest", real)
    assert _run(env)["status"] == "COMPLETED"


# 12 --------------------------------------------------------------------------------------------
def test_disk_full_during_export_preserves_source(env, monkeypatch):
    monkeypatch.setattr(partitions.shutil, "disk_usage", lambda p: SimpleNamespace(free=0, total=1, used=1))
    report = _run(env)
    assert report["status"] == "ABORTED_EXPORT_FAILED" and _total(env.live) == 52
    assert not list(env.parts.glob("C_*.db"))


# 13 --------------------------------------------------------------------------------------------
def test_row_count_or_hash_mismatch_means_no_delete(env):
    partitions.split_day(env.live, DAY, env.parts, now=AFTER, disk_margin_bytes=0)
    manifest_path = env.parts / "manifests" / f"C_{DAY}.json"
    original = manifest_path.read_text()
    manifest = json.loads(original)
    manifest["rows"] += 1  # archive row count no longer matches
    manifest_path.write_text(json.dumps(manifest))
    report = _run(env)
    assert report["status"] == "ABORTED_VERIFICATION_FAILED" and "row count" in report["reason"]
    assert _total(env.live) == 52
    manifest_path.write_text(original)
    with closing(sqlite3.connect(env.live, isolation_level=None)) as conn:  # late write: hash no longer matches
        conn.execute("UPDATE lead_lag_decisions SET payload_json='{}' WHERE id = 1")
    report = _run(env)
    assert report["deleted_rows"] == 0 and _total(env.live) == 52


# 14 --------------------------------------------------------------------------------------------
def test_current_day_partition_is_never_deleted(env):
    during_day = DAY_START + 3600  # DAY is "today"
    report = _run(env, now=during_day)
    assert DAY.isoformat() not in report["plan"]["candidates"] and _count(env.live) == 50
    assert not (env.parts / f"C_{DAY}.db").exists()
    report = _run(env)  # day after: NEXT_DAY is today and keeps its row
    assert NEXT_DAY.isoformat() not in report["plan"]["candidates"] and _count(env.live, NEXT_DAY) == 1


# 15 --------------------------------------------------------------------------------------------
def test_second_ctrl_c_during_retention_aborts_and_preserves_source(env, monkeypatch, tmp_path):
    pressed = {"now": False}
    real_split = exit_retention.split_day

    def split_then_ctrl_c(*a, **k):
        out = real_split(*a, **k)
        pressed["now"] = True
        return out
    monkeypatch.setattr(exit_retention, "split_day", split_then_ctrl_c)
    report = _run(env, should_abort=lambda: pressed["now"])
    assert report["status"] == "ABORTED" and "operator abort" in report["reason"] and _total(env.live) == 52

    monkeypatch.setenv("RESEARCH_RETENTION_ON_EXIT", "1")
    calls = []

    class Child:
        def wait(self, timeout=None):
            calls.append("wait")
            if len(calls) == 1:
                raise KeyboardInterrupt  # second Ctrl+C while the launcher waits
            return -15

        def terminate(self):
            calls.append("terminate")
    assert launcher.run_exit_retention(tmp_path, launcher.EXIT_OPERATOR_STOP, popen=lambda *a, **k: Child()) \
        == "operator_aborted"
    assert "terminate" in calls


def test_child_signal_handler_aborts_and_rolls_back(env):
    """A real SIGINT to the child mid-run leaves the source intact (or completes atomically)."""
    child = subprocess.Popen([sys.executable, "-c", (
        "import sys, time; sys.path.insert(0, '.');"
        "import scripts.research_exit_retention as m;"
        "from bot.research import partitions;"
        "real = partitions._row_digest\n"
        "def slow(d, r):\n    time.sleep(0.05); real(d, r)\n"
        "partitions._row_digest = slow\n"
        f"sys.argv = ['x', '--db', {str(env.live)!r}, '--partitions', {str(env.parts)!r}, '--export', "
        f"{str(env.export)!r}, '--offsite', {str(env.icloud)!r}, '--timeout-sec', '60']\n"
        "sys.exit(m.main())")], stdout=subprocess.PIPE, text=True)
    time.sleep(1.5)
    child.send_signal(2)
    out, _ = child.communicate(timeout=30)
    summary = json.loads(out.strip().splitlines()[-1])
    assert summary["status"] == "ABORTED" and child.returncode == 3 and _total(env.live) == 52
