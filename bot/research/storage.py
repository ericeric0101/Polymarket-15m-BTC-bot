"""Explicit read-only offline storage measurement; never run on a tick path."""
from __future__ import annotations
import shutil
import time
from pathlib import Path


class StorageSummary:
    def __init__(self, *, interval_sec: float = 300):
        self.interval_sec = max(1, interval_sec)
        self._next_check = 0.0
        self._cache = None
        self._cache_paths = None

    def measure(self, *, journal: Path, research: Path, btc_dir: Path | None = None,
                now_monotonic: float | None = None, writer_queues: dict | None = None) -> dict:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        paths = (str(journal), str(research), str(btc_dir))
        if self._cache is not None and paths == self._cache_paths and now < self._next_check:
            return {**self._cache, 'cached': True, 'writer_queue_depths': dict(writer_queues or {})}
        self._next_check = now + self.interval_sec
        self._cache_paths = paths
        result = {'state': 'HEALTHY', 'reason_codes': [], 'cached': False,
                  'writer_queue_depths': dict(writer_queues or {}), 'measured_monotonic': now}
        def size(path):
            return path.stat().st_size if path.is_file() else None
        devices = {}
        for label, directory in [('research', research.parent), ('journal', journal.parent), ('btc', btc_dir)]:
            result[label + '_free_disk'] = None
            if directory is None:
                continue
            try:
                device = directory.stat().st_dev
                if device not in devices:
                    devices[device] = shutil.disk_usage(directory).free
                result[label + '_free_disk'] = devices[device]
            except OSError:
                result['state'] = 'UNKNOWN'
                result['reason_codes'].append(label.upper() + '_FILESYSTEM_UNAVAILABLE')
        # Compatibility alias: only the research filesystem, not all paths.
        result['free_disk'] = result['research_free_disk']
        try:
            result.update(
                          journal_db_size=size(journal), research_db_size=size(research),
                          research_wal_size=size(Path(str(research) + '-wal')),
                          journal_wal_size=size(Path(str(journal) + '-wal')),
                          btc_parquet_size=sum(path.stat().st_size for path in btc_dir.rglob('*.parquet')) if btc_dir and btc_dir.is_dir() else None)
            if result['journal_db_size'] is None or result['research_db_size'] is None:
                result['state'] = 'UNKNOWN'; result['reason_codes'].append('DB_FILE_UNAVAILABLE')
        except OSError:
            result['state'] = 'UNKNOWN'; result['reason_codes'].append('STORAGE_MEASUREMENT_UNAVAILABLE')
        # No new low-disk thresholds: existing component guards own them.
        self._cache = result
        return dict(result)


def archival_readiness(*, collection_closed: bool, writers_stopped: bool,
                       integrity_verified: bool, backup_verified: bool) -> dict:
    state = 'ACTIVE' if not collection_closed or not writers_stopped else 'CLOSED'
    if state == 'CLOSED' and integrity_verified and backup_verified:
        state = 'ARCHIVABLE'
    return {'state': state, 'automatic_action': None,
            'reason_codes': [code for ready, code in ((collection_closed, 'COLLECTION_ACTIVE'),
                (writers_stopped, 'WRITERS_RUNNING'), (integrity_verified, 'INTEGRITY_NOT_VERIFIED'),
                (backup_verified, 'BACKUP_NOT_VERIFIED')) if not ready]}
