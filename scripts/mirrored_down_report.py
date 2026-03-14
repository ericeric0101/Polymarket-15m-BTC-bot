#!/usr/bin/env python3
"""
Estimate how a mirrored DOWN bot would have performed over the same fill stream.

Model assumptions:
1. The mirrored bot trades the opposite outcome token with the same timestamps,
   order types, and quantities as the recorded UP bot fills.
2. Mirrored fill price is approximated as (1 - actual_fill_price).
3. Mirrored commission is scaled by the observed effective fee rate for that fill:
      fee_rate = actual_commission / (actual_price * qty)
      mirror_commission = fee_rate * mirror_price * qty
4. Settlement is reconstructed per traded token bucket using the same fill sequence,
   with any residual mirrored inventory resolving to DOWN when the market settles DOWN.

This is intentionally a conservative offline estimate. It does not model queue position,
fill probability, or whether the mirrored quotes would really have executed.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


EPS = 1e-9


def _to_iso_utc(hours: int) -> Optional[str]:
    if hours <= 0:
        return None
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _norm_side(side_val: object) -> str:
    txt = str(side_val or "").strip().lower()
    if txt in {"1", "buy", "bid"} or "buy" in txt:
        return "buy"
    if txt in {"2", "sell", "ask"} or "sell" in txt:
        return "sell"
    return ""


def _safe_json_loads(s: object) -> object:
    if s is None:
        return None
    try:
        return json.loads(str(s))
    except Exception:
        return None


@dataclass
class Fill:
    id: int
    ts: str
    run_id: str
    token_id: str
    client_order_id: str
    side: str
    price: float
    qty: float
    commission_usdc: float


@dataclass
class Settlement:
    id: int
    ts: str
    run_id: str
    slug: str
    outcome: str
    inventory_shares: float


@dataclass
class TokenSummary:
    run_id: str
    token_id: str
    first_ts: str
    last_ts: str
    buy_fills: int
    sell_fills: int
    buy_qty: float
    sell_qty: float
    mirror_realized_pnl: float
    mirror_settlement_pnl: float
    mirror_total_pnl: float
    mirror_open_qty: float
    starts_with_sell: bool
    settlement_slug: str
    settlement_outcome: str
    matched_settlement_ts: str


def _iter_fills(conn: sqlite3.Connection, cutoff_iso: Optional[str]) -> Iterable[Fill]:
    where: List[str] = ["event_type='ORDER_FILLED'"]
    params: List[object] = []
    if cutoff_iso:
        where.append("ts>=?")
        params.append(cutoff_iso)
    sql = f"""
        SELECT id, ts, run_id, token_id, client_order_id, side, price, qty, commission_usdc
        FROM order_events
        WHERE {" AND ".join(where)}
        ORDER BY id ASC
    """
    for row in conn.execute(sql, tuple(params)).fetchall():
        price = float(row["price"] or 0.0)
        qty = float(row["qty"] or 0.0)
        if price <= 0.0 or qty <= 0.0:
            continue
        yield Fill(
            id=int(row["id"]),
            ts=str(row["ts"]),
            run_id=str(row["run_id"]),
            token_id=str(row["token_id"] or ""),
            client_order_id=str(row["client_order_id"] or ""),
            side=_norm_side(row["side"]),
            price=price,
            qty=qty,
            commission_usdc=float(row["commission_usdc"] or 0.0),
        )


def _load_settlements(conn: sqlite3.Connection, cutoff_iso: Optional[str]) -> Dict[str, List[Settlement]]:
    where: List[str] = ["event_type='MARKET_SETTLEMENT'"]
    params: List[object] = []
    if cutoff_iso:
        where.append("ts>=?")
        params.append(cutoff_iso)
    sql = f"""
        SELECT id, ts, run_id, payload_json
        FROM strategy_events
        WHERE {" AND ".join(where)}
        ORDER BY id ASC
    """
    out: Dict[str, List[Settlement]] = {}
    for row in conn.execute(sql, tuple(params)).fetchall():
        payload = _safe_json_loads(row["payload_json"])
        if not isinstance(payload, dict):
            continue
        settlement = Settlement(
            id=int(row["id"]),
            ts=str(row["ts"]),
            run_id=str(row["run_id"]),
            slug=str(payload.get("slug") or ""),
            outcome=str(payload.get("outcome") or "").strip().upper(),
            inventory_shares=float(payload.get("inventory_shares") or 0.0),
        )
        out.setdefault(settlement.run_id, []).append(settlement)
    return out


def _mirror_price(price: float) -> float:
    return max(0.0, min(1.0, 1.0 - price))


def _mirror_commission(fill: Fill) -> float:
    notional = fill.price * fill.qty
    if notional <= EPS or fill.commission_usdc <= EPS:
        return 0.0
    fee_rate = fill.commission_usdc / notional
    return fee_rate * (_mirror_price(fill.price) * fill.qty)


def _match_settlement(
    settlements_by_run: Dict[str, List[Settlement]],
    used_ids: set[int],
    run_id: str,
    last_ts: str,
) -> Optional[Settlement]:
    for settlement in settlements_by_run.get(run_id, []):
        if settlement.id in used_ids:
            continue
        if settlement.ts >= last_ts:
            used_ids.add(settlement.id)
            return settlement
    return None


def _summarize_token_bucket(
    fills: List[Fill],
    settlement: Optional[Settlement],
) -> TokenSummary:
    inv_qty = 0.0
    inv_cost = 0.0
    realized = 0.0
    buy_fills = 0
    sell_fills = 0
    buy_qty = 0.0
    sell_qty = 0.0

    for fill in fills:
        px = _mirror_price(fill.price)
        commission = _mirror_commission(fill)
        if fill.side == "buy":
            buy_fills += 1
            buy_qty += fill.qty
            inv_qty += fill.qty
            inv_cost += (px * fill.qty) + commission
            continue
        if fill.side == "sell":
            sell_fills += 1
            sell_qty += fill.qty
            qty = min(fill.qty, inv_qty)
            if qty <= EPS:
                continue
            avg_cost = inv_cost / inv_qty if inv_qty > EPS else 0.0
            cost_out = avg_cost * qty
            proceeds = (px * qty) - commission
            realized += proceeds - cost_out
            inv_qty -= qty
            inv_cost -= cost_out
            if inv_qty <= EPS:
                inv_qty = 0.0
                inv_cost = 0.0

    settlement_pnl = 0.0
    settlement_slug = ""
    settlement_outcome = ""
    settlement_ts = ""
    if settlement is not None and inv_qty > EPS:
        settlement_slug = settlement.slug
        settlement_outcome = settlement.outcome
        settlement_ts = settlement.ts
        redeem_value = inv_qty if settlement.outcome == "DOWN" else 0.0
        settlement_pnl = redeem_value - inv_cost
        inv_qty = 0.0
        inv_cost = 0.0

    return TokenSummary(
        run_id=fills[0].run_id,
        token_id=fills[0].token_id,
        first_ts=fills[0].ts,
        last_ts=fills[-1].ts,
        buy_fills=buy_fills,
        sell_fills=sell_fills,
        buy_qty=buy_qty,
        sell_qty=sell_qty,
        mirror_realized_pnl=realized,
        mirror_settlement_pnl=settlement_pnl,
        mirror_total_pnl=realized + settlement_pnl,
        mirror_open_qty=inv_qty,
        starts_with_sell=(fills[0].side == "sell"),
        settlement_slug=settlement_slug,
        settlement_outcome=settlement_outcome,
        matched_settlement_ts=settlement_ts,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Mirror DOWN bot estimate from trade_journal.db")
    parser.add_argument("--db", default="./logs/trade_journal.db", help="SQLite DB path")
    parser.add_argument("--hours", type=int, default=12, help="Lookback hours (<=0 for all)")
    parser.add_argument("--limit", type=int, default=20, help="Max token buckets to print")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found: {db_path}")
        return 1

    cutoff_iso = _to_iso_utc(args.hours) if args.hours and args.hours > 0 else None

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 2000")

    fills_by_bucket: Dict[Tuple[str, str], List[Fill]] = {}
    for fill in _iter_fills(conn, cutoff_iso):
        if not fill.token_id:
            continue
        fills_by_bucket.setdefault((fill.run_id, fill.token_id), []).append(fill)

    settlements_by_run = _load_settlements(conn, cutoff_iso)
    used_settlement_ids: set[int] = set()
    summaries: List[TokenSummary] = []

    for bucket_key in sorted(fills_by_bucket.keys(), key=lambda k: fills_by_bucket[k][0].id):
        fills = fills_by_bucket[bucket_key]
        probe = _summarize_token_bucket(fills, settlement=None)
        settlement = None
        if probe.mirror_open_qty > EPS:
            settlement = _match_settlement(
                settlements_by_run=settlements_by_run,
                used_ids=used_settlement_ids,
                run_id=fills[0].run_id,
                last_ts=fills[-1].ts,
            )
        summaries.append(_summarize_token_bucket(fills, settlement))

    actual = conn.execute(
        """
        SELECT
          SUM(COALESCE(json_extract(payload_json, '$.realized_net_usdc'), 0)) AS realized_pnl,
          SUM(COALESCE(commission_usdc, 0)) AS commission,
          COUNT(*) AS fills
        FROM order_events
        WHERE event_type='ORDER_FILLED'
          AND (? IS NULL OR ts >= ?)
        """,
        (cutoff_iso, cutoff_iso),
    ).fetchone()
    actual_settlement = conn.execute(
        """
        SELECT
          SUM(COALESCE(json_extract(payload_json, '$.settlement_pnl_usdc'), 0)) AS settlement_pnl,
          COUNT(*) AS settlements
        FROM strategy_events
        WHERE event_type='MARKET_SETTLEMENT'
          AND (? IS NULL OR ts >= ?)
        """,
        (cutoff_iso, cutoff_iso),
    ).fetchone()
    conn.close()

    total_realized = sum(s.mirror_realized_pnl for s in summaries)
    total_settlement = sum(s.mirror_settlement_pnl for s in summaries)
    total_open_qty = sum(s.mirror_open_qty for s in summaries)
    unmatched_open = [s for s in summaries if s.mirror_open_qty > EPS]
    sell_first = [s for s in summaries if s.starts_with_sell]
    down_settled = [s for s in summaries if s.settlement_outcome == "DOWN"]
    up_settled = [s for s in summaries if s.settlement_outcome == "UP"]

    print("=" * 88)
    print("Mirrored DOWN Bot Estimate")
    print("=" * 88)
    print(f"DB: {db_path}")
    print(f"Lookback hours: {args.hours if args.hours > 0 else 'ALL'}")
    if cutoff_iso:
        print(f"UTC cutoff: {cutoff_iso}")
    print()
    print("[Model]")
    print("mirror_fill_price = 1 - actual_fill_price")
    print("mirror_fee = observed_fee_rate * mirror_notional")
    print("same timestamps / same qty / same order-type path as actual bot")
    print()
    print("[Actual bot]")
    print(f"fills={int(actual['fills'] or 0)}")
    print(f"realized_pnl_usdc={float(actual['realized_pnl'] or 0.0):.6f}")
    print(f"settlement_pnl_usdc={float(actual_settlement['settlement_pnl'] or 0.0):.6f}")
    print(f"combined_pnl_usdc={float(actual['realized_pnl'] or 0.0) + float(actual_settlement['settlement_pnl'] or 0.0):.6f}")
    print()
    print("[Mirrored DOWN estimate]")
    print(f"token_buckets={len(summaries)}")
    print(f"realized_pnl_usdc={total_realized:.6f}")
    print(f"settlement_pnl_usdc={total_settlement:.6f}")
    print(f"combined_pnl_usdc={total_realized + total_settlement:.6f}")
    print(f"matched_down_settlements={len(down_settled)}")
    print(f"matched_up_settlements={len(up_settled)}")
    print(f"open_inventory_shares_without_settlement={total_open_qty:.6f}")
    print(f"sell_first_buckets_at_window_start={len(sell_first)}")
    print()
    print("[Delta vs actual]")
    actual_combined = float(actual['realized_pnl'] or 0.0) + float(actual_settlement['settlement_pnl'] or 0.0)
    mirror_combined = total_realized + total_settlement
    print(f"mirror_minus_actual_usdc={mirror_combined - actual_combined:.6f}")
    print()
    print("[Largest token buckets by mirror total pnl]")
    ranked = sorted(summaries, key=lambda s: s.mirror_total_pnl, reverse=True)
    for summary in ranked[: args.limit]:
        token_short = summary.token_id[:12]
        print(
            f"{summary.first_ts} run={summary.run_id} token={token_short} "
            f"buy_qty={summary.buy_qty:.4f} sell_qty={summary.sell_qty:.4f} "
            f"mirror_realized={summary.mirror_realized_pnl:.6f} "
            f"mirror_settle={summary.mirror_settlement_pnl:.6f} "
            f"mirror_total={summary.mirror_total_pnl:.6f} "
            f"settlement={summary.settlement_outcome or 'OPEN'} "
            f"slug={summary.settlement_slug or '-'}"
        )

    if unmatched_open:
        print()
        print("[Open buckets without settlement in window]")
        for summary in unmatched_open[: args.limit]:
            print(
                f"{summary.first_ts} run={summary.run_id} token={summary.token_id[:12]} "
                f"open_qty={summary.mirror_open_qty:.6f}"
            )

    if sell_first:
        print()
        print("[Buckets that start with a sell inside the window]")
        for summary in sell_first[: args.limit]:
            print(
                f"{summary.first_ts} run={summary.run_id} token={summary.token_id[:12]} "
                f"sell_qty={summary.sell_qty:.6f} mirror_total={summary.mirror_total_pnl:.6f}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
