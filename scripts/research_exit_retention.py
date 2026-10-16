#!/usr/bin/env python3
"""Graceful-exit research retention (child process started by the launcher; see bot/research/exit_retention.py).

SIGINT (a second Ctrl+C) or SIGTERM (launcher timeout) aborts immediately: an open
deleting transaction rolls back and source data is preserved. Holds the research
maintenance lock so it never overlaps the startup maintenance pass.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot.research.exit_retention import run_exit_retention  # noqa: E402

LOCK = ROOT / "data" / "research_partitions" / ".maintenance.lock"
_aborted = False


def _abort(signum, _frame):
    global _aborted
    _aborted = True
    raise KeyboardInterrupt(f"signal {signum}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=os.getenv("TWAP_RESEARCH_DB_PATH") or "data/research/twap_forward_shadow.db")
    parser.add_argument("--partitions", default="data/research_partitions")
    parser.add_argument("--export", default="data/research_export")
    parser.add_argument("--offsite", default=os.getenv("RESEARCH_OFFSITE_DIR") or None)
    parser.add_argument("--timeout-sec", type=float, required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGINT, _abort)
    signal.signal(signal.SIGTERM, _abort)
    deadline = time.monotonic() + args.timeout_sec

    def log(record):
        print(json.dumps(record, default=str), flush=True)

    part_dir = Path(args.partitions)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        with LOCK.open("w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                report = {"status": "REFUSED_MAINTENANCE_RUNNING"}
            else:
                report = run_exit_retention(
                    live_db=Path(args.db), part_dir=part_dir, export_root=Path(args.export),
                    offsite_root=Path(args.offsite).expanduser() if args.offsite else None,
                    deadline=deadline, should_abort=lambda: _aborted, log=log)
    except KeyboardInterrupt as exc:
        report = {"status": "ABORTED", "reason": f"interrupted: {exc}"}
    folder = part_dir / "retention_log"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    (folder / f"{stamp}_exit_retention.json").write_text(json.dumps(report, indent=1, default=str))
    log({"step": "summary", "status": report["status"], "reason": report.get("reason"),
         "deleted_days": report.get("deleted_days", []), "deleted_rows": report.get("deleted_rows", 0),
         "bytes": report.get("bytes")})
    return 0 if report["status"] in ("COMPLETED", "NOTHING_TO_DO", "NOTHING_DELETED") else 3


if __name__ == "__main__":
    raise SystemExit(main())
