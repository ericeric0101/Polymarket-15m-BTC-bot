#!/usr/bin/env python3
"""Manually archive expired high-frequency lead/lag rows without touching trade PnL."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path


RAW_PARTITIONS = {
    "snapshots": "observed_ts_ms / 1000.0",
    "reference_1s": "bucket_epoch_ms / 1000.0",
    "lead_lag_decisions": "decision_epoch_ns / 1000000000.0",
    "latency_spans": "created_epoch_ns / 1000000000.0",
}


def _init_manifest(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS lead_lag_archive_manifest (
             table_name TEXT NOT NULL, partition_day TEXT NOT NULL,
             archive_path TEXT NOT NULL, row_count INTEGER NOT NULL,
             sha256 TEXT NOT NULL, archived_epoch_ms INTEGER NOT NULL,
             PRIMARY KEY (table_name, partition_day)
           )"""
    )


def eligible_partitions(conn: sqlite3.Connection, *, cutoff_ts: float) -> list[tuple[str, str, int]]:
    """Return closed UTC-day raw partitions strictly older than the cutoff."""
    result: list[tuple[str, str, int]] = []
    for table, epoch_expr in RAW_PARTITIONS.items():
        try:
            rows = conn.execute(
                f"""SELECT date({epoch_expr}, 'unixepoch') AS day, count(*)
                      FROM {table} WHERE {epoch_expr} < ? GROUP BY day ORDER BY day""",
                (cutoff_ts,),
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        result.extend((table, str(day), int(count)) for day, count in rows if day)
    return result


def _archive_partition(conn: sqlite3.Connection, *, table: str, day: str, archive_dir: Path) -> tuple[Path, int, str]:
    epoch_expr = RAW_PARTITIONS[table]
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE date({epoch_expr}, 'unixepoch')=? ORDER BY rowid", (day,)
    )
    columns = [item[0] for item in rows.description]
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f"{table}-{day}.jsonl.gz"
    temporary = archive_dir / f".{table}-{day}.{os.getpid()}.tmp.gz"
    count = 0
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(zip(columns, row)), ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            count += 1
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    with gzip.open(temporary, "rt", encoding="utf-8") as handle:
        verified = sum(1 for _ in handle)
    if verified != count:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"archive verification failed for {table}/{day}: {verified} != {count}")
    os.replace(temporary, target)
    return target, count, digest


def apply_retention(db_path: str, archive_dir: str, raw_retention_days: int) -> list[tuple[str, str, int]]:
    """Export verified closed partitions, then delete only their source rows.

    It is operator-invoked, never accesses ``trade_journal.db``, retains
    lead/lag markouts, and deliberately never runs VACUUM.
    """
    cutoff = time.time() - max(2, raw_retention_days) * 86400
    exported: list[tuple[str, str, int]] = []
    with sqlite3.connect(db_path) as conn:
        _init_manifest(conn)
        for table, day, expected_count in eligible_partitions(conn, cutoff_ts=cutoff):
            exists = conn.execute(
                "SELECT 1 FROM lead_lag_archive_manifest WHERE table_name=? AND partition_day=?",
                (table, day),
            ).fetchone()
            if exists:
                continue
            target, count, digest = _archive_partition(
                conn, table=table, day=day, archive_dir=Path(archive_dir),
            )
            if count != expected_count:
                raise RuntimeError(f"row count changed while archiving {table}/{day}")
            epoch_expr = RAW_PARTITIONS[table]
            with conn:
                deleted = conn.execute(
                    f"DELETE FROM {table} WHERE date({epoch_expr}, 'unixepoch')=?", (day,)
                ).rowcount
                if deleted != count:
                    raise RuntimeError(f"delete verification failed for {table}/{day}: {deleted} != {count}")
                conn.execute(
                    """INSERT INTO lead_lag_archive_manifest
                       (table_name, partition_day, archive_path, row_count, sha256, archived_epoch_ms)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (table, day, str(target), count, digest, int(time.time() * 1000)),
                )
            exported.append((table, day, count))
    return exported


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="logs/hyperliquid_lead_lag.db")
    parser.add_argument("--archive-dir", default="logs/lead_lag_archive")
    parser.add_argument("--raw-retention-days", type=int, default=7)
    parser.add_argument("--apply", action="store_true", help="export verified partitions and then prune raw rows")
    args = parser.parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")
    cutoff = time.time() - max(2, args.raw_retention_days) * 86400
    with sqlite3.connect(db_path) as conn:
        _init_manifest(conn)
        rows = eligible_partitions(conn, cutoff_ts=cutoff)
    print("eligible_raw_partitions=" + json.dumps(rows))
    print("trade_journal_untouched=true markouts_retained=true vacuum_run=false")
    if args.apply:
        print("archived_and_pruned=" + json.dumps(apply_retention(str(db_path), args.archive_dir, args.raw_retention_days)))


if __name__ == "__main__":
    main()
