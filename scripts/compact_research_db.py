#!/usr/bin/env python3
"""Report research DB size; VACUUM is opt-in and never a runtime operation."""
from __future__ import annotations
import argparse
import sqlite3
import sys
from contextlib import nullcontext
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.journal_path import MaintenanceLockError, require_bot_stopped  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/research/hyperliquid_lead_lag.db")
    parser.add_argument("--vacuum", action="store_true", help="explicitly VACUUM after reporting")
    args = parser.parse_args(); path = Path(args.db)
    if not path.is_file() and args.db == "data/research/hyperliquid_lead_lag.db": path = Path("logs/hyperliquid_lead_lag.db")
    if not path.is_file(): parser.error(f"research DB not found: {args.db}")
    try:
        with require_bot_stopped() if args.vacuum else nullcontext():
            return _report(path, vacuum=args.vacuum)
    except MaintenanceLockError as exc:
        print(f"REFUSED: {exc}. Stop the bot before VACUUM.", file=sys.stderr)
        return 4


def _report(path: Path, *, vacuum: bool) -> int:
    with sqlite3.connect(path) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for table in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{table}: {count}")
        print(f"size_mb_before={path.stat().st_size / 1024 / 1024:.3f}")
        if vacuum:
            conn.execute("VACUUM")
    print(f"size_mb_after={path.stat().st_size / 1024 / 1024:.3f}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
