"""Authoritative per-market PnL attribution from the trade journal."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
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


def _empty_market_pnl(slug: str) -> dict[str, Any]:
    return {
        "slug": slug,
        "buy_notional_usdc": 0.0, "buy_fee_usdc": 0.0,
        "maker_sell_proceeds_usdc": 0.0, "taker_exit_proceeds_usdc": 0.0,
        "sell_fee_usdc": 0.0, "redeem_value_usdc": 0.0,
        "redeem_value_source": "settlement_estimate", "fill_count": 0,
        "buy_fill_count": 0, "maker_sell_fill_count": 0,
        "taker_exit_fill_count": 0, "reported_cycle_pnl_usdc": None,
        "computed_pnl_usdc": 0.0, "attributable_pnl_usdc": None,
        "accounting_status": "no_tracked_entry", "reconciliation_adjustment_usdc": None,
    }


def load_market_pnl_attributions(
    db_path: str | Path,
    slugs: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Load a whole journal in bounded passes, preserving attribution status.

    A SELL without a journaled BUY is cash activity but not attributable bot
    PnL: the inventory predates this journal or was restored externally.  It
    stays visible as ``pre_journal_inventory`` instead of inflating strategy
    performance.  One set-based scan also avoids the old report's O(markets ×
    journal) JSON scans on large databases.
    """
    requested = {str(slug) for slug in slugs} if slugs else None
    result: dict[str, dict[str, Any]] = {}

    def item(slug: str) -> dict[str, Any]:
        if slug not in result:
            result[slug] = _empty_market_pnl(slug)
        return result[slug]

    with sqlite3.connect(str(db_path)) as conn:
        taker_ids: dict[str, set[str]] = defaultdict(set)
        for client_order_id, raw in conn.execute(
            "SELECT client_order_id, payload_json FROM order_events WHERE event_type='ORDER_TAKER_EXIT_SUBMIT'"
        ):
            payload = _payload(raw)
            slug = str(payload.get("slug") or "")
            if slug and (requested is None or slug in requested):
                taker_ids[slug].add(str(client_order_id or ""))

        for client_order_id, side, price, qty, raw in conn.execute(
            """SELECT client_order_id, side, price, qty, payload_json
               FROM order_events WHERE event_type='ORDER_FILLED' ORDER BY id"""
        ):
            payload = _payload(raw)
            slug = str(payload.get("slug") or "")
            if not slug or (requested is not None and slug not in requested):
                continue
            row = item(slug)
            fill_side = str(side or "").upper()
            notional, fee = _num(price) * _num(qty), _num(payload.get("effective_fee_usdc"))
            row["fill_count"] += 1
            if fill_side == "BUY":
                row["buy_fill_count"] += 1
                row["buy_notional_usdc"] += notional
                row["buy_fee_usdc"] += fee
            elif fill_side == "SELL":
                if str(client_order_id or "") in taker_ids[slug]:
                    row["taker_exit_fill_count"] += 1
                    row["taker_exit_proceeds_usdc"] += notional
                else:
                    row["maker_sell_fill_count"] += 1
                    row["maker_sell_proceeds_usdc"] += notional
                row["sell_fee_usdc"] += fee

        redeem_seen: dict[str, set[str]] = defaultdict(set)
        settlement_redeem: dict[str, float] = {}
        for event_type, raw in conn.execute(
            """SELECT event_type, payload_json FROM strategy_events
               WHERE event_type IN ('REDEEM_EXECUTED', 'MARKET_SETTLEMENT', 'MARKET_CYCLE_PNL')
               ORDER BY id"""
        ):
            payload = _payload(raw)
            slug = str(payload.get("slug") or "")
            if not slug or (requested is not None and slug not in requested):
                continue
            row = item(slug)
            if event_type == "REDEEM_EXECUTED" and int(_num(payload.get("status"))) == 1:
                # A successful redemption transaction does not by itself
                # establish the USDC received.  Older rows only recorded
                # redeemed shares, so preserve the settlement-value fallback
                # rather than silently treating their cash value as zero.
                if "redeem_cash_usdc" not in payload:
                    continue
                identity = str(payload.get("condition_id") or payload.get("tx_hash") or payload.get("redeem_activity_tx_hash") or "")
                if identity and identity not in redeem_seen[slug]:
                    redeem_seen[slug].add(identity)
                    row["redeem_value_usdc"] += _num(payload.get("redeem_cash_usdc"))
                    row["redeem_value_source"] = "onchain_redeem"
            elif event_type == "MARKET_SETTLEMENT":
                settlement_redeem[slug] = _num(payload.get("redeem_value_usdc"))
            elif event_type == "MARKET_CYCLE_PNL":
                row["reported_cycle_pnl_usdc"] = _num(payload.get("cycle_combined_pnl_usdc"))

        for slug, value in settlement_redeem.items():
            row = item(slug)
            if not redeem_seen[slug]:
                row["redeem_value_usdc"] = value

    for row in result.values():
        row["computed_pnl_usdc"] = (
            row["maker_sell_proceeds_usdc"] + row["taker_exit_proceeds_usdc"]
            + row["redeem_value_usdc"] - row["buy_notional_usdc"]
            - row["buy_fee_usdc"] - row["sell_fee_usdc"]
        )
        has_exit_cash = any(row[name] > 0 for name in (
            "maker_sell_proceeds_usdc", "taker_exit_proceeds_usdc", "redeem_value_usdc",
        ))
        if row["buy_fill_count"] == 0 and has_exit_cash:
            row["accounting_status"] = "pre_journal_inventory"
        elif row["buy_fill_count"] > 0:
            row["accounting_status"] = "complete"
            row["attributable_pnl_usdc"] = row["computed_pnl_usdc"]
            if row["reported_cycle_pnl_usdc"] is not None:
                row["reconciliation_adjustment_usdc"] = (
                    row["reported_cycle_pnl_usdc"] - row["computed_pnl_usdc"]
                )
    return result


def load_market_pnl_attribution(db_path: str | Path, slug: str) -> dict[str, Any]:
    """Return a reproducible PnL ledger for one market.

    `computed_pnl_usdc` is derived only from fills and redemption value.  The
    journal's `MARKET_CYCLE_PNL` remains separately reported, so recovery after
    process restarts or external reconciliation cannot be silently hidden.
    """
    slug = str(slug or "")
    if not slug:
        return _empty_market_pnl("")
    return load_market_pnl_attributions(db_path, {slug}).get(slug, _empty_market_pnl(slug))
