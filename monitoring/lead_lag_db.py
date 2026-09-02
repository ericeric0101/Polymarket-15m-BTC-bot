"""Asynchronous, dedicated persistence for cross-market lead/lag research."""
from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class LeadLagDB:
    def __init__(self, db_path: str = "logs/hyperliquid_lead_lag.db") -> None:
        self.db_path = str(Path(db_path))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[tuple[str, str, int | None, int, dict[str, Any]]] = queue.Queue(maxsize=20_000)
        self._stop = threading.Event()
        self._init_schema()
        self._thread = threading.Thread(target=self._writer, daemon=True, name="lead-lag-db-writer")
        self._thread.start()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    polymarket_slug TEXT NOT NULL,
                    hyperliquid_market_id INTEGER,
                    observed_ts_ms INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_lead_lag_scope_time
                  ON snapshots(run_id, polymarket_slug, hyperliquid_market_id, observed_ts_ms);
            """)

    def enqueue_snapshot(self, *, run_id: str, polymarket_slug: str, hyperliquid_market_id: int | None, observed_ts: float, payload: dict[str, Any]) -> None:
        row = (str(run_id), str(polymarket_slug), hyperliquid_market_id, int(float(observed_ts) * 1000), dict(payload))
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            # Research loss must never add latency to execution. The next 5s
            # snapshot is still useful and the gap is visible in the report.
            return

    def _writer(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            rows = []
            try:
                rows.append(self._queue.get(timeout=0.5))
            except queue.Empty:
                continue
            while len(rows) < 100:
                try:
                    rows.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                with self._connect() as conn:
                    conn.executemany(
                        "INSERT INTO snapshots (run_id, polymarket_slug, hyperliquid_market_id, observed_ts_ms, payload_json) VALUES (?, ?, ?, ?, ?)",
                        [(run_id, slug, market_id, ts_ms, json.dumps(payload, ensure_ascii=False)) for run_id, slug, market_id, ts_ms, payload in rows],
                    )
                    conn.commit()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3.0)
