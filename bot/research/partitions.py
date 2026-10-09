"""Tier C: daily partitions of the high-frequency research store.

``split_day`` copies one completed UTC day of ``lead_lag_decisions`` from the
live research DB (opened read-only) into a closed, read-only per-day SQLite file
and verifies it row-by-row with a content digest computed on both sides.

``deletion_eligibility`` reports, per day, whether evidence is sufficient to
(a) remove that day's rows from the live DB and (b) remove the day partition
after the retention period. This module deliberately performs no deletion:
removal is enabled only after operators review eligibility evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from bot.research import daily_export, decision_export, path_export
from bot.research.daily_export import SUMMARY_SLACK_SEC, _day_bounds, _sha256

TABLE = "lead_lag_decisions"
PARTITION_VERSION = 1
RETENTION_DAYS = 7
DISK_MARGIN_BYTES = 5 * 1024 ** 3
BATCH = 5000


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, timeout=30)


def _row_digest(digest: "hashlib._Hash", row: tuple) -> None:
    rid, run_id, slug, market_id, epoch, payload = row
    digest.update(f"{rid}\x1f{run_id}\x1f{slug}\x1f{market_id}\x1f{epoch}\x1f".encode())
    digest.update((payload or "").encode())
    digest.update(b"\x1e")


def _day_ns(day: date) -> tuple[int, int]:
    start, end = _day_bounds(day)
    return int(start * 1e9), int(end * 1e9)


def partition_paths(part_dir: Path, day: date) -> tuple[Path, Path]:
    return part_dir / f"C_{day.isoformat()}.db", part_dir / "manifests" / f"C_{day.isoformat()}.json"


def split_day(live_db: Path, day: date, part_dir: Path, *, now: float | None = None,
              disk_margin_bytes: int = DISK_MARGIN_BYTES) -> dict[str, Any]:
    now = time.time() if now is None else now
    if now < _day_bounds(day)[1] + SUMMARY_SLACK_SEC:
        raise ValueError(f"{day} is not complete yet")
    target, manifest_path = partition_paths(part_dir, day)
    if partition_is_verified(part_dir, day):
        return json.loads(manifest_path.read_text())
    lo, hi = _day_ns(day)
    source = _ro(live_db)
    try:
        ddl = [row[0] for row in source.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name = ? AND sql IS NOT NULL ORDER BY type DESC", (TABLE,))]
        count, payload_bytes = source.execute(
            f"SELECT COUNT(*), COALESCE(SUM(length(payload_json)), 0) FROM {TABLE} "
            f"WHERE decision_epoch_ns >= ? AND decision_epoch_ns < ?", (lo, hi)).fetchone()
        needed = int(payload_bytes * 1.5) + disk_margin_bytes
        free = shutil.disk_usage(part_dir if part_dir.exists() else part_dir.parent).free
        if free < needed:
            raise OSError(f"insufficient free space for partition {day}: free={free} needed={needed}")
        part_dir.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.unlink(missing_ok=True)
        source_digest = hashlib.sha256()
        with sqlite3.connect(temporary) as dest:
            for statement in ddl:
                dest.execute(statement)
            cursor = source.execute(
                f"SELECT id, run_id, slug, market_id, decision_epoch_ns, payload_json FROM {TABLE} "
                f"WHERE decision_epoch_ns >= ? AND decision_epoch_ns < ? ORDER BY id", (lo, hi))
            while True:
                batch = cursor.fetchmany(BATCH)
                if not batch:
                    break
                for row in batch:
                    _row_digest(source_digest, row)
                dest.executemany(f"INSERT INTO {TABLE} (id, run_id, slug, market_id, decision_epoch_ns, "
                                 f"payload_json) VALUES (?, ?, ?, ?, ?, ?)", batch)
            dest.commit()
    finally:
        source.close()
    check = _ro(temporary)
    try:
        copied_digest = hashlib.sha256()
        for row in check.execute(f"SELECT id, run_id, slug, market_id, decision_epoch_ns, payload_json "
                                 f"FROM {TABLE} ORDER BY id"):
            _row_digest(copied_digest, row)
        copied_count, copied_bytes = check.execute(
            f"SELECT COUNT(*), COALESCE(SUM(length(payload_json)), 0) FROM {TABLE}").fetchone()
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    os.replace(temporary, target)
    os.chmod(target, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # closed day: read-only
    problems = []
    if copied_count != count:
        problems.append(f"row_count {copied_count} != {count}")
    if copied_bytes != payload_bytes:
        problems.append(f"payload_bytes {copied_bytes} != {payload_bytes}")
    if copied_digest.hexdigest() != source_digest.hexdigest():
        problems.append("content_digest_mismatch")
    if integrity != "ok":
        problems.append(f"integrity_check {integrity}")
    manifest = {
        "tier": "C_partition", "partition_version": PARTITION_VERSION, "date_utc": day.isoformat(),
        "created_at_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(), "source_db": str(live_db),
        "file": target.name, "file_sha256": _sha256(target), "file_bytes": target.stat().st_size,
        "rows": count, "payload_bytes": payload_bytes, "content_sha256": source_digest.hexdigest(),
        "epoch_ns_range": [lo, hi], "verification_problems": problems, "verified": not problems,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_manifest = manifest_path.with_suffix(".tmp")
    temporary_manifest.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    os.replace(temporary_manifest, manifest_path)
    return manifest


def partition_is_verified(part_dir: Path, day: date) -> bool:
    target, manifest_path = partition_paths(part_dir, day)
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return False
    return bool(manifest.get("verified")) and target.is_file() and _sha256(target) == manifest.get("file_sha256")


def live_rows_still_match(live_db: Path, part_dir: Path, day: date) -> bool:
    """The live DB still holds exactly the partitioned rows for ``day`` (no late writes)."""
    _target, manifest_path = partition_paths(part_dir, day)
    manifest = json.loads(manifest_path.read_text())
    lo, hi = manifest["epoch_ns_range"]
    digest = hashlib.sha256()
    conn = _ro(live_db)
    try:
        for row in conn.execute(f"SELECT id, run_id, slug, market_id, decision_epoch_ns, payload_json FROM {TABLE} "
                                f"WHERE decision_epoch_ns >= ? AND decision_epoch_ns < ? ORDER BY id", (lo, hi)):
            _row_digest(digest, row)
    finally:
        conn.close()
    return digest.hexdigest() == manifest["content_sha256"]


def _offsite_verified(out_root: Path, tier: str, day: date) -> bool:
    try:
        manifest = json.loads((out_root / "manifests" / f"{tier}_{day.isoformat()}.json").read_text())
    except (OSError, ValueError):
        return False
    offsite = manifest.get("offsite")
    if not offsite:
        return False
    return bool(offsite.get("verified")) if tier == "A" else all(m.get("verified") for m in offsite.values())


def deletion_eligibility(day: date, *, live_db: Path, part_dir: Path, export_root: Path,
                         now: float | None = None, retention_days: int = RETENTION_DAYS) -> dict[str, Any]:
    now = time.time() if now is None else now
    checks = {
        "day_complete": now >= _day_bounds(day)[1] + SUMMARY_SLACK_SEC,
        "partition_verified": partition_is_verified(part_dir, day),
        "A_verified": daily_export.day_is_exported(export_root, day),
        "B_verified": decision_export.day_is_exported(export_root, day),
        "A_offsite_verified": _offsite_verified(export_root, "A", day),
        "B_offsite_verified": _offsite_verified(export_root, "B", day),
        # Narrow per-second paths are what replaces tier C long term; without a
        # verified offsite copy the day's path evidence would be lost.
        "P_verified": path_export.day_is_exported(export_root, day),
        "P_offsite_verified": path_export.day_is_exported(export_root, day, require_offsite=True),
    }
    checks["live_rows_match_partition"] = bool(checks["partition_verified"]) and live_rows_still_match(
        live_db, part_dir, day)
    age_days = (now - _day_bounds(day)[1]) / 86400
    checks["older_than_retention"] = age_days >= retention_days
    exports = ("A_verified", "B_verified", "P_verified", "A_offsite_verified", "B_offsite_verified",
               "P_offsite_verified")
    live_ok = all(checks[k] for k in ("day_complete", "partition_verified", "live_rows_match_partition", *exports))
    partition_ok = all(checks[k] for k in ("partition_verified", "older_than_retention", *exports))
    return {"date_utc": day.isoformat(), "age_days": round(age_days, 2), "checks": checks,
            "live_rows_deletion_eligible": live_ok, "partition_deletion_eligible": partition_ok,
            "deletion_enabled": False}
