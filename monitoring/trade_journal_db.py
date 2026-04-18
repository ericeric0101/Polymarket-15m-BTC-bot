"""
SQLite trade journal for run_bot live/simulation diagnostics and analytics.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TradeJournalDB:
    """
    Lightweight SQLite writer.
    - Opens a short-lived connection per write (safe with multi-thread callbacks)
    - Never raises to strategy path; logs and continues
    """

    def __init__(self, db_path: str = "./logs/trade_journal.db") -> None:
        self.db_path = str(Path(db_path))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        ddl = """
        CREATE TABLE IF NOT EXISTS strategy_runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            mode TEXT NOT NULL,
            test_mode INTEGER NOT NULL,
            maker_mode INTEGER NOT NULL,
            instrument_id TEXT,
            selected_slug TEXT,
            notes_json TEXT
        );

        CREATE TABLE IF NOT EXISTS order_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            run_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            client_order_id TEXT,
            venue_order_id TEXT,
            side TEXT,
            price REAL,
            qty REAL,
            status TEXT,
            reason TEXT,
            instrument_id TEXT,
            token_id TEXT,
            fee_rate_bps INTEGER,
            expected_net_usdc REAL,
            commission_usdc REAL,
            payload_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_order_events_run_ts ON order_events(run_id, ts);
        CREATE INDEX IF NOT EXISTS idx_order_events_client ON order_events(client_order_id);

        CREATE TABLE IF NOT EXISTS strategy_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            run_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_strategy_events_run_ts ON strategy_events(run_id, ts);
        """
        try:
            with self._connect() as conn:
                conn.executescript(ddl)
                conn.commit()
        except Exception as e:
            logger.warning(f"TradeJournalDB schema init failed: {e}")

    def log_run_start(
        self,
        run_id: str,
        mode: str,
        test_mode: bool,
        maker_mode: bool,
        instrument_id: Optional[str] = None,
        selected_slug: Optional[str] = None,
        notes: Optional[Dict[str, Any]] = None,
    ) -> None:
        sql = """
        INSERT OR REPLACE INTO strategy_runs
        (run_id, started_at, ended_at, mode, test_mode, maker_mode, instrument_id, selected_slug, notes_json)
        VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    sql,
                    (
                        run_id,
                        _utc_now_iso(),
                        mode,
                        int(test_mode),
                        int(maker_mode),
                        instrument_id,
                        selected_slug,
                        json.dumps(notes or {}, ensure_ascii=False),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"TradeJournalDB log_run_start failed: {e}")

    def log_run_stop(self, run_id: str, notes: Optional[Dict[str, Any]] = None) -> None:
        select_sql = "SELECT notes_json FROM strategy_runs WHERE run_id=?"
        update_sql = "UPDATE strategy_runs SET ended_at=?, notes_json=? WHERE run_id=?"
        try:
            with self._connect() as conn:
                existing_notes: Dict[str, Any] = {}
                row = conn.execute(select_sql, (run_id,)).fetchone()
                if row and row[0]:
                    try:
                        parsed = json.loads(row[0])
                        if isinstance(parsed, dict):
                            existing_notes = parsed
                    except Exception:
                        existing_notes = {}
                merged_notes = {**existing_notes, **(notes or {})}
                conn.execute(
                    update_sql,
                    (_utc_now_iso(), json.dumps(merged_notes, ensure_ascii=False), run_id),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"TradeJournalDB log_run_stop failed: {e}")

    def log_strategy_event(self, run_id: str, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        sql = "INSERT INTO strategy_events (ts, run_id, event_type, payload_json) VALUES (?, ?, ?, ?)"
        try:
            with self._connect() as conn:
                conn.execute(
                    sql,
                    (
                        _utc_now_iso(),
                        run_id,
                        event_type,
                        json.dumps(payload or {}, ensure_ascii=False),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"TradeJournalDB log_strategy_event failed: {e}")

    def log_order_event(
        self,
        run_id: str,
        event_type: str,
        client_order_id: Optional[str] = None,
        venue_order_id: Optional[str] = None,
        side: Optional[str] = None,
        price: Optional[float] = None,
        qty: Optional[float] = None,
        status: Optional[str] = None,
        reason: Optional[str] = None,
        instrument_id: Optional[str] = None,
        token_id: Optional[str] = None,
        fee_rate_bps: Optional[int] = None,
        expected_net_usdc: Optional[float] = None,
        commission_usdc: Optional[float] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        sql = """
        INSERT INTO order_events (
            ts, run_id, event_type, client_order_id, venue_order_id, side, price, qty, status, reason,
            instrument_id, token_id, fee_rate_bps, expected_net_usdc, commission_usdc, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    sql,
                    (
                        _utc_now_iso(),
                        run_id,
                        event_type,
                        client_order_id,
                        venue_order_id,
                        side,
                        price,
                        qty,
                        status,
                        reason,
                        instrument_id,
                        token_id,
                        fee_rate_bps,
                        expected_net_usdc,
                        commission_usdc,
                        json.dumps(payload or {}, ensure_ascii=False),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"TradeJournalDB log_order_event failed: {e}")

    def load_latest_locked_strike(self, slug: str) -> Optional[Dict[str, Any]]:
        if not slug:
            return None
        sql = """
        SELECT ts, payload_json
        FROM strategy_events
        WHERE event_type = 'MARKET_STRIKE_LOCKED'
        ORDER BY id DESC
        LIMIT 200
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(sql).fetchall()
            for ts, payload_json in rows:
                try:
                    payload = json.loads(payload_json or "{}")
                except Exception:
                    continue
                if str(payload.get("slug") or "") != str(slug):
                    continue
                strike = payload.get("strike")
                source = str(payload.get("strike_source") or "")
                if strike is None or not source:
                    continue
                strike_dec = Decimal(str(strike))
                if strike_dec <= 0:
                    continue
                return {
                    "ts": str(ts or ""),
                    "slug": str(slug),
                    "strike": strike_dec,
                    "strike_source": source,
                    "authoritative": bool(payload.get("authoritative", False)),
                    "sample_dt_sec": payload.get("sample_dt_sec"),
                }
        except Exception as e:
            logger.debug(f"TradeJournalDB load_latest_locked_strike failed: {e}")
        return None
