#!/usr/bin/env python3
"""
Per-trade realized edge report for Polymarket BTC up/down fills.

Definition used in this report (BUY fills only):
  realized_edge_per_share = payoff_per_share - fill_price - fee_per_share
  realized_edge_usdc      = realized_edge_per_share * fill_qty

where payoff_per_share is 1.0 if the bought token's outcome matches settlement
result, otherwise 0.0.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


GAMMA_MARKETS_BY_SLUG = "https://gamma-api.polymarket.com/markets?slug={slug}"


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


def _norm_outcome(x: object) -> str:
    txt = str(x or "").strip().upper()
    if txt in {"UP", "YES"}:
        return "UP"
    if txt in {"DOWN", "NO"}:
        return "DOWN"
    return txt


def _safe_json_loads(s: object) -> object:
    if s is None:
        return None
    try:
        return json.loads(str(s))
    except Exception:
        return None


def _fetch_market_by_slug(slug: str, timeout_sec: float = 15.0) -> Optional[dict]:
    url = GAMMA_MARKETS_BY_SLUG.format(slug=urllib.parse.quote(slug, safe=""))
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "curl/8.7.1",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8")
    parsed = json.loads(raw)
    if isinstance(parsed, list) and parsed:
        first = parsed[0]
        if isinstance(first, dict):
            return first
    return None


def _extract_token_outcome_map(market: dict) -> Dict[str, str]:
    token_ids_raw = market.get("clobTokenIds")
    outcomes_raw = market.get("outcomes")
    token_ids = _safe_json_loads(token_ids_raw)
    outcomes = _safe_json_loads(outcomes_raw)
    if not isinstance(token_ids, list) or not isinstance(outcomes, list):
        return {}
    n = min(len(token_ids), len(outcomes))
    out: Dict[str, str] = {}
    for i in range(n):
        token_id = str(token_ids[i])
        outcome = _norm_outcome(outcomes[i])
        if token_id:
            out[token_id] = outcome
    return out


@dataclass
class FillRow:
    id: int
    ts: str
    run_id: str
    client_order_id: str
    token_id: str
    instrument_id: str
    side: str
    price: float
    qty: float
    commission_usdc: float
    expected_net_usdc: Optional[float]
    directional_edge_ps_submit: Optional[float]
    directional_edge_usdc_submit: Optional[float]
    p_fair_submit: Optional[float]
    fee_ps_submit: Optional[float]
    other_cost_ps_submit: Optional[float]
    exec_penalty_usdc_submit: Optional[float]
    robust_net_usdc_submit: Optional[float]


def _iter_buy_fills(conn: sqlite3.Connection, run_id: Optional[str], cutoff_iso: Optional[str]) -> Iterable[FillRow]:
    where: List[str] = ["event_type='ORDER_FILLED'"]
    params: List[object] = []
    if run_id:
        where.append("run_id=?")
        params.append(run_id)
    if cutoff_iso:
        where.append("ts>=?")
        params.append(cutoff_iso)

    sql = f"""
        SELECT
          id, ts, run_id, client_order_id, token_id, instrument_id, side,
          price, qty, commission_usdc, expected_net_usdc, payload_json
        FROM order_events
        WHERE {" AND ".join(where)}
        ORDER BY id ASC
    """
    cur = conn.execute(sql, tuple(params))
    for row in cur.fetchall():
        side = _norm_side(row["side"])
        if side != "buy":
            continue
        price = float(row["price"] or 0.0)
        qty = float(row["qty"] or 0.0)
        if price <= 0.0 or qty <= 0.0:
            continue
        payload = _safe_json_loads(row["payload_json"])
        payload_d = payload if isinstance(payload, dict) else {}
        yield FillRow(
            id=int(row["id"]),
            ts=str(row["ts"]),
            run_id=str(row["run_id"]),
            client_order_id=str(row["client_order_id"] or ""),
            token_id=str(row["token_id"] or ""),
            instrument_id=str(row["instrument_id"] or ""),
            side=side,
            price=price,
            qty=qty,
            commission_usdc=float(row["commission_usdc"] or 0.0),
            expected_net_usdc=(float(row["expected_net_usdc"]) if row["expected_net_usdc"] is not None else None),
            directional_edge_ps_submit=(
                float(payload_d["directional_edge_ps_submit"])
                if payload_d.get("directional_edge_ps_submit") is not None
                else None
            ),
            directional_edge_usdc_submit=(
                float(payload_d["directional_edge_usdc_submit"])
                if payload_d.get("directional_edge_usdc_submit") is not None
                else None
            ),
            p_fair_submit=(
                float(payload_d["p_fair_submit"])
                if payload_d.get("p_fair_submit") is not None
                else None
            ),
            fee_ps_submit=(
                float(payload_d["fee_ps_submit"])
                if payload_d.get("fee_ps_submit") is not None
                else None
            ),
            other_cost_ps_submit=(
                float(payload_d["other_cost_ps_submit"])
                if payload_d.get("other_cost_ps_submit") is not None
                else None
            ),
            exec_penalty_usdc_submit=(
                float(payload_d["exec_penalty_usdc_submit"])
                if payload_d.get("exec_penalty_usdc_submit") is not None
                else None
            ),
            robust_net_usdc_submit=(
                float(payload_d["robust_net_usdc_submit"])
                if payload_d.get("robust_net_usdc_submit") is not None
                else None
            ),
        )


def _load_settlements(conn: sqlite3.Connection, run_id: Optional[str], cutoff_iso: Optional[str]) -> Dict[str, str]:
    where: List[str] = ["event_type='MARKET_SETTLEMENT'"]
    params: List[object] = []
    if run_id:
        where.append("run_id=?")
        params.append(run_id)
    if cutoff_iso:
        where.append("ts>=?")
        params.append(cutoff_iso)

    sql = f"SELECT payload_json FROM strategy_events WHERE {' AND '.join(where)}"
    out: Dict[str, str] = {}
    for row in conn.execute(sql, tuple(params)).fetchall():
        payload = _safe_json_loads(row["payload_json"])
        if not isinstance(payload, dict):
            continue
        slug = str(payload.get("slug") or "")
        outcome = _norm_outcome(payload.get("outcome"))
        if slug and outcome in {"UP", "DOWN"}:
            out[slug] = outcome
    return out


def _fmt(x: float, digits: int = 6) -> str:
    return f"{x:.{digits}f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute realized edge per BUY fill from settled BTC up/down markets")
    parser.add_argument("--db", default="./logs/trade_journal.db", help="SQLite DB path")
    parser.add_argument("--run-id", default=None, help="Optional run_id filter")
    parser.add_argument("--hours", type=int, default=0, help="Lookback hours (<=0 means all time)")
    parser.add_argument(
        "--csv-out",
        default="./logs/reports/realized_edge_trades.csv",
        help="Output CSV path for per-trade rows",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found: {db_path}")
        return 1

    cutoff_iso = _to_iso_utc(args.hours)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    settlements = _load_settlements(conn, args.run_id, cutoff_iso)
    if not settlements:
        print("No MARKET_SETTLEMENT rows found in selected scope.")
        conn.close()
        return 1

    token_to_slug_outcome: Dict[str, Tuple[str, str]] = {}
    market_fetch_errors: List[str] = []
    for slug in sorted(settlements.keys()):
        try:
            market = _fetch_market_by_slug(slug)
            if market is None:
                market_fetch_errors.append(f"{slug}: empty response")
                continue
            mapping = _extract_token_outcome_map(market)
            if not mapping:
                market_fetch_errors.append(f"{slug}: no token/outcome map")
                continue
            for token_id, outcome in mapping.items():
                token_to_slug_outcome[token_id] = (slug, outcome)
        except Exception as e:
            market_fetch_errors.append(f"{slug}: {e}")

    rows_out: List[dict] = []
    unknown_token_rows = 0
    unresolved_rows = 0
    for fill in _iter_buy_fills(conn, args.run_id, cutoff_iso):
        meta = token_to_slug_outcome.get(fill.token_id)
        if meta is None:
            unknown_token_rows += 1
            continue
        slug, token_outcome = meta
        settle_outcome = settlements.get(slug)
        if settle_outcome not in {"UP", "DOWN"}:
            unresolved_rows += 1
            continue

        fee_per_share = fill.commission_usdc / fill.qty if fill.qty > 0 else 0.0
        payoff_per_share = 1.0 if token_outcome == settle_outcome else 0.0
        realized_edge_per_share = payoff_per_share - fill.price - fee_per_share
        realized_edge_usdc = realized_edge_per_share * fill.qty
        expected_edge_per_share = (
            (fill.expected_net_usdc / fill.qty) if (fill.expected_net_usdc is not None and fill.qty > 0) else None
        )

        rows_out.append(
            {
                "fill_id": fill.id,
                "ts": fill.ts,
                "run_id": fill.run_id,
                "client_order_id": fill.client_order_id,
                "slug": slug,
                "token_id": fill.token_id,
                "token_outcome": token_outcome,
                "settlement_outcome": settle_outcome,
                "price": fill.price,
                "qty": fill.qty,
                "commission_usdc": fill.commission_usdc,
                "fee_per_share": fee_per_share,
                "payoff_per_share": payoff_per_share,
                "realized_edge_per_share": realized_edge_per_share,
                "realized_edge_usdc": realized_edge_usdc,
                "expected_net_usdc": fill.expected_net_usdc,
                "expected_edge_per_share_proxy": expected_edge_per_share,
                "directional_edge_ps_submit": fill.directional_edge_ps_submit,
                "directional_edge_usdc_submit": fill.directional_edge_usdc_submit,
                "p_fair_submit": fill.p_fair_submit,
                "fee_ps_submit": fill.fee_ps_submit,
                "other_cost_ps_submit": fill.other_cost_ps_submit,
                "exec_penalty_usdc_submit": fill.exec_penalty_usdc_submit,
                "robust_net_usdc_submit": fill.robust_net_usdc_submit,
            }
        )

    conn.close()

    out_path = Path(args.csv_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "fill_id",
        "ts",
        "run_id",
        "client_order_id",
        "slug",
        "token_id",
        "token_outcome",
        "settlement_outcome",
        "price",
        "qty",
        "commission_usdc",
        "fee_per_share",
        "payoff_per_share",
        "realized_edge_per_share",
        "realized_edge_usdc",
        "expected_net_usdc",
        "expected_edge_per_share_proxy",
        "directional_edge_ps_submit",
        "directional_edge_usdc_submit",
        "p_fair_submit",
        "fee_ps_submit",
        "other_cost_ps_submit",
        "exec_penalty_usdc_submit",
        "robust_net_usdc_submit",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_out)

    total = len(rows_out)
    if total == 0:
        print("No matched BUY fills for settled markets in selected scope.")
        print(f"unknown_token_rows={unknown_token_rows} unresolved_rows={unresolved_rows}")
        if market_fetch_errors:
            print("market_fetch_errors:")
            for err in market_fetch_errors[:20]:
                print(f"- {err}")
        return 1

    sum_edge = sum(r["realized_edge_usdc"] for r in rows_out)
    avg_edge_per_share = sum(r["realized_edge_per_share"] for r in rows_out) / total
    wins = sum(1 for r in rows_out if r["realized_edge_usdc"] > 0)
    losses = sum(1 for r in rows_out if r["realized_edge_usdc"] < 0)
    win_rate = (wins / total) * 100.0

    def _group_stats(outcome_key: str) -> Tuple[int, float, float]:
        subset = [r for r in rows_out if r["token_outcome"] == outcome_key]
        if not subset:
            return (0, 0.0, 0.0)
        return (
            len(subset),
            sum(r["realized_edge_usdc"] for r in subset),
            sum(r["realized_edge_per_share"] for r in subset) / len(subset),
        )

    up_n, up_sum, up_avg_ps = _group_stats("UP")
    down_n, down_sum, down_avg_ps = _group_stats("DOWN")
    directional_rows = [r for r in rows_out if r["directional_edge_ps_submit"] is not None]
    directional_n = len(directional_rows)
    directional_avg_ps = (
        sum(float(r["directional_edge_ps_submit"]) for r in directional_rows) / directional_n
        if directional_n > 0
        else 0.0
    )
    directional_avg_usdc = (
        sum(float(r["directional_edge_usdc_submit"]) for r in directional_rows if r["directional_edge_usdc_submit"] is not None) / directional_n
        if directional_n > 0
        else 0.0
    )

    print("=" * 96)
    print("Realized Edge Report (BUY fills only)")
    print("=" * 96)
    print(f"db: {db_path}")
    print(f"run_id: {args.run_id or '(all)'}")
    print(f"lookback_hours: {args.hours if args.hours > 0 else '(all time)'}")
    if cutoff_iso:
        print(f"cutoff_utc: {cutoff_iso}")
    print(f"settled_markets: {len(settlements)}")
    print(f"token_map_size: {len(token_to_slug_outcome)}")
    print(f"matched_fills: {total}")
    print(f"wins/losses: {wins}/{losses} (win_rate={win_rate:.2f}%)")
    print(f"sum_realized_edge_usdc: {_fmt(sum_edge)}")
    print(f"avg_realized_edge_per_share: {_fmt(avg_edge_per_share)}")
    print(f"csv_out: {out_path}")
    print("")
    print("By bought outcome token:")
    print(f"- UP   fills={up_n:<5d} sum_edge_usdc={_fmt(up_sum):>12} avg_edge_per_share={_fmt(up_avg_ps):>10}")
    print(f"- DOWN fills={down_n:<5d} sum_edge_usdc={_fmt(down_sum):>12} avg_edge_per_share={_fmt(down_avg_ps):>10}")
    print("")
    print("Directional edge snapshot coverage (from ORDER_FILLED payload):")
    print(f"- rows_with_directional_snapshot: {directional_n}/{total}")
    if directional_n > 0:
        print(f"- avg_directional_edge_ps_submit: {_fmt(directional_avg_ps)}")
        print(f"- avg_directional_edge_usdc_submit: {_fmt(directional_avg_usdc)}")
    print("")
    print(f"unmatched_token_rows: {unknown_token_rows}")
    print(f"unresolved_rows: {unresolved_rows}")
    if market_fetch_errors:
        print(f"market_fetch_errors: {len(market_fetch_errors)}")
        for err in market_fetch_errors[:20]:
            print(f"- {err}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
