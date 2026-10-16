"""Verified research retention on final graceful process exit (RESEARCH_RETENTION_ON_EXIT).

Runs in a separate process started by the launcher only after the trading loop has
ended for good (operator Ctrl+C / SIGTERM); never on a node rollover. Sequence:

  1. graceful exit detected (caller)        6. split completed past days into C partitions
  2. writers closed (strategy teardown)     7. re-verify each partition: file hash, row count, content hash
  3. no process holds the DB (lsof)         8. re-verify the offsite (iCloud) A/B/P copies by hash
  4. WAL checkpoint of the research DB      9. delete duplicated live rows, one transaction per day
  5. dry-run plan, abort if unexpected     10. report reclaimed logical / physical bytes

Fail safe: any verification failure, unavailable cloud copy, another DB holder, a
timeout or an operator abort (second Ctrl+C) stops before deletion; a deletion
interrupted mid-transaction rolls back. The current UTC day is never touched. Every
step is idempotent, so a crash or SIGKILL just leaves work for the next graceful exit.
No compaction (VACUUM) is performed here.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bot.research import partitions, retention
from bot.research.daily_export import SUMMARY_SLACK_SEC, _day_bounds, _sha256
from bot.research.partitions import TABLE, _row_digest, deletion_eligibility, partition_paths, split_day

FUTURE_TOLERANCE_SEC = 3600


class RetentionAborted(RuntimeError):
    """Timeout or operator abort: stop now, preserve everything not yet committed."""


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, timeout=30)


def _utc_day(ts: float) -> date:
    return datetime.fromtimestamp(ts, timezone.utc).date()


def build_plan(live_db: Path, part_dir: Path, *, now: float) -> dict[str, Any]:
    """Dry run: which past days exist, which would be partitioned, which are already done."""
    today = _utc_day(now)
    conn = _ro(live_db)
    try:
        lo, hi = conn.execute(f"SELECT MIN(decision_epoch_ns), MAX(decision_epoch_ns) FROM {TABLE}").fetchone()
        days = []
        if lo is not None:
            day = _utc_day(lo / 1e9)
            while day <= _utc_day(hi / 1e9):
                start_ns, end_ns = partitions._day_ns(day)
                rows = conn.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE decision_epoch_ns >= ? AND decision_epoch_ns < ?",
                                    (start_ns, end_ns)).fetchone()[0]
                complete = now >= _day_bounds(day)[1] + SUMMARY_SLACK_SEC
                days.append({"date_utc": day.isoformat(), "live_rows": rows, "complete": complete,
                             "is_current_day": day >= today,
                             "partition_verified": partitions.partition_is_verified(part_dir, day)})
                day = date.fromordinal(day.toordinal() + 1)
    finally:
        conn.close()
    problems = []
    if hi is not None and hi / 1e9 > now + FUTURE_TOLERANCE_SEC:
        problems.append(f"rows timestamped in the future (max={hi / 1e9:.0f}, now={now:.0f})")
    candidates = [d for d in days if d["complete"] and not d["is_current_day"] and d["live_rows"] > 0]
    return {"today_utc": today.isoformat(), "days": days, "candidates": [d["date_utc"] for d in candidates],
            "would_partition": [d["date_utc"] for d in candidates if not d["partition_verified"]],
            "problems": problems}


def verify_partition_archive(part_dir: Path, day: date) -> list[str]:
    """Independent re-check of the C archive: file hash, row count, content hash, integrity."""
    target, manifest_path = partition_paths(part_dir, day)
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        return [f"manifest unreadable: {exc}"]
    if not manifest.get("verified"):
        return ["manifest not verified"]
    if not target.is_file():
        return ["partition file missing"]
    problems = []
    if _sha256(target) != manifest.get("file_sha256"):
        problems.append("partition file sha256 mismatch")
    try:
        conn = _ro(target)
        try:
            digest, count = hashlib.sha256(), 0
            for row in conn.execute(f"SELECT id, run_id, slug, market_id, decision_epoch_ns, payload_json FROM {TABLE} ORDER BY id"):
                _row_digest(digest, row)
                count += 1
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return problems + [f"partition unreadable: {exc}"]
    if count != manifest.get("rows"):
        problems.append(f"partition row count {count} != manifest {manifest.get('rows')}")
    if digest.hexdigest() != manifest.get("content_sha256"):
        problems.append("partition content sha256 mismatch")
    if integrity != "ok":
        problems.append(f"partition integrity_check {integrity}")
    return problems


def verify_offsite_copies(export_root: Path, offsite_root: Path | None, day: date) -> dict[str, Any]:
    """Re-hash each A/B/P offsite copy recorded for ``day``.

    ``unavailable``: the cloud folder is not configured/reachable (no delete at all).
    ``pending``: the day has no export/offsite copy yet (skip the day; the next exit retries).
    ``problems``: a recorded copy is missing or differs (verification failure: no delete at all).
    """
    result: dict[str, Any] = {"unavailable": None, "pending": [], "problems": []}
    if offsite_root is None:
        result["unavailable"] = "offsite not configured"
        return result
    if not offsite_root.is_dir():
        result["unavailable"] = f"offsite root unavailable: {offsite_root}"
        return result
    for tier in ("A", "B", "P"):
        try:
            manifest = json.loads((export_root / "manifests" / f"{tier}_{day.isoformat()}.json").read_text())
        except (OSError, ValueError):
            result["pending"].append(f"{tier} not exported yet")
            continue
        offsite = manifest.get("offsite")
        entries = [offsite] if tier == "A" and offsite else list((offsite or {}).values())
        if not entries:
            result["pending"].append(f"{tier} has no offsite copy yet")
        for entry in entries:
            path = Path(entry.get("path", ""))
            if not path.is_file():
                result["problems"].append(f"{tier} offsite copy missing: {path.name}")
            elif _sha256(path) != entry.get("sha256"):
                result["problems"].append(f"{tier} offsite copy hash mismatch: {path.name}")
    return result


def _db_bytes(live_db: Path) -> dict[str, int]:
    conn = _ro(live_db)
    try:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        conn.close()
    wal = Path(str(live_db) + "-wal")
    return {"file_bytes": live_db.stat().st_size, "wal_bytes": wal.stat().st_size if wal.exists() else 0,
            "freelist_bytes": page_size * free_pages}


def run_exit_retention(*, live_db: Path, part_dir: Path, export_root: Path, offsite_root: Path | None,
                       deadline: float, now: float | None = None,
                       should_abort: Callable[[], bool] = lambda: False,
                       in_use_check: Callable[[Path], None] = retention.assert_not_in_use,
                       log: Callable[[dict[str, Any]], None] = lambda record: None) -> dict[str, Any]:
    """Execute steps 2-10. Returns a report; never raises for expected refusals."""
    now = time.time() if now is None else now
    report: dict[str, Any] = {"status": "STARTED", "deleted_days": [], "deleted_rows": 0, "refusals": []}

    def step(name: str, **fields: Any) -> None:
        record = {"step": name, "at_utc": datetime.now(timezone.utc).isoformat(), **fields}
        report.setdefault("steps", []).append(record)
        log(record)

    def checkpoint(where: str) -> None:
        if should_abort():
            raise RetentionAborted(f"operator abort before {where}")
        if time.monotonic() >= deadline:
            raise RetentionAborted(f"timeout before {where}")

    def finish(status: str, reason: str | None = None) -> dict[str, Any]:
        report["status"] = status
        if reason:
            report["reason"] = reason
        step("finished", status=status, reason=reason)
        return report

    try:
        if not live_db.is_file():
            return finish("NOTHING_TO_DO", "research DB missing")
        step("writers_closed", detail="research and journal writers are stopped by strategy teardown before exit")
        checkpoint("lsof check")
        try:
            in_use_check(live_db)
        except retention.RetentionRefused as exc:
            return finish("REFUSED_DB_IN_USE", str(exc))
        step("no_other_db_holder")
        checkpoint("WAL checkpoint")
        conn = sqlite3.connect(live_db, timeout=5, isolation_level=None)
        try:
            busy, wal_pages, moved = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            conn.close()
        step("wal_checkpoint", busy=busy, wal_pages=wal_pages, checkpointed_pages=moved)
        if busy:
            return finish("REFUSED_WAL_BUSY", "WAL checkpoint could not complete (another connection)")
        bytes_before = _db_bytes(live_db)

        checkpoint("dry-run plan")
        plan = build_plan(live_db, part_dir, now=now)
        step("dry_run_plan", today_utc=plan["today_utc"], candidates=plan["candidates"],
             would_partition=plan["would_partition"], problems=plan["problems"],
             days=[{k: d[k] for k in ("date_utc", "live_rows", "partition_verified", "is_current_day")} for d in plan["days"]])
        report["plan"] = plan
        if plan["problems"]:
            return finish("ABORTED_UNEXPECTED_PLAN", "; ".join(plan["problems"]))
        if not plan["candidates"]:
            return finish("NOTHING_TO_DO", "no completed past day holds live rows")

        candidate_days = [date.fromisoformat(d) for d in plan["candidates"]]
        exported_days = []
        for day in candidate_days:
            checkpoint(f"export {day}")
            if day >= _utc_day(now):  # defence in depth: never the current day
                return finish("ABORTED_UNEXPECTED_PLAN", f"refusing current day {day}")
            _target, existing_manifest = partition_paths(part_dir, day)
            if existing_manifest.exists() and not partitions.partition_is_verified(part_dir, day):
                try:
                    trashed = json.loads(existing_manifest.read_text()).get("status") == "moved_to_trash"
                except (OSError, ValueError):
                    trashed = False
                if not trashed:
                    # Never silently rebuild an archive that no longer verifies: operator review first.
                    return finish("ABORTED_VERIFICATION_FAILED", f"{day}: existing partition fails verification")
                report["refusals"].append({"date_utc": day.isoformat(), "reason": "partition already expired to Trash"})
                continue
            try:
                manifest = split_day(live_db, day, part_dir, now=now)
            except (OSError, sqlite3.Error, ValueError) as exc:
                return finish("ABORTED_EXPORT_FAILED", f"{day}: {exc}")
            step("export_partition", date_utc=day.isoformat(), rows=manifest["rows"], verified=manifest["verified"],
                 problems=manifest["verification_problems"])
            if not manifest["verified"]:
                return finish("ABORTED_EXPORT_FAILED", f"{day}: {manifest['verification_problems']}")
            exported_days.append(day)

        deletable = []
        for day in exported_days:
            checkpoint(f"verify {day}")
            archive_problems = verify_partition_archive(part_dir, day)
            offsite = verify_offsite_copies(export_root, offsite_root, day)
            eligibility = deletion_eligibility(day, live_db=live_db, part_dir=part_dir, export_root=export_root, now=now)
            failing = [k for k, v in eligibility["checks"].items() if not v and k != "older_than_retention"]
            step("verify_day", date_utc=day.isoformat(), archive_problems=archive_problems, offsite=offsite,
                 eligible=eligibility["live_rows_deletion_eligible"], failing_checks=failing)
            if archive_problems:
                return finish("ABORTED_VERIFICATION_FAILED", f"{day}: {archive_problems}")
            if offsite["unavailable"]:
                return finish("ABORTED_CLOUD_UNAVAILABLE", offsite["unavailable"])
            if offsite["problems"]:
                return finish("ABORTED_VERIFICATION_FAILED", f"{day}: {offsite['problems']}")
            if offsite["pending"] or not eligibility["live_rows_deletion_eligible"]:
                report["refusals"].append({"date_utc": day.isoformat(), "reason": "exports pending; kept for a later exit",
                                           "pending": offsite["pending"], "failing_checks": failing})
                continue
            deletable.append(day)

        logical = 0
        for day in deletable:
            checkpoint(f"delete {day}")
            try:
                in_use_check(live_db)  # re-checked right before each deleting transaction
            except retention.RetentionRefused as exc:
                return finish("REFUSED_DB_IN_USE", str(exc))
            try:
                record = retention.delete_live_rows(live_db, day, part_dir=part_dir, export_root=export_root,
                                                    now=now, check_in_use=False)
            except retention.RetentionRefused as exc:
                return finish("ABORTED_VERIFICATION_FAILED", str(exc))
            manifest = json.loads(partition_paths(part_dir, day)[1].read_text())
            logical += int(manifest.get("payload_bytes") or 0)
            report["deleted_days"].append(day.isoformat())
            report["deleted_rows"] += record["rows"]
            step("delete_live_rows", date_utc=day.isoformat(), rows=record["rows"])

        bytes_after = _db_bytes(live_db)
        report["bytes"] = {"before": bytes_before, "after": bytes_after, "logical_payload_bytes_deleted": logical,
                           "physical_file_bytes_reclaimed": bytes_before["file_bytes"] - bytes_after["file_bytes"],
                           "reusable_freelist_bytes": bytes_after["freelist_bytes"],
                           "note": "no VACUUM: freed pages are reused by SQLite; the file shrinks only on manual compaction"}
        step("reclaimed_bytes", **report["bytes"])
        return finish("COMPLETED" if report["deleted_days"] else "NOTHING_DELETED")
    except (RetentionAborted, KeyboardInterrupt) as exc:
        return finish("ABORTED", str(exc) or type(exc).__name__)
