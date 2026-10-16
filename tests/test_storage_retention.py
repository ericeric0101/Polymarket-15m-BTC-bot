from pathlib import Path
import json
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest
import monitoring.storage_retention as retention
from monitoring.storage_retention import (StoragePolicy, RetentionManager, atomic_json,
    record_completed_backup, maintenance_lock, PINNED_DEFAULT, logging_options)
from scripts.storage_maintenance import main
from bot.research.storage import StorageSummary


@pytest.fixture
def root(tmp_path):
    (tmp_path/'monitoring').mkdir()
    (tmp_path/'monitoring/trade_journal_db.py').write_text('fixture marker')
    for directory in ('backups','logs/bot','data/analysis_snapshots','data/research'):
        (tmp_path/directory).mkdir(parents=True)
    return tmp_path


def make_db(path):
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE evidence (value INTEGER)')
        c.execute('INSERT INTO evidence VALUES (1)')


def manifest(root, names):
    records=[]
    for i,name in enumerate(names):
        p=root/'backups'/name;make_db(p);s=p.stat()
        records.append({'name':name,'completed_ts':i+1,'size':s.st_size,'mtime_ns':s.st_mtime_ns,'method':'sqlite_online_backup'})
    atomic_json(root/'backups/backup_retention.json', {'version':1,'current':names[-1], 'completed':records})


def test_count_bounded_newest_known_good_retained(root, monkeypatch):
    manifest(root,['old.db','previous.db','trade_journal.db'])
    monkeypatch.setattr(retention,'in_use',lambda p:False)
    manager=RetentionManager(root,open_check=lambda p:False)
    plan=manager.plan()
    assert [Path(x['path']).name for x in plan]==['old.db']
    assert manager.apply()==[str(root/'backups/old.db')]
    assert (root/'backups/previous.db').exists() and (root/'backups/trade_journal.db').exists()


def test_never_delete_only_good_or_unknown_backups(root, monkeypatch):
    manifest(root,['only_good.db','corrupt.db','trade_journal.db'])
    (root/'backups/corrupt.db').write_bytes(b'bad')
    (root/'backups/trade_journal.db').write_bytes(b'bad')
    (root/'backups/unknown.db').write_bytes(b'unknown')
    monkeypatch.setattr(retention,'in_use',lambda p:False)
    assert RetentionManager(root,StoragePolicy(backup_retention_count=1),open_check=lambda p:False).plan()==[]
    assert (root/'backups/only_good.db').exists()


@pytest.mark.parametrize('age,busy,eligible',[(25,False,True),(1,False,False),(25,True,False)])
def test_orphan_temp_requires_age_and_no_owner(root,age,busy,eligible):
    p=root/'backups/.trade_journal.db.tmp';p.write_bytes(b'unpublished')
    now=time.time();os.utime(p,(now-age*3600,now-age*3600))
    manager=RetentionManager(root,open_check=lambda p:busy)
    assert bool(manager.plan(now=now)) is eligible


def test_backup_owner_lock_prevents_apply(root):
    p=root/'backups/.trade_journal.db.tmp';p.write_bytes(b'unpublished');os.utime(p,(1,1))
    manager=RetentionManager(root,open_check=lambda p:False)
    with maintenance_lock(root/'backups'):
        with pytest.raises(BlockingIOError):manager.apply()
    assert p.exists()


@pytest.mark.parametrize('state,name,expired,expected',[
 ('PINNED','pinned',True,False),('TEMPORARY','temp',True,True),
 ('TEMPORARY','recent',False,False),('UNKNOWN','unknown',True,False),
 ('TEMPORARY',PINNED_DEFAULT,True,False)])
def test_classified_snapshot_expiry_only(root,state,name,expired,expected):
    d=root/'data/analysis_snapshots'/name;d.mkdir();(d/'trade_journal_snapshot.db').write_bytes(b'offline')
    atomic_json(d/'retention.json',{'version':1,'state':state,'expires_ts':1 if expired else time.time()+86400})
    plan=RetentionManager(root,open_check=lambda p:False).plan()
    assert bool(plan) is expected


def test_pin_requires_explicit_unpin_and_new_ttl(root):
    d=root/'data/analysis_snapshots'/PINNED_DEFAULT;d.mkdir()
    m=RetentionManager(root,open_check=lambda p:False)
    with pytest.raises(ValueError):m.classify_snapshot(d.name,pinned=False)
    m.classify_snapshot(d.name,pinned=False,unpin=True,now=100)
    data=json.loads((d/'retention.json').read_text())
    assert data['expires_ts']==100+7*86400 and data['explicit_unpin']


def test_wal_and_unknown_snapshot_members_retain_whole_group(root):
    d=root/'data/analysis_snapshots/temp';d.mkdir()
    atomic_json(d/'retention.json',{'version':1,'state':'TEMPORARY','expires_ts':1})
    for name in ('trade_journal_snapshot.db','trade_journal_snapshot.db-wal'):(d/name).write_text('keep')
    assert RetentionManager(root,open_check=lambda p:False).plan()==[]


def test_dry_run_zero_mutations_apply_only_eligible(root):
    temp=root/'backups/.trade_journal.db.tmp';temp.write_bytes(b'orphan');os.utime(temp,(1,1))
    authority=root/'data/research/twap_forward_shadow.db';authority.write_text('primary')
    before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    main(['--root',str(root)])
    assert before=={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    m=RetentionManager(root,open_check=lambda p:False);m.apply()
    assert not temp.exists() and authority.read_text()=='primary'


def test_protect_and_path_traversal_symlink_wrong_root(root,tmp_path):
    temp=root/'backups/.trade_journal.db.tmp';temp.write_bytes(b'orphan');os.utime(temp,(1,1))
    m=RetentionManager(root,open_check=lambda p:False)
    assert m.plan(protect=['backups'])==[]
    for value in ['../outside',str(root.parent/'outside')]:
        with pytest.raises(ValueError):m.plan(protect=[value])
    with pytest.raises(ValueError):RetentionManager(root/'logs')
    d=root/'data/analysis_snapshots/link';d.symlink_to(root/'data/research',target_is_directory=True)
    with pytest.raises(ValueError):m.classify_snapshot('link')
    authority=root/'data/research/main.db';authority.write_text('keep')
    temp.unlink();temp.symlink_to(authority)
    assert m.plan()==[]


def test_open_detection_fail_closed(monkeypatch,tmp_path):
    monkeypatch.setattr(retention.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1,stdout=b'',stderr=b'permission warning'))
    assert retention.in_use(tmp_path/'x')


def test_loguru_rotation_compression_count_and_age(root):
    policy=StoragePolicy(log_max_files=2)
    options=logging_options(root/'logs/bot',policy)
    assert options['rotation']=='20 MB' and options['compression']=='gz' and options['enqueue']
    for i in range(3):
        p=root/'logs/bot'/f'terminal_bot.2026-10-07_01-00-00_{i:06}.log.gz';p.write_bytes(b'compressed')
        os.utime(p,(100+i,100+i))
    (root/'logs/bot/terminal_bot.log').write_text('active')
    (root/'logs/bot/user_report.log.gz').write_text('unknown')
    m=RetentionManager(root,policy,open_check=lambda p:False)
    plan=m.plan(now=103)
    assert len(plan)==1 and '000000' in plan[0]['path']
    assert (root/'logs/bot/terminal_bot.log').exists()
    assert len(m.plan(now=100+8*86400))==3


def test_loguru_retention_callback_preserves_current_and_external_logs(root,monkeypatch):
    old=root/'logs/bot/terminal_bot.2026-10-07_01-00-00_000001.log.gz';old.write_text('old');os.utime(old,(1,1))
    active=root/'logs/bot/terminal_bot.log';active.write_text('current')
    unknown=root/'logs/bot/manual.log.gz';unknown.write_text('manual')
    monkeypatch.setattr(retention,'in_use',lambda p:False)
    options=logging_options(root/'logs/bot',StoragePolicy())
    options['retention']([str(old),str(active),str(unknown)])
    assert not old.exists() and active.exists() and unknown.exists()


def test_watermarks_and_preserved_backup_budget():
    p=StoragePolicy();g=retention.GIB
    assert p.backup_required_bytes(3*g,g)==7*g
    assert [p.state(n*g,3*g,7*g) for n in (30,15,8,4)]==['NORMAL','WARNING','CRITICAL','EMERGENCY']
    with pytest.raises(ValueError):StoragePolicy(backup_retention_count=3)


def test_runtime_telemetry_is_cached_and_does_not_scan_on_repeated_access(root,monkeypatch):
    journal=root/'journal.db';journal.write_bytes(b'primary')
    backup=root/'backups/trade_journal.db';backup.write_bytes(b'backup')
    calls=[]
    def disk(p):calls.append(p);return SimpleNamespace(free=30*retention.GIB)
    monkeypatch.setattr('bot.research.storage.shutil.disk_usage',disk)
    s=StorageSummary()
    first=s.measure_runtime(root=root,journal=journal,backup=backup,policy=StoragePolicy(),backup_health={},now_monotonic=1)
    second=s.measure_runtime(root=root,journal=journal,backup=backup,policy=StoragePolicy(),backup_health={},now_monotonic=2)
    assert len(calls)==2 and second['cached'] and first['storage_state']=='NORMAL'
    assert {'filesystem_free_gib','journal_db_gib','journal_wal_gib','backup_dir_gib','backup_count',
            'analysis_snapshot_dir_gib','log_dir_gib','next_backup_required_gib'} <= first.keys()


def test_completed_manifest_is_atomic_and_does_not_accumulate_files(root):
    p=root/'backups/trade_journal.db';make_db(p)
    for _ in range(3):record_completed_backup(p,root/'journal.db')
    manifest_data=json.loads((root/'backups/backup_retention.json').read_text())
    assert len(manifest_data['completed'])==1
    assert not list((root/'backups').glob('*.tmp'))


def test_actual_loguru_rotation_is_compressed_and_bounded(root,monkeypatch):
    from loguru import logger
    monkeypatch.setattr(retention,'in_use',lambda p:False)
    directory=root/'logs/bot'
    options=logging_options(directory,StoragePolicy(log_max_files=2))
    options['rotation']='100 B'
    sink=logger.add(str(directory/'terminal_bot.log'),**options)
    try:
        for i in range(5):logger.debug('retention rotation synthetic {} {}',i,'x'*150)
        logger.complete()
    finally:logger.remove(sink)
    assert (directory/'terminal_bot.log').exists()
    assert 1 <= len(list(directory.glob('*.log.gz'))) <= 2


def test_primary_in_backup_namespace_and_hardlinked_snapshot_are_protected(root,monkeypatch):
    manifest(root,['source.db','previous.db','trade_journal.db'])
    p=root/'backups/backup_retention.json';m=json.loads(p.read_text());m['source']=str(root/'backups/source.db');atomic_json(p,m)
    monkeypatch.setattr(retention,'in_use',lambda p:False)
    manager=RetentionManager(root,open_check=lambda p:False)
    assert manager.plan()==[]
    d=root/'data/analysis_snapshots/temp';d.mkdir()
    os.link(root/'backups/source.db',d/'trade_journal_snapshot.db')
    atomic_json(d/'retention.json',{'version':1,'state':'TEMPORARY','expires_ts':1})
    assert manager.plan()==[]


def test_cli_apply_and_dry_pin_respect_explicit_mutation(root,monkeypatch):
    monkeypatch.setattr(retention.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1,stdout=b'',stderr=b''))
    d=root/'data/analysis_snapshots/example';d.mkdir()
    main(['--root',str(root),'--pin','example'])
    assert not (d/'retention.json').exists()
    p=root/'backups/.trade_journal.db.tmp';p.write_text('orphan');os.utime(p,(1,1))
    main(['--root',str(root),'--apply'])
    assert not p.exists()


def test_changed_identity_and_swapped_symlink_parent_cannot_unlink_authority(root):
    p=root/'backups/old.db';p.write_text('old');expected=retention.identity(p)
    p.write_text('changed identity')
    assert not retention.unlink_checked(root,p,expected)
    assert p.exists()
    directory=root/'data/analysis_snapshots/temp';directory.symlink_to(root/'data/research',target_is_directory=True)
    with pytest.raises(ValueError):retention.unlink_checked(root,directory/'main.db',expected)
