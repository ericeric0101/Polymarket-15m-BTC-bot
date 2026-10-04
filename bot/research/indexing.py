"""Idempotent, opt-in index plan for a copied/offline research database."""
from __future__ import annotations
import sqlite3

INDEX_NAME = "idx_lead_lag_run_slug_time"
INDEX_SQL = f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON lead_lag_decisions(run_id, slug, decision_epoch_ns)"

def index_status(conn: sqlite3.Connection) -> bool:
    return any(row[1] == INDEX_NAME for row in conn.execute("PRAGMA index_list(lead_lag_decisions)"))

def apply_index_plan(conn: sqlite3.Connection) -> bool:
    """Caller owns the copied DB; returns whether this call created the index."""
    existed = index_status(conn)
    conn.execute(INDEX_SQL)
    conn.commit()
    return not existed
