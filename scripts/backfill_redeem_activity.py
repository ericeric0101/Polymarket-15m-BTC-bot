#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


DATA_API = "https://data-api.polymarket.com"


def _json_loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _norm_tx(tx_hash: str | None) -> str:
    raw = str(tx_hash or "").strip().lower()
    return raw[2:] if raw.startswith("0x") else raw


def _norm_addr(addr: str | None) -> str:
    return str(addr or "").strip().lower()


def _fetch_redeem_activity(user: str, *, limit: int) -> list[dict[str, Any]]:
    params = {
        "user": user,
        "limit": min(max(limit, 1), 500),
        "type": "REDEEM",
        "sortBy": "TIMESTAMP",
        "sortDirection": "DESC",
    }
    response = requests.get(f"{DATA_API}/activity", params=params, timeout=20)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def _activity_indexes(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    by_tx: dict[str, dict[str, Any]] = {}
    by_slug_condition: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        tx = _norm_tx(row.get("transactionHash"))
        if tx:
            by_tx[tx] = row
        slug = str(row.get("slug") or row.get("eventSlug") or "")
        condition_id = str(row.get("conditionId") or "").lower()
        if slug and condition_id:
            by_slug_condition[(slug, condition_id)] = row
    return by_tx, by_slug_condition


def _activity_amount(row: dict[str, Any]) -> float:
    value = row.get("usdcSize")
    if value is None:
        value = row.get("size")
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _fetch_latest_payload_row(conn: sqlite3.Connection, event_type: str, slug: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, payload_json
        FROM strategy_events
        WHERE event_type = ?
          AND (
            json_extract(payload_json, '$.slug') = ?
            OR json_extract(payload_json, '$.market_slug') = ?
          )
        ORDER BY id DESC
        LIMIT 1
        """,
        (event_type, slug, slug),
    ).fetchone()


def _reconcile_settlement_rows(conn: sqlite3.Connection, slug: str, amount: float, reconciled_at: str) -> None:
    settlement_row = _fetch_latest_payload_row(conn, "MARKET_SETTLEMENT", slug)
    settlement_pnl: float | None = None
    if settlement_row is not None:
        payload = _json_loads(settlement_row["payload_json"])
        inventory_shares = float(payload.get("inventory_shares") or 0.0)
        inventory_cost = float(payload.get("inventory_cost_usdc") or 0.0)
        settlement_pnl = amount - inventory_cost
        payload["redeem_value_usdc"] = amount
        payload["redeem_per_share"] = amount / inventory_shares if inventory_shares > 0 else 0.0
        payload["settlement_pnl_usdc"] = settlement_pnl
        payload["settlement_reconciled_source"] = "polymarket_data_api_activity"
        payload["settlement_reconciled_at"] = reconciled_at

        inventory_side = str(payload.get("inventory_side") or "").upper()
        if amount > 0 and inventory_side in {"UP", "DOWN"}:
            payload["outcome"] = inventory_side
            payload["outcome_reconciled_from_redeem"] = True

        conn.execute(
            "UPDATE strategy_events SET payload_json = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), int(settlement_row["id"])),
        )

    cycle_row = _fetch_latest_payload_row(conn, "MARKET_CYCLE_PNL", slug)
    if cycle_row is not None and settlement_pnl is not None:
        payload = _json_loads(cycle_row["payload_json"])
        fill_realized = float(payload.get("cycle_fill_realized_usdc") or 0.0)
        payload["cycle_settlement_pnl_usdc"] = settlement_pnl
        payload["cycle_combined_pnl_usdc"] = fill_realized + settlement_pnl
        payload["cycle_pnl_reconciled_source"] = "polymarket_data_api_activity"
        payload["cycle_pnl_reconciled_at"] = reconciled_at
        conn.execute(
            "UPDATE strategy_events SET payload_json = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), int(cycle_row["id"])),
        )


def backfill(db_path: Path, user: str, *, limit: int, dry_run: bool) -> int:
    activity_rows = _fetch_redeem_activity(user, limit=limit)
    by_tx, by_slug_condition = _activity_indexes(activity_rows)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, payload_json
            FROM strategy_events
            WHERE event_type = 'REDEEM_EXECUTED'
            ORDER BY id DESC
            LIMIT 2000
            """
        ).fetchall()

        updates: list[tuple[str, int, str, float]] = []
        reconciled_at = datetime.now(timezone.utc).isoformat()
        for row in rows:
            payload = _json_loads(row["payload_json"])
            tx = _norm_tx(payload.get("tx_hash") or payload.get("transactionHash"))
            slug = str(payload.get("slug") or payload.get("market_slug") or "")
            condition_id = str(payload.get("condition_id") or payload.get("conditionId") or "").lower()
            activity = by_tx.get(tx)
            if activity is None and slug and condition_id:
                activity = by_slug_condition.get((slug, condition_id))
            if activity is None:
                continue

            amount = _activity_amount(activity)
            # Data API activity is the first per-condition source that tells
            # us the actual collateral payout.  Keep it distinct from the
            # position-token quantity logged when the transaction was sent.
            payload["redeem_cash_usdc"] = amount
            payload["redeem_activity_size"] = float(activity.get("size") or 0.0)
            payload["redeem_activity_usdc_size"] = amount
            payload["redeem_activity_timestamp"] = activity.get("timestamp")
            payload["redeem_activity_source"] = "polymarket_data_api_activity"
            payload["redeem_activity_reconciled_at"] = reconciled_at
            if activity.get("transactionHash"):
                payload["redeem_activity_tx_hash"] = activity.get("transactionHash")
            if activity.get("conditionId"):
                payload["redeem_activity_condition_id"] = activity.get("conditionId")
            if activity.get("slug"):
                payload["slug"] = activity.get("slug")
                payload["market_slug"] = activity.get("slug")

            updates.append((json.dumps(payload, ensure_ascii=False), int(row["id"]), slug, amount))

        if not dry_run:
            conn.executemany(
                "UPDATE strategy_events SET payload_json = ? WHERE id = ?",
                [(payload_json, row_id) for payload_json, row_id, _, _ in updates],
            )
            for _, _, slug, amount in updates:
                if slug:
                    _reconcile_settlement_rows(conn, slug, amount, reconciled_at)
            conn.commit()
    finally:
        conn.close()

    for _, _, slug, amount in updates[:20]:
        print(f"reconciled slug={slug} redeem_cash_usdc={amount:.6f}")
    print(
        f"activity_rows={len(activity_rows)} redeem_events_scanned={len(rows)} "
        f"updated={len(updates)} db={db_path} dry_run={dry_run}"
    )
    return len(updates)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill actual redeem amounts from Polymarket Data API activity.")
    parser.add_argument("--db", default=os.getenv("TRADE_DB_PATH", "./logs/trade_journal.db"))
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--user", default="")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv(args.env_file, override=False)
    user = args.user or os.getenv("POLYMARKET_WALLET_ADDRESS") or os.getenv("WALLET_ADDRESS") or ""
    user = _norm_addr(user)
    if not user:
        raise SystemExit("missing user address; set POLYMARKET_WALLET_ADDRESS or pass --user")

    db_path = Path(args.db).expanduser()
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    if not db_path.exists():
        raise SystemExit(f"db not found: {db_path}")

    backfill(db_path, user, limit=args.limit, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
