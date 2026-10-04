"""Observer-only Binance 1-second history collector with bounded async storage."""
from __future__ import annotations

import math
import os
import queue
import shutil
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger


_PARQUET_SCHEMA = None


def _schema():
    global _PARQUET_SCHEMA
    if _PARQUET_SCHEMA is None:
        import pyarrow as pa
        _PARQUET_SCHEMA = pa.schema([
            ("ts_sec", pa.int64()),
            ("open", pa.float64()), ("high", pa.float64()),
            ("low", pa.float64()), ("close", pa.float64()),
            ("volume", pa.float64()), ("trade_count", pa.int64()),
            ("vwap", pa.float64()),
            ("first_source_ts", pa.int64()), ("last_source_ts", pa.int64()),
            ("first_received_ts", pa.int64()), ("last_received_ts", pa.int64()),
        ])
    return _PARQUET_SCHEMA


@dataclass
class _Bar:
    ts_sec: int
    open: float
    high: float
    low: float
    close: float
    first_source_ts: int
    last_source_ts: int
    first_received_ts: int
    last_received_ts: int
    volume: float | None
    trade_count: int | None
    _quote_volume: float | None
    _volume_known: bool
    _trade_count_known: bool

    @classmethod
    def first(cls, ts_sec: int, price: float, source_ms: int, received_ms: int,
              quantity: float | None, is_aggtrade: bool) -> "_Bar":
        return cls(ts_sec, price, price, price, price, source_ms, source_ms,
                   received_ms, received_ms, quantity, 1 if is_aggtrade else None,
                   price * quantity if quantity is not None else None,
                   quantity is not None, is_aggtrade)

    def add(self, price: float, source_ms: int, received_ms: int,
            quantity: float | None, is_aggtrade: bool) -> None:
        if source_ms < self.first_source_ts:
            self.open = price
            self.first_source_ts = source_ms
            self.first_received_ts = received_ms
        if source_ms >= self.last_source_ts:
            self.close = price
            self.last_source_ts = source_ms
            self.last_received_ts = received_ms
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        if quantity is None:
            self._volume_known = False
        else:
            if self._volume_known:
                self.volume = float(self.volume or 0.0) + quantity
                self._quote_volume = float(self._quote_volume or 0.0) + price * quantity
        if is_aggtrade and self._trade_count_known:
            self.trade_count = int(self.trade_count or 0) + 1
        elif not is_aggtrade:
            self._trade_count_known = False
        if not self._volume_known:
            self.volume = None
            self._quote_volume = None
        if not self._trade_count_known:
            self.trade_count = None

    def row(self) -> dict[str, Any]:
        vwap = (self._quote_volume / self.volume
                if self.volume is not None and self.volume > 0 and self._quote_volume is not None else None)
        return {"ts_sec": self.ts_sec, "open": self.open, "high": self.high,
                "low": self.low, "close": self.close, "volume": self.volume,
                "trade_count": self.trade_count, "vwap": vwap,
                "first_source_ts": self.first_source_ts, "last_source_ts": self.last_source_ts,
                "first_received_ts": self.first_received_ts, "last_received_ts": self.last_received_ts}


def atomic_write_parquet_part(directory: Path, rows: list[dict[str, Any]]) -> Path:
    """Write a new immutable part file; existing history is never overwritten."""
    if not rows:
        raise ValueError("cannot write empty Parquet part")
    import pyarrow as pa
    import pyarrow.parquet as pq

    directory.mkdir(parents=True, exist_ok=True)
    day = datetime.fromtimestamp(int(rows[0]["ts_sec"]), timezone.utc).date().isoformat()
    used = []
    for existing in directory.glob(f"BTCUSDT_1s_{day}_part-*.parquet"):
        try:
            used.append(int(existing.stem.rsplit("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    part = max(used, default=0) + 1
    final_path = directory / f"BTCUSDT_1s_{day}_part-{part:05d}.parquet"
    temp_path = directory / f".{final_path.name}.{uuid.uuid4().hex}.tmp"
    table = pa.Table.from_pylist(rows, schema=_schema())
    try:
        pq.write_table(table, temp_path, compression="zstd", use_dictionary=False)
        with temp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Atomic rename remains the primary guarantee on platforms that do
            # not allow fsync on directory descriptors.
            pass
        return final_path
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def load_btc_1s_history(start_ts: int | float, end_ts: int | float,
                        data_dir: str | Path = "data/btc_history_1s") -> list[dict[str, Any]]:
    """Load sorted rows in [start_ts, end_ts), deduped by ts_sec.

    When duplicate seconds occur across restart chunks, prefer the row with the
    greatest last_source_ts; ties deterministically prefer the later part path.
    """
    import pyarrow.parquet as pq

    root = Path(data_dir)
    start, end = int(math.floor(float(start_ts))), int(math.ceil(float(end_ts)))
    selected: dict[int, tuple[int, str, dict[str, Any]]] = {}
    for path in sorted(root.glob("BTCUSDT_1s_*.parquet")):
        try:
            table = pq.read_table(path, columns=[field.name for field in _schema()])
            for row in table.to_pylist():
                ts = int(row["ts_sec"])
                if ts < start or ts >= end:
                    continue
                rank = (int(row.get("last_source_ts") or 0), str(path))
                if ts not in selected or rank >= selected[ts][:2]:
                    selected[ts] = (rank[0], rank[1], row)
        except Exception as exc:
            logger.warning(f"BTC 1s history part skipped path={path.name} error={exc}")
    return [selected[ts][2] for ts in sorted(selected)]


class BTC1sHistoryCollector:
    """Small callback-side aggregation; all filesystem I/O runs on a worker."""

    def __init__(self, data_dir: str | Path = "data/btc_history_1s", *,
                 min_free_disk_gb: float = 5.0, queue_rows: int = 300,
                 batch_rows: int = 300, metrics_interval_sec: float = 60.0,
                 free_disk_provider=shutil.disk_usage):
        self.data_dir = Path(data_dir)
        self.min_free_disk_gb = max(0.0, float(min_free_disk_gb))
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max(1, int(queue_rows)))
        self.batch_rows = max(1, int(batch_rows))
        self.metrics_interval_sec = max(1.0, float(metrics_interval_sec))
        self._free_disk_provider = free_disk_provider
        self._lock = threading.Lock()
        self._bars: dict[int, _Bar] = {}
        self._watermark_sec: int | None = None
        self._latencies_ms: deque[float] = deque(maxlen=600)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._enabled = True
        self._failure_reason = ""
        self._bars_completed = 0
        self._bars_written = 0
        self._dropped_history_rows = 0
        self._last_source_ts = 0
        self._last_metrics_at = time.monotonic()
        self._last_disk_check_at = 0.0
        self._written_paths: list[Path] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def dropped_history_rows(self) -> int:
        return self._dropped_history_rows

    def start(self) -> None:
        if self._stop.is_set():
            return
        if self._thread is not None and self._thread.is_alive():
            return
        try:
            import pyarrow  # noqa: F401
        except Exception as exc:
            self._disable(f"parquet_dependency_unavailable:{type(exc).__name__}")
            return
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self._disable(f"history_directory_unavailable:{type(exc).__name__}:{exc}")
            return
        self._thread = threading.Thread(target=self._writer_loop, name="btc-1s-history-writer", daemon=True)
        self._thread.start()

    def observe_aggtrade(self, *, price: Any, source_ts_ms: int | None,
                         received_ts: float, quantity: Any = None) -> None:
        """Called passively by existing Binance aggTrade callback; never performs I/O."""
        if not self._enabled or self._stop.is_set():
            return
        try:
            px = float(price)
            src_ms = int(source_ts_ms) if source_ts_ms is not None else 0
            recv_ms = int(float(received_ts) * 1000)
            qty = float(quantity) if quantity is not None else None
            if not math.isfinite(px) or px <= 0 or src_ms <= 0 or (qty is not None and (not math.isfinite(qty) or qty < 0)):
                return
            ts_sec = src_ms // 1000
            with self._lock:
                if not self._enabled or self._stop.is_set():
                    return
                if self._watermark_sec is not None and ts_sec < self._watermark_sec - 2:
                    self._dropped_history_rows += 1
                    return
                self._watermark_sec = max(ts_sec, self._watermark_sec or ts_sec)
                self._last_source_ts = max(src_ms, self._last_source_ts)
                self._latencies_ms.append(float(recv_ms - src_ms))
                bar = self._bars.get(ts_sec)
                if bar is None:
                    self._bars[ts_sec] = _Bar.first(ts_sec, px, src_ms, recv_ms, qty, True)
                else:
                    bar.add(px, src_ms, recv_ms, qty, True)
                due = sorted(sec for sec in self._bars if sec < self._watermark_sec - 1)
                for sec in due:
                    completed = self._bars.pop(sec).row()
                    self._enqueue_nonblocking(completed)
        except Exception as exc:
            # Collector errors must never propagate into the Binance/live path.
            self._disable(f"observer_error:{type(exc).__name__}:{exc}")

    def _enqueue_nonblocking(self, row: dict[str, Any]) -> None:
        self._bars_completed += 1
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            self._dropped_history_rows += 1

    def _disk_ok(self) -> bool:
        now = time.monotonic()
        if now - self._last_disk_check_at < 60.0:
            return self._enabled
        self._last_disk_check_at = now
        try:
            free_bytes = int(self._free_disk_provider(self.data_dir).free)
            free_gb = free_bytes / (1024 ** 3)
            if free_gb < self.min_free_disk_gb:
                self._disable(f"free_disk_below_minimum:{free_gb:.2f}GB")
                return False
            return True
        except Exception as exc:
            self._disable(f"disk_check_failed:{type(exc).__name__}")
            return False

    def _writer_loop(self) -> None:
        batch: list[dict[str, Any]] = []
        last_write = time.monotonic()
        while not self._stop.is_set() or not self._queue.empty():
            try:
                row = self._queue.get(timeout=1.0)
                batch.append(row)
                while len(batch) < self.batch_rows:
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
            except queue.Empty:
                pass
            should_flush = batch and (len(batch) >= self.batch_rows or
                                      time.monotonic() - last_write >= 30.0 or self._stop.is_set())
            if should_flush:
                if not self._disk_ok():
                    batch.clear()
                    last_write = time.monotonic()
                    continue
                try:
                    by_day: dict[str, list[dict[str, Any]]] = {}
                    for row in batch:
                        day = datetime.fromtimestamp(row["ts_sec"], timezone.utc).date().isoformat()
                        by_day.setdefault(day, []).append(row)
                    for day_rows in by_day.values():
                        path = atomic_write_parquet_part(self.data_dir, day_rows)
                        self._written_paths.append(path)
                        self._bars_written += len(day_rows)
                    batch.clear()
                    last_write = time.monotonic()
                except Exception as exc:
                    self._disable(f"parquet_write_failed:{type(exc).__name__}:{exc}")
                    batch.clear()
            if time.monotonic() - self._last_metrics_at >= self.metrics_interval_sec:
                self._emit_metrics()
                self._last_metrics_at = time.monotonic()
        if batch and self._enabled and self._disk_ok():
            try:
                by_day: dict[str, list[dict[str, Any]]] = {}
                for row in batch:
                    day = datetime.fromtimestamp(row["ts_sec"], timezone.utc).date().isoformat()
                    by_day.setdefault(day, []).append(row)
                for day_rows in by_day.values():
                    path = atomic_write_parquet_part(self.data_dir, day_rows)
                    self._written_paths.append(path)
                    self._bars_written += len(day_rows)
            except Exception as exc:
                self._disable(f"parquet_write_failed:{type(exc).__name__}:{exc}")
        self._emit_metrics()

    def _disable(self, reason: str) -> None:
        if self._enabled:
            self._enabled = False
            self._failure_reason = str(reason)
            logger.warning(f"BTC 1s history collector disabled; live feed continues: reason={reason}")

    def _emit_metrics(self) -> None:
        latencies = sorted(self._latencies_ms)
        def percentile(p: float) -> float | None:
            if not latencies:
                return None
            return latencies[min(len(latencies) - 1, int((len(latencies) - 1) * p))]
        age = max(0.0, time.time() - self._last_source_ts / 1000) if self._last_source_ts else None
        logger.info(
            "BTC1S history: bars_completed={} buffered={} written={} dropped={} "
            "last_source_age={} source_receive_p50_ms={} source_receive_p95_ms={} writer_queue_depth={} enabled={}",
            self._bars_completed, len(self._bars), self._bars_written, self._dropped_history_rows,
            f"{age:.2f}" if age is not None else "unknown",
            f"{percentile(.50):.1f}" if percentile(.50) is not None else "unknown",
            f"{percentile(.95):.1f}" if percentile(.95) is not None else "unknown",
            self._queue.qsize(), self._enabled,
        )

    def stop(self, *, timeout_sec: float = 5.0) -> bool:
        with self._lock:
            if not self._stop.is_set():
                # Observer acceptance and final bar flush share this lock.
                self._stop.set()
                for sec in sorted(self._bars):
                    self._enqueue_nonblocking(self._bars[sec].row())
                self._bars.clear()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.1, float(timeout_sec)))
        return ((thread is None or not thread.is_alive()) and self._queue.empty()
                and not self._bars and not self._failure_reason
                and self._dropped_history_rows == 0
                and self._bars_written == self._bars_completed)
