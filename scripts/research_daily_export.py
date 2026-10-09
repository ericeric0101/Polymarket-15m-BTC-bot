#!/usr/bin/env python3
"""Export tier A (per-market summary) for completed UTC days.

Examples:
  python3 scripts/research_daily_export.py --date 2026-10-07
  python3 scripts/research_daily_export.py --pending        # every completed day missing a verified manifest

Offsite mirror: set RESEARCH_OFFSITE_DIR (e.g. a Google Drive / Dropbox synced folder) or pass --offsite.
The research DB is opened read-only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.research.daily_export import completed_days, day_is_exported, export_day  # noqa: E402
from bot.research.store import ResearchStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="data/research/twap_forward_shadow.db")
    parser.add_argument("--out", default="data/research_export")
    parser.add_argument("--offsite", default=os.getenv("RESEARCH_OFFSITE_DIR") or None)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--date", type=date.fromisoformat)
    group.add_argument("--pending", action="store_true")
    parser.add_argument("--allow-partial", action="store_true", help="export an incomplete day (never verified)")
    args = parser.parse_args()
    out_root = Path(args.out)
    offsite = Path(args.offsite).expanduser() if args.offsite else None
    store = ResearchStore(args.db)
    days = [args.date] if args.date else [d for d in completed_days(Path(args.db)) if not day_is_exported(out_root, d)]
    failures = 0
    for day in days:
        manifest = export_day(store, day, out_root, offsite_root=offsite, allow_partial=args.allow_partial)
        failures += not manifest["verified"]
        print(json.dumps({key: manifest[key] for key in ("date_utc", "rows", "markets_settled",
                                                         "markets_with_native_v2", "verified",
                                                         "verification_problems", "offsite")}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
