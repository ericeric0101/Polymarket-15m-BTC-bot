#!/usr/bin/env python3
"""Trade-journal diagnostic retention CLI (see monitoring/journal_retention.py).

Default is a dry run (archive + verify, no delete). ``--apply`` also deletes
verified diagnostic rows older than the 14-day hot window. ``vacuum-once`` is a
separate one-time operator step. ``restore`` materializes archived rows for
research. Never touches core events, today's rows, or rows inside the hot window.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.runtime_env import load_runtime_env  # noqa: E402
from monitoring import journal_retention as jr  # noqa: E402


def _paths() -> tuple[Path, Path, Path, Path | None]:
    load_runtime_env(repo_root=ROOT)
    journal = Path(os.getenv("TRADE_DB_PATH", "./data/trading/trade_journal.db"))
    journal = journal if journal.is_absolute() else (ROOT / journal).resolve()
    backup = journal.parent.parent / "backups" / journal.name  # same default as TradeJournal
    archive = ROOT / "data" / "journal_archive"
    mirror_raw = os.getenv("JOURNAL_ARCHIVE_MIRROR_DIR", "").strip()
    mirror = Path(os.path.expanduser(mirror_raw)) if mirror_raw else None
    return journal, backup, archive, mirror


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="archive + verify (dry run unless --apply)")
    run.add_argument("--apply", action="store_true")
    sub.add_parser("vacuum-once", help="one-time VACUUM after the first successful archive+delete")
    restore = sub.add_parser("restore", help="materialize archived rows into a new SQLite file")
    restore.add_argument("--out", required=True)
    restore.add_argument("--start-day")
    restore.add_argument("--end-day")
    parser.add_argument("--apply", action="store_true", help=argparse.SUPPRESS)  # `journal_retention.py --apply`
    args = parser.parse_args(argv)
    journal, backup, archive, mirror = _paths()
    try:
        if args.cmd == "vacuum-once":
            result = jr.vacuum_once(journal_path=journal, archive_dir=archive, backup_path=backup)
        elif args.cmd == "restore":
            result = jr.restore_to_sqlite(archive, Path(args.out), start_day=args.start_day, end_day=args.end_day)
        else:
            result = jr.run_retention(journal_path=journal, archive_dir=archive, backup_path=backup,
                                      apply=bool(getattr(args, "apply", False)), mirror_dir=mirror)
    except jr.RetentionRefused as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}))
        return 3
    print(json.dumps(result, default=str))
    if isinstance(result, dict) and result.get("status") not in (None, "ok", "skipped_journal_busy"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
