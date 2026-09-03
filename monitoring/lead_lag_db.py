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
    # SQLite treats NULL values as distinct in UNIQUE/PRIMARY KEY checks.
    # Cross-market references have no Outcome market id, so persist a stable
    # sentinel instead of NULL to make one-second upserts real.
    GLOBAL_MARKET_ID = -1
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
                CREATE TABLE IF NOT EXISTS reference_1s (
                    run_id TEXT NOT NULL, slug TEXT NOT NULL, market_id INTEGER,
                    bucket_epoch_ms INTEGER NOT NULL, source TEXT NOT NULL,
                    price_cents INTEGER NOT NULL, received_epoch_ns INTEGER NOT NULL,
                    PRIMARY KEY (run_id, slug, market_id, bucket_epoch_ms, source)
                );
                CREATE TABLE IF NOT EXISTS lead_lag_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    slug TEXT NOT NULL, market_id INTEGER, decision_epoch_ns INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lead_lag_markouts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    slug TEXT NOT NULL, market_id INTEGER, candidate_epoch_ns INTEGER NOT NULL,
                    horizon_ms INTEGER NOT NULL, observed_epoch_ns INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(run_id, slug, market_id, candidate_epoch_ns, horizon_ms)
                );
                CREATE TABLE IF NOT EXISTS latency_spans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    client_order_id TEXT NOT NULL, name TEXT NOT NULL,
                    started_monotonic_ns INTEGER NOT NULL, ended_monotonic_ns INTEGER NOT NULL,
                    elapsed_ns INTEGER NOT NULL, created_epoch_ns INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_lead_lag_latency_name ON latency_spans(name, created_epoch_ns);
                CREATE INDEX IF NOT EXISTS idx_lead_lag_markout_horizon_time
                  ON lead_lag_markouts(horizon_ms, candidate_epoch_ns);
                CREATE INDEX IF NOT EXISTS idx_lead_lag_decision_time
                  ON lead_lag_decisions(decision_epoch_ns);
            """)

    def enqueue_snapshot(self, *, run_id: str, polymarket_slug: str, hyperliquid_market_id: int | None, observed_ts: float, payload: dict[str, Any]) -> None:
        row = (str(run_id), str(polymarket_slug), hyperliquid_market_id, int(float(observed_ts) * 1000), dict(payload))
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            # Research loss must never add latency to execution. The next 5s
            # snapshot is still useful and the gap is visible in the report.
            return

    def enqueue_reference_1s(self, *, run_id: str, slug: str, market_id: int | None, bucket_epoch_ms: int, source: str, price_cents: int, received_epoch_ns: int) -> None:
        persisted_market_id = self.GLOBAL_MARKET_ID if market_id is None else int(market_id)
        self._enqueue_sql("reference", (str(run_id), str(slug), persisted_market_id, int(bucket_epoch_ms), str(source), int(price_cents), int(received_epoch_ns)))

    def enqueue_decision(self, *, run_id: str, slug: str, market_id: int | None, decision_epoch_ns: int, payload: dict[str, Any]) -> None:
        self._enqueue_sql("decision", (str(run_id), str(slug), market_id, int(decision_epoch_ns), dict(payload)))

    def enqueue_latency(self, *, run_id: str, client_order_id: str, name: str, started_monotonic_ns: int, ended_monotonic_ns: int, created_epoch_ns: int) -> None:
        self._enqueue_sql("latency", (str(run_id), str(client_order_id), str(name), int(started_monotonic_ns), int(ended_monotonic_ns), int(created_epoch_ns)))

    def enqueue_markout(self, *, run_id: str, slug: str, market_id: int | None, candidate_epoch_ns: int, horizon_ms: int, observed_epoch_ns: int, payload: dict[str, Any]) -> None:
        self._enqueue_sql("markout", (str(run_id), str(slug), market_id, int(candidate_epoch_ns), int(horizon_ms), int(observed_epoch_ns), dict(payload)))

    def _enqueue_sql(self, kind: str, row: tuple[Any, ...]) -> None:
        try:
            self._queue.put_nowait((kind, row))
        except queue.Full:
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
                    snapshots = [row for row in rows if isinstance(row, tuple) and len(row) == 5]
                    if snapshots:
                        conn.executemany(
                        "INSERT INTO snapshots (run_id, polymarket_slug, hyperliquid_market_id, observed_ts_ms, payload_json) VALUES (?, ?, ?, ?, ?)",
                        [(run_id, slug, market_id, ts_ms, json.dumps(payload, ensure_ascii=False)) for run_id, slug, market_id, ts_ms, payload in snapshots],
                        )
                    for kind, row in (item for item in rows if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str)):
                        if kind == "reference":
                            conn.execute("INSERT OR REPLACE INTO reference_1s VALUES (?, ?, ?, ?, ?, ?, ?)", row)
                        elif kind == "decision":
                            conn.execute("INSERT INTO lead_lag_decisions (run_id, slug, market_id, decision_epoch_ns, payload_json) VALUES (?, ?, ?, ?, ?)", (*row[:4], json.dumps(row[4], ensure_ascii=False)))
                        elif kind == "latency":
                            conn.execute("INSERT INTO latency_spans (run_id, client_order_id, name, started_monotonic_ns, ended_monotonic_ns, elapsed_ns, created_epoch_ns) VALUES (?, ?, ?, ?, ?, ?, ?)", (*row[:5], max(0, row[4] - row[3]), row[5]))
                        elif kind == "markout":
                            conn.execute("INSERT OR IGNORE INTO lead_lag_markouts (run_id, slug, market_id, candidate_epoch_ns, horizon_ms, observed_epoch_ns, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)", (*row[:6], json.dumps(row[6], ensure_ascii=False)))
                    conn.commit()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3.0)
