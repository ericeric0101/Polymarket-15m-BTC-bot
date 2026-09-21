"""
SQLite trade journal for run_bot live/simulation diagnostics and analytics.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

from bot.entry_session_policy import MARKOUT_CALIBRATION_START_UTC, is_taipei_weeknight_entry_session

from loguru import logger


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> Any:
    """Keep journal payloads queryable when strategy telemetry uses Decimal."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _summarize_adverse_markouts(
    values: list[float],
    *,
    min_samples: int,
    horizon_sec: int,
    lookback_hours: float,
) -> Optional[Dict[str, float | int | str]]:
    """Build a robust calibration from non-negative per-share observations."""
    values = sorted(max(0.0, float(value)) for value in values)
    sample_count = len(values)
    if sample_count < int(min_samples):
        return None
    cap_index = max(0, math.ceil(sample_count * 0.90) - 1)
    p90_cap = values[cap_index]
    adverse = sum(min(value, p90_cap) for value in values) / sample_count
    if adverse <= 0:
        return None
    return {
        "sample_count": sample_count,
        "adverse_markout_per_share": adverse,
        "raw_mean_adverse_markout_per_share": sum(values) / sample_count,
        "winsor_cap_per_share": p90_cap,
        "method": "winsorized_p90_mean",
        "horizon_sec": float(horizon_sec),
        "lookback_hours": float(lookback_hours),
    }


class TradeJournalDB:
    """
    Lightweight SQLite writer.
    - Opens a short-lived connection per write (safe with multi-thread callbacks)
    - Never raises to strategy path; logs and continues
    """

    SCHEMA_VERSION = 1
    _REQUIRED_COLUMNS = {
        "strategy_runs": {"run_id", "started_at", "mode", "test_mode", "maker_mode"},
        "order_events": {
            "id", "ts", "run_id", "event_type", "client_order_id", "venue_order_id", "side",
            "price", "qty", "status", "reason", "instrument_id", "token_id", "fee_rate_bps",
            "expected_net_usdc", "commission_usdc", "payload_json",
        },
        "strategy_events": {"id", "ts", "run_id", "event_type", "payload_json"},
    }
    # These writes are required to reconstruct live exposure or prevent a
    # duplicate entry after a restart.  Diagnostic/shadow writes must not turn
    # a healthy primary journal into a permanent BUY circuit-breaker.
    _CRITICAL_ORDER_EVENTS = {
        # These are the only order writes that create or confirm exposure.
        # A terminal status is useful evidence, but a prior intent/submit plus
        # external inventory recovery remains conservative if it cannot persist.
        "ORDER_SUBMIT", "ORDER_MAKER_INTENT", "ORDER_FAST_FOLLOW_INTENT", "ORDER_FILLED",
    }
    _CRITICAL_STRATEGY_EVENTS = {
        "FAST_FOLLOW_RISK_STATE", "MARKET_BUY_COUNT_UPDATED",
        "MARKET_STOP_LOSS_COUNT_UPDATED",
    }

    def __init__(self, db_path: str = "./data/trading/trade_journal.db", backup_path: Optional[str] = None,
                 backup_interval_sec: float = 30.0, legacy_db_path: Optional[str | Path] = None,
                 journal_meta_path: Optional[str | Path] = None) -> None:
        self.db_path = str(Path(db_path))
        target = Path(self.db_path)
        self.journal_meta_path = Path(journal_meta_path) if journal_meta_path is not None else target.parent / "journal_meta.json"
        self._marker_present = self._valid_marker_exists()
        self._migration_error: Optional[str] = None
        self._startup_origin = "existing" if target.is_file() else "fresh"
        self._unexpectedly_missing = False
        if not target.is_file():
            legacy = Path(legacy_db_path) if legacy_db_path is not None else self._default_legacy_path(target)
            if legacy is not None and legacy.is_file() and legacy.resolve() != target.resolve():
                try:
                    self._migrate_legacy_journal(legacy, target)
                    self._startup_origin = "migrated"
                    logger.warning(f"Migrated historical trade journal: source={legacy} target={target}")
                except Exception as exc:
                    self._migration_error = str(exc)
                    self._startup_origin = "migration_failed"
                    logger.error(f"Trade journal migration failed: source={legacy} target={target} error={exc}")
            elif self._marker_present:
                # This host has previously had a primary journal. Do not turn
                # its disappearance into a brand-new installation and reset a
                # same-market successful-BUY budget.
                self._startup_origin = "unexpectedly_missing"
                self._unexpectedly_missing = True
        default_backup = Path(self.db_path).parent.parent / "backups" / Path(self.db_path).name
        self.backup_path = str(Path(backup_path)) if backup_path else str(default_backup)
        self._existed_at_startup = Path(self.db_path).is_file()
        self._schema_init_error: Optional[str] = None
        self._runtime_health: Dict[str, Any] = {
            "ready": True, "state": "HEALTHY", "reason": "ready", "consecutive_failures": 0,
        }
        self._health_lock = threading.Lock()
        self._last_health_probe_monotonic = 0.0
        self._backup_interval_sec = max(1.0, float(backup_interval_sec))
        self._backup_dirty = False
        # Coalesce the initial burst of startup telemetry too; a caller can
        # still force a synchronous snapshot via stop()/flush_backup().
        self._last_backup_monotonic = time.monotonic()
        self._backup_lock = threading.Lock()
        self._backup_flush_lock = threading.Lock()
        self._backup_wakeup = threading.Event()
        self._backup_stop = threading.Event()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        if not self._unexpectedly_missing:
            self._init_schema()
        self._startup_health = self._assess_startup_health()
        if self._startup_health.get("ready") and not self._marker_present:
            try:
                self._write_installation_marker()
                self._marker_present = True
            except Exception as exc:
                self._startup_health = {
                    "ready": False, "reason": "journal_marker_write_failed", "error": str(exc),
                }
        self._backup_thread = threading.Thread(target=self._backup_worker, daemon=True, name="trade-journal-backup")
        self._backup_thread.start()

    @staticmethod
    def _default_legacy_path(target: Path) -> Optional[Path]:
        """Find the pre-migration journal only for the standard data/trading layout."""
        if target.parent.name == "trading" and target.parent.parent.name == "data":
            return target.parent.parent.parent / "logs" / "trade_journal.db"
        return None

    def _valid_marker_exists(self) -> bool:
        try:
            with self.journal_meta_path.open("r", encoding="utf-8") as handle:
                marker = json.load(handle)
            return bool(marker.get("journal_initialized") is True and marker.get("installation_id"))
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def _write_installation_marker(self) -> None:
        self.journal_meta_path.parent.mkdir(parents=True, exist_ok=True)
        marker = {
            "format_version": 1,
            "installation_id": str(uuid.uuid4()),
            "journal_initialized": True,
        }
        temporary = self.journal_meta_path.with_name(f".{self.journal_meta_path.name}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(marker, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.journal_meta_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _migrate_legacy_journal(source: Path, target: Path) -> None:
        """Copy an SQLite journal through its backup API and atomically publish it."""
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.migrating")
        try:
            with sqlite3.connect(str(source)) as source_conn, sqlite3.connect(str(temporary)) as destination_conn:
                source_conn.backup(destination_conn)
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def startup_health(self) -> Dict[str, Any]:
        """Return the immutable-at-startup journal gate used for new BUYs."""
        return dict(self._startup_health)

    def runtime_health(self) -> Dict[str, Any]:
        with self._health_lock:
            state = str(self._runtime_health.get("state") or "HEALTHY")
        if state in {"DEGRADED", "FAILED"}:
            self._probe_runtime_health()
        with self._health_lock:
            return dict(self._runtime_health)

    def _mark_runtime_failure(self, reason: str, error: Exception, *, event_type: str = "") -> None:
        error_text = str(error)
        lowered = error_text.lower()
        unrecoverable = any(token in lowered for token in (
            "malformed", "not a database", "database disk image is malformed", "schema", "readonly",
        ))
        with self._health_lock:
            failures = int(self._runtime_health.get("consecutive_failures", 0)) + 1
            state = "FAILED" if unrecoverable or failures >= 3 else "DEGRADED"
            self._runtime_health = {
                "ready": False,
                "state": state,
                "reason": "journal_unrecoverable" if unrecoverable else reason,
                "error": error_text,
                "event_type": str(event_type),
                "consecutive_failures": failures,
            }
        logger.error(
            "Trade journal critical runtime failure; new BUYs must be blocked: "
            f"event_type={event_type} error={error}"
        )

    def _probe_runtime_health(self) -> None:
        """Recover automatically only after an actual SQLite read succeeds."""
        now = time.monotonic()
        with self._health_lock:
            if now - self._last_health_probe_monotonic < 0.05:
                return
            self._last_health_probe_monotonic = now
        try:
            conn = self._connect()
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
        except Exception:
            return
        with self._health_lock:
            self._runtime_health = {
                "ready": True, "state": "HEALTHY", "reason": "recovered", "consecutive_failures": 0,
            }
        logger.warning("Trade journal runtime health recovered after a successful probe")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _backup_after_write(self) -> bool:
        """Snapshot through SQLite's backup API, then atomically publish it."""
        backup = Path(self.backup_path)
        temporary = backup.with_name(f".{backup.name}.tmp")
        try:
            backup.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as source, sqlite3.connect(str(temporary)) as destination:
                source.backup(destination)
            os.replace(temporary, backup)
            return True
        except Exception as e:
            try:
                temporary.unlink(missing_ok=True)
            except Exception:
                pass
            logger.error(f"TradeJournalDB backup failed after write: {e}")
            return False

    def _schedule_backup(self) -> None:
        """Coalesce snapshots off the order/fill callback hot path."""
        with self._backup_lock:
            self._backup_dirty = True
        self._backup_wakeup.set()

    def flush_backup(self) -> bool:
        """Synchronously publish one snapshot; intended for shutdown/tests."""
        # A caller at shutdown must not observe a clean dirty flag while the
        # background worker is still publishing the same snapshot.
        with self._backup_flush_lock:
            with self._backup_lock:
                if not self._backup_dirty:
                    return True
                self._backup_dirty = False
            success = self._backup_after_write()
            if not success:
                with self._backup_lock:
                    self._backup_dirty = True
                return False
            self._last_backup_monotonic = time.monotonic()
            return True

    def _backup_worker(self) -> None:
        while not self._backup_stop.is_set():
            self._backup_wakeup.wait(timeout=1.0)
            self._backup_wakeup.clear()
            with self._backup_lock:
                dirty = self._backup_dirty
            if not dirty:
                continue
            elapsed = time.monotonic() - self._last_backup_monotonic
            if elapsed < self._backup_interval_sec:
                self._backup_stop.wait(self._backup_interval_sec - elapsed)
            if not self._backup_stop.is_set():
                self.flush_backup()

    def stop(self) -> None:
        self._backup_stop.set()
        self._backup_wakeup.set()
        self._backup_thread.join(timeout=2.0)
        self.flush_backup()

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
        CREATE INDEX IF NOT EXISTS idx_strategy_events_type_id ON strategy_events(event_type, id);

        CREATE TABLE IF NOT EXISTS journal_schema (
            version INTEGER NOT NULL
        );
        """
        try:
            with self._connect() as conn:
                conn.executescript(ddl)
                version = conn.execute("SELECT version FROM journal_schema LIMIT 1").fetchone()
                if version is None:
                    conn.execute("INSERT INTO journal_schema(version) VALUES (?)", (self.SCHEMA_VERSION,))
                conn.commit()
        except Exception as e:
            self._schema_init_error = str(e)
            logger.warning(f"TradeJournalDB schema init failed: {e}")

    def _assess_startup_health(self) -> Dict[str, Any]:
        """Fail closed when a restart cannot safely restore trading risk state."""
        if self._unexpectedly_missing:
            return {"ready": False, "reason": "journal_unexpectedly_missing"}
        if self._migration_error:
            return {"ready": False, "reason": "legacy_migration_failed", "error": self._migration_error}
        if self._schema_init_error:
            return {"ready": False, "reason": "schema_init_failed", "error": self._schema_init_error}
        try:
            with self._connect() as conn:
                tables = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                for table, required in self._REQUIRED_COLUMNS.items():
                    if table not in tables:
                        return {"ready": False, "reason": "schema_invalid", "table": table}
                    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                    if not required.issubset(columns):
                        return {"ready": False, "reason": "schema_invalid", "table": table}
                row = conn.execute("SELECT version FROM journal_schema LIMIT 1").fetchone()
                if row is None or int(row[0]) != self.SCHEMA_VERSION:
                    return {"ready": False, "reason": "schema_invalid", "table": "journal_schema"}
                event_count = sum(
                    int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    for table in self._REQUIRED_COLUMNS
                )
                if event_count <= 0:
                    # An empty journal is safe only as an absence of *journal*
                    # evidence. Startup inventory recovery remains authoritative
                    # and will force SELL-only if the venue reports exposure.
                    reason = (
                        "fresh_initialized" if self._startup_origin == "fresh"
                        else "empty_journal_no_recovery_evidence"
                    )
                    return {"ready": True, "reason": reason, "event_count": 0}
        except Exception as e:
            logger.error(f"Trade journal startup healthcheck failed; BUYs blocked: {e}")
            return {"ready": False, "reason": "read_failed", "error": str(e)}
        reason = "migrated_legacy_journal" if self._startup_origin == "migrated" else "ready"
        return {"ready": True, "reason": reason, "event_count": event_count}

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
                        _json_dumps(notes or {}),
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
                    (_utc_now_iso(), _json_dumps(merged_notes), run_id),
                )
                conn.commit()
            self._schedule_backup()
        except Exception as e:
            logger.debug(f"TradeJournalDB log_run_stop failed: {e}")

    def load_market_guard_counts(self, slug: str) -> Optional[Dict[str, int]]:
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
            logger.error(f"TradeJournalDB load_market_guard_counts failed; BUYs must remain blocked: {e}")
            return None

    def load_fast_follow_night_risk(self, night_key: str) -> Optional[Dict[str, Any]]:
        """Recover filled-entry limits after a process restart.

        Older risk snapshots recorded submissions only. Their count is
        returned separately to support a conservative one-night migration.
        """
        if not night_key:
            return {
                "filled_entries": 0, "pending_entries": 0,
                "legacy_attempted_entries": 0, "attempted_entries": 0,
                "open_position_instruments": [],
                "realized_pnl_usdc": 0.0,
            }
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT payload_json
                    FROM strategy_events
                    WHERE event_type='FAST_FOLLOW_RISK_STATE'
                      AND json_extract(payload_json, '$.night_key')=?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (night_key,),
                ).fetchone()
            payload = json.loads(row[0] or "{}") if row else {}
            has_filled_entries = "filled_entries" in payload
            filled_entries = max(0, int(payload.get("filled_entries") or 0)) if has_filled_entries else 0
            legacy_attempted_entries = (
                0 if has_filled_entries else max(0, int(payload.get("attempted_entries") or 0))
            )
            return {
                "filled_entries": filled_entries,
                "pending_entries": max(0, int(payload.get("pending_entries") or 0)),
                "legacy_attempted_entries": legacy_attempted_entries,
                # Retained for callers which only render the old field.
                "attempted_entries": filled_entries,
                "open_position_instruments": [
                    str(item) for item in (payload.get("open_position_instruments") or [])
                    if str(item or "")
                ],
                "realized_pnl_usdc": float(payload.get("realized_pnl_usdc") or 0.0),
            }
        except Exception as e:
            logger.error(f"TradeJournalDB load_fast_follow_night_risk failed; fast-follow BUYs blocked: {e}")
            return None

    def load_maker_buy_markout_calibration(
        self,
        *,
        lookback_hours: float,
        horizon_sec: int,
        min_samples: int,
        entry_regime_bucket: Optional[str] = None,
    ) -> Optional[Dict[str, float | int | str]]:
        """Return a robust adverse BUY markout estimate for one entry regime.

        A single arithmetic mean lets a few violent reversals dominate every
        future maker quote.  We cap observations at their own P90 before
        averaging: adverse selection remains a real cost, while one 67.5-cent
        event cannot dictate the cost of an otherwise ordinary regime.
        """
        try:
            with self._connect() as conn:
                where_bucket = ""
                params: list[Any] = [int(horizon_sec), f"-{float(lookback_hours):g} hours"]
                if entry_regime_bucket:
                    where_bucket = " AND json_extract(payload_json, '$.entry_regime_bucket')=?"
                    params.append(str(entry_regime_bucket))
                rows = conn.execute(
                    """
                    SELECT CASE
                      WHEN CAST(json_extract(payload_json, '$.signed_markout_ps') AS REAL) < 0
                      THEN -CAST(json_extract(payload_json, '$.signed_markout_ps') AS REAL)
                      ELSE 0
                    END AS adverse_markout_per_share
                    FROM order_events
                    WHERE event_type='FILL_MARKOUT'
                      AND side='BUY'
                      AND json_extract(payload_json, '$.liquidity_class')='maker'
                      AND CAST(json_extract(payload_json, '$.horizon_sec') AS INTEGER)=?
                      AND julianday(ts) >= julianday('now', ?)
                    """ + where_bucket,
                    params,
                ).fetchall()
            return _summarize_adverse_markouts(
                [float(row[0] or 0.0) for row in rows],
                min_samples=min_samples,
                horizon_sec=horizon_sec,
                lookback_hours=lookback_hours,
            )
        except Exception as e:
            logger.debug(f"TradeJournalDB load maker markout calibration failed: {e}")
            return None

    def load_maker_buy_markout_calibrations(
        self,
        *,
        lookback_hours: float,
        horizon_sec: int,
        min_samples: int,
        taipei_weeknight_schema_v2_only: bool = False,
    ) -> Dict[str, Dict[str, float | int | str]]:
        """Return global fallback plus independently measured entry regimes."""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT
                      CASE WHEN CAST(json_extract(payload_json, '$.signed_markout_ps') AS REAL) < 0
                        THEN -CAST(json_extract(payload_json, '$.signed_markout_ps') AS REAL)
                        ELSE 0
                      END AS adverse_markout_per_share,
                      json_extract(payload_json, '$.entry_regime_bucket') AS entry_regime_bucket,
                      CAST(json_extract(payload_json, '$.entry_side_score') AS REAL) AS entry_side_score,
                      CAST(json_extract(payload_json, '$.entry_time_left_sec') AS REAL) AS entry_time_left_sec,
                      ts,
                      json_extract(payload_json, '$.slug') AS slug,
                      CAST(json_extract(payload_json, '$.markout_context_schema_version') AS INTEGER)
                        AS markout_context_schema_version
                    FROM order_events
                    WHERE event_type='FILL_MARKOUT'
                      AND side='BUY'
                      AND json_extract(payload_json, '$.liquidity_class')='maker'
                      AND CAST(json_extract(payload_json, '$.horizon_sec') AS INTEGER)=?
                      AND julianday(ts) >= julianday('now', ?)
                    ORDER BY ts ASC, id ASC
                    """,
                    (int(horizon_sec), f"-{float(lookback_hours):g} hours"),
                ).fetchall()
        except Exception as e:
            logger.debug(f"TradeJournalDB load markout calibrations failed: {e}")
            return {}
        if taipei_weeknight_schema_v2_only:
            selected_rows = []
            seen_markets: set[str] = set()
            for row in rows:
                try:
                    observed_at = datetime.fromisoformat(str(row[4]).replace("Z", "+00:00"))
                    slug = str(row[5] or "")
                    if (
                        observed_at < MARKOUT_CALIBRATION_START_UTC
                        or int(row[6] or 0) != 2
                        or not slug
                        or slug in seen_markets
                        or not is_taipei_weeknight_entry_session(observed_at)
                    ):
                        continue
                except (TypeError, ValueError):
                    continue
                seen_markets.add(slug)
                selected_rows.append(row)
            rows = selected_rows
        global_values = [float(row[0] or 0.0) for row in rows]
        global_calibration = _summarize_adverse_markouts(
            global_values,
            min_samples=min_samples,
            horizon_sec=horizon_sec,
            lookback_hours=lookback_hours,
        )
        if not global_calibration:
            return {}
        calibrations: Dict[str, Dict[str, float | int | str]] = {
            "global": {
                **global_calibration,
                "source": (
                    "taipei_weeknight_schema_v2_first_market"
                    if taipei_weeknight_schema_v2_only
                    else "global_fallback"
                ),
            },
        }
        for bucket in ("10_30", "30_60", "60_plus"):
            calibration = _summarize_adverse_markouts(
                [
                    float(row[0] or 0.0)
                    for row in rows
                    if str(row[1] or "") == bucket
                    and abs(float(row[2] or 0.0)) >= 0.35
                    and 300.0 <= float(row[3] or -1.0) < 600.0
                ],
                lookback_hours=lookback_hours,
                horizon_sec=horizon_sec,
                min_samples=min_samples,
            )
            if calibration:
                calibrations[bucket] = {
                    **calibration,
                    "source": f"entry_regime_bucket:{bucket}",
                }
        return calibrations

    def load_strong_directional_regime_calibrations(
        self,
        *,
        lookback_hours: float,
        min_score_abs: float,
        min_samples: int,
    ) -> Dict[str, Dict[str, float | int]]:
        """Return settled hit-rates for independently measured distance bins.

        This intentionally uses the first qualifying observation per market.
        Repeated quote-cycle telemetry must not turn one market into many
        statistically dependent training examples.  Only bins with enough
        independent settled markets are returned.
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    WITH eligible_candidates AS (
                      SELECT
                        e.id,
                        e.ts,
                        json_extract(e.payload_json, '$.slug') AS slug,
                        CASE json_extract(e.payload_json, '$.main_candidate_side')
                          WHEN 'BUY_UP' THEN 'UP'
                          WHEN 'BUY_DOWN' THEN 'DOWN'
                        END AS outcome,
                        ABS(CAST(json_extract(e.payload_json, '$.main_score') AS REAL)) AS score_abs,
                        CASE json_extract(e.payload_json, '$.main_candidate_side')
                          WHEN 'BUY_UP' THEN CAST(json_extract(e.payload_json, '$.spot_minus_strike') AS REAL)
                          WHEN 'BUY_DOWN' THEN -CAST(json_extract(e.payload_json, '$.spot_minus_strike') AS REAL)
                        END AS signed_spot_distance,
                        CAST(json_extract(e.payload_json, '$.time_left_sec') AS REAL) AS time_left_sec
                      FROM strategy_events e
                      WHERE e.event_type='LIVE_SIGNAL_COMPARE'
                        AND ABS(CAST(json_extract(e.payload_json, '$.main_score') AS REAL)) >= ?
                        AND CAST(json_extract(e.payload_json, '$.main_side_locked') AS INTEGER)=1
                        AND CAST(json_extract(e.payload_json, '$.time_left_sec') AS REAL) >= 300
                        AND CAST(json_extract(e.payload_json, '$.time_left_sec') AS REAL) < 600
                        AND CASE json_extract(e.payload_json, '$.main_candidate_side')
                          WHEN 'BUY_UP' THEN CAST(json_extract(e.payload_json, '$.spot_minus_strike') AS REAL)
                          WHEN 'BUY_DOWN' THEN -CAST(json_extract(e.payload_json, '$.spot_minus_strike') AS REAL)
                        END >= 10
                        AND julianday(e.ts) >= julianday('now', ?)
                    ), candidates AS (
                      SELECT
                        *,
                        ROW_NUMBER() OVER (
                          PARTITION BY slug
                          ORDER BY ts ASC, id ASC
                        ) AS rn
                      FROM eligible_candidates
                    ), settlements AS (
                      SELECT
                        json_extract(payload_json, '$.slug') AS slug,
                        UPPER(json_extract(payload_json, '$.outcome')) AS outcome,
                        ROW_NUMBER() OVER (
                          PARTITION BY json_extract(payload_json, '$.slug')
                          ORDER BY ts DESC, id DESC
                        ) AS rn
                      FROM strategy_events
                      WHERE event_type='MARKET_SETTLEMENT'
                    )
                    SELECT
                      CASE
                        WHEN c.signed_spot_distance >= 10 AND c.signed_spot_distance < 30 THEN '10_30'
                        WHEN c.signed_spot_distance >= 30 AND c.signed_spot_distance < 60 THEN '30_60'
                        WHEN c.signed_spot_distance >= 60 THEN '60_plus'
                      END AS distance_bucket,
                      COUNT(*) AS sample_count,
                      SUM(CASE WHEN c.outcome=s.outcome THEN 1 ELSE 0 END) AS wins
                    FROM candidates c
                    JOIN settlements s ON s.slug=c.slug AND s.rn=1
                    WHERE c.rn=1
                      AND c.outcome IN ('UP', 'DOWN')
                      AND s.outcome IN ('UP', 'DOWN')
                      AND c.signed_spot_distance >= 10
                    GROUP BY distance_bucket
                    """,
                    (float(min_score_abs), f"-{float(lookback_hours):g} hours"),
                ).fetchall()
            calibrated: Dict[str, Dict[str, float | int]] = {}
            for row in rows:
                bucket = str(row[0] or "")
                sample_count = int(row[1] or 0)
                wins = int(row[2] or 0)
                if bucket not in {"10_30", "30_60", "60_plus"} or sample_count < int(min_samples):
                    continue
                calibrated[bucket] = {
                    "sample_count": sample_count,
                    "wins": wins,
                    "losses": sample_count - wins,
                    "win_probability": float(wins / sample_count),
                    "min_score_abs": float(min_score_abs),
                    "lookback_hours": float(lookback_hours),
                }
            return calibrated
        except Exception as e:
            logger.debug(f"TradeJournalDB load strong directional regime calibrations failed: {e}")
            return {}

    def reconcile_redeem_cycle(
        self,
        slug: str,
        redeem_value_usdc: float,
        *,
        tx_hash: str = "",
        condition_id: str = "",
    ) -> Optional[Dict[str, float | bool]]:
        """Reconcile a market's journal PnL to its confirmed redemption cash flow.

        Settlement is initially estimated from the local outcome snapshot.  A
        successful redemption is the authoritative cash event, so it must
        replace that estimate in-place.  Appending another MARKET_CYCLE_PNL
        creates two totals for the same market and makes dashboard sums wrong.
        """
        slug = str(slug or "")
        if not slug:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT id, payload_json FROM strategy_events
                    WHERE event_type='MARKET_CYCLE_PNL'
                      AND json_extract(payload_json, '$.slug')=?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (slug,),
                ).fetchone()
                cash_row = conn.execute(
                    """
                    SELECT
                      COALESCE(SUM(CASE WHEN side='BUY' THEN price * qty +
                        COALESCE(CAST(json_extract(payload_json, '$.effective_fee_usdc') AS REAL), 0.0)
                      ELSE 0 END), 0.0) AS buy_cost,
                      COALESCE(SUM(CASE WHEN side='SELL' THEN price * qty -
                        COALESCE(CAST(json_extract(payload_json, '$.effective_fee_usdc') AS REAL), 0.0)
                      ELSE 0 END), 0.0) AS sell_proceeds
                    FROM order_events
                    WHERE event_type='ORDER_FILLED'
                      AND json_extract(payload_json, '$.slug')=?
                    """,
                    (slug,),
                ).fetchone()
                buy_cost = float(cash_row[0] or 0.0)
                sell_proceeds = float(cash_row[1] or 0.0)
                if buy_cost <= 0 and sell_proceeds <= 0:
                    return None

                redeem_value = max(0.0, float(redeem_value_usdc or 0.0))
                # Keep the two components additive and cash-based.  The
                # resulting combined value is authoritative even when a
                # restart lost the in-memory inventory-cost allocation.
                fill_realized = sell_proceeds
                settlement_pnl = redeem_value - buy_cost
                cycle_combined = fill_realized + settlement_pnl
                reconciled_at = _utc_now_iso()
                reconciliation = {
                    "buy_cost_usdc": buy_cost,
                    "sell_proceeds_usdc": sell_proceeds,
                    "redeem_value_usdc": redeem_value,
                    "cycle_fill_realized_usdc": fill_realized,
                    "cycle_settlement_pnl_usdc": settlement_pnl,
                    "cycle_combined_pnl_usdc": cycle_combined,
                    "cycle_pnl_reconciled_source": "onchain_redeem",
                    "cycle_pnl_reconciled_at": reconciled_at,
                    "redeem_tx_hash": str(tx_hash or ""),
                    "redeem_condition_id": str(condition_id or ""),
                }

                settlement = conn.execute(
                    """
                    SELECT id, payload_json FROM strategy_events
                    WHERE event_type='MARKET_SETTLEMENT'
                      AND json_extract(payload_json, '$.slug')=?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (slug,),
                ).fetchone()
                if settlement is not None:
                    settlement_payload = json.loads(settlement[1] or "{}")
                    if not isinstance(settlement_payload, dict):
                        settlement_payload = {}
                    inventory_shares = float(settlement_payload.get("inventory_shares") or 0.0)
                    inventory_cost = float(settlement_payload.get("inventory_cost_usdc") or 0.0)
                    settlement_payload.update(
                        {
                            "redeem_value_usdc": redeem_value,
                            "redeem_per_share": (
                                redeem_value / inventory_shares if inventory_shares > 0 else 0.0
                            ),
                            "settlement_pnl_usdc": redeem_value - inventory_cost,
                            "settlement_reconciled_source": "onchain_redeem",
                            "settlement_reconciled_at": reconciled_at,
                            "redeem_tx_hash": str(tx_hash or ""),
                            "redeem_condition_id": str(condition_id or ""),
                        }
                    )
                    conn.execute(
                        "UPDATE strategy_events SET payload_json=? WHERE id=?",
                        (_json_dumps(settlement_payload), int(settlement[0])),
                    )

                wrote_cycle_pnl = row is None
                if row is not None:
                    cycle_payload = json.loads(row[1] or "{}")
                    if not isinstance(cycle_payload, dict):
                        cycle_payload = {}
                    cycle_payload.update(reconciliation)
                    conn.execute(
                        "UPDATE strategy_events SET payload_json=? WHERE id=?",
                        (_json_dumps(cycle_payload), int(row[0])),
                    )
                conn.commit()
            return {**reconciliation, "wrote_cycle_pnl": wrote_cycle_pnl}
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
                      'SHADOW_SIM_ENTRY_REQUOTED',
                      'SHADOW_SIM_ENTRY_CANCELLED',
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

    def log_strategy_event(self, run_id: str, event_type: str, payload: Optional[Dict[str, Any]] = None) -> bool:
        sql = "INSERT INTO strategy_events (ts, run_id, event_type, payload_json) VALUES (?, ?, ?, ?)"
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._connect()
            conn.execute(
                sql,
                (
                    _utc_now_iso(),
                    run_id,
                    event_type,
                    _json_dumps(payload or {}),
                ),
            )
            conn.commit()
            self._schedule_backup()
            return True
        except Exception as e:
            if event_type in self._CRITICAL_STRATEGY_EVENTS:
                self._mark_runtime_failure("write_failed", e, event_type=event_type)
            logger.debug(f"TradeJournalDB log_strategy_event failed: {e}")
            return False
        finally:
            if conn is not None:
                conn.close()

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
    ) -> bool:
        sql = """
        INSERT INTO order_events (
            ts, run_id, event_type, client_order_id, venue_order_id, side, price, qty, status, reason,
            instrument_id, token_id, fee_rate_bps, expected_net_usdc, commission_usdc, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._connect()
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
                    _json_dumps(payload or {}),
                ),
            )
            conn.commit()
            self._schedule_backup()
            return True
        except Exception as e:
            if event_type in self._CRITICAL_ORDER_EVENTS:
                self._mark_runtime_failure("write_failed", e, event_type=event_type)
            logger.debug(f"TradeJournalDB log_order_event failed: {e}")
            return False
        finally:
            if conn is not None:
                conn.close()

    def load_recent_buy_submits(self, instrument_id: str, limit: int = 20) -> list[Dict[str, Any]]:
        if not instrument_id:
            return []
        sql = """
        SELECT ts, client_order_id, price, qty, payload_json
        FROM order_events
        WHERE event_type IN (
            'ORDER_SUBMIT', 'ORDER_MAKER_INTENT',
            'ORDER_FAST_FOLLOW_INTENT', 'ORDER_FAST_FOLLOW_SUBMIT'
        )
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
                    "strike_status": str(payload.get("strike_status") or ""),
                    "sample_dt_sec": payload.get("sample_dt_sec"),
                }
        except Exception as e:
            logger.debug(f"TradeJournalDB load_latest_locked_strike failed: {e}")
        return None
