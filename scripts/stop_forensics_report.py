#!/usr/bin/env python3
"""Export research-only stop-forensics events from the async research journal."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/research/hyperliquid_lead_lag.db")
    parser.add_argument("--output-dir", default="reports/stop_forensics")
    args = parser.parse_args(); outdir = Path(args.output_dir); outdir.mkdir(parents=True, exist_ok=True)
    db_path = Path(args.db)
    if not db_path.is_file() and args.db == "data/research/hyperliquid_lead_lag.db" and Path("logs/hyperliquid_lead_lag.db").is_file():
        db_path = Path("logs/hyperliquid_lead_lag.db")
    if not db_path.is_file():
        parser.error(f"research database not found: {args.db}")
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT decision_epoch_ns, payload_json FROM lead_lag_decisions ORDER BY id").fetchall()
    checkpoints, candidates, smart = [], [], []
    for epoch_ns, raw in rows:
        payload = json.loads(raw or "{}")
        kind = payload.get("event_type")
        payload["observed_ts"] = int(epoch_ns) / 1_000_000_000
        if kind == "STOP_SHADOW_CHECKPOINT": checkpoints.append(payload)
        elif kind == "STOP_SHADOW_CANDIDATE": candidates.append(payload)
        elif kind == "POST_ENTRY_SMART_MONEY_SNAPSHOT": smart.append(payload)
    for name, items in (("stop_shadow_checkpoints.csv", checkpoints), ("stop_candidate_comparison.csv", candidates), ("post_entry_smart_money.csv", smart)):
        keys = sorted({key for item in items for key in item}) or ["observed_ts"]
        with (outdir / name).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=keys, extrasaction="ignore", lineterminator="\n")
            writer.writeheader(); writer.writerows(items)
    # The 10s file intentionally retains evidence availability rather than
    # presenting settlement-only outcomes as executable replay results.
    with (outdir / "stop_persistence_10s_review.csv").open("w", newline="", encoding="utf-8") as handle:
        rows10 = [item for item in candidates if item.get("candidate") == "STOP_SHADOW_P10"]
        keys = sorted({key for item in rows10 for key in item}) or ["observed_ts"]
        writer = csv.DictWriter(handle, fieldnames=keys, extrasaction="ignore", lineterminator="\n"); writer.writeheader(); writer.writerows(rows10)
    print(f"events: checkpoints={len(checkpoints)} candidates={len(candidates)} smart={len(smart)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
