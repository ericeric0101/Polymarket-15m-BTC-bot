"""Authoritative per-market PnL attribution from the trade journal."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def _payload(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def load_market_pnl_attribution(db_path: str | Path, slug: str) -> dict[str, Any]:
    """Return a reproducible PnL ledger for one market.

    `computed_pnl_usdc` is derived only from fills and redemption value.  The
    journal's `MARKET_CYCLE_PNL` remains separately reported, so recovery after
    process restarts or external reconciliation cannot be silently hidden.
    """
    slug = str(slug or "")
    result: dict[str, Any] = {
        "slug": slug,
        "buy_notional_usdc": 0.0,
        "buy_fee_usdc": 0.0,
        "maker_sell_proceeds_usdc": 0.0,
        "taker_exit_proceeds_usdc": 0.0,
        "sell_fee_usdc": 0.0,
        "redeem_value_usdc": 0.0,
        "redeem_value_source": "settlement_estimate",
        "fill_count": 0,
        "buy_fill_count": 0,
        "maker_sell_fill_count": 0,
        "taker_exit_fill_count": 0,
        "reported_cycle_pnl_usdc": None,
        "computed_pnl_usdc": 0.0,
        "reconciliation_adjustment_usdc": None,
    }
    if not slug:
        return result

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        submitted_taker_ids = {
            str(row["client_order_id"] or "")
            for row in conn.execute(
                """
                SELECT client_order_id FROM order_events
                WHERE event_type='ORDER_TAKER_EXIT_SUBMIT'
                  AND json_extract(payload_json, '$.slug')=?
                """,
                (slug,),
            )
        }
        fills = conn.execute(
            """
            SELECT client_order_id, side, price, qty, payload_json
            FROM order_events
            WHERE event_type='ORDER_FILLED'
              AND json_extract(payload_json, '$.slug')=?
            ORDER BY id
            """,
            (slug,),
        ).fetchall()
        for row in fills:
            payload = _payload(row["payload_json"])
            side = str(row["side"] or "").upper()
            notional = _num(row["price"]) * _num(row["qty"])
            fee = _num(payload.get("effective_fee_usdc"))
            result["fill_count"] += 1
            if side == "BUY":
                result["buy_fill_count"] += 1
                result["buy_notional_usdc"] += notional
                result["buy_fee_usdc"] += fee
            elif side == "SELL":
                if str(row["client_order_id"] or "") in submitted_taker_ids:
                    result["taker_exit_fill_count"] += 1
                    result["taker_exit_proceeds_usdc"] += notional
                else:
                    result["maker_sell_fill_count"] += 1
                    result["maker_sell_proceeds_usdc"] += notional
                result["sell_fee_usdc"] += fee

        # A confirmed REDEEM_EXECUTED amount is authoritative.  Fall back to
        # the local settlement estimate only for markets not yet redeemed.
        redeem_rows = conn.execute(
            """
            SELECT payload_json FROM strategy_events
            WHERE event_type='REDEEM_EXECUTED'
              AND json_extract(payload_json, '$.slug')=?
              AND COALESCE(CAST(json_extract(payload_json, '$.status') AS INTEGER), 0)=1
            ORDER BY id DESC
            """,
            (slug,),
        ).fetchall()
        seen_redemptions: set[str] = set()
        for row in redeem_rows:
            payload = _payload(row["payload_json"])
            if "redeem_cash_usdc" not in payload:
                continue
            identity = str(
                payload.get("condition_id")
                or payload.get("tx_hash")
                or payload.get("redeem_activity_tx_hash")
                or ""
            )
            if not identity or identity in seen_redemptions:
                continue
            seen_redemptions.add(identity)
            result["redeem_value_usdc"] += _num(payload.get("redeem_cash_usdc"))
        if seen_redemptions:
            result["redeem_value_source"] = "onchain_redeem"
        else:
            settlement_rows = conn.execute(
            """
            SELECT payload_json FROM strategy_events
            WHERE event_type='MARKET_SETTLEMENT'
              AND json_extract(payload_json, '$.slug')=?
            ORDER BY id
            """,
            (slug,),
            ).fetchall()
            for row in settlement_rows:
                result["redeem_value_usdc"] += _num(_payload(row["payload_json"]).get("redeem_value_usdc"))

        cycle = conn.execute(
            """
            SELECT payload_json FROM strategy_events
            WHERE event_type='MARKET_CYCLE_PNL'
              AND json_extract(payload_json, '$.slug')=?
            ORDER BY id DESC LIMIT 1
            """,
            (slug,),
        ).fetchone()
        if cycle:
            cycle_payload = _payload(cycle["payload_json"])
            result["reported_cycle_pnl_usdc"] = _num(cycle_payload.get("cycle_combined_pnl_usdc"))

        result["computed_pnl_usdc"] = (
            result["maker_sell_proceeds_usdc"]
            + result["taker_exit_proceeds_usdc"]
            + result["redeem_value_usdc"]
            - result["buy_notional_usdc"]
            - result["buy_fee_usdc"]
            - result["sell_fee_usdc"]
        )
        if result["reported_cycle_pnl_usdc"] is not None:
            result["reconciliation_adjustment_usdc"] = (
                result["reported_cycle_pnl_usdc"] - result["computed_pnl_usdc"]
            )
        return result
    finally:
        conn.close()
