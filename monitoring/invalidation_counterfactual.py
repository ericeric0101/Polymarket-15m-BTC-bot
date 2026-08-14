"""Counterfactual reporting for confirmed-side-invalidation exits.

The report is deliberately conservative: it only evaluates a price that was
actually journaled at the time of a confirmed invalidation or a submitted
protective exit.  It never fabricates an earlier fill from a later settlement.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from monitoring.pnl_attribution import load_market_pnl_attribution


def _payload(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def load_invalidation_counterfactual(
    db_path: str | Path,
    slug: str,
) -> dict[str, Any] | None:
    """Return one evidence-backed invalidation exit comparison for a market."""
    slug = str(slug or "")
    if not slug:
        return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        audit_row = conn.execute(
            """
            SELECT ts, payload_json FROM strategy_events
            WHERE event_type='EXIT_AUDIT'
              AND json_extract(payload_json, '$.slug')=?
              AND json_extract(payload_json, '$.locked_side_invalidated') IN (1, '1', 'true', 'True')
              AND json_extract(payload_json, '$.best_bid') > 0
              AND json_extract(payload_json, '$.sellable_qty') > 0
            ORDER BY id LIMIT 1
            """,
            (slug,),
        ).fetchone()
        source = "exit_audit"
        if audit_row:
            evidence = _payload(audit_row["payload_json"])
            evidence_ts = str(audit_row["ts"])
        else:
            fallback = conn.execute(
                """
                SELECT ts, payload_json FROM order_events
                WHERE event_type='ORDER_TAKER_EXIT_SUBMIT'
                  AND json_extract(payload_json, '$.slug')=?
                  AND reason IN ('invalidation_recovery', 'offside_near_close', 'stop_loss')
                  AND json_extract(payload_json, '$.best_bid') > 0
                ORDER BY id LIMIT 1
                """,
                (slug,),
            ).fetchone()
            if not fallback:
                return None
            evidence = _payload(fallback["payload_json"])
            evidence_ts = str(fallback["ts"])
            source = "submitted_taker_exit_fallback"

        quantity = _num(evidence.get("sellable_qty") or evidence.get("qty"))
        avg_entry = _num(evidence.get("avg_entry"))
        best_bid = _num(evidence.get("best_bid"))
        if quantity <= 0 or avg_entry <= 0 or best_bid <= 0:
            return None
        counterfactual_gross_pnl = best_bid * quantity - avg_entry * quantity
        attribution = load_market_pnl_attribution(db_path, slug)
        actual_pnl = attribution["reported_cycle_pnl_usdc"]
        return {
            "slug": slug,
            "evidence_source": source,
            "evidence_ts": evidence_ts,
            "best_bid": best_bid,
            "quantity": quantity,
            "avg_entry": avg_entry,
            "time_left_sec": evidence.get("time_left_sec"),
            "recovery_ratio": evidence.get("recovery_ratio"),
            "counterfactual_gross_pnl_usdc": counterfactual_gross_pnl,
            "actual_cycle_pnl_usdc": actual_pnl,
            "gross_improvement_vs_actual_usdc": (
                counterfactual_gross_pnl - actual_pnl if actual_pnl is not None else None
            ),
        }
    finally:
        conn.close()
