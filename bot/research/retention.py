"""Tier C retention actions, gated by ``partitions.deletion_eligibility``.

* ``delete_live_rows`` removes one partitioned day from the live research DB.
  The rows are re-hashed inside the deleting transaction and must equal the
  verified partition content, so nothing is removed that is not preserved.
  Requires that no process has the live DB open (the bot must be stopped).
* ``trash_partition`` moves an expired day partition to the macOS Trash; it is
  never unlinked here, so final removal stays an explicit operator action.

Every action is appended to a JSON log under ``<partitions>/retention_log``.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from bot.research.partitions import TABLE, _row_digest, deletion_eligibility, partition_paths

DISK_MARGIN_BYTES = 2 * 1024 ** 3
TRASH_DIR = Path.home() / ".Trash"


class RetentionRefused(RuntimeError):
    pass


def assert_not_in_use(live_db: Path) -> None:
    """Fail closed unless lsof proves no other process has the DB or its WAL open."""
    paths = [str(live_db)] + [str(live_db) + suffix for suffix in ("-wal", "-shm") if Path(str(live_db) + suffix).exists()]
    try:
        result = subprocess.run(["lsof", "-t", "--", *paths], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RetentionRefused(f"cannot verify the live DB is unused: {exc}") from exc
    holders = {pid for pid in result.stdout.split() if pid.strip() and int(pid) != os.getpid()}
    if holders:
        raise RetentionRefused(f"live DB is open by pid(s) {sorted(holders)}; stop the bot first")


def _log(part_dir: Path, record: dict[str, Any]) -> None:
    folder = part_dir / "retention_log"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    (folder / f"{stamp}_{record['action']}_{record['date_utc']}.json").write_text(json.dumps(record, indent=1, sort_keys=True))


def delete_live_rows(live_db: Path, day: date, *, part_dir: Path, export_root: Path,
                     now: float | None = None, check_in_use: bool = True) -> dict[str, Any]:
    eligibility = deletion_eligibility(day, live_db=live_db, part_dir=part_dir, export_root=export_root, now=now)
    if not eligibility["live_rows_deletion_eligible"]:
        failing = [k for k, v in eligibility["checks"].items() if not v and k != "older_than_retention"]
        raise RetentionRefused(f"{day}: live rows not eligible: {failing}")
    if check_in_use:
        assert_not_in_use(live_db)
    manifest = json.loads(partition_paths(part_dir, day)[1].read_text())
    lo, hi = manifest["epoch_ns_range"]
    conn = sqlite3.connect(live_db, timeout=30, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        digest = hashlib.sha256()
        count = 0
        for row in conn.execute(f"SELECT id, run_id, slug, market_id, decision_epoch_ns, payload_json FROM {TABLE} "
                                f"WHERE decision_epoch_ns >= ? AND decision_epoch_ns < ? ORDER BY id", (lo, hi)):
            _row_digest(digest, row)
            count += 1
        if count != manifest["rows"] or digest.hexdigest() != manifest["content_sha256"]:
            conn.execute("ROLLBACK")
            raise RetentionRefused(f"{day}: live rows changed since partitioning; nothing deleted")
        deleted = conn.execute(f"DELETE FROM {TABLE} WHERE decision_epoch_ns >= ? AND decision_epoch_ns < ?",
                               (lo, hi)).rowcount
        if deleted != count:
            conn.execute("ROLLBACK")
            raise RetentionRefused(f"{day}: delete affected {deleted} rows, expected {count}; rolled back")
        conn.execute("COMMIT")
    finally:
        conn.close()
    record = {"action": "delete_live_rows", "date_utc": day.isoformat(), "rows": count,
              "content_sha256": manifest["content_sha256"], "partition": manifest["file"],
              "at_utc": datetime.now(timezone.utc).isoformat(), "eligibility": eligibility}
    _log(part_dir, record)
    return record


def vacuum(live_db: Path, *, part_dir: Path, check_in_use: bool = True) -> dict[str, Any]:
    if check_in_use:
        assert_not_in_use(live_db)
    conn = sqlite3.connect(live_db, timeout=30, isolation_level=None)
    try:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        pages, free_pages = conn.execute("PRAGMA page_count").fetchone()[0], conn.execute("PRAGMA freelist_count").fetchone()[0]
        needed = (pages - free_pages) * page_size * 2 + DISK_MARGIN_BYTES
        free = shutil.disk_usage(live_db.parent).free
        if free < needed:
            raise RetentionRefused(f"VACUUM needs {needed} bytes free, have {free}")
        before = live_db.stat().st_size
        started = time.monotonic()
        conn.execute("VACUUM")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    record = {"action": "vacuum", "date_utc": "all", "bytes_before": before, "bytes_after": live_db.stat().st_size,
              "elapsed_sec": round(time.monotonic() - started, 2), "integrity_check": integrity,
              "at_utc": datetime.now(timezone.utc).isoformat()}
    _log(part_dir, record)
    if integrity != "ok":
        raise RetentionRefused(f"integrity_check after VACUUM: {integrity}")
    return record


def trash_partition(day: date, *, live_db: Path, part_dir: Path, export_root: Path, now: float | None = None,
                    trash_dir: Path = TRASH_DIR) -> dict[str, Any]:
    eligibility = deletion_eligibility(day, live_db=live_db, part_dir=part_dir, export_root=export_root, now=now)
    if not eligibility["partition_deletion_eligible"]:
        failing = [k for k, v in eligibility["checks"].items() if not v and k != "live_rows_match_partition"]
        raise RetentionRefused(f"{day}: partition not eligible: {failing}")
    target, manifest_path = partition_paths(part_dir, day)
    trash_dir.mkdir(parents=True, exist_ok=True)
    destination = trash_dir / f"{target.stem}_{int(time.time())}{target.suffix}"
    os.replace(target, destination)  # same APFS volume: a rename, never an unlink
    manifest = json.loads(manifest_path.read_text())
    manifest.update({"status": "moved_to_trash", "trash_path": str(destination),
                     "trashed_at_utc": datetime.now(timezone.utc).isoformat()})
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    record = {"action": "trash_partition", "date_utc": day.isoformat(), "trash_path": str(destination),
              "file_bytes": manifest["file_bytes"], "at_utc": manifest["trashed_at_utc"], "eligibility": eligibility}
    _log(part_dir, record)
    return record
