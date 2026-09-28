#!/usr/bin/env python3
"""Export compact, event-driven TWAP-forward research telemetry."""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
from collections import Counter
from pathlib import Path


EVENTS = {"TWAP_STRIKE_CROSS", "TWAP_PROJECTED_SIDE_CHANGE", "TMINUS_CHECKPOINT", "MARKET_TWAP_SUMMARY"}


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = sorted({key for row in rows for key in row}) or ["observed_ts"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/research/hyperliquid_lead_lag.db")
    parser.add_argument("--output-dir", default="reports/twap_forward")
    args = parser.parse_args(); db = Path(args.db)
    if not db.is_file() and args.db == "data/research/hyperliquid_lead_lag.db": db = Path("logs/hyperliquid_lead_lag.db")
    if not db.is_file(): parser.error(f"research DB not found: {args.db}")
    out = Path(args.output_dir); out.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        raw = conn.execute("SELECT decision_epoch_ns,payload_json FROM lead_lag_decisions ORDER BY id").fetchall()
    rows = []
    for ts, text in raw:
        payload = json.loads(text or "{}")
        if payload.get("event_type") in EVENTS:
            payload["observed_ts"] = int(ts) / 1_000_000_000
            rows.append(payload)
    summaries = [r for r in rows if r.get("event_type") == "MARKET_TWAP_SUMMARY"]
    checkpoints = [r for r in rows if r.get("event_type") == "TMINUS_CHECKPOINT"]
    crosses = [r for r in rows if r.get("event_type") == "TWAP_STRIKE_CROSS"]
    projection = [r for r in rows if r.get("event_type") == "TWAP_PROJECTED_SIDE_CHANGE"]
    write_csv(out / "market_twap_summary.csv", summaries)
    write_csv(out / "twap_checkpoints.csv", checkpoints)
    write_csv(out / "twap_cross_events.csv", crosses)
    write_csv(out / "twap_projection_events.csv", projection)
    # Empty first-generation placeholders are explicit rather than fabricated.
    for name in ("twap_vs_smart_money.csv", "twap_vs_polymarket_move.csv", "fast_spot_lead_lag.csv",
                 "projection_accuracy.csv", "stop_twap_forensics.csv", "twap_exit_urgency.csv"):
        write_csv(out / name, [])
    usage = shutil.disk_usage(db.parent)
    health = [{"research_db": str(db), "current_db_size_mb": round(db.stat().st_size / 1024 / 1024, 3),
               "disk_free_gb": round(usage.free / 1024 / 1024 / 1024, 3),
               "event_count": len(rows), "event_counts": json.dumps(Counter(r.get("event_type") for r in rows), sort_keys=True),
               "raw_retention_enabled": False}]
    write_csv(out / "storage_health.csv", health)
    write_csv(out / "data_quality.csv", [{"event_rows": len(rows), "summary_rows": len(summaries),
                                           "note": "Official TWAP only; reconstructed TWAP is not persisted in this first release."}])
    (out / "summary.md").write_text(
        "# TWAP forward research\n\n"
        "This is event-driven, shadow-only telemetry. Official current TWAP is Polymarket RTDS Chainlink 60s TWAP; "
        "the flat/trend settlement projections are estimates and have no live authority. "
        f"Captured events: {len(rows)}; summaries: {len(summaries)}.\n", encoding="utf-8")
    print(f"wrote {len(rows)} TWAP events to {out}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
