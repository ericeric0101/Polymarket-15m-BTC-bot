"""Bounded exception metadata sent to the existing independent journal worker."""
from __future__ import annotations

from collections import Counter, OrderedDict
import sqlite3
import time
from typing import Any, Callable


class WriterFailureDiagnostics:
    INTERVAL_SEC = 60.0
    MAX_EMISSIONS_PER_INTERVAL = 8
    MAX_SIGNATURES = 16
    MESSAGE_LIMIT = 256
    EVENT_TYPE = "RESEARCH_WRITER_FAILURE"
    # Exception text can contain SQL parameters or account material. Keep only
    # recognized generic diagnostics, never arbitrary text or their suffixes.
    SAFE_MESSAGES = (
        "database is locked", "database table is locked", "database or disk is full",
        "disk I/O error", "attempt to write a readonly database", "unable to open database file",
        "database disk image is malformed", "cannot start a transaction within a transaction",
        "cannot commit - no transaction is active", "Cannot operate on a closed database.",
        "SQLite objects created in a thread can only be used in that same thread.",
        "no such table", "no such column", "UNIQUE constraint failed", "NOT NULL constraint failed",
        "CHECK constraint failed", "FOREIGN KEY constraint failed", "datatype mismatch",
        "Circular reference detected", "Out of range float values are not JSON compliant",
        "interrupted", "out of memory",
    )

    def __init__(self, *, journal: Any, writer_name: str, db_target: str,
                 run_id: str, identity: Callable[[], dict] | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time) -> None:
        self.journal = journal
        self.writer_name = writer_name
        self.db_target = db_target
        self.run_id = run_id
        self.identity = identity
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self._signatures: OrderedDict[tuple, float] = OrderedDict()
        self._window_start: float | None = None
        self._window_emissions = 0
        self._pending_suppressed = 0
        self.suppressed_total = 0
        self.enqueue_failures = 0
        self.last_failure: dict = {}

    @classmethod
    def _message(cls, exc: Exception) -> tuple[str, bool]:
        raw = str(exc)
        short = raw[:cls.MESSAGE_LIMIT]
        for safe in cls.SAFE_MESSAGES:
            if short.lower().startswith(safe.lower()):
                return safe[:cls.MESSAGE_LIMIT], raw != safe
        if short.startswith("Object of type ") and " is not JSON serializable" in short:
            return "Object type is not JSON serializable", True
        return "Unclassified exception message withheld", True

    @staticmethod
    def batch_types(rows: list[tuple]) -> dict[str, int]:
        # Only queue protocol kinds, not payload event names or row contents.
        counts: Counter[str] = Counter()
        for row in rows:
            if isinstance(row, tuple) and len(row) == 5:
                counts["snapshot"] += 1
            elif isinstance(row, tuple) and len(row) == 2 and isinstance(row[0], str) and row[0] in {
                "reference", "decision", "latency", "markout",
            }:
                counts[row[0]] += 1
            else:
                counts["unknown"] += 1
        return dict(counts)

    def record(self, *, exc: Exception, stage: str, rows: list[tuple],
               queue_depth: int, write_errors: int, queue_drops: int,
               commit_acknowledged: bool) -> None:
        """Called only by one writer; errors never escape into its failure path."""
        try:
            self._record(exc=exc, stage=stage, rows=rows, queue_depth=queue_depth,
                         write_errors=write_errors, queue_drops=queue_drops,
                         commit_acknowledged=commit_acknowledged)
        except Exception:
            self.enqueue_failures += 1

    def health_snapshot(self) -> dict:
        return {**self.last_failure, "suppressed_error_count": self.suppressed_total,
                "pending_suppressed_count": self._pending_suppressed,
                "diagnostic_enqueue_failures": self.enqueue_failures}

    def _record(self, *, exc: Exception, stage: str, rows: list[tuple],
                queue_depth: int, write_errors: int, queue_drops: int,
                commit_acknowledged: bool) -> None:
        now = self.monotonic()
        timestamp = self.wall_clock()
        exception_class = type(exc).__name__[:64]
        code = getattr(exc, "sqlite_errorcode", None) if isinstance(exc, sqlite3.Error) else None
        name = getattr(exc, "sqlite_errorname", None) if isinstance(exc, sqlite3.Error) else None
        code = code if isinstance(code, int) else None
        name = str(name)[:64] if name is not None else None
        self.last_failure = {"last_failure_ts": timestamp, "last_failure_stage": stage,
                             "last_exception_class": exception_class, "last_sqlite_errorname": name}
        signature = (stage, exception_class, code, name)
        last_emitted = self._signatures.get(signature)
        if self._window_start is None or now - self._window_start >= self.INTERVAL_SEC:
            self._window_start, self._window_emissions = now, 0
        if ((last_emitted is not None and now - last_emitted < self.INTERVAL_SEC)
                or self._window_emissions >= self.MAX_EMISSIONS_PER_INTERVAL):
            self._pending_suppressed += 1
            self.suppressed_total += 1
            return
        self._window_emissions += 1
        self._signatures[signature] = now
        self._signatures.move_to_end(signature)
        while len(self._signatures) > self.MAX_SIGNATURES:
            self._signatures.popitem(last=False)
        message, redacted = self._message(exc)
        identity = self.identity() if self.identity else {}
        cycle = identity.get("cycle_idx") if isinstance(identity, dict) else None
        payload = {
            "timestamp": timestamp, "run_id": self.run_id,
            "cycle_idx": cycle if isinstance(cycle, int) else None,
            "writer_name": self.writer_name, "db_target": self.db_target,
            "failure_stage": stage, "exception_class": exception_class,
            "exception_message": message, "exception_message_redacted": redacted,
            "sqlite_errorcode": code, "sqlite_errorname": name,
            "batch_item_count": len(rows), "batch_type_counts": self.batch_types(rows),
            "queue_depth_at_failure": queue_depth, "write_errors_after_increment": write_errors,
            "queue_drops": queue_drops, "commit_acknowledged": "YES" if commit_acknowledged else "NO",
            "suppressed_count": self._pending_suppressed,
            "suppressed_error_count_total": self.suppressed_total,
            "diagnostic_enqueue_failures": self.enqueue_failures,
            "authority": "research_writer_diagnostic_only",
        }
        try:
            accepted = self.journal.enqueue_strategy_event(self.run_id, self.EVENT_TYPE, payload)
        except Exception:
            accepted = False
        if accepted:
            self._pending_suppressed = 0
        else:
            self.enqueue_failures += 1
            self._pending_suppressed += 1
