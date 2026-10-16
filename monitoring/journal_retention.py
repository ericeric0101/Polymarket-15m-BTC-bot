"""Trade-journal diagnostic retention: archive -> verify -> delete.

Policy (2026-10-09):
* Core trading/accounting events stay in the main journal forever.
* Diagnostic/telemetry events (explicit allow-list below) stay for the most
  recent ``HOT_RETENTION_DAYS`` complete UTC days; older complete days are
  archived to ``data/journal_archive/`` and only then deleted.
* ``ENTRY_DECISION_TRACE`` is archived whole (no schema change, no field drop).

Fail-safe order for each UTC day and table:
1. export rows (ordered by id) to a temporary xz JSONL file, fsync, atomic rename;
2. write a manifest (row count, id/ts range, per-type counts, content sha256,
   file sha256);
3. re-read the archive and require identical count, ids and content hash;
4. delete only with ``apply=True``, no other process holding the journal open,
   a backup that already contains every row to be deleted, and inside one
   IMMEDIATE transaction that re-hashes the live rows and checks the deleted
   row count. Any mismatch rolls back. Nothing is deleted on any failure.
The optional mirror (external disk) is copy + hash verify only; it never
deletes and never blocks the local archive. VACUUM is a separate, one-time,
operator-run step (``vacuum_once``), never part of the daily run.
"""
from __future__ import annotations

import hashlib
import json
import lzma
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

HOT_RETENTION_DAYS = 14
ARCHIVE_FORMAT_VERSION = 1
LSOF = "/usr/sbin/lsof"
VACUUM_MARKER = "VACUUM_DONE.json"

DIAGNOSTIC_EVENT_TYPES: dict[str, frozenset[str]] = {
    "strategy_events": frozenset({
        "ENTRY_DECISION_TRACE",
        "QUOTE_TRANSPORT_TELEMETRY",
        "SIDE_DECISION_OBSERVATION",
        "BUY_PATH_DIAGNOSTIC",
        "SIDE_DECISION",
        "ENTRY_CONFIRMATION_OBSERVATION",
        "SMART_MONEY_OBSERVATION",
        "EVENT_LOOP_CONSUMER_TIMING",
        "LIVE_SIGNAL_COMPARE",
        "SHADOW_SIGNAL_CANDIDATE_LIVE",
        "MAIN_SIGNAL_CANDIDATE_LIVE",
        "ENTRY_REGIME_OBSERVATION",
        "ACCOUNT_SUMMARY",
    }),
    "order_events": frozenset({
        "ENTRY_EDGE_OBSERVATION",
        "DEPTH_RISK_SHADOW_MARKOUT",
        "DEPTH_RISK_SHADOW_CANDIDATE",
        "ORDER_OBSERVE_BUY_BLOCKED",
    }),
}
TABLES = tuple(DIAGNOSTIC_EVENT_TYPES)


class RetentionRefused(RuntimeError):
    """A precondition for a destructive step is not met; nothing was changed."""


class ArchiveIntegrityError(RuntimeError):
    """An archive file does not match its manifest."""


def is_diagnostic(table: str, event_type: str) -> bool:
    return event_type in DIAGNOSTIC_EVENT_TYPES.get(table, frozenset())


def cutoff_day(now: datetime, hot_days: int = HOT_RETENTION_DAYS) -> str:
    """Days strictly before this UTC date are archivable (today + 14 full days stay hot)."""
    return (now.astimezone(timezone.utc).date() - timedelta(days=hot_days)).isoformat()


def _next_day(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()


def _marks(table: str) -> str:
    return ",".join("?" * len(DIAGNOSTIC_EVENT_TYPES[table]))


def _types(table: str) -> tuple[str, ...]:
    return tuple(sorted(DIAGNOSTIC_EVENT_TYPES[table]))


DayBounds = dict[str, dict[str, tuple[int, int]]]


def day_id_bounds(conn: sqlite3.Connection, *, now: datetime, hot_days: int = HOT_RETENTION_DAYS) -> DayBounds:
    """One scan per table: archivable UTC day -> (min id, max id) of its diagnostic rows."""
    cutoff = cutoff_day(now, hot_days)
    bounds: DayBounds = {}
    for table in TABLES:
        rows = conn.execute(
            f"SELECT substr(ts,1,10), MIN(id), MAX(id) FROM {table} "
            f"WHERE event_type IN ({_marks(table)}) AND ts < ? GROUP BY 1",
            (*_types(table), cutoff),
        ).fetchall()
        bounds[table] = {d: (int(lo), int(hi)) for d, lo, hi in rows if d and d < cutoff}
    return bounds


def eligible_days(conn: sqlite3.Connection, *, now: datetime, hot_days: int = HOT_RETENTION_DAYS) -> list[str]:
    bounds = day_id_bounds(conn, now=now, hot_days=hot_days)
    return sorted({day for per_table in bounds.values() for day in per_table})


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _select_rows(conn: sqlite3.Connection, table: str, day: str, bounds: Optional[DayBounds] = None) -> list[tuple]:
    if bounds is not None:
        span = bounds.get(table, {}).get(day)
        if span is None:
            return []
        # The id range comes from a scan in this same run; the ts/type filter
        # keeps the selection exact. Rows outside the range are never deleted.
        return conn.execute(
            f"SELECT * FROM {table} WHERE id BETWEEN ? AND ? AND ts >= ? AND ts < ? "
            f"AND event_type IN ({_marks(table)}) ORDER BY id",
            (span[0], span[1], day, _next_day(day), *_types(table)),
        ).fetchall()
    return conn.execute(
        f"SELECT * FROM {table} WHERE ts >= ? AND ts < ? AND event_type IN ({_marks(table)}) ORDER BY id",
        (day, _next_day(day), *_types(table)),
    ).fetchall()


def _line(row: Iterable[Any]) -> bytes:
    return (json.dumps(list(row), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _summary(rows: list[tuple], lines: list[bytes], columns: list[str]) -> dict[str, Any]:
    idx_type, idx_ts = columns.index("event_type"), columns.index("ts")
    per_type: dict[str, int] = {}
    for row in rows:
        per_type[row[idx_type]] = per_type.get(row[idx_type], 0) + 1
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line)
    return {
        "rows": len(rows),
        "min_id": rows[0][0] if rows else None,
        "max_id": rows[-1][0] if rows else None,
        "min_ts": min((r[idx_ts] for r in rows), default=None),
        "max_ts": max((r[idx_ts] for r in rows), default=None),
        "per_event_type": dict(sorted(per_type.items())),
        "content_sha256": digest.hexdigest(),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_archive_file(path: Path, lines: list[bytes]) -> None:
    tmp = path.with_name(f".tmp-{path.name}-{os.getpid()}")
    with lzma.open(tmp, "wb", preset=6) as fh:
        for line in lines:
            fh.write(line)
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o444)
    _fsync_dir(path.parent)


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f".tmp-{path.name}-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _read_archive_lines(path: Path) -> list[bytes]:
    with lzma.open(path, "rb") as fh:
        return fh.read().splitlines(keepends=True)


def _day_paths(archive_dir: Path, day: str) -> tuple[Path, Path]:
    month_dir = archive_dir / day[:7]
    return month_dir, month_dir / f"journal_diag_{day}.manifest.json"


def verify_table_archive(month_dir: Path, entry: dict[str, Any]) -> None:
    path = month_dir / entry["file"]
    if not path.is_file():
        raise ArchiveIntegrityError(f"missing archive file {path}")
    if _sha256_file(path) != entry["file_sha256"]:
        raise ArchiveIntegrityError(f"file sha256 mismatch {path}")
    lines = _read_archive_lines(path)
    rows = [json.loads(line) for line in lines]
    check = _summary([tuple(r) for r in rows], lines, entry["columns"])
    for key in ("rows", "min_id", "max_id", "content_sha256", "per_event_type"):
        if check[key] != entry[key]:
            raise ArchiveIntegrityError(f"readback {key} mismatch {path}")


def verify_manifest(month_dir: Path, manifest: dict[str, Any]) -> None:
    for entry in manifest["tables"].values():
        if entry["rows"]:
            verify_table_archive(month_dir, entry)


def journal_in_use(journal_path: Path) -> bool:
    """True if any process holds the journal (or its -wal/-shm) open. Fails closed."""
    targets = [str(journal_path)] + [str(journal_path) + s for s in ("-wal", "-shm") if Path(str(journal_path) + s).exists()]
    try:
        proc = subprocess.run([LSOF, "-t", *targets], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return True
    pids = {p for p in proc.stdout.split() if p.strip()}
    if proc.returncode not in (0, 1) or (proc.returncode == 1 and proc.stderr.strip()):
        return True
    pids.discard(str(os.getpid()))
    return bool(pids)


def _backup_covers(backup_path: Optional[Path], table: str, max_id: int) -> bool:
    if backup_path is None or not Path(backup_path).is_file():
        return False
    try:
        con = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
        try:
            row = con.execute(f"SELECT MAX(id) FROM {table}").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return row is not None and row[0] is not None and int(row[0]) >= int(max_id)


def _archive_day(conn: sqlite3.Connection, archive_dir: Path, day: str, journal_path: Path, hot_days: int,
                 bounds: Optional[DayBounds] = None) -> dict[str, Any]:
    """Create (or re-verify) the archive for one day. Never deletes."""
    month_dir, manifest_path = _day_paths(archive_dir, day)
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        verify_manifest(month_dir, manifest)
        return manifest
    month_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "format_version": ARCHIVE_FORMAT_VERSION, "day_utc": day, "hot_retention_days": hot_days,
        "source_journal": str(journal_path), "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "archived", "tables": {},
    }
    for table in TABLES:
        columns = _columns(conn, table)
        rows = _select_rows(conn, table, day, bounds)
        lines = [_line(r) for r in rows]
        entry = {"columns": columns, "file": f"journal_diag_{day}_{table}.jsonl.xz", **_summary(rows, lines, columns)}
        if rows:
            path = month_dir / entry["file"]
            _write_archive_file(path, lines)
            entry["file_sha256"] = _sha256_file(path)
            entry["file_bytes"] = path.stat().st_size
            verify_table_archive(month_dir, entry)
        manifest["tables"][table] = entry
    _write_json_atomic(manifest_path, manifest)
    verify_manifest(month_dir, json.loads(manifest_path.read_text(encoding="utf-8")))
    return manifest


def _delete_day(conn: sqlite3.Connection, archive_dir: Path, day: str, manifest: dict[str, Any],
                backup_path: Optional[Path], bounds: Optional[DayBounds] = None) -> int:
    month_dir, manifest_path = _day_paths(archive_dir, day)
    verify_manifest(month_dir, manifest)  # archive must still verify right before deleting
    for table, entry in manifest["tables"].items():
        if entry["rows"] and not _backup_covers(backup_path, table, entry["max_id"]):
            raise RetentionRefused(f"backup does not contain {table} rows up to id {entry['max_id']}")
    deleted = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, entry in manifest["tables"].items():
            columns = _columns(conn, table)
            rows = _select_rows(conn, table, day, bounds)
            if not rows:
                continue  # already deleted (idempotent)
            live = _summary(rows, [_line(r) for r in rows], columns)
            if live["content_sha256"] != entry["content_sha256"] or live["rows"] != entry["rows"]:
                raise RetentionRefused(f"{table} {day}: live rows differ from verified archive")
            cur = conn.execute(
                f"DELETE FROM {table} WHERE id BETWEEN ? AND ? AND ts >= ? AND ts < ? "
                f"AND event_type IN ({_marks(table)})",
                (entry["min_id"], entry["max_id"], day, _next_day(day), *_types(table)),
            )
            if cur.rowcount != entry["rows"]:
                raise RetentionRefused(f"{table} {day}: deleted {cur.rowcount} != archived {entry['rows']}")
            deleted += cur.rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    manifest = {**manifest, "status": "deleted", "deleted_at_utc": datetime.now(timezone.utc).isoformat()}
    _write_json_atomic(manifest_path, manifest)
    return deleted


def mirror_archive(archive_dir: Path, mirror_dir: Optional[Path]) -> dict[str, Any]:
    """Copy verified archive files to an external disk if mounted; never deletes anything."""
    if mirror_dir is None:
        return {"status": "disabled", "copied": 0}
    mirror_dir = Path(mirror_dir)
    if not mirror_dir.is_dir():
        return {"status": "unavailable", "copied": 0}
    copied, errors = 0, []
    for local in sorted(Path(archive_dir).rglob("*")):
        if not local.is_file() or local.name.startswith(".tmp-") or local.name == VACUUM_MARKER:
            continue
        remote = mirror_dir / local.relative_to(archive_dir)
        try:
            local_sha = _sha256_file(local)
            if remote.is_file() and _sha256_file(remote) == local_sha:
                continue
            remote.parent.mkdir(parents=True, exist_ok=True)
            tmp = remote.with_name(f".tmp-{remote.name}-{os.getpid()}")
            _copy_file(local, tmp)
            if _sha256_file(tmp) != local_sha:
                tmp.unlink()
                raise OSError(f"mirror hash mismatch {remote}")
            if remote.exists():
                os.chmod(remote, 0o644)
            os.replace(tmp, remote)
            copied += 1
        except OSError as exc:
            errors.append(f"{local.name}: {exc}")
    return {"status": "failed" if errors else "ok", "copied": copied, "errors": errors[:20]}


def _copy_file(src: Path, dst: Path) -> None:
    shutil.copyfile(src, dst)
    with open(dst, "rb") as fh:
        os.fsync(fh.fileno())


def run_retention(*, journal_path: Path, archive_dir: Path, backup_path: Optional[Path], now: Optional[datetime] = None,
                  apply: bool = False, hot_days: int = HOT_RETENTION_DAYS, mirror_dir: Optional[Path] = None,
                  journal_busy_fn: Callable[[Path], bool] = journal_in_use) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    journal_path, archive_dir = Path(journal_path), Path(archive_dir)
    report: dict[str, Any] = {"status": "ok", "apply": apply, "cutoff_day": cutoff_day(now, hot_days),
                              "archived_days": [], "deleted_rows": 0, "errors": []}
    if not journal_path.is_file():
        report["status"] = "failed"
        report["errors"].append("journal missing")
        return report
    if journal_busy_fn(journal_path):
        report["status"] = "skipped_journal_busy"
        report["mirror"] = mirror_archive(archive_dir, mirror_dir)
        return report
    conn = sqlite3.connect(journal_path, isolation_level=None, timeout=5)
    try:
        bounds = day_id_bounds(conn, now=now, hot_days=hot_days)
        for day in sorted({d for per_table in bounds.values() for d in per_table}):
            try:
                manifest = _archive_day(conn, archive_dir, day, journal_path, hot_days, bounds)
                report["archived_days"].append(day)
                if apply:
                    report["deleted_rows"] += _delete_day(conn, archive_dir, day, manifest, backup_path, bounds)
            except (RetentionRefused, ArchiveIntegrityError, OSError, sqlite3.Error, ValueError) as exc:
                report["status"] = "failed"
                report["errors"].append(f"{day}: {type(exc).__name__}: {exc}")
    finally:
        conn.close()
    report["mirror"] = mirror_archive(archive_dir, mirror_dir)
    return report


def iter_archived_rows(archive_dir: Path, table: str, *, event_types: Optional[set[str]] = None,
                       start_day: Optional[str] = None, end_day: Optional[str] = None) -> Iterator[list[Any]]:
    """Yield archived rows (lists in column order) after verifying each file against its manifest."""
    for manifest_path in sorted(Path(archive_dir).rglob("journal_diag_*.manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        day = manifest["day_utc"]
        if (start_day and day < start_day) or (end_day and day > end_day):
            continue
        entry = manifest["tables"].get(table)
        if not entry or not entry["rows"]:
            continue
        verify_table_archive(manifest_path.parent, entry)
        idx = entry["columns"].index("event_type")
        for line in _read_archive_lines(manifest_path.parent / entry["file"]):
            row = json.loads(line)
            if event_types is None or row[idx] in event_types:
                yield row


def restore_to_sqlite(archive_dir: Path, out_db: Path, **filters: Any) -> dict[str, int]:
    """Materialize verified archive rows into a new SQLite file for research (ATTACH-able)."""
    out_db = Path(out_db)
    if out_db.exists():
        raise RetentionRefused(f"refusing to overwrite {out_db}")
    con = sqlite3.connect(out_db)
    counts: dict[str, int] = {}
    try:
        for table in TABLES:
            columns: Optional[list[str]] = None
            n = 0
            for manifest_path in sorted(Path(archive_dir).rglob("journal_diag_*.manifest.json")):
                entry = json.loads(manifest_path.read_text(encoding="utf-8"))["tables"].get(table)
                if entry and entry["rows"]:
                    columns = entry["columns"]
                    break
            if columns is None:
                continue
            con.execute(f"CREATE TABLE {table} ({', '.join(columns)})")
            marks = ",".join("?" * len(columns))
            for row in iter_archived_rows(archive_dir, table, **filters):
                con.execute(f"INSERT INTO {table} VALUES ({marks})", row)
                n += 1
            counts[table] = n
        con.commit()
    finally:
        con.close()
    return counts


def vacuum_once(*, journal_path: Path, archive_dir: Path, backup_path: Path,
                journal_busy_fn: Callable[[Path], bool] = journal_in_use, free_space_factor: float = 2.2) -> dict[str, Any]:
    """One-time operator VACUUM after the first successful archive+delete. Never scheduled."""
    journal_path, archive_dir = Path(journal_path), Path(archive_dir)
    marker = archive_dir / VACUUM_MARKER
    if marker.exists():
        raise RetentionRefused("one-time VACUUM already performed")
    manifests = sorted(archive_dir.rglob("journal_diag_*.manifest.json"))
    deleted = [json.loads(p.read_text(encoding="utf-8")) for p in manifests]
    if not any(m.get("status") == "deleted" for m in deleted):
        raise RetentionRefused("no successful archive+delete yet")
    for path, manifest in zip(manifests, deleted):
        verify_manifest(path.parent, manifest)
    if journal_busy_fn(journal_path):
        raise RetentionRefused("journal is open by another process")
    if not Path(backup_path).is_file():
        raise RetentionRefused("verified backup missing")
    bcon = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        if bcon.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RetentionRefused("backup quick_check failed")
    finally:
        bcon.close()
    before = journal_path.stat().st_size
    free = shutil.disk_usage(journal_path.parent).free
    if free < before * free_space_factor:
        raise RetentionRefused(f"insufficient free space {free} < {before * free_space_factor:.0f}")
    con = sqlite3.connect(journal_path, isolation_level=None)
    try:
        started = time.time()
        con.execute("VACUUM")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()
    result = {"bytes_before": before, "bytes_after": journal_path.stat().st_size, "integrity_check": integrity,
              "duration_sec": round(time.time() - started, 1), "at_utc": datetime.now(timezone.utc).isoformat()}
    if integrity != "ok":
        raise RetentionRefused(f"integrity_check after VACUUM: {integrity}")
    _write_json_atomic(marker, result)
    return result
