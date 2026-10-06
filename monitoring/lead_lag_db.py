"""Asynchronous, dedicated persistence for cross-market lead/lag research."""
from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from bot.research.provenance import RESEARCH_SCHEMA_VERSION
from monitoring.research_writer_diagnostics import WriterFailureDiagnostics


class LeadLagDB:
    # SQLite treats NULL values as distinct in UNIQUE/PRIMARY KEY checks.
    # Cross-market references have no Outcome market id, so persist a stable
    # sentinel instead of NULL to make one-second upserts real.
    GLOBAL_MARKET_ID = -1
    def __init__(self, db_path: str = "data/research/hyperliquid_lead_lag.db") -> None:
        self.db_path = str(Path(db_path))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[tuple[str, str, int | None, int, dict[str, Any]]] = queue.Queue(maxsize=20_000)
        self._stop = threading.Event()
        self._health_lock = threading.Lock()
        self._queue_drops = 0
        self._write_errors = 0
        self._decision_rows_written = 0
        self._late_enqueue_rejections = 0
        self._writer_stage = "CONNECT"
        self._failure_diagnostics: WriterFailureDiagnostics | None = None
        self._init_schema()
        self._thread = threading.Thread(target=self._writer, daemon=True, name="lead-lag-db-writer")
        self._thread.start()

    def _connect(self) -> sqlite3.Connection:
        self._writer_stage = "CONNECT"
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        self._writer_stage = "PRAGMA"
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def set_failure_diagnostics(self, *, journal: Any, writer_name: str,
                                run_id: str, identity=None) -> None:
        """Bind the existing independent journal before producers enqueue work."""
        # Never recurse into this research DB, including path aliases.
        try:
            if journal is None:
                return
            target = Path(self.db_path).resolve()
            journal_target = Path(str(journal.db_path)).resolve()
            if (journal_target == target or
                    (journal_target.exists() and target.exists() and journal_target.samefile(target))):
                return
            self._failure_diagnostics = WriterFailureDiagnostics(
                journal=journal, writer_name=writer_name, run_id=run_id,
                db_target=Path(self.db_path).name, identity=identity,
            )
        except Exception:
            # Diagnostics must not turn an unavailable sink into startup failure.
            return

    def _record_failure(self, exc: Exception, rows: list, committed: bool) -> None:
        try:
            diagnostics = self._failure_diagnostics
            if diagnostics is not None:
                with self._health_lock:
                    errors, drops = self._write_errors, self._queue_drops
                diagnostics.record(exc=exc, stage=self._writer_stage, rows=rows,
                                   queue_depth=self._queue.qsize(), write_errors=errors,
                                   queue_drops=drops, commit_acknowledged=committed)
        except Exception:
            pass

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
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
        finally:
            conn.close()

    def enqueue_snapshot(self, *, run_id: str, polymarket_slug: str, hyperliquid_market_id: int | None, observed_ts: float, payload: dict[str, Any]) -> None:
        versioned_payload = {**dict(payload), "research_schema_version": RESEARCH_SCHEMA_VERSION}
        row = (str(run_id), str(polymarket_slug), hyperliquid_market_id, int(float(observed_ts) * 1000), versioned_payload)
        self._enqueue(row)

    def enqueue_reference_1s(self, *, run_id: str, slug: str, market_id: int | None, bucket_epoch_ms: int, source: str, price_cents: int, received_epoch_ns: int) -> None:
        persisted_market_id = self.GLOBAL_MARKET_ID if market_id is None else int(market_id)
        self._enqueue_sql("reference", (str(run_id), str(slug), persisted_market_id, int(bucket_epoch_ms), str(source), int(price_cents), int(received_epoch_ns)))

    def enqueue_decision(self, *, run_id: str, slug: str, market_id: int | None, decision_epoch_ns: int, payload: dict[str, Any]) -> bool:
        versioned_payload = {**dict(payload), "research_schema_version": RESEARCH_SCHEMA_VERSION}
        return self._enqueue(("decision", (str(run_id), str(slug), market_id, int(decision_epoch_ns), versioned_payload)))

    def research_health(self) -> dict[str, Any]:
        with self._health_lock:
            return {"queue_depth": self._queue.qsize(), "queue_capacity": self._queue.maxsize,
                    "queue_drops": self._queue_drops, "write_errors": self._write_errors,
                    "decision_rows_written": self._decision_rows_written,
                    "late_enqueue_rejections": self._late_enqueue_rejections,
                    "writer_failure_diagnostics": self._failure_diagnostics.health_snapshot()
                    if self._failure_diagnostics is not None else None}

    def enqueue_latency(self, *, run_id: str, client_order_id: str, name: str, started_monotonic_ns: int, ended_monotonic_ns: int, created_epoch_ns: int) -> None:
        self._enqueue_sql("latency", (str(run_id), str(client_order_id), str(name), int(started_monotonic_ns), int(ended_monotonic_ns), int(created_epoch_ns)))

    def enqueue_markout(self, *, run_id: str, slug: str, market_id: int | None, candidate_epoch_ns: int, horizon_ms: int, observed_epoch_ns: int, payload: dict[str, Any]) -> None:
        self._enqueue_sql("markout", (str(run_id), str(slug), market_id, int(candidate_epoch_ns), int(horizon_ms), int(observed_epoch_ns), dict(payload)))

    def _enqueue_sql(self, kind: str, row: tuple[Any, ...]) -> None:
        self._enqueue((kind, row))

    def _enqueue(self, row: tuple[Any, ...]) -> bool:
        with self._health_lock:
            if self._stop.is_set():
                self._late_enqueue_rejections += 1
                return False
            try:
                self._queue.put_nowait(row)
                return True
            except queue.Full:
                self._queue_drops += 1
                return False

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
            conn = None
            committed = False
            self._writer_stage = "CONNECT"
            try:
                conn = self._connect()
                snapshots = [row for row in rows if isinstance(row, tuple) and len(row) == 5]
                if snapshots:
                    self._writer_stage = "SERIALIZE"
                    parameters = [(run_id, slug, market_id, ts_ms, json.dumps(payload, ensure_ascii=False)) for run_id, slug, market_id, ts_ms, payload in snapshots]
                    self._writer_stage = "EXECUTEMANY"
                    conn.executemany(
                        "INSERT INTO snapshots (run_id, polymarket_slug, hyperliquid_market_id, observed_ts_ms, payload_json) VALUES (?, ?, ?, ?, ?)",
                        parameters,
                    )
                for kind, row in (item for item in rows if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str)):
                    self._writer_stage = "EXECUTE"
                    if kind == "reference":
                        conn.execute("INSERT OR REPLACE INTO reference_1s VALUES (?, ?, ?, ?, ?, ?, ?)", row)
                    elif kind == "decision":
                        self._writer_stage = "SERIALIZE"
                        serialized = json.dumps(row[4], ensure_ascii=False)
                        self._writer_stage = "EXECUTE"
                        conn.execute("INSERT INTO lead_lag_decisions (run_id, slug, market_id, decision_epoch_ns, payload_json) VALUES (?, ?, ?, ?, ?)", (*row[:4], serialized))
                    elif kind == "latency":
                        conn.execute("INSERT INTO latency_spans (run_id, client_order_id, name, started_monotonic_ns, ended_monotonic_ns, elapsed_ns, created_epoch_ns) VALUES (?, ?, ?, ?, ?, ?, ?)", (*row[:5], max(0, row[4] - row[3]), row[5]))
                    elif kind == "markout":
                        self._writer_stage = "SERIALIZE"
                        serialized = json.dumps(row[6], ensure_ascii=False)
                        self._writer_stage = "EXECUTE"
                        conn.execute("INSERT OR IGNORE INTO lead_lag_markouts (run_id, slug, market_id, candidate_epoch_ns, horizon_ms, observed_epoch_ns, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)", (*row[:6], serialized))
                self._writer_stage = "COMMIT"
                conn.commit()
                committed = True
                decision_count = sum(1 for item in rows if isinstance(item, tuple) and len(item) == 2 and item[0] == "decision")
                with self._health_lock:
                    self._decision_rows_written += decision_count
            except Exception as exc:
                with self._health_lock:
                    self._write_errors += 1
                self._record_failure(exc, rows, committed)
            finally:
                if conn is not None:
                    try:
                        self._writer_stage = "CLOSE"
                        conn.close()
                    except Exception as exc:
                        with self._health_lock:
                            self._write_errors += 1
                        self._record_failure(exc, rows, committed)

    def stop(self, *, timeout_sec: float = 3.0) -> bool:
        """Terminal stop: success requires drained work and no known data loss."""
        with self._health_lock:
            self._stop.set()
        self._thread.join(timeout=max(0.1, float(timeout_sec)))
        with self._health_lock:
            return (not self._thread.is_alive() and self._queue.empty()
                    and self._write_errors == 0 and self._queue_drops == 0)
