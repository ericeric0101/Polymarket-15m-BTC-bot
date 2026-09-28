#!/usr/bin/env python3
"""Export compact, event-driven TWAP-forward research telemetry."""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
from collections import Counter
from pathlib import Path


EVENTS = {"TWAP_STRIKE_CROSS", "TWAP_PROJECTED_SIDE_CHANGE", "TMINUS_CHECKPOINT", "MARKET_TWAP_SUMMARY"}


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = sorted({key for row in rows for key in row}) or ["observed_ts"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/research/twap_forward_shadow.db")
    parser.add_argument("--output-dir", default="reports/twap_forward")
    args = parser.parse_args(); db = Path(args.db)
    # The separate DB is the live default.  Retain the old shared path only as
    # a read-only compatibility fallback for pre-separation historical reports.
    if not db.is_file() and args.db == "data/research/twap_forward_shadow.db":
        db = Path("data/research/hyperliquid_lead_lag.db")
    if not db.is_file() and args.db == "data/research/hyperliquid_lead_lag.db":
        db = Path("logs/hyperliquid_lead_lag.db")
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
    canonical_summaries = [r for r in summaries if r.get("settlement_reference_is_canonical") is True]
    proxy_summaries = [r for r in summaries if r not in canonical_summaries]
    # Accuracy needs observed outcome labels and is intentionally restricted to
    # canonical Chainlink-60s settlement references.  Do not turn a Binance or
    # generic external fallback into a primary accuracy observation.
    accuracy_rows = []
    for row in canonical_summaries:
        predicted = row.get("last_projected_side")
        settlement = row.get("settlement_side")
        if predicted in {"UP", "DOWN"} and settlement in {"UP", "DOWN"}:
            accuracy_rows.append({"market_slug": row.get("market_slug"), "predicted_side": predicted,
                                  "settlement_side": settlement, "correct": predicted == settlement,
                                  "settlement_reference_source": row.get("settlement_reference_source")})
    write_csv(out / "projection_accuracy.csv", accuracy_rows)
    # Empty first-generation placeholders are explicit rather than fabricated.
    for name in ("twap_vs_smart_money.csv", "twap_vs_polymarket_move.csv", "fast_spot_lead_lag.csv",
                 "stop_twap_forensics.csv", "twap_exit_urgency.csv"):
        write_csv(out / name, [])
    usage = shutil.disk_usage(db.parent)
    cap, min_free = float(os.getenv("TWAP_RESEARCH_MAX_DB_MB", "500")), float(os.getenv("TWAP_RESEARCH_MIN_FREE_DISK_GB", "10"))
    main_mb = db.stat().st_size / 1024 / 1024
    wal_path, shm_path = Path(f"{db}-wal"), Path(f"{db}-shm")
    wal_mb = wal_path.stat().st_size / 1024 / 1024 if wal_path.is_file() else 0.0
    shm_mb = shm_path.stat().st_size / 1024 / 1024 if shm_path.is_file() else 0.0
    db_size_mb, disk_free_gb = round(main_mb + wal_mb + shm_mb, 3), round(usage.free / 1024 / 1024 / 1024, 3)
    guard_rows = [r for r in rows if r.get("event_type") == "RESEARCH_STORAGE_GUARD_TRIGGERED"]
    cap_exceeded, free_low = db_size_mb >= cap, disk_free_gb < min_free
    effective_guard = bool(guard_rows) or cap_exceeded or free_low
    health = [{"research_db": str(db), "current_db_size_mb": db_size_mb, "db_size_mb": db_size_mb,
               "db_main_mb": round(main_mb, 3), "db_wal_mb": round(wal_mb, 3), "db_shm_mb": round(shm_mb, 3),
               "db_total_disk_mb": db_size_mb, "guard_scope": "TWAP_OPTIONAL_EVENTS_ONLY",
               "configured_db_cap_mb": cap, "db_cap_exceeded": cap_exceeded,
               "disk_free_gb": disk_free_gb, "configured_min_free_gb": min_free, "free_disk_low": free_low,
               "storage_guard_triggered": effective_guard, "guard_reason": guard_rows[-1].get("trigger_reason") if guard_rows else ("db_size_cap" if cap_exceeded else "free_disk_low" if free_low else ""),
               "optional_research_writes_enabled": not effective_guard,
               "event_count": len(rows), "event_counts": json.dumps(Counter(r.get("event_type") for r in rows), sort_keys=True),
               "raw_retention_enabled": False}]
    write_csv(out / "storage_health.csv", health)
    quality_note = "Official TWAP only; reconstructed TWAP is not persisted in this first release."
    if len(canonical_summaries) == 0:
        quality_note += " INSUFFICIENT_CANONICAL_SETTLEMENT_LABELS."
    write_csv(out / "data_quality.csv", [{"event_rows": len(rows), "summary_rows": len(summaries),
                                           "canonical_twap_settlements": len(canonical_summaries),
                                           "noncanonical_proxy_settlements": len(proxy_summaries), "note": quality_note}])
    (out / "summary.md").write_text(
        "# TWAP forward research\n\n"
        "This is event-driven, shadow-only telemetry. Official current TWAP is Polymarket RTDS Chainlink 60s TWAP; "
        "the flat/trend settlement projections are estimates and have no live authority. "
        f"Captured events: {len(rows)}; summaries: {len(summaries)}; canonical labels: {len(canonical_summaries)}; "
        f"noncanonical proxy labels: {len(proxy_summaries)}.\n", encoding="utf-8")
    print(f"wrote {len(rows)} TWAP events to {out}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
