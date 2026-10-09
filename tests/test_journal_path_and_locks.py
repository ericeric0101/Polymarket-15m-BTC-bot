"""Canonical TRADE_DB_PATH resolver, journal writer lock, maintenance guards.

Hermetic: temporary repo roots and lock files only; never the real journal.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bot import journal_path as jp
from bot.journal_path import (
    DEFAULT_TRADE_DB_PATH,
    MaintenanceLockError,
    acquire_journal_writer_lock,
    journal_writer_lock_path,
    release_journal_writer_lock,
    require_bot_stopped,
    resolve_trade_db_path,
)
from bot.process_lock import ProcessLock

REPO_ROOT = Path(__file__).resolve().parents[1]


def _repo(tmp_path, *, profile=None, dotenv=None):
    root = tmp_path / "repo"
    (root / "config" / "profiles").mkdir(parents=True)
    (root / "config" / "profiles" / "btc15_twap_v3.env").write_text(profile or "")
    if dotenv is not None:
        (root / ".env").write_text(dotenv)
    return root


# --- resolver ------------------------------------------------------------------

def test_resolver_precedence_explicit_shell_dotenv_profile_default(tmp_path):
    root = _repo(tmp_path, profile="TRADE_DB_PATH=profile.db\n", dotenv="TRADE_DB_PATH=dotenv.db\n")
    assert resolve_trade_db_path("cli.db", repo_root=root, environ={"TRADE_DB_PATH": "shell.db"}) == root / "cli.db"
    assert resolve_trade_db_path(repo_root=root, environ={"TRADE_DB_PATH": "shell.db"}) == root / "shell.db"
    assert resolve_trade_db_path(repo_root=root, environ={}) == root / "dotenv.db"
    (root / ".env").write_text("")
    assert resolve_trade_db_path(repo_root=root, environ={}) == root / "profile.db"
    (root / "config" / "profiles" / "btc15_twap_v3.env").write_text("")
    assert resolve_trade_db_path(repo_root=root, environ={}) == root / "data" / "trading" / "trade_journal.db"


def test_resolver_anchors_relative_paths_at_repo_root_not_cwd(tmp_path, monkeypatch):
    root = _repo(tmp_path, dotenv="TRADE_DB_PATH=./logs/trade_journal.db\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert resolve_trade_db_path(repo_root=root, environ={}) == root / "logs" / "trade_journal.db"


def test_resolver_never_mutates_the_process_environment(tmp_path, monkeypatch):
    root = _repo(tmp_path, dotenv="TRADE_DB_PATH=dotenv.db\nSOME_NEW_KEY=1\n")
    monkeypatch.delenv("TRADE_DB_PATH", raising=False)
    monkeypatch.delenv("SOME_NEW_KEY", raising=False)
    resolve_trade_db_path(repo_root=root)
    assert "TRADE_DB_PATH" not in os.environ and "SOME_NEW_KEY" not in os.environ


def test_bot_default_and_resolver_default_are_one_constant(monkeypatch):
    from bot.app_config import AppConfig
    monkeypatch.delenv("TRADE_DB_PATH", raising=False)
    assert AppConfig.from_env(enable_terminal_dashboard=False).operations.trade_db_path == DEFAULT_TRADE_DB_PATH


def test_no_reader_keeps_a_private_journal_default():
    readers = ["dashboard.py", "scripts/build_outcome_provenance.py", "scripts/fetch_official_resolutions.py",
               "scripts/research_daily_export.py", "scripts/backfill_redeem_activity.py",
               "scripts/reset_session_pnl_guard.py"]
    for rel in readers:
        text = (REPO_ROOT / rel).read_text()
        assert "logs/trade_journal.db" not in text, rel
        assert "resolve_trade_db_path" in text, rel


def test_dashboard_uses_the_canonical_resolver(monkeypatch, tmp_path):
    import dashboard
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "x.db"))
    assert dashboard._resolve_db_path() == tmp_path / "x.db"
    assert dashboard._resolve_db_path("rel.db") == REPO_ROOT / "rel.db"


# --- journal writer lock -----------------------------------------------------------

def _other_process_can_lock(lock_path: Path) -> bool:
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from bot.process_lock import ProcessLock;"
        "print('ACQUIRED' if ProcessLock(sys.argv[2]).acquire() else 'BLOCKED')"
    )
    out = subprocess.run([sys.executable, "-c", code, str(REPO_ROOT), str(lock_path)],
                         capture_output=True, text=True, timeout=30, check=True).stdout.strip()
    return out == "ACQUIRED"


def test_writer_lock_is_exclusive_across_processes_and_reentrant_in_process(tmp_path):
    journal = tmp_path / "trade_journal.db"
    try:
        assert acquire_journal_writer_lock(journal) is True
        assert acquire_journal_writer_lock(journal) is True  # node rebuild, same process
        assert _other_process_can_lock(journal_writer_lock_path(journal)) is False
    finally:
        release_journal_writer_lock(journal)
    assert _other_process_can_lock(journal_writer_lock_path(journal)) is True


def test_launcher_refuses_to_start_when_another_bot_writes_the_journal(monkeypatch, tmp_path):
    from bot import launcher
    journal = tmp_path / "trade_journal.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(journal))

    def must_not_run(**_kwargs):
        raise AssertionError("node cycle started despite the journal writer lock")

    monkeypatch.setattr(launcher, "_run_integrated_bot_cycles", must_not_run)
    other = ProcessLock(journal_writer_lock_path(journal))  # separate open file = another writer
    assert other.acquire()
    try:
        for simulation in (True, False):  # DRY-RUN and LIVE alike
            reason = launcher.run_integrated_bot(simulation=simulation, test_mode=simulation, auth={"pk": "x"})
            assert reason == launcher.EXIT_JOURNAL_WRITER_LOCK_HELD
    finally:
        other.release()


def test_launcher_holds_the_lock_while_running_and_releases_it_after(monkeypatch, tmp_path):
    from bot import launcher
    journal = tmp_path / "trade_journal.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(journal))
    seen = []

    def cycles(**_kwargs):
        seen.append(_other_process_can_lock(journal_writer_lock_path(journal)))
        return "operator_stop"

    monkeypatch.setattr(launcher, "_run_integrated_bot_cycles", cycles)
    assert launcher.run_integrated_bot(simulation=True, test_mode=True, auth={"pk": "x"}) == "operator_stop"
    assert seen == [False]
    assert _other_process_can_lock(journal_writer_lock_path(journal)) is True


# --- destructive maintenance guards ------------------------------------------------

def _guard_env(tmp_path):
    return {"TRADE_DB_PATH": str(tmp_path / "trade_journal.db"),
            "LIVE_PROCESS_LOCK_PATH": str(tmp_path / "live.lock")}


def test_require_bot_stopped_refuses_while_journal_writer_lock_is_held(tmp_path):
    env = _guard_env(tmp_path)
    held = ProcessLock(journal_writer_lock_path(env["TRADE_DB_PATH"]))
    assert held.acquire()
    try:
        with pytest.raises(MaintenanceLockError, match="writer lock is held"):
            with require_bot_stopped(environ=env):
                pass
    finally:
        held.release()


def test_require_bot_stopped_refuses_while_live_lock_is_held(tmp_path):
    env = _guard_env(tmp_path)
    held = ProcessLock(env["LIVE_PROCESS_LOCK_PATH"])
    assert held.acquire()
    try:
        with pytest.raises(MaintenanceLockError, match="LIVE bot process lock"):
            with require_bot_stopped(environ=env):
                pass
    finally:
        held.release()


def test_require_bot_stopped_blocks_bot_start_for_its_duration(tmp_path):
    env = _guard_env(tmp_path)
    with require_bot_stopped(environ=env):
        assert _other_process_can_lock(journal_writer_lock_path(env["TRADE_DB_PATH"])) is False
    assert _other_process_can_lock(journal_writer_lock_path(env["TRADE_DB_PATH"])) is True


def _run_script(rel, args, env_extra):
    env = {**os.environ, **env_extra}
    return subprocess.run([sys.executable, str(REPO_ROOT / rel), *args], capture_output=True, text=True,
                          timeout=60, env=env, cwd=str(REPO_ROOT))


def test_reset_session_guard_apply_refuses_while_bot_runs_and_leaves_journal_untouched(tmp_path):
    from monitoring.trade_journal_db import TradeJournalDB
    env = _guard_env(tmp_path)
    db = TradeJournalDB(env["TRADE_DB_PATH"], backup_path=str(tmp_path / "backup.db"))
    db.stop()
    before = Path(env["TRADE_DB_PATH"]).read_bytes()
    held = ProcessLock(journal_writer_lock_path(env["TRADE_DB_PATH"]))
    assert held.acquire()
    try:
        result = _run_script("scripts/reset_session_pnl_guard.py",
                             ["--date", "2026-10-09", "--reason", "test", "--apply"], env)
    finally:
        held.release()
    assert result.returncode == 4, result.stderr
    assert "REFUSED" in result.stderr
    assert Path(env["TRADE_DB_PATH"]).read_bytes() == before


def test_compact_vacuum_refuses_while_bot_runs(tmp_path):
    import sqlite3
    env = _guard_env(tmp_path)
    research = tmp_path / "research.db"
    with sqlite3.connect(research) as conn:
        conn.execute("CREATE TABLE t (x)")
    held = ProcessLock(journal_writer_lock_path(env["TRADE_DB_PATH"]))
    assert held.acquire()
    try:
        refused = _run_script("scripts/compact_research_db.py", ["--db", str(research), "--vacuum"], env)
        report_only = _run_script("scripts/compact_research_db.py", ["--db", str(research)], env)
    finally:
        held.release()
    assert refused.returncode == 4 and "REFUSED" in refused.stderr
    assert report_only.returncode == 0  # read-only report needs no lock


@pytest.mark.parametrize("rel", [
    "scripts/reset_session_pnl_guard.py",
    "scripts/research_partition.py",
    "scripts/compact_research_db.py",
    "scripts/archive_lead_lag_research.py",
    "scripts/backfill_redeem_activity.py",
])
def test_every_destructive_maintenance_script_takes_the_bot_stopped_guard(rel):
    assert "require_bot_stopped" in (REPO_ROOT / rel).read_text()


def test_resolver_module_is_import_light():
    # Scripts and the dashboard import it; it must not pull in the trading runtime.
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import bot.journal_path;"
            "print(any(m.startswith('nautilus_trader') for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code, str(REPO_ROOT)], capture_output=True, text=True,
                         timeout=60, check=True).stdout.strip()
    assert out == "False"
    assert jp.DEFAULT_LIVE_PROCESS_LOCK_PATH == "/tmp/polymarket-btc-15m-live.lock"
