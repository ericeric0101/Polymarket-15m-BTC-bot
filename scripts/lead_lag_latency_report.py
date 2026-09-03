#!/usr/bin/env python3
"""Read-only p50/p95/p99 latency report for the dedicated lead/lag DB."""
from __future__ import annotations
import argparse
import sqlite3


def percentile(values, point):
    if not values:
        return None
    return values[min(len(values) - 1, int((len(values) - 1) * point))] / 1_000_000


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="logs/hyperliquid_lead_lag.db")
    args = parser.parse_args()
    with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as conn:
        names = [row[0] for row in conn.execute("SELECT DISTINCT name FROM latency_spans")]
        for name in names:
            values = sorted(row[0] for row in conn.execute("SELECT elapsed_ns FROM latency_spans WHERE name=?", (name,)))
            print(f"{name}: count={len(values)} p50_ms={percentile(values,.50)} p95_ms={percentile(values,.95)} p99_ms={percentile(values,.99)}")


if __name__ == "__main__":
    main()
