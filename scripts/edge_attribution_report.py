#!/usr/bin/env python3
"""
Edge attribution report for trade_journal.db.

This script summarizes where performance is likely coming from:
- Execution quality (fill/reject/cancel rates)
- Fee burden (commission and implied bps)
- Expected edge proxy (expected_net_usdc on submitted/filled orders)
- Taker-exit usage and risk controls
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:.2f}%"


def _fmt_num(x: Optional[float], digits: int = 4) -> str:
    if x is None:
        return "n/a"
    return f"{x:.{digits}f}"


def _build_where(run_id: Optional[str], cutoff_iso: Optional[str]) -> Tuple[str, List[Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    if cutoff_iso:
        clauses.append("ts >= ?")
        params.append(cutoff_iso)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def _print_header(title: str) -> None:
    print("\n" + "=" * 90)
    print(title)
    print("=" * 90)


def main() -> int:
    parser = argparse.ArgumentParser(description="Edge attribution report from trade_journal.db")
    parser.add_argument("--db", default="./logs/trade_journal.db", help="SQLite DB path")
    parser.add_argument("--run-id", default=None, help="Filter by run_id")
    parser.add_argument("--hours", type=int, default=24, help="Lookback window in hours (ignored if <=0)")
    parser.add_argument("--top", type=int, default=10, help="Top N rows for breakdown tables")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found: {db_path}")
        return 1

    cutoff_iso: Optional[str] = None
    if args.hours and args.hours > 0:
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).isoformat()

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    where, params = _build_where(args.run_id, cutoff_iso)

    _print_header("Scope")
    print(f"db: {db_path}")
    print(f"run_id: {args.run_id or '(all)'}")
    print(f"lookback_hours: {args.hours if args.hours and args.hours > 0 else '(all time)'}")
    if cutoff_iso:
        print(f"cutoff_utc: {cutoff_iso}")

    latest = cur.execute("SELECT MAX(ts) AS ts FROM order_events").fetchone()
    print(f"latest_order_event_ts: {latest['ts'] if latest and latest['ts'] else 'n/a'}")

    summary = cur.execute(
        f"""
        SELECT
          COUNT(*) AS total_events,
          SUM(CASE WHEN event_type='ORDER_SUBMIT' THEN 1 ELSE 0 END) AS submits,
          SUM(CASE WHEN event_type='ORDER_FILLED' THEN 1 ELSE 0 END) AS fills,
          SUM(CASE WHEN event_type IN ('ORDER_DENIED','ORDER_REJECTED') THEN 1 ELSE 0 END) AS rejects,
          SUM(CASE WHEN event_type LIKE 'ORDER_CANCEL%' OR event_type='ORDER_CANCELED' THEN 1 ELSE 0 END) AS cancels,
          SUM(CASE WHEN event_type='ORDER_SKIP_INVENTORY_CAP' THEN 1 ELSE 0 END) AS inv_cap_skips,
          SUM(CASE WHEN event_type='ORDER_TAKER_EXIT_SUBMIT' THEN 1 ELSE 0 END) AS taker_exit_submits,
          SUM(CASE WHEN event_type='ORDER_FILLED' THEN COALESCE(commission_usdc,0) ELSE 0 END) AS commission_total,
          SUM(CASE WHEN event_type='ORDER_FILLED' THEN COALESCE(price,0)*COALESCE(qty,0) ELSE 0 END) AS fill_notional,
          AVG(CASE WHEN event_type='ORDER_SUBMIT' THEN expected_net_usdc END) AS avg_expected_net_submit,
          SUM(CASE WHEN event_type='ORDER_SUBMIT' THEN COALESCE(expected_net_usdc,0) ELSE 0 END) AS sum_expected_net_submit
        FROM order_events
        {where}
        """,
        params,
    ).fetchone()

    submits = int(summary["submits"] or 0)
    fills = int(summary["fills"] or 0)
    rejects = int(summary["rejects"] or 0)
    cancels = int(summary["cancels"] or 0)
    inv_cap_skips = int(summary["inv_cap_skips"] or 0)
    taker_exit_submits = int(summary["taker_exit_submits"] or 0)
    commission_total = float(summary["commission_total"] or 0.0)
    fill_notional = float(summary["fill_notional"] or 0.0)
    avg_expected_net_submit = summary["avg_expected_net_submit"]
    sum_expected_net_submit = float(summary["sum_expected_net_submit"] or 0.0)

    fill_rate = (fills / submits) if submits > 0 else None
    reject_rate = (rejects / submits) if submits > 0 else None
    cancel_to_submit = (cancels / submits) if submits > 0 else None
    inv_cap_skip_rate = (inv_cap_skips / submits) if submits > 0 else None
    taker_exit_rate_submit = (taker_exit_submits / submits) if submits > 0 else None
    taker_exit_rate_fill = (taker_exit_submits / fills) if fills > 0 else None
    fee_bps_on_fills = ((commission_total / fill_notional) * 10000.0) if fill_notional > 0 else None

    _print_header("Execution Quality")
    print(f"total_events: {int(summary['total_events'] or 0)}")
    print(f"submits: {submits}")
    print(f"fills: {fills}")
    print(f"rejects: {rejects}")
    print(f"cancels: {cancels}")
    print(f"fill_rate: {_fmt_pct(fill_rate)}")
    print(f"reject_rate: {_fmt_pct(reject_rate)}")
    print(f"cancel_to_submit: {_fmt_pct(cancel_to_submit)}")
    print(f"inventory_cap_skip_rate: {_fmt_pct(inv_cap_skip_rate)}")

    _print_header("Edge & Cost Proxy")
    print(f"avg_expected_net_submit_usdc: {_fmt_num(float(avg_expected_net_submit) if avg_expected_net_submit is not None else None, 6)}")
    print(f"sum_expected_net_submit_usdc: {_fmt_num(sum_expected_net_submit, 6)}")
    print(f"commission_total_usdc: {_fmt_num(commission_total, 6)}")
    print(f"fill_notional_usdc: {_fmt_num(fill_notional, 6)}")
    print(f"implied_fee_bps_on_fills: {_fmt_num(fee_bps_on_fills, 2)}")

    filled_expected = cur.execute(
        f"""
        WITH submits AS (
          SELECT run_id, client_order_id, MAX(expected_net_usdc) AS expected_net
          FROM order_events
          {where} AND event_type='ORDER_SUBMIT' AND client_order_id IS NOT NULL
          GROUP BY run_id, client_order_id
        ),
        filled_orders AS (
          SELECT run_id, client_order_id
          FROM order_events
          {where} AND event_type='ORDER_FILLED' AND client_order_id IS NOT NULL
          GROUP BY run_id, client_order_id
        )
        SELECT
          COUNT(*) AS filled_order_count,
          SUM(COALESCE(s.expected_net,0)) AS sum_expected_net_filled_orders
        FROM filled_orders f
        LEFT JOIN submits s
          ON s.run_id=f.run_id AND s.client_order_id=f.client_order_id
        """,
        params + params,
    ).fetchone()
    print(f"filled_order_count: {int(filled_expected['filled_order_count'] or 0)}")
    print(f"sum_expected_net_filled_orders_usdc: {_fmt_num(float(filled_expected['sum_expected_net_filled_orders'] or 0.0), 6)}")

    _print_header("Taker Exit Usage")
    print(f"taker_exit_submits: {taker_exit_submits}")
    print(f"taker_exit_per_submit: {_fmt_pct(taker_exit_rate_submit)}")
    print(f"taker_exit_per_fill: {_fmt_pct(taker_exit_rate_fill)}")

    _print_header("Top Rejection Reasons")
    rows = cur.execute(
        f"""
        SELECT reason, COUNT(*) AS cnt
        FROM order_events
        {where} AND event_type IN ('ORDER_DENIED','ORDER_REJECTED')
        GROUP BY reason
        ORDER BY cnt DESC
        LIMIT ?
        """,
        params + [args.top],
    ).fetchall()
    if not rows:
        print("(none)")
    else:
        for r in rows:
            reason = r["reason"] if r["reason"] else "(empty)"
            print(f"- {r['cnt']:>5} | {reason}")

    _print_header("Per-Run Summary")
    rows = cur.execute(
        f"""
        SELECT
          run_id,
          MIN(ts) AS first_ts,
          MAX(ts) AS last_ts,
          SUM(CASE WHEN event_type='ORDER_SUBMIT' THEN 1 ELSE 0 END) AS submits,
          SUM(CASE WHEN event_type='ORDER_FILLED' THEN 1 ELSE 0 END) AS fills,
          SUM(CASE WHEN event_type IN ('ORDER_DENIED','ORDER_REJECTED') THEN 1 ELSE 0 END) AS rejects,
          SUM(CASE WHEN event_type='ORDER_TAKER_EXIT_SUBMIT' THEN 1 ELSE 0 END) AS taker_exit_submits,
          SUM(CASE WHEN event_type='ORDER_FILLED' THEN COALESCE(commission_usdc,0) ELSE 0 END) AS commission_total
        FROM order_events
        {where}
        GROUP BY run_id
        ORDER BY last_ts DESC
        LIMIT ?
        """,
        params + [args.top],
    ).fetchall()
    if not rows:
        print("(none)")
    else:
        for r in rows:
            s = int(r["submits"] or 0)
            f = int(r["fills"] or 0)
            rej = int(r["rejects"] or 0)
            tx = int(r["taker_exit_submits"] or 0)
            fill_rate_run = (f / s) if s > 0 else None
            reject_rate_run = (rej / s) if s > 0 else None
            print(
                f"- run={r['run_id']} "
                f"submits={s} fills={f} rejects={rej} tx_exit={tx} "
                f"fill_rate={_fmt_pct(fill_rate_run)} reject_rate={_fmt_pct(reject_rate_run)} "
                f"commission={_fmt_num(float(r['commission_total'] or 0.0), 4)} "
                f"first={r['first_ts']} last={r['last_ts']}"
            )

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

