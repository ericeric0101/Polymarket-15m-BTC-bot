#!/usr/bin/env python3
"""Safe, manual-only retention preview; never alters trade_journal.db."""
from __future__ import annotations
import argparse
import sqlite3
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="logs/hyperliquid_lead_lag.db")
    parser.add_argument("--raw-retention-days", type=int, default=7)
    parser.add_argument("--apply", action="store_true", help="reserved; archival is intentionally not enabled")
    args = parser.parse_args()
    cutoff_ms = int((time.time() - max(2, args.raw_retention_days) * 86400) * 1000)
    with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as conn:
        old_rows = conn.execute("SELECT count(*) FROM snapshots WHERE observed_ts_ms < ?", (cutoff_ms,)).fetchone()[0]
        due_markouts = conn.execute("SELECT count(*) FROM lead_lag_decisions d WHERE d.decision_epoch_ns < ? AND NOT EXISTS (SELECT 1 FROM lead_lag_markouts m WHERE m.candidate_epoch_ns=d.decision_epoch_ns AND m.horizon_ms=60000)", (cutoff_ms * 1_000_000,)).fetchone()[0]
    print(f"eligible_raw_rows={old_rows} incomplete_60s_markouts={due_markouts} apply_requested={args.apply}")
    if args.apply:
        raise SystemExit("Refusing mutation: archive/export verification must be separately approved.")


if __name__ == "__main__":
    main()
