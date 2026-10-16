#!/usr/bin/env python3
"""Trade-journal diagnostic retention CLI (see monitoring/journal_retention.py).

Default is a dry run (archive + verify, no delete). ``--apply`` also deletes
verified diagnostic rows older than the 14-day hot window. ``vacuum-once`` is a
separate one-time operator step. ``restore`` materializes archived rows for
research. Never touches core events, today's rows, or rows inside the hot window.

Destructive modes (``run --apply``, ``vacuum-once``) hold the bot-stopped guard
for their whole run: the journal writer lock plus a free LIVE process lock.
``--exit-hook`` (used only by the launcher's final-exit hook, whose parent still
owns its LIVE lock) holds the writer lock alone. A held lock refuses with exit
code 4 and deletes nothing. The dry run and ``restore`` take no lock.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.journal_path import (  # noqa: E402
    JournalWriterGuard,
    MaintenanceLockError,
    require_bot_stopped,
    resolve_trade_db_path,
    resolved_runtime_env,
)
from monitoring import journal_retention as jr  # noqa: E402


def _paths(archive_dir: str | None) -> tuple[Path, Path, Path, Path | None]:
    journal = resolve_trade_db_path(repo_root=ROOT)
    backup = journal.parent.parent / "backups" / journal.name  # same default as TradeJournalDB
    archive = Path(archive_dir).expanduser() if archive_dir else ROOT / "data" / "journal_archive"
    mirror_raw = str(resolved_runtime_env(repo_root=ROOT).get("JOURNAL_ARCHIVE_MIRROR_DIR") or "").strip()
    mirror = Path(os.path.expanduser(mirror_raw)) if mirror_raw else None
    return journal, backup, archive, mirror


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--archive-dir", help="archive directory (default: data/journal_archive)")
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="cmd")
    run = sub.add_parser("run", parents=[common], help="archive + verify (dry run unless --apply)")
    run.add_argument("--apply", action="store_true")
    run.add_argument("--exit-hook", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("vacuum-once", parents=[common], help="one-time VACUUM after the first successful archive+delete")
    restore = sub.add_parser("restore", parents=[common], help="materialize archived rows into a new SQLite file")
    restore.add_argument("--out", required=True)
    restore.add_argument("--start-day")
    restore.add_argument("--end-day")
    args = parser.parse_args(argv)
    journal, backup, archive, mirror = _paths(args.archive_dir)
    apply = bool(getattr(args, "apply", False))
    destructive = args.cmd == "vacuum-once" or apply
    if not destructive:
        guard = nullcontext()
    elif getattr(args, "exit_hook", False):
        guard = JournalWriterGuard(journal)
    else:
        guard = require_bot_stopped(journal_path=journal, repo_root=ROOT)
    try:
        with guard:
            if args.cmd == "vacuum-once":
                result = jr.vacuum_once(journal_path=journal, archive_dir=archive, backup_path=backup)
            elif args.cmd == "restore":
                result = jr.restore_to_sqlite(archive, Path(args.out), start_day=args.start_day,
                                              end_day=args.end_day)
            else:
                result = jr.run_retention(journal_path=journal, archive_dir=archive, backup_path=backup,
                                          apply=apply, mirror_dir=mirror)
    except MaintenanceLockError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 4
    except jr.RetentionRefused as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}))
        return 3
    print(json.dumps(result, default=str))
    if isinstance(result, dict) and result.get("status") not in (None, "ok", "skipped_journal_busy"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
