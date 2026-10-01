#!/usr/bin/env python3
"""Validate daily coverage and latency of offline BTC 1-second Parquet parts."""
from __future__ import annotations

import argparse
import csv
import math
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.btc_1s_history import load_btc_1s_history


def validate_day(day: date, data_dir: Path) -> dict:
    start = int(datetime.combine(day, time.min, tzinfo=timezone.utc).timestamp())
    end = start + 86400
    pattern = f"BTCUSDT_1s_{day.isoformat()}_part-*.parquet"
    import pyarrow.parquet as pq
    raw_count = 0
    for path in sorted(data_dir.glob(pattern)):
        try:
            raw_count += pq.read_metadata(path).num_rows
        except Exception:
            continue
    rows = load_btc_1s_history(start, end, data_dir)
    timestamps = [int(row["ts_sec"]) for row in rows]
    gaps = [timestamps[idx] - timestamps[idx - 1] - 1 for idx in range(1, len(timestamps))
            if timestamps[idx] - timestamps[idx - 1] > 1]
    latencies = sorted(float(row["last_received_ts"] - row["last_source_ts"])
                       for row in rows if row.get("last_received_ts") is not None and row.get("last_source_ts") is not None)

    def percentile(values: list[float], p: float) -> float | None:
        if not values:
            return None
        rank = (len(values) - 1) * p
        low, high = math.floor(rank), math.ceil(rank)
        if low == high:
            return values[low]
        return values[low] * (high - rank) + values[high] * (rank - low)

    return {
        "date_utc": day.isoformat(), "row_count": len(rows), "raw_part_rows": raw_count,
        "expected_seconds": 86400, "coverage_pct": len(rows) / 86400 * 100,
        "missing_seconds": max(0, 86400 - len(rows)), "duplicate_seconds": max(0, raw_count - len(rows)),
        "first_ts": timestamps[0] if timestamps else None, "last_ts": timestamps[-1] if timestamps else None,
        "gap_count": len(gaps), "largest_gap_sec": max(gaps, default=0),
        "median_source_receive_latency_ms": percentile(latencies, .5),
        "p95_source_receive_latency_ms": percentile(latencies, .95),
        "latency_semantics": "last_received_ts - last_source_ts per bar; approximate event latency",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="UTC day as YYYY-MM-DD")
    parser.add_argument("--data-dir", type=Path, default=Path("data/btc_history_1s"))
    args = parser.parse_args()
    result = validate_day(date.fromisoformat(args.date), args.data_dir)
    writer = csv.DictWriter(sys.stdout, fieldnames=list(result), lineterminator="\n")
    writer.writeheader()
    writer.writerow(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
