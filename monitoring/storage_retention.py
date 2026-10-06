"""Conservative retention authority. No trading decisions or authoritative DB writes.

Unknown artifacts are never eligible. All deletion is scoped, identity-checked,
cooperatively locked and fail-closed when open-file detection is unavailable.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import fcntl
import json
import math
import os
import re
import sqlite3
import subprocess
import time
import stat
import sys

GIB = 1024 ** 3
PINNED_DEFAULT = 'freshness_audit_20261006_231849_+0800'
ROTATED_LOG = re.compile(r'^terminal_bot\.\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d+\.log\.gz$')
SNAPSHOT_MEMBERS = {'trade_journal_snapshot.db', 'twap_forward_shadow_snapshot.db',
                    'snapshot_manifest.json', 'retention.json'}


@dataclass(frozen=True)
class StoragePolicy:
    backup_retention_count: int = 2
    analysis_ttl_days: int = 7
    log_retention_days: int = 7
    log_max_files: int = 50
    warning_free_gib: float = 20.0
    critical_free_gib: float = 10.0
    orphan_safety_hours: int = 24

    def __post_init__(self):
        if not 1 <= self.backup_retention_count <= 2:
            raise ValueError('completed backup retention must be 1 or 2')
        if min(self.analysis_ttl_days, self.log_retention_days, self.log_max_files, self.orphan_safety_hours) <= 0:
            raise ValueError('retention limits must be positive')
        if not self.warning_free_gib > self.critical_free_gib >= 5:
            raise ValueError('warning > critical >= existing 5 GiB primary safety floor required')

    @classmethod
    def from_env(cls):
        return cls(backup_retention_count=int(os.getenv('BACKUP_RETENTION_COUNT', '2')),
                   analysis_ttl_days=int(os.getenv('ANALYSIS_SNAPSHOT_DEFAULT_TTL_DAYS', '7')),
                   log_retention_days=int(os.getenv('LOG_RETENTION_DAYS', '7')),
                   log_max_files=int(os.getenv('LOG_RETENTION_MAX_FILES', '50')),
                   warning_free_gib=float(os.getenv('STORAGE_WARNING_FREE_GIB', '20')),
                   critical_free_gib=float(os.getenv('STORAGE_CRITICAL_FREE_GIB', '10')))

    @staticmethod
    def backup_required_bytes(image_bytes, rollback_bytes=0):
        # Preserve the 1fa2ba0 guard exactly: image + rollback + image reserve.
        return 2 * image_bytes + rollback_bytes

    def state(self, free_bytes, image_bytes, required_bytes):
        if free_bytes < max(5 * GIB, image_bytes):
            return 'EMERGENCY'
        if free_bytes < max(self.critical_free_gib * GIB, required_bytes):
            return 'CRITICAL'
        if free_bytes < max(self.warning_free_gib * GIB, 2 * required_bytes):
            return 'WARNING'
        return 'NORMAL'


def safe_child(base: Path, value: str | Path) -> Path:
    """Reject traversal, symlink parents, hardlinks and outside-namespace paths."""
    base = Path(os.path.abspath(base))
    path = Path(value)
    path = path if path.is_absolute() else base / path
    path = Path(os.path.abspath(path))
    if '..' in Path(value).parts or not path.is_relative_to(base) or path == base:
        raise ValueError('path outside managed namespace')
    for parent in (base, *base.parents, path, *path.parents):
        if parent.is_symlink():
            raise ValueError('symlinks are not managed')
    if path.is_file() and path.stat().st_nlink != 1:
        raise ValueError('hardlinked files are not managed')
    return path


@contextmanager
def maintenance_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    lock = safe_child(directory, '.storage-retention.lock')
    with lock.open('a+b') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_json(path: Path, value):
    temporary = safe_child(path.parent, '.' + path.name + '.tmp')
    try:
        with temporary.open('w', encoding='utf8') as f:
            json.dump(value, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def metadata(path):
    try:
        if path.is_symlink(): return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) and value.get('version') == 1 else {}
    except (OSError, ValueError):
        return {}


def record_completed_backup(backup: Path, source: Path):
    """Completion metadata only after SQLite backup/close/atomic publication.

    No integrity scan on the 30s writer path. Explicit maintenance verifies a
    newer image before removing any older completed image.
    """
    manifest = safe_child(backup.parent, 'backup_retention.json')
    old = metadata(manifest).get('completed', [])
    stat = backup.stat()
    current = {'name': backup.name, 'completed_ts': time.time(), 'size': stat.st_size,
               'mtime_ns': stat.st_mtime_ns, 'method': 'sqlite_online_backup'}
    records = [r for r in old if isinstance(r, dict) and r.get('name') != backup.name]
    atomic_json(manifest, {'version': 1, 'current': backup.name, 'source': str(source.resolve()), 'completed': [current, *records]})


def in_use(path: Path) -> bool:
    try:
        result = subprocess.run(['lsof', '-t', '--', str(path)], capture_output=True, timeout=5)
        # Exit 1 without output means no matching handles; all uncertainty retains.
        return result.returncode != 1 or bool(result.stdout) or bool(result.stderr)
    except (OSError, subprocess.TimeoutExpired):
        return True


def identity(path):
    st = path.stat()
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def verified_backup(path):
    try:
        if path.is_symlink() or in_use(path): return False
        if any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')): return False
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True) as c:
            return c.execute('PRAGMA quick_check').fetchall() == [('ok',)]
    except (OSError, sqlite3.Error):
        return False



def unlink_checked(root, path, expected):
    """Anchor deletion to directory descriptors; never follow a swapped parent.

    Runtime owners share the advisory lock. Device/inode/size/mtime are checked
    again at unlink time, with no symlink following or hardlink deletion.
    """
    path = safe_child(root, path)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.relative_to(root).parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        st = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        actual = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if actual != tuple(expected) or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            return False
        os.unlink(path.name, dir_fd=fd)
        return True
    finally:
        os.close(fd)

def expired_logs(files, policy, now):
    logs = []
    for path in files:
        try:
            if ROTATED_LOG.fullmatch(path.name) and path.is_file() and not path.is_symlink():
                logs.append(path)
        except OSError: pass
    logs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p for i, p in enumerate(logs)
            if i >= policy.log_max_files or now - p.stat().st_mtime > policy.log_retention_days * 86400]


class LogRetention:
    """Loguru handles rotation/compression; the shared policy chooses retention."""
    def __init__(self, directory, policy):
        self.directory, self.policy = Path(directory).absolute(), policy

    def __call__(self, files):
        try:
            with maintenance_lock(self.directory):
                for value in files:
                    safe_child(self.directory, Path(value).absolute())
                for path in expired_logs([Path(p).absolute() for p in files], self.policy, time.time()):
                    if not in_use(path): unlink_checked(self.directory, path, identity(path))
        except (OSError, ValueError) as exc:
            print(f"Storage log retention deferred; preserve files: {exc}", file=sys.stderr)
            return  # retain on uncertainty / maintenance lock contention


def logging_options(directory, policy):
    return {'rotation': '20 MB', 'retention': LogRetention(directory, policy),
            'compression': 'gz', 'level': 'DEBUG', 'enqueue': True}


class RetentionManager:
    def __init__(self, root, policy=None, *, open_check=in_use):
        self.root = Path(root).absolute()
        if self.root.is_symlink() or not (self.root / 'monitoring/trade_journal_db.py').is_file():
            raise ValueError('root must be a repository checkout')
        self.policy = policy or StoragePolicy.from_env()
        self.open_check = open_check

    def classify_snapshot(self, name, *, pinned=True, unpin=False, now=None):
        directory = safe_child(self.root / 'data/analysis_snapshots', name)
        if not directory.is_dir() or directory.parent != self.root / 'data/analysis_snapshots':
            raise ValueError('only immediate snapshot directories may be classified')
        old = metadata(directory / 'retention.json')
        if not pinned and (directory.name == PINNED_DEFAULT or old.get('state') == 'PINNED') and not unpin:
            raise ValueError('pinned snapshot requires explicit --unpin')
        now = time.time() if now is None else now
        with maintenance_lock(directory.parent):
            atomic_json(safe_child(directory, 'retention.json'),
                        {'version': 1, 'state': 'PINNED' if pinned else 'TEMPORARY',
                         'classified_ts': now, 'expires_ts': now + self.policy.analysis_ttl_days * 86400,
                         'explicit_unpin': bool(unpin)})

    def plan(self, *, protect=(), now=None):
        now = time.time() if now is None else now
        protected = [safe_child(self.root, value) for value in protect]
        candidates = []
        def add(path, reason, group, witness=None):
            try:
                path = safe_child(self.root, path)
                if path.suffix in ('.wal', '.shm') or path.name.endswith(('-wal', '-shm')): return
                if any(path == p or path.is_relative_to(p) for p in protected) or self.open_check(path): return
                candidates.append({'path': str(path), 'reason': reason, 'group': str(group),
                                   'identity': identity(path), 'witness': str(witness) if witness else None})
            except (OSError, ValueError): pass
        backups = self.root / 'backups'
        manifest = metadata(backups / 'backup_retention.json')
        current = manifest.get('current', 'trade_journal.db')
        try: current_path = safe_child(backups, current)
        except (ValueError, TypeError): current_path = backups / 'trade_journal.db'
        # Fixed temporary target known from the existing backup implementation.
        for temp in (backups / ('.' + current_path.name + '.tmp'), backups / '.backup_retention.json.tmp'):
            if temp.is_file() and now - temp.stat().st_mtime > self.policy.orphan_safety_hours * 3600:
                add(temp, 'STALE_ORPHAN_TEMP', backups)
        records = []
        for record in manifest.get('completed', []):
            try:
                p = safe_child(backups, record['name'])
                if p.parent != backups or p.suffix != '.db' or str(p.resolve()) == manifest.get('source'): continue
                st = p.stat()
                if record.get('method') == 'sqlite_online_backup' and st.st_size == record['size'] and st.st_mtime_ns == record['mtime_ns']:
                    records.append((record['completed_ts'], p))
            except (KeyError, TypeError, OSError, ValueError): pass
        records.sort(reverse=True)
        retained = {current_path} | {p for _, p in [(ts, p) for ts, p in records if p != current_path][:self.policy.backup_retention_count - 1]}
        for timestamp, p in records:
            if p in retained: continue
            witness = next((new for ts, new in records if ts > timestamp and new in retained and new != p
                            and verified_backup(new)), None)
            if witness is not None: add(p, 'REDUNDANT_VERIFIED_BACKUP', backups, witness)
        logs = self.root / 'logs/bot'
        for p in expired_logs(list(logs.glob('*.log.gz')), self.policy, now):
            add(p, 'EXPIRED_COMPRESSED_LOG', logs)
        snapshots = self.root / 'data/analysis_snapshots'
        for directory in snapshots.iterdir() if snapshots.is_dir() else ():
            try:
                directory = safe_child(snapshots, directory)
                if not directory.is_dir(): continue
                m = metadata(directory / 'retention.json')
                if m.get('state') != 'TEMPORARY' or not isinstance(m.get('expires_ts'), (int, float)) or not math.isfinite(m['expires_ts']) or now <= m['expires_ts']: continue
                if directory.name == PINNED_DEFAULT and m.get('explicit_unpin') is not True: continue
                files = list(directory.iterdir())
                if not files or any(not p.is_file() or p.name not in SNAPSHOT_MEMBERS or p.is_symlink() or self.open_check(p) for p in files): continue
                # Validate the whole group before adding any member.
                for p in files: safe_child(directory, p)
                for p in files: add(p, 'EXPIRED_CLASSIFIED_SNAPSHOT', snapshots)
            except (OSError, ValueError): pass
        priority = {'STALE_ORPHAN_TEMP': 0, 'EXPIRED_COMPRESSED_LOG': 1,
                    'REDUNDANT_VERIFIED_BACKUP': 2, 'EXPIRED_CLASSIFIED_SNAPSHOT': 3}
        return sorted(candidates, key=lambda row: (priority[row['reason']], row['path']))

    def apply(self, *, protect=(), now=None):
        # Replan under the SAME locks used by runtime producers. No stale plan
        # from a prior dry run authorizes deletion.
        from contextlib import ExitStack
        removed = []
        with ExitStack() as stack:
            for directory in ('backups', 'logs/bot', 'data/analysis_snapshots'):
                stack.enter_context(maintenance_lock(self.root / directory))
            for candidate in self.plan(protect=protect, now=now):
                p = Path(candidate['path'])
                print('DELETE', p, candidate['reason'], flush=True)
                safe_child(self.root, p)
                if identity(p) != tuple(candidate['identity']) or self.open_check(p): continue
                if candidate['witness'] and not verified_backup(Path(candidate['witness'])): continue
                if unlink_checked(self.root, p, candidate['identity']):
                    removed.append(str(p))
        return removed
