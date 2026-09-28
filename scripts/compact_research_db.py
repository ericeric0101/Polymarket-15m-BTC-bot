#!/usr/bin/env python3
"""Report research DB size; VACUUM is opt-in and never a runtime operation."""
from __future__ import annotations
import argparse
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/research/hyperliquid_lead_lag.db")
    parser.add_argument("--vacuum", action="store_true", help="explicitly VACUUM after reporting")
    args = parser.parse_args(); path = Path(args.db)
    if not path.is_file() and args.db == "data/research/hyperliquid_lead_lag.db": path = Path("logs/hyperliquid_lead_lag.db")
    if not path.is_file(): parser.error(f"research DB not found: {args.db}")
    with sqlite3.connect(path) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for table in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{table}: {count}")
        print(f"size_mb_before={path.stat().st_size / 1024 / 1024:.3f}")
        if args.vacuum:
            conn.execute("VACUUM")
    print(f"size_mb_after={path.stat().st_size / 1024 / 1024:.3f}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
