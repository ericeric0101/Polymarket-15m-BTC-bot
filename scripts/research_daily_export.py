#!/usr/bin/env python3
"""Export tiers A (per-market summary), B (entry decisions) and P (per-second paths) for completed UTC days.

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
from bot.research import decision_export, path_export  # noqa: E402
from bot.research.daily_export import completed_days, day_is_exported, export_day  # noqa: E402
from bot.research.store import ResearchStore  # noqa: E402
from bot.runtime_env import load_runtime_env  # noqa: E402
from bot.journal_path import resolve_trade_db_path  # noqa: E402


def main() -> int:
    load_runtime_env()  # profile + .env (shell values win); provides RESEARCH_OFFSITE_DIR
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="data/research/twap_forward_shadow.db")
    parser.add_argument("--journal", default=None, help="trade journal (default: canonical TRADE_DB_PATH)")
    parser.add_argument("--out", default="data/research_export")
    parser.add_argument("--offsite", default=os.getenv("RESEARCH_OFFSITE_DIR") or None)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--date", type=date.fromisoformat)
    group.add_argument("--pending", action="store_true")
    parser.add_argument("--allow-partial", action="store_true", help="export an incomplete day (never verified)")
    args = parser.parse_args()
    args.journal = str(resolve_trade_db_path(args.journal))
    out_root = Path(args.out)
    offsite = Path(args.offsite).expanduser() if args.offsite else None
    store = ResearchStore(args.db)
    days = [args.date] if args.date else [
        d for d in completed_days(Path(args.db))
        if not (day_is_exported(out_root, d) and decision_export.day_is_exported(out_root, d)
                and path_export.day_is_exported(out_root, d))]
    failures = 0
    for day in days:
        a = export_day(store, day, out_root, offsite_root=offsite, allow_partial=args.allow_partial)
        b = decision_export.export_day(Path(args.journal), day, out_root, offsite_root=offsite,
                                       allow_partial=args.allow_partial)
        p = path_export.export_day(store, day, out_root, offsite_root=offsite, allow_partial=args.allow_partial)
        failures += (not a["verified"]) + (not b["verified"]) + (not p["verified"])
        print(json.dumps({"date_utc": a["date_utc"],
                          "A": {k: a[k] for k in ("rows", "markets_settled", "markets_with_native_v2",
                                                  "verified", "verification_problems")},
                          "A_offsite_verified": (a["offsite"] or {}).get("verified"),
                          "B": {name: meta["rows"] for name, meta in b["files"].items()},
                          "B_verified": b["verified"], "B_problems": b["verification_problems"],
                          "B_offsite_verified": (all(m["verified"] for m in b["offsite"].values())
                                                 if b["offsite"] else None),
                          "P": {name: meta["rows"] for name, meta in p["files"].items()},
                          "P_verified": p["verified"], "P_problems": p["verification_problems"],
                          "P_offsite_verified": (all(m["verified"] for m in p["offsite"].values())
                                                 if p["offsite"] else None)}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
