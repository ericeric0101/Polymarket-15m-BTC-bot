"""Standalone multi-source 1-second BTC price recorder (research only).

Runs as its own process (``scripts/price_recorder.py``), never inside the trading
bot, so a slow disk, a stuck socket or a crash here cannot touch trading I/O.

Storage design (see reports/spot_fallback_duty_cycle_20261015 discussion):
- one row per source per local-receipt second (last price, high/low, update count,
  last source and receipt clocks), not every tick;
- rows accumulate in memory and are written as one zstd Parquet file per UTC hour
  (``<root>/YYYY-MM-DD/HH.parquet``), so a day is ~24 files, not thousands;
- bounded in-memory buffer (drops are counted, never block the feeds);
- writes stop while free disk is below a floor; day directories older than the
  retention window are deleted.
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

SOURCES = ("chainlink_spot", "chainlink_twap60", "binance_btcusdt", "coinbase_btcusd")
_DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SCHEMA = None


def _schema():
    global _SCHEMA
    if _SCHEMA is None:
        import pyarrow as pa
        _SCHEMA = pa.schema([
            ("source", pa.string()),
            ("ts_sec", pa.int64()),            # local receipt second (UTC epoch)
            ("close", pa.float64()),           # last price received in the second
            ("high", pa.float64()),
            ("low", pa.float64()),
            ("n_updates", pa.int32()),
            ("last_source_ms", pa.int64()),    # feed's own clock for the close; null if absent
            ("last_received_ms", pa.int64()),  # local wall clock for the close
        ])
    return _SCHEMA


@dataclass
class _Bucket:
    ts_sec: int
    close: float
    high: float
    low: float
    n_updates: int
    last_source_ms: Optional[int]
    last_received_ms: int

    def row(self, source: str) -> dict[str, Any]:
        return {"source": source, "ts_sec": self.ts_sec, "close": self.close, "high": self.high,
                "low": self.low, "n_updates": self.n_updates, "last_source_ms": self.last_source_ms,
                "last_received_ms": self.last_received_ms}


class SecondBucketer:
    """Collapses ticks into one row per source per local-receipt second."""

    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self._emit = emit
        self._lock = threading.Lock()
        self._open: dict[str, _Bucket] = {}

    def observe(self, source: str, price: float, received_ts: float, source_ms: Optional[int]) -> None:
        if not (price > 0) or price != price:
            return
        sec = int(received_ts)
        received_ms = int(received_ts * 1000)
        finished = None
        with self._lock:
            bucket = self._open.get(source)
            if bucket is None or sec > bucket.ts_sec:
                if bucket is not None:
                    finished = bucket.row(source)
                self._open[source] = _Bucket(sec, price, price, price, 1, source_ms, received_ms)
            elif sec == bucket.ts_sec:
                bucket.close = price
                bucket.high = max(bucket.high, price)
                bucket.low = min(bucket.low, price)
                bucket.n_updates += 1
                bucket.last_source_ms = source_ms
                bucket.last_received_ms = received_ms
        if finished is not None:
            self._emit(finished)

    def flush_older_than(self, now_ts: float) -> None:
        """Close buckets whose second has passed (a quiet feed must not hold its last row)."""
        sec = int(now_ts)
        done = []
        with self._lock:
            for source, bucket in list(self._open.items()):
                if bucket.ts_sec < sec:
                    done.append(bucket.row(source))
                    del self._open[source]
        for row in done:
            self._emit(row)


def _hour_key(ts_sec: int) -> tuple[str, str]:
    dt = datetime.fromtimestamp(ts_sec, timezone.utc)
    return dt.date().isoformat(), f"{dt.hour:02d}"


def write_hour_file(root: Path, day: str, hour: str, rows: list[dict[str, Any]]) -> Path:
    """Atomically write one hour's rows; a second write in the same hour gets a suffix."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    directory = root / day
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{hour}.parquet"
    n = 1
    while final.exists():
        n += 1
        final = directory / f"{hour}_{n}.parquet"
    temp = directory / f".{final.name}.{uuid.uuid4().hex}.tmp"
    rows = sorted(rows, key=lambda r: (r["ts_sec"], r["source"]))
    try:
        pq.write_table(pa.Table.from_pylist(rows, schema=_schema()), temp, compression="zstd")
        os.replace(temp, final)
    finally:
        temp.unlink(missing_ok=True)
    return final


@dataclass
class RecorderStats:
    rows_buffered: int = 0
    rows_written: int = 0
    rows_dropped_buffer_full: int = 0
    rows_dropped_disk_low: int = 0
    files_written: int = 0
    write_errors: int = 0
    days_deleted: int = 0
    last_error: Optional[str] = None
    rows_by_source: dict[str, int] = field(default_factory=dict)


class HourlyParquetStore:
    """Bounded in-memory buffer flushed to one Parquet file per UTC hour."""

    def __init__(
        self,
        root: str | Path,
        *,
        retention_days: int = 30,
        min_free_disk_gb: float = 10.0,
        max_buffer_rows: int = 60_000,
        free_disk_provider: Callable[[Path], Any] = shutil.disk_usage,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self.retention_days = int(retention_days)
        self.min_free_disk_gb = float(min_free_disk_gb)
        self.max_buffer_rows = int(max_buffer_rows)
        self._free_disk = free_disk_provider
        self._clock = clock
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._buffered = 0
        self.stats = RecorderStats()

    def add(self, row: dict[str, Any]) -> None:
        with self._lock:
            if self._buffered >= self.max_buffer_rows:
                self.stats.rows_dropped_buffer_full += 1
                return
            self._rows.setdefault(_hour_key(int(row["ts_sec"])), []).append(row)
            self._buffered += 1
            self.stats.rows_buffered = self._buffered
            src = str(row["source"])
            self.stats.rows_by_source[src] = self.stats.rows_by_source.get(src, 0) + 1

    def disk_ok(self) -> bool:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            free_gb = int(self._free_disk(self.root).free) / (1024 ** 3)
        except Exception as exc:  # unknown disk state: do not write
            self.stats.last_error = f"disk_check_failed:{type(exc).__name__}"
            return False
        return free_gb >= self.min_free_disk_gb

    def flush(self, *, include_current_hour: bool = False) -> list[Path]:
        """Write completed hours (and the current one on shutdown)."""
        current = _hour_key(int(self._clock()))
        with self._lock:
            keys = [k for k in self._rows if include_current_hour or k < current]
            batches = {k: self._rows.pop(k) for k in keys}
            self._buffered -= sum(len(v) for v in batches.values())
            self.stats.rows_buffered = self._buffered
        if not batches:
            return []
        if not self.disk_ok():
            self.stats.rows_dropped_disk_low += sum(len(v) for v in batches.values())
            return []
        written = []
        for (day, hour), rows in sorted(batches.items()):
            try:
                written.append(write_hour_file(self.root, day, hour, rows))
                self.stats.rows_written += len(rows)
                self.stats.files_written += 1
            except Exception as exc:
                self.stats.write_errors += 1
                self.stats.last_error = f"write_failed:{type(exc).__name__}:{exc}"
        return written

    def enforce_retention(self) -> list[Path]:
        """Delete day directories older than the retention window (only YYYY-MM-DD dirs)."""
        if not self.root.exists():
            return []
        cutoff = (datetime.fromtimestamp(self._clock(), timezone.utc).date()
                  - timedelta(days=self.retention_days))
        removed = []
        for child in self.root.iterdir():
            if not child.is_dir() or not _DAY_DIR.match(child.name):
                continue
            try:
                day = datetime.strptime(child.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed.append(child)
        self.stats.days_deleted += len(removed)
        return removed


def parse_coinbase_ticker(payload: Any) -> Optional[tuple[float, Optional[int]]]:
    """(price, source_ms) from a Coinbase Exchange ``ticker`` message, else None."""
    if not isinstance(payload, dict) or payload.get("type") != "ticker":
        return None
    if str(payload.get("product_id", "")).upper() != "BTC-USD":
        return None
    try:
        price = float(payload.get("price"))
    except (TypeError, ValueError):
        return None
    if not price > 0:
        return None
    source_ms = None
    raw_time = payload.get("time")
    if isinstance(raw_time, str) and raw_time:
        try:
            source_ms = int(datetime.fromisoformat(raw_time.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            source_ms = None
    return price, source_ms
