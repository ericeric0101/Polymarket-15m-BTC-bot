"""Backup failures must not change primary journal/exposure authority."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import monitoring.trade_journal_db as journal
from monitoring.trade_journal_db import TradeJournalDB


@contextmanager
def stopped_worker_db(tmp_path):
    db = TradeJournalDB(tmp_path / 'primary' / 'journal.db',
                        backup_path=tmp_path / 'backups' / 'journal.db', backup_interval_sec=30)
    # Exercise the same flush authority deterministically, with no worker races.
    db._backup_stop.set()
    db._backup_wakeup.set()
    db._backup_thread.join(timeout=5)
    assert not db._backup_thread.is_alive()
    try:
        yield db
    finally:
        db.stop()


def write_primary(db, n=1):
    db.log_strategy_event('run', 'DIAGNOSTIC', {'n': n})


def primary_count(db):
    with sqlite3.connect(Path(db.db_path).resolve().as_uri() + '?mode=ro', uri=True) as conn:
        return conn.execute('SELECT COUNT(*) FROM strategy_events').fetchone()[0]


def test_unavailable_backup_directory_does_not_fail_primary_write(tmp_path):
    with stopped_worker_db(tmp_path) as db:
        blocker = tmp_path / 'not_a_directory'
        blocker.write_text('keep')
        db.backup_path = str(blocker / 'journal.db')
        write_primary(db)
        assert not db.flush_backup()
        assert primary_count(db) == 1
        assert db.runtime_health_snapshot()['ready'] is True
        assert db._backup_dirty is True
        assert db.backup_health_snapshot()['failure_stage'] == 'directory'
        assert blocker.read_text() == 'keep'


def test_low_space_preflight_preserves_last_good_backup_and_existing_temp(tmp_path, monkeypatch):
    with stopped_worker_db(tmp_path) as db:
        write_primary(db)
        assert db.flush_backup()
        backup = Path(db.backup_path)
        original = backup.read_bytes()
        temporary = backup.with_name('.journal.db.tmp')
        temporary.write_bytes(b'prior partial image')
        monkeypatch.setattr(journal.shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
        write_primary(db, 2)
        assert not db.flush_backup()
        assert backup.read_bytes() == original
        assert temporary.read_bytes() == b'prior partial image'
        assert primary_count(db) == 2
        health = db.backup_health_snapshot()
        assert health['failure_stage'] == 'space_check'
        assert health['required_free_bytes'] == 2 * health['image_bytes'] + len(b'prior partial image')
        assert health['failures_total'] == 1
        assert db.runtime_health_snapshot()['ready'] is True


@pytest.mark.parametrize('error', ['database or disk is full', 'unable to open database file'])
def test_sqlite_backup_failure_keeps_primary_durable_and_closes_connections(tmp_path, monkeypatch, error):
    with stopped_worker_db(tmp_path) as db:
        write_primary(db)
        assert db.flush_backup()
        original = Path(db.backup_path).read_bytes()
        real_connect = journal.sqlite3.connect
        connections = []
        class FailedBackup(sqlite3.Connection):
            def backup(self, *args, **kwargs):
                raise sqlite3.OperationalError(error)
        def connect(path, *args, **kwargs):
            kwargs['factory'] = FailedBackup
            conn = real_connect(path, *args, **kwargs)
            connections.append(conn)
            return conn
        write_primary(db, 2)
        with monkeypatch.context() as m:
            m.setattr(journal.sqlite3, 'connect', connect)
            assert not db.flush_backup()
        assert len(connections) == 2
        for conn in connections:
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                conn.execute('SELECT 1')
        assert Path(db.backup_path).read_bytes() == original
        assert not Path(db.backup_path).with_name('.journal.db.tmp').exists()
        assert primary_count(db) == 2
        assert db._backup_dirty is True
        assert db.runtime_health_snapshot()['state'] == 'HEALTHY'
        assert db.backup_health_snapshot()['error'] == error
        assert db.flush_backup()  # explicit flush resumes after transient failure


def test_destination_open_failure_is_reported_separately_and_recovers(tmp_path, monkeypatch):
    with stopped_worker_db(tmp_path) as db:
        write_primary(db)
        real_connect = journal.sqlite3.connect
        def connect(path, *args, **kwargs):
            if str(path).endswith('.journal.db.tmp'):
                raise sqlite3.OperationalError('unable to open database file')
            return real_connect(path, *args, **kwargs)
        with monkeypatch.context() as m:
            m.setattr(journal.sqlite3, 'connect', connect)
            assert not db.flush_backup()
        assert db.backup_health_snapshot()['failure_stage'] == 'destination_open'
        assert db.flush_backup()
        assert db.backup_health_snapshot()['state'] == 'HEALTHY'


def test_failed_worker_flush_is_cooled_down_despite_repeated_write_wakeups(tmp_path, monkeypatch):
    with stopped_worker_db(tmp_path) as db:
        clock = [100.0]
        monkeypatch.setattr(journal.time, 'monotonic', lambda: clock[0])
        attempts = []
        def backup():
            attempts.append(clock[0])
            return len(attempts) > 1
        monkeypatch.setattr(db, '_backup_after_write', backup)
        write_primary(db)
        assert not db.flush_backup(force=False)
        for n in range(10):
            write_primary(db, n)
            assert not db.flush_backup(force=False)
        assert attempts == [100.0]
        assert db._backup_dirty is True
        clock[0] = 129.99
        assert not db.flush_backup(force=False)
        clock[0] = 130.0
        assert db.flush_backup(force=False)
        assert attempts == [100.0, 130.0]
        assert db._backup_dirty is False
        assert primary_count(db) == 11


def test_shutdown_force_keeps_existing_final_backup_contract(tmp_path, monkeypatch):
    with stopped_worker_db(tmp_path) as db:
        write_primary(db)
        with monkeypatch.context() as m:
            m.setattr(db, '_backup_after_write', lambda: False)
            assert not db.flush_backup(force=False)
        db.stop()
        assert Path(db.backup_path).is_file()
        assert db._backup_dirty is False


def test_backup_retention_is_one_published_image_and_source_is_read_only(tmp_path, monkeypatch):
    with stopped_worker_db(tmp_path) as db:
        real_connect = journal.sqlite3.connect
        source_uris = []
        def connect(path, *args, **kwargs):
            if kwargs.get('uri'):
                source_uris.append(path)
            return real_connect(path, *args, **kwargs)
        monkeypatch.setattr(journal.sqlite3, 'connect', connect)
        for n in range(4):
            write_primary(db, n)
            assert db.flush_backup()
        assert source_uris and all(str(path).endswith('?mode=ro') for path in source_uris)
        assert sorted(p.name for p in Path(db.backup_path).parent.iterdir()) == ['journal.db']
        with real_connect(db.backup_path) as conn:
            assert conn.execute('PRAGMA integrity_check').fetchone() == ('ok',)
            assert conn.execute('SELECT COUNT(*) FROM strategy_events').fetchone()[0] == 4


def test_same_instance_backup_flushes_do_not_overlap_and_new_dirty_write_is_kept(tmp_path, monkeypatch):
    with stopped_worker_db(tmp_path) as db:
        write_primary(db)
        entered = threading.Event()
        release = threading.Event()
        active = []
        attempts = []
        def backup():
            assert not active
            active.append(True)
            attempts.append(True)
            entered.set()
            assert release.wait(timeout=5)
            active.pop()
            return True
        monkeypatch.setattr(db, '_backup_after_write', backup)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(db.flush_backup)
            assert entered.wait(timeout=5)
            second = pool.submit(db.flush_backup)
            write_primary(db, 2)  # primary persistence never waits on the flush lock
            release.set()
            assert first.result(timeout=5)
            assert second.result(timeout=5)
        assert len(attempts) == 2
        assert primary_count(db) == 2
        assert db._backup_dirty is False
