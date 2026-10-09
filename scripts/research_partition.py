#!/usr/bin/env python3
"""Tier C maintenance: split completed days into verified partitions and report deletion eligibility.

  python3 scripts/research_partition.py split --date 2026-10-07
  python3 scripts/research_partition.py split --all-completed
  python3 scripts/research_partition.py eligibility --all-completed
  python3 scripts/research_partition.py retention --all-completed            # plan only
  python3 scripts/research_partition.py retention --all-completed --apply    # bot must be stopped

retention --apply: delete live-DB rows of eligible partitioned days (re-hashed in the
deleting transaction), VACUUM once, then move expired partitions to the macOS Trash.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contextlib import nullcontext  # noqa: E402

from bot.journal_path import MaintenanceLockError, require_bot_stopped  # noqa: E402
from bot.research.daily_export import SUMMARY_SLACK_SEC, _day_bounds  # noqa: E402
from bot.research.partitions import deletion_eligibility, split_day  # noqa: E402
from bot.research.retention import RetentionRefused, assert_not_in_use, delete_live_rows, trash_partition, vacuum  # noqa: E402


def _days_in_live_db(live_db: Path) -> list[date]:
    conn = sqlite3.connect(f"file:{live_db.resolve()}?mode=ro", uri=True, timeout=30)
    try:
        lo, hi = conn.execute("SELECT MIN(decision_epoch_ns), MAX(decision_epoch_ns) FROM lead_lag_decisions").fetchone()
    finally:
        conn.close()
    if lo is None:
        return []
    first = datetime.fromtimestamp(lo / 1e9, timezone.utc).date()
    last = datetime.fromtimestamp(hi / 1e9, timezone.utc).date()
    now = time.time()
    days, day = [], first
    while day <= last:
        if now >= _day_bounds(day)[1] + SUMMARY_SLACK_SEC:
            days.append(day)
        day = date.fromordinal(day.toordinal() + 1)
    return days


def _days_in_partitions(part_dir: Path) -> list[date]:
    days = []
    for manifest in sorted((part_dir / "manifests").glob("C_*.json")):
        try:
            if json.loads(manifest.read_text()).get("status") != "moved_to_trash":
                days.append(date.fromisoformat(manifest.stem[2:]))
        except (OSError, ValueError):
            continue
    return days


def _retention(days, live, part_dir, export_root, *, apply: bool) -> int:
    plan = [deletion_eligibility(day, live_db=live, part_dir=part_dir, export_root=export_root) for day in days]
    live_days = [p["date_utc"] for p in plan if p["live_rows_deletion_eligible"]]
    trash_days = [p["date_utc"] for p in plan if p["partition_deletion_eligible"]]
    print(json.dumps({"plan": {"delete_live_rows": live_days, "trash_partitions": trash_days}, "apply": apply}))
    if not apply:
        return 0
    try:
        assert_not_in_use(live)
    except RetentionRefused as exc:
        print(json.dumps({"refused": str(exc)}))
        return 2
    deleted = 0
    for day in map(date.fromisoformat, live_days):
        try:
            record = delete_live_rows(live, day, part_dir=part_dir, export_root=export_root)
            deleted += record["rows"]
            print(json.dumps({k: record[k] for k in ("action", "date_utc", "rows")}))
        except RetentionRefused as exc:
            print(json.dumps({"refused": str(exc)}))
    if deleted:
        print(json.dumps({k: v for k, v in vacuum(live, part_dir=part_dir).items()}))
    for day in map(date.fromisoformat, trash_days):
        try:
            record = trash_partition(day, live_db=live, part_dir=part_dir, export_root=export_root)
            print(json.dumps({k: record[k] for k in ("action", "date_utc", "trash_path", "file_bytes")}))
        except RetentionRefused as exc:
            print(json.dumps({"refused": str(exc)}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("split", "eligibility", "retention"))
    parser.add_argument("--apply", action="store_true", help="retention: perform actions (default: plan only)")
    parser.add_argument("--db", default="data/research/twap_forward_shadow.db")
    parser.add_argument("--partitions", default="data/research_partitions")
    parser.add_argument("--export", default="data/research_export")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--date", type=date.fromisoformat)
    group.add_argument("--all-completed", action="store_true")
    args = parser.parse_args()
    live, part_dir, export_root = Path(args.db), Path(args.partitions), Path(args.export)
    days = [args.date] if args.date else _days_in_live_db(live)
    failures = 0
    if args.command == "retention":
        if not args.date:  # live rows may already be gone; partitions still need expiry
            days = sorted(set(days) | set(_days_in_partitions(part_dir)))
        try:
            with require_bot_stopped() if args.apply else nullcontext():
                return _retention(days, live, part_dir, export_root, apply=args.apply)
        except MaintenanceLockError as exc:
            print(f"REFUSED: {exc}. Stop the bot before retention --apply.", file=sys.stderr)
            return 4
    for day in days:
        if args.command == "split":
            manifest = split_day(live, day, part_dir)
            failures += not manifest["verified"]
            print(json.dumps({k: manifest[k] for k in ("date_utc", "rows", "payload_bytes", "file_bytes",
                                                       "verified", "verification_problems")}))
        else:
            print(json.dumps(deletion_eligibility(day, live_db=live, part_dir=part_dir, export_root=export_root)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
