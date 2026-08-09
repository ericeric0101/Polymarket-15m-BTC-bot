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

    def load_market_guard_counts(self, slug: str) -> Dict[str, int]:
        """Recover per-market risk limits after a process or node restart."""
        slug = str(slug or "")
        if not slug:
            return {"buy_count": 0, "protective_exit_count": 0}
        try:
            with self._connect() as conn:
                buy_row = conn.execute(
                    """
                    SELECT COUNT(DISTINCT client_order_id)
                    FROM order_events
                    WHERE event_type='ORDER_FILLED'
                      AND side='BUY'
                      AND json_extract(payload_json, '$.slug')=?
                    """,
                    (slug,),
                ).fetchone()
                exit_row = conn.execute(
                    """
                    SELECT COUNT(DISTINCT fill.client_order_id)
                    FROM order_events AS fill
                    JOIN order_events AS submit
                      ON submit.client_order_id=fill.client_order_id
                    WHERE fill.event_type='ORDER_FILLED'
                      AND fill.side='SELL'
                      AND submit.event_type='ORDER_TAKER_EXIT_SUBMIT'
                      AND submit.reason IN ('stop_loss', 'invalidation_recovery', 'offside_near_close')
                      AND json_extract(fill.payload_json, '$.slug')=?
                    """,
                    (slug,),
                ).fetchone()
            return {
                "buy_count": int(buy_row[0] or 0),
                "protective_exit_count": int(exit_row[0] or 0),
            }
        except Exception as e:
            logger.debug(f"TradeJournalDB load_market_guard_counts failed: {e}")
            return {"buy_count": 0, "protective_exit_count": 0}

    def reconcile_redeem_cycle(self, slug: str, redeem_value_usdc: float) -> Optional[Dict[str, float]]:
        """Rebuild missing cycle PnL when redemption happens after a restart."""
        slug = str(slug or "")
        if not slug:
            return None
        try:
            with self._connect() as conn:
                existing = conn.execute(
                    """
                    SELECT 1 FROM strategy_events
                    WHERE event_type='MARKET_CYCLE_PNL'
                      AND json_extract(payload_json, '$.slug')=?
                    LIMIT 1
                    """,
                    (slug,),
                ).fetchone()
                if existing:
                    return None
                row = conn.execute(
                    """
                    SELECT
                      COALESCE(SUM(CASE WHEN side='BUY' THEN price * qty ELSE 0 END), 0.0) AS buy_cost,
                      COALESCE(SUM(CASE WHEN side='SELL' THEN price * qty -
                        COALESCE(CAST(json_extract(payload_json, '$.effective_fee_usdc') AS REAL), 0.0)
                      ELSE 0 END), 0.0) AS sell_proceeds
                    FROM order_events
                    WHERE event_type='ORDER_FILLED'
                      AND json_extract(payload_json, '$.slug')=?
                    """,
                    (slug,),
                ).fetchone()
            buy_cost = float(row[0] or 0.0)
            sell_proceeds = float(row[1] or 0.0)
            if buy_cost <= 0 and sell_proceeds <= 0:
                return None
            redeem_value = max(0.0, float(redeem_value_usdc or 0.0))
            return {
                "buy_cost_usdc": buy_cost,
                "sell_proceeds_usdc": sell_proceeds,
                "redeem_value_usdc": redeem_value,
                "cycle_combined_pnl_usdc": redeem_value + sell_proceeds - buy_cost,
            }
        except Exception as e:
            logger.debug(f"TradeJournalDB reconcile_redeem_cycle failed: {e}")
            return None

    def load_shadow_simulation(self, slug: str) -> Optional[Dict[str, Any]]:
        """Return the latest lifecycle state for a dry-run shadow simulation."""
        slug = str(slug or "")
        if not slug:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT payload_json
                    FROM order_events
                    WHERE event_type IN (
                      'SHADOW_SIM_ENTRY_CANDIDATE',
                      'SHADOW_SIM_ENTRY_FILLED',
                      'SHADOW_SIM_ENTRY_EXPIRED',
                      'SHADOW_SIM_SETTLED'
                    )
                      AND json_extract(payload_json, '$.slug')=?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (slug,),
                ).fetchone()
            if not row or not row[0]:
                return None
            payload = json.loads(row[0])
            return payload if isinstance(payload, dict) else None
        except Exception as e:
            logger.debug(f"TradeJournalDB load_shadow_simulation failed: {e}")
            return None

    def load_fair_edge_bucket_shadow_simulations(self, slug: str) -> list[Dict[str, Any]]:
        """Return the latest persisted state for each fair-edge research candidate."""
        slug = str(slug or "")
        if not slug:
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT payload_json
                    FROM order_events
                    WHERE event_type IN (
                      'FAIR_EDGE_BUCKET_SHADOW_CANDIDATE',
                      'FAIR_EDGE_BUCKET_SHADOW_FILLED',
                      'FAIR_EDGE_BUCKET_SHADOW_EXPIRED',
                      'FAIR_EDGE_BUCKET_SHADOW_SETTLED'
                    )
                      AND json_extract(payload_json, '$.slug')=?
                    ORDER BY id ASC
                    """,
                    (slug,),
                ).fetchall()
            states: Dict[str, Dict[str, Any]] = {}
            for (raw_payload,) in rows:
                payload = json.loads(raw_payload or "{}")
                if not isinstance(payload, dict):
                    continue
                simulation_id = str(payload.get("simulation_id") or "")
                if simulation_id:
                    states[simulation_id] = payload
            return list(states.values())
        except Exception as e:
            logger.debug(f"TradeJournalDB load_fair_edge_bucket_shadow_simulations failed: {e}")
            return []

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

    def load_recent_buy_submits(self, instrument_id: str, limit: int = 20) -> list[Dict[str, Any]]:
        if not instrument_id:
            return []
        sql = """
        SELECT ts, client_order_id, price, qty, payload_json
        FROM order_events
        WHERE event_type = 'ORDER_SUBMIT'
          AND UPPER(COALESCE(side, '')) = 'BUY'
          AND (
              instrument_id = ?
              OR json_extract(payload_json, '$.submitted_instrument_id') = ?
              OR json_extract(payload_json, '$.instrument_id') = ?
          )
        ORDER BY id DESC
        LIMIT ?
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, (instrument_id, instrument_id, instrument_id, int(limit))).fetchall()
        except Exception as e:
            logger.debug(f"TradeJournalDB load_recent_buy_submits failed: {e}")
            return []

        out: list[Dict[str, Any]] = []
        for ts, client_order_id, price, qty, payload_json in rows:
            payload: Dict[str, Any] = {}
            try:
                parsed = json.loads(payload_json or "{}")
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                payload = {}
            epoch_ts = 0.0
            try:
                epoch_ts = datetime.fromisoformat(str(ts)).timestamp()
            except Exception:
                epoch_ts = 0.0
            out.append(
                {
                    "ts": ts,
                    "epoch_ts": epoch_ts,
                    "client_order_id": client_order_id,
                    "price": price,
                    "qty": qty,
                    "payload": payload,
                }
            )
        return out

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
