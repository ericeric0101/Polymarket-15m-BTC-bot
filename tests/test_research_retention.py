"""Tier C retention actions (tmp only; partitions go to a tmp 'Trash', never unlinked)."""
import json
import sqlite3
import subprocess
import sys
import time

import pytest

from bot.research import partitions, retention
from test_research_tiers_bc import AFTER, DAY, DAY_START, _exports, _live, _rows


@pytest.fixture
def ready(tmp_path):
    live = _live(tmp_path, _rows())
    partitions.split_day(live, DAY, tmp_path / "parts", now=AFTER, disk_margin_bytes=0)
    _exports(tmp_path, offsite=True)
    return live, dict(part_dir=tmp_path / "parts", export_root=tmp_path / "export")


def _count(live):
    with sqlite3.connect(live) as conn:
        return conn.execute("SELECT count(*) FROM lead_lag_decisions").fetchone()[0]


def test_delete_live_rows_removes_exactly_the_partitioned_day(ready, tmp_path):
    live, kw = ready
    record = retention.delete_live_rows(live, DAY, now=AFTER, **kw)
    assert record["rows"] == 50 and _count(live) == 2  # the before/after-day rows remain
    assert list((tmp_path / "parts" / "retention_log").glob("*delete_live_rows*"))
    with pytest.raises(retention.RetentionRefused):  # second run: nothing eligible to delete
        retention.delete_live_rows(live, DAY, now=AFTER, **kw)


def test_delete_refuses_without_exports_or_with_changed_rows(tmp_path):
    live = _live(tmp_path, _rows())
    partitions.split_day(live, DAY, tmp_path / "parts", now=AFTER, disk_margin_bytes=0)
    kw = dict(part_dir=tmp_path / "parts", export_root=tmp_path / "export")
    with pytest.raises(retention.RetentionRefused):
        retention.delete_live_rows(live, DAY, now=AFTER, **kw)  # no A/B/P exports yet
    _exports(tmp_path, offsite=True)
    with sqlite3.connect(live) as conn:
        conn.execute("UPDATE lead_lag_decisions SET payload_json='{}' WHERE id = 1")
    with pytest.raises(retention.RetentionRefused):
        retention.delete_live_rows(live, DAY, now=AFTER, **kw)
    assert _count(live) == 52


def test_live_db_held_open_by_another_process_blocks_deletion(ready):
    live, kw = ready
    holder = subprocess.Popen([sys.executable, "-c", f"f=open({str(live)!r},'rb'); import time; time.sleep(30)"])
    try:
        time.sleep(0.5)
        with pytest.raises(retention.RetentionRefused, match="stop the bot"):
            retention.delete_live_rows(live, DAY, now=AFTER, **kw)
    finally:
        holder.kill()
    assert _count(live) == 52


def test_vacuum_shrinks_and_checks_integrity(ready):
    live, kw = ready
    retention.delete_live_rows(live, DAY, now=AFTER, **kw)
    record = retention.vacuum(live, part_dir=kw["part_dir"])
    assert record["integrity_check"] == "ok" and record["bytes_after"] <= record["bytes_before"]


def test_partition_moves_to_trash_only_after_retention(ready, tmp_path):
    live, kw = ready
    trash = tmp_path / "Trash"
    with pytest.raises(retention.RetentionRefused):
        retention.trash_partition(DAY, live_db=live, now=AFTER, trash_dir=trash, **kw)
    record = retention.trash_partition(DAY, live_db=live, now=AFTER + 8 * 86400, trash_dir=trash, **kw)
    moved = list(trash.iterdir())
    assert len(moved) == 1 and str(moved[0]) == record["trash_path"]
    assert not (tmp_path / "parts" / f"C_{DAY}.db").exists()
    manifest = json.loads((tmp_path / "parts" / "manifests" / f"C_{DAY}.json").read_text())
    assert manifest["status"] == "moved_to_trash"


def test_retention_module_never_unlinks_files():
    import inspect
    source = inspect.getsource(retention)
    for forbidden in ("unlink(", "os.remove", "rmtree"):
        assert forbidden not in source


# --- Startup maintenance hook -------------------------------------------------------------------

def _project(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "research_maintenance.py").write_text("print('ok')\n")
    return tmp_path


def test_startup_maintenance_runs_niced_and_detached(tmp_path, monkeypatch):
    import subprocess as sp
    from bot import launcher
    calls = []
    monkeypatch.delenv("RESEARCH_MAINTENANCE_ON_START", raising=False)
    monkeypatch.setattr(sp, "Popen", lambda cmd, **kw: calls.append((cmd, kw)) or type("P", (), {"pid": 4242})())
    assert launcher.start_research_maintenance(_project(tmp_path)) == 4242
    cmd, kw = calls[0]
    assert cmd[:3] == ["nice", "-n", "15"] and cmd[-1].endswith("research_maintenance.py")
    assert kw["start_new_session"] is True and kw["stdin"] is sp.DEVNULL
    assert (tmp_path / "logs" / "research_maintenance.log").is_file()


def test_startup_maintenance_can_be_disabled_and_never_raises(tmp_path, monkeypatch):
    import subprocess as sp
    from bot import launcher
    monkeypatch.setenv("RESEARCH_MAINTENANCE_ON_START", "0")
    assert launcher.start_research_maintenance(_project(tmp_path)) is None
    monkeypatch.setenv("RESEARCH_MAINTENANCE_ON_START", "1")
    monkeypatch.setattr(sp, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("no fork")))
    assert launcher.start_research_maintenance(tmp_path) is None
