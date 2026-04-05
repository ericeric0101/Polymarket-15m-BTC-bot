#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _cutoff(hours: int) -> Optional[datetime]:
    if hours <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _fmt(value: Optional[float], digits: int = 6) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _safe_float(value, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


@dataclass
class ShadowPaperRow:
    slug: str
    ts: datetime
    side: str
    pnl: float
    entry_price: float
    exit_price: float
    edge: float
    shadow_score: float
    time_left_sec: float
    outcome: str
    close_kind: str
    exit_reason: str


@dataclass
class MainBotMarketRow:
    slug: str
    fill_realized_pnl: float
    settlement_pnl: float

    @property
    def combined_pnl(self) -> float:
        return self.fill_realized_pnl + self.settlement_pnl


def _load_shadow_paper_rows(db_path: Path, cutoff: Optional[datetime], run_id: Optional[str]) -> List[ShadowPaperRow]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    where = ["event_type in ('SHADOW_PAPER_TRADE_SETTLEMENT','SHADOW_PAPER_TRADE_EXIT')"]
    params: List[object] = []
    if run_id:
        where.append("run_id = ?")
        params.append(run_id)
    rows = conn.execute(
        f"""
        SELECT ts, payload_json
        FROM strategy_events
        WHERE {' AND '.join(where)}
        ORDER BY ts
        """,
        params,
    ).fetchall()
    conn.close()

    out: List[ShadowPaperRow] = []
    for row in rows:
        ts = _parse_ts(str(row["ts"]))
        if cutoff and ts < cutoff:
            continue
        payload = json.loads(row["payload_json"] or "{}")
        slug = str(payload.get("slug") or "")
        side = str(payload.get("side") or "")
        realized_pnl = payload.get("realized_pnl")
        entry_price = payload.get("entry_price")
        exit_price = payload.get("exit_price")
        if not slug or side not in {"BUY_UP", "BUY_DOWN"} or realized_pnl is None or entry_price is None or exit_price is None:
            continue
        out.append(
            ShadowPaperRow(
                slug=slug,
                ts=ts,
                side=side,
                pnl=float(realized_pnl),
                entry_price=float(entry_price),
                exit_price=float(exit_price),
                edge=float(payload.get("candidate_edge") or 0.0),
                shadow_score=float(payload.get("shadow_score") or 0.0),
                time_left_sec=float(payload.get("time_left_sec") or 0.0),
                outcome=str(payload.get("settlement_outcome") or ""),
                close_kind=str(payload.get("close_kind") or ("settlement" if row["ts"] else "")),
                exit_reason=str(payload.get("exit_reason") or ""),
            )
        )
    return out


def _load_main_bot_markets(db_path: Path, cutoff: Optional[datetime]) -> Dict[str, MainBotMarketRow]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cutoff_iso = cutoff.isoformat() if cutoff else None

    fill_where = ["event_type='ORDER_FILLED'"]
    fill_params: List[object] = []
    if cutoff_iso:
        fill_where.append("ts >= ?")
        fill_params.append(cutoff_iso)
    fill_rows = conn.execute(
        f"""
        SELECT payload_json
        FROM order_events
        WHERE {' AND '.join(fill_where)}
        """,
        fill_params,
    ).fetchall()

    settlement_where = ["event_type='MARKET_SETTLEMENT'"]
    settlement_params: List[object] = []
    if cutoff_iso:
        settlement_where.append("ts >= ?")
        settlement_params.append(cutoff_iso)
    settlement_rows = conn.execute(
        f"""
        SELECT payload_json
        FROM strategy_events
        WHERE {' AND '.join(settlement_where)}
        """,
        settlement_params,
    ).fetchall()
    conn.close()

    by_slug: Dict[str, MainBotMarketRow] = {}
    for row in fill_rows:
        payload = json.loads(row["payload_json"] or "{}")
        slug = str(payload.get("slug") or payload.get("market_slug") or "")
        if not slug:
            continue
        realized = float(payload.get("realized_net_usdc") or 0.0)
        curr = by_slug.setdefault(slug, MainBotMarketRow(slug=slug, fill_realized_pnl=0.0, settlement_pnl=0.0))
        curr.fill_realized_pnl += realized

    for row in settlement_rows:
        payload = json.loads(row["payload_json"] or "{}")
        slug = str(payload.get("slug") or payload.get("market_slug") or "")
        if not slug:
            continue
        settlement_pnl = float(payload.get("settlement_pnl_usdc") or 0.0)
        curr = by_slug.setdefault(slug, MainBotMarketRow(slug=slug, fill_realized_pnl=0.0, settlement_pnl=0.0))
        curr.settlement_pnl += settlement_pnl

    return by_slug


def _summarize_shadow(rows: List[ShadowPaperRow]) -> None:
    if not rows:
        print("shadow_paper: no settled rows")
        return
    wins = sum(1 for row in rows if row.pnl > 0)
    total = sum(row.pnl for row in rows)
    avg_edge = sum(row.edge for row in rows) / len(rows)
    avg_score = sum(row.shadow_score for row in rows) / len(rows)
    avg_entry = sum(row.entry_price for row in rows) / len(rows)
    print(
        f"shadow_paper: count={len(rows)} win_rate={wins/len(rows):.2%} "
        f"total_pnl={total:.6f} avg_pnl={total/len(rows):.6f} "
        f"avg_edge={avg_edge:.6f} avg_score={avg_score:+.4f} avg_entry={avg_entry:.4f}"
    )
    close_kind_counts: Dict[str, int] = defaultdict(int)
    for row in rows:
        close_kind_counts[row.close_kind or "unknown"] += 1
    if close_kind_counts:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(close_kind_counts.items()))
        print(f"  close_mix: {parts}")


def _summarize_overlap(rows: List[ShadowPaperRow], main_by_slug: Dict[str, MainBotMarketRow]) -> None:
    overlap = [row for row in rows if row.slug in main_by_slug]
    if not overlap:
        print("overlap_with_main_bot: no overlapping settled markets")
        return
    shadow_total = sum(row.pnl for row in overlap)
    main_total = sum(main_by_slug[row.slug].combined_pnl for row in overlap)
    improved = sum(1 for row in overlap if row.pnl > main_by_slug[row.slug].combined_pnl)
    worsened = sum(1 for row in overlap if row.pnl < main_by_slug[row.slug].combined_pnl)
    tied = len(overlap) - improved - worsened
    print(
        f"overlap_with_main_bot: count={len(overlap)} "
        f"shadow_total={shadow_total:.6f} main_total={main_total:.6f} "
        f"delta={shadow_total - main_total:+.6f}"
    )
    print(f"  improved={improved} worsened={worsened} tied={tied}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Report for shadow_feature_probe against main bot realized+settlement PnL")
    ap.add_argument("--shadow-db", default="./logs/shadow_probe.db", help="SQLite DB path for shadow probe")
    ap.add_argument("--trade-db", default="./logs/trade_journal.db", help="SQLite DB path for main bot journal")
    ap.add_argument("--hours", type=int, default=24, help="Lookback hours; <=0 means all rows")
    ap.add_argument("--run-id", default=None, help="Optional shadow probe run_id filter")
    ap.add_argument("--show", type=int, default=12, help="How many overlap rows to print")
    args = ap.parse_args()

    shadow_db = Path(args.shadow_db)
    trade_db = Path(args.trade_db)
    if not shadow_db.exists():
        print(f"Shadow DB not found: {shadow_db}")
        return 1
    if not trade_db.exists():
        print(f"Trade DB not found: {trade_db}")
        return 1

    cutoff = _cutoff(args.hours)
    shadow_rows = _load_shadow_paper_rows(shadow_db, cutoff, args.run_id)
    main_by_slug = _load_main_bot_markets(trade_db, cutoff)

    print("=" * 112)
    print("Shadow Feature Probe Report")
    print("=" * 112)
    print(f"shadow_db: {shadow_db}")
    print(f"trade_db: {trade_db}")
    print(f"lookback_hours: {args.hours if args.hours > 0 else 'ALL'}")
    print(f"run_id: {args.run_id or 'ALL'}")
    if cutoff:
        print(f"cutoff_utc: {cutoff.isoformat()}")
    print()
    print(f"shadow_paper_rows: {len(shadow_rows)}")
    print(f"shadow_paper_markets: {len({row.slug for row in shadow_rows})}")
    print(f"main_bot_markets: {len(main_by_slug)}")
    print()
    _summarize_shadow(shadow_rows)
    _summarize_overlap(shadow_rows, main_by_slug)

    overlap = [row for row in shadow_rows if row.slug in main_by_slug]
    if overlap:
        print()
        print("[Recent overlap rows]")
        for row in sorted(overlap, key=lambda item: item.ts)[-args.show :]:
            main_row = main_by_slug[row.slug]
            print(
                f"{row.ts.isoformat()} {row.slug} {row.side} "
                f"shadow_pnl={_fmt(row.pnl,4)} main_combined={_fmt(main_row.combined_pnl,4)} "
                f"delta={_fmt(row.pnl - main_row.combined_pnl,4)} "
                f"edge={_fmt(row.edge,4)} score={_fmt(row.shadow_score,4)} "
                f"close={row.close_kind}:{row.exit_reason or '-'} outcome={row.outcome or '-'}"
            )

    if shadow_rows:
        print()
        print("[Recent shadow paper settlements]")
        for row in sorted(shadow_rows, key=lambda item: item.ts)[-args.show :]:
            print(
                f"{row.ts.isoformat()} {row.slug} {row.side} "
                f"entry={_fmt(row.entry_price,4)} exit={_fmt(row.exit_price,4)} pnl={_fmt(row.pnl,4)} "
                f"edge={_fmt(row.edge,4)} score={_fmt(row.shadow_score,4)} "
                f"t_left={_fmt(row.time_left_sec,1)}s close={row.close_kind}:{row.exit_reason or '-'} outcome={row.outcome or '-'}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
