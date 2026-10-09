#!/usr/bin/env python3
"""Tier C maintenance: split completed days into verified partitions and report deletion eligibility.

  python3 scripts/research_partition.py split --date 2026-10-07
  python3 scripts/research_partition.py split --all-completed
  python3 scripts/research_partition.py eligibility --all-completed

No command deletes anything; eligibility is evidence for the operator.
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
from bot.research.daily_export import SUMMARY_SLACK_SEC, _day_bounds  # noqa: E402
from bot.research.partitions import deletion_eligibility, split_day  # noqa: E402


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("split", "eligibility"))
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
