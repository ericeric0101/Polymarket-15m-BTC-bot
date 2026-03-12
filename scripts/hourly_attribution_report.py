#!/usr/bin/env python3
"""
Hourly attribution report for recent performance.

Breakdown per hour:
- Fill realized net
- Settlement pnl
- Combined pnl
- Fees / implied bps
- Taker exits
- Directional edge snapshot averages
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple


def _fmt(x: float, digits: int = 6) -> str:
    return f"{x:.{digits}f}"


def _cutoff_iso_utc(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _norm_side(side: str) -> str:
    s = str(side or "").strip().lower()
    if s in {"1", "buy", "bid"}:
        return "buy"
    if s in {"2", "sell", "ask"}:
        return "sell"
    return ""


def _safe_float(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _compute_monitor_kpis(
    cur: sqlite3.Cursor,
    cutoff_iso: str,
    high_cost_threshold: float,
) -> Dict[str, float]:
    taker_sell_row = cur.execute(
        """
        SELECT
          SUM(CASE WHEN side IN ('2','SELL','sell') THEN 1 ELSE 0 END) AS sell_fills,
          SUM(
            CASE
              WHEN side IN ('2','SELL','sell')
               AND COALESCE(CAST(json_extract(payload_json,'$.liquidity_side') AS TEXT), '') IN ('2','TAKER','taker')
              THEN 1 ELSE 0
            END
          ) AS taker_sell_fills
        FROM order_events
        WHERE event_type='ORDER_FILLED'
          AND ts >= ?
        """,
        (cutoff_iso,),
    ).fetchone()
    sell_fills = int(taker_sell_row["sell_fills"] or 0)
    taker_sell_fills = int(taker_sell_row["taker_sell_fills"] or 0)
    taker_sell_pct = (100.0 * taker_sell_fills / sell_fills) if sell_fills > 0 else 0.0

    phase_rows = cur.execute(
        """
        SELECT ts, json_extract(payload_json,'$.slug') AS slug
        FROM strategy_events
        WHERE event_type='MARKET_PHASE_CHANGE'
          AND ts >= ?
        ORDER BY ts
        """,
        (cutoff_iso,),
    ).fetchall()
    prev_phase_row = cur.execute(
        """
        SELECT ts, json_extract(payload_json,'$.slug') AS slug
        FROM strategy_events
        WHERE event_type='MARKET_PHASE_CHANGE'
          AND ts < ?
        ORDER BY ts DESC
        LIMIT 1
        """,
        (cutoff_iso,),
    ).fetchone()
    timeline: List[Tuple[str, str]] = []
    if prev_phase_row and prev_phase_row["slug"]:
        timeline.append((str(prev_phase_row["ts"]), str(prev_phase_row["slug"])))
    for r in phase_rows:
        slug = r["slug"]
        if slug:
            timeline.append((str(r["ts"]), str(slug)))
    timeline.sort(key=lambda x: x[0])
    phase_ts = [x[0] for x in timeline]

    fills = cur.execute(
        """
        SELECT ts, side, price, qty, payload_json
        FROM order_events
        WHERE event_type='ORDER_FILLED'
          AND ts >= ?
        ORDER BY ts
        """,
        (cutoff_iso,),
    ).fetchall()
    settlements = cur.execute(
        """
        SELECT
          json_extract(payload_json,'$.slug') AS slug,
          json_extract(payload_json,'$.outcome') AS outcome,
          COALESCE(json_extract(payload_json,'$.settlement_pnl_usdc'),0) AS settlement_pnl_usdc
        FROM strategy_events
        WHERE event_type='MARKET_SETTLEMENT'
          AND ts >= ?
        """,
        (cutoff_iso,),
    ).fetchall()

    by_slug: Dict[str, Dict[str, float]] = {}
    for row in fills:
        if not phase_ts:
            continue
        ts = str(row["ts"])
        i = bisect_right(phase_ts, ts) - 1
        if i < 0:
            continue
        slug = timeline[i][1]
        if not slug:
            continue
        side = _norm_side(str(row["side"]))
        px = _safe_float(row["price"], 0.0)
        qty = _safe_float(row["qty"], 0.0)
        payload = {}
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            payload = {}
        realized = _safe_float(payload.get("realized_net_usdc"), 0.0)
        d = by_slug.setdefault(
            slug,
            {
                "buy_notional": 0.0,
                "buy_qty": 0.0,
                "sell_notional": 0.0,
                "sell_qty": 0.0,
                "max_buy": 0.0,
                "realized": 0.0,
                "settlement": 0.0,
                "is_up": 0.0,
            },
        )
        if side == "buy":
            d["buy_notional"] += px * qty
            d["buy_qty"] += qty
            d["max_buy"] = max(d["max_buy"], px)
        elif side == "sell":
            d["sell_notional"] += px * qty
            d["sell_qty"] += qty
        d["realized"] += realized

    for row in settlements:
        slug = str(row["slug"] or "")
        if not slug:
            continue
        d = by_slug.setdefault(
            slug,
            {
                "buy_notional": 0.0,
                "buy_qty": 0.0,
                "sell_notional": 0.0,
                "sell_qty": 0.0,
                "max_buy": 0.0,
                "realized": 0.0,
                "settlement": 0.0,
                "is_up": 0.0,
            },
        )
        d["settlement"] = _safe_float(row["settlement_pnl_usdc"], 0.0)
        d["is_up"] = 1.0 if str(row["outcome"] or "").upper() == "UP" else 0.0

    high_cost_roundtrip_loss = 0.0
    high_cost_roundtrip_n = 0
    up_settle_but_sold_lower_loss = 0.0
    up_settle_but_sold_lower_n = 0
    for _, d in by_slug.items():
        if d["buy_qty"] <= 0 or d["sell_qty"] <= 0:
            continue
        avg_buy = d["buy_notional"] / d["buy_qty"]
        avg_sell = d["sell_notional"] / d["sell_qty"]
        if avg_sell >= avg_buy:
            continue
        combined = d["realized"] + d["settlement"]
        if d["max_buy"] >= high_cost_threshold:
            high_cost_roundtrip_n += 1
            high_cost_roundtrip_loss += combined
        if d["is_up"] > 0.5:
            up_settle_but_sold_lower_n += 1
            up_settle_but_sold_lower_loss += combined

    return {
        "taker_sell_pct": taker_sell_pct,
        "sell_fills": float(sell_fills),
        "taker_sell_fills": float(taker_sell_fills),
        "high_cost_roundtrip_loss": high_cost_roundtrip_loss,
        "high_cost_roundtrip_n": float(high_cost_roundtrip_n),
        "up_settle_but_sold_lower_loss": up_settle_but_sold_lower_loss,
        "up_settle_but_sold_lower_n": float(up_settle_but_sold_lower_n),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Hourly attribution report from trade_journal.db")
    ap.add_argument("--db", default="./logs/trade_journal.db", help="SQLite DB path")
    ap.add_argument("--hours", type=int, default=24, help="Lookback hours")
    ap.add_argument(
        "--high-cost-threshold",
        type=float,
        default=0.80,
        help="High-cost buy threshold for high_cost_roundtrip_loss KPI",
    )
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"DB not found: {db}")
        return 1

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cutoff_iso = _cutoff_iso_utc(args.hours)

    rows = cur.execute(
        """
        WITH fills AS (
          SELECT
            substr(ts,1,13) AS h,
            COUNT(*) AS fills,
            SUM(COALESCE(json_extract(payload_json,'$.realized_net_usdc'),0)) AS fill_realized_usdc,
            SUM(COALESCE(commission_usdc,0)) AS commissions_usdc,
            SUM(COALESCE(price,0) * COALESCE(qty,0)) AS fill_notional_usdc,
            AVG(COALESCE(json_extract(payload_json,'$.directional_edge_ps_submit'), NULL)) AS avg_directional_edge_ps,
            AVG(COALESCE(json_extract(payload_json,'$.directional_edge_usdc_submit'), NULL)) AS avg_directional_edge_usdc
          FROM order_events
          WHERE event_type='ORDER_FILLED'
            AND ts >= ?
          GROUP BY h
        ),
        sett AS (
          SELECT
            substr(ts,1,13) AS h,
            COUNT(*) AS settlements,
            SUM(COALESCE(json_extract(payload_json,'$.settlement_pnl_usdc'),0)) AS settlement_pnl_usdc
          FROM strategy_events
          WHERE event_type='MARKET_SETTLEMENT'
            AND ts >= ?
          GROUP BY h
        ),
        exits AS (
          SELECT
            substr(ts,1,13) AS h,
            COUNT(*) AS taker_exits
          FROM order_events
          WHERE event_type='ORDER_TAKER_EXIT_SUBMIT'
            AND ts >= ?
          GROUP BY h
        ),
        hs AS (
          SELECT h FROM fills
          UNION
          SELECT h FROM sett
          UNION
          SELECT h FROM exits
        )
        SELECT
          hs.h AS hour_utc,
          COALESCE(f.fills,0) AS fills,
          COALESCE(f.fill_realized_usdc,0) AS fill_realized_usdc,
          COALESCE(f.commissions_usdc,0) AS commissions_usdc,
          COALESCE(f.fill_notional_usdc,0) AS fill_notional_usdc,
          CASE
            WHEN COALESCE(f.fill_notional_usdc,0) > 0
            THEN (COALESCE(f.commissions_usdc,0) / f.fill_notional_usdc) * 10000.0
            ELSE NULL
          END AS implied_fee_bps,
          COALESCE(s.settlements,0) AS settlements,
          COALESCE(s.settlement_pnl_usdc,0) AS settlement_pnl_usdc,
          COALESCE(e.taker_exits,0) AS taker_exits,
          COALESCE(f.avg_directional_edge_ps, NULL) AS avg_directional_edge_ps,
          COALESCE(f.avg_directional_edge_usdc, NULL) AS avg_directional_edge_usdc,
          COALESCE(f.fill_realized_usdc,0) + COALESCE(s.settlement_pnl_usdc,0) AS combined_pnl_usdc
        FROM hs
        LEFT JOIN fills f ON f.h = hs.h
        LEFT JOIN sett s ON s.h = hs.h
        LEFT JOIN exits e ON e.h = hs.h
        ORDER BY hs.h
        """,
        (cutoff_iso, cutoff_iso, cutoff_iso),
    ).fetchall()
    kpis = _compute_monitor_kpis(cur, cutoff_iso=cutoff_iso, high_cost_threshold=float(args.high_cost_threshold))

    if not rows:
        print("No rows in selected window.")
        conn.close()
        return 0

    print("=" * 112)
    print("Hourly Attribution Report")
    print("=" * 112)
    print(f"db: {db}")
    print(f"lookback_hours: {args.hours}")
    print(f"cutoff_utc: {cutoff_iso}")
    print("")
    print(
        "hour_utc | fills | fill_realized | settlement | combined | fee_bps | taker_exits | avg_dir_edge_ps"
    )
    print("-" * 112)

    total_combined = 0.0
    total_fill_realized = 0.0
    total_settlement = 0.0
    total_commission = 0.0
    total_notional = 0.0
    total_taker_exits = 0
    neg_combined_hours = 0
    notes: List[str] = []

    for r in rows:
        fill_realized = float(r["fill_realized_usdc"] or 0.0)
        settlement = float(r["settlement_pnl_usdc"] or 0.0)
        combined = float(r["combined_pnl_usdc"] or 0.0)
        fee_bps = r["implied_fee_bps"]
        taker_exits = int(r["taker_exits"] or 0)
        avg_dir_edge_ps = r["avg_directional_edge_ps"]

        total_fill_realized += fill_realized
        total_settlement += settlement
        total_combined += combined
        total_commission += float(r["commissions_usdc"] or 0.0)
        total_notional += float(r["fill_notional_usdc"] or 0.0)
        total_taker_exits += taker_exits
        if combined < 0:
            neg_combined_hours += 1

        print(
            f"{r['hour_utc']} | {int(r['fills'] or 0):>5d} | "
            f"{_fmt(fill_realized):>12} | {_fmt(settlement):>10} | {_fmt(combined):>10} | "
            f"{_fmt(float(fee_bps),2) if fee_bps is not None else 'n/a':>7} | "
            f"{taker_exits:>11d} | "
            f"{_fmt(float(avg_dir_edge_ps),4) if avg_dir_edge_ps is not None else 'n/a':>14}"
        )

    implied_fee_bps_total = (total_commission / total_notional) * 10000.0 if total_notional > 0 else None
    print("-" * 112)
    print(
        f"TOTAL    |       | {_fmt(total_fill_realized):>12} | {_fmt(total_settlement):>10} | "
        f"{_fmt(total_combined):>10} | "
        f"{_fmt(implied_fee_bps_total,2) if implied_fee_bps_total is not None else 'n/a':>7} | "
        f"{total_taker_exits:>11d} | {'-':>14}"
    )

    if implied_fee_bps_total is not None and implied_fee_bps_total > 220:
        notes.append("Fee bps is elevated; reduce taker usage and requote churn.")
    if total_settlement < 0 and abs(total_settlement) > max(1.0, abs(total_fill_realized) * 0.5):
        notes.append("Settlement loss dominates; tighten pre-close inventory target.")
    if total_taker_exits >= max(6, args.hours // 3):
        notes.append("Taker exits are frequent; widen maker exit runway before fail-safe taker.")
    if neg_combined_hours >= max(4, args.hours // 2):
        notes.append("Many negative hours; enable/strengthen regime guard and BUY edge gate.")

    print("")
    print("KPI Monitor:")
    print(
        f"- taker_sell_pct: {_fmt(float(kpis['taker_sell_pct']),2)}% "
        f"(taker_sell_fills={int(kpis['taker_sell_fills'])}/{int(kpis['sell_fills'])})"
    )
    print(
        f"- high_cost_roundtrip_loss: {_fmt(float(kpis['high_cost_roundtrip_loss']))} "
        f"(markets={int(kpis['high_cost_roundtrip_n'])}, threshold>={float(args.high_cost_threshold):.2f})"
    )
    print(
        f"- up_settle_but_sold_lower_loss: {_fmt(float(kpis['up_settle_but_sold_lower_loss']))} "
        f"(markets={int(kpis['up_settle_but_sold_lower_n'])})"
    )
    print("")
    print("Suggestions:")
    has_suggestion = False
    if notes:
        for n in notes:
            print(f"- {n}")
        has_suggestion = True
    if float(kpis["taker_sell_pct"]) >= 20.0:
        print("- Taker SELL ratio is high; tighten active-exit guards and prioritize passive unwind.")
        has_suggestion = True
    if float(kpis["high_cost_roundtrip_loss"]) < -1.0:
        print("- High-cost roundtrip loss is material; strengthen sell cost floor and high-cost cooldown.")
        has_suggestion = True
    if float(kpis["up_settle_but_sold_lower_loss"]) < -1.0:
        print("- UP-settled but sold-lower loss is material; reduce premature inventory unwinds.")
        has_suggestion = True
    if not has_suggestion:
        print("- No acute risk signal in this window.")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
