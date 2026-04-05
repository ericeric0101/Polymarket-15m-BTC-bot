#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
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


def _fmt(value: Optional[float], digits: int = 4) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


@dataclass
class MainBotMarketRow:
    slug: str
    fill_count: int
    buy_qty: float
    sell_qty: float
    fill_realized_pnl: float
    settlement_pnl: float
    outcome: str

    @property
    def combined_pnl(self) -> float:
        return self.fill_realized_pnl + self.settlement_pnl

    @property
    def pnl_per_buy_share(self) -> Optional[float]:
        if self.buy_qty <= 0:
            return None
        return self.combined_pnl / self.buy_qty


@dataclass
class ShadowRow:
    slug: str
    ts: datetime
    side: str
    entry_qty: float
    entry_price: float
    exit_price: float
    pnl_per_share: float
    edge: float
    shadow_score: float
    close_kind: str
    exit_reason: str
    outcome: str

    @property
    def total_pnl(self) -> float:
        return self.pnl_per_share * self.entry_qty


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
        SELECT side, qty, payload_json
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
        curr = by_slug.setdefault(
            slug,
            MainBotMarketRow(
                slug=slug,
                fill_count=0,
                buy_qty=0.0,
                sell_qty=0.0,
                fill_realized_pnl=0.0,
                settlement_pnl=0.0,
                outcome="",
            ),
        )
        curr.fill_count += 1
        curr.fill_realized_pnl += float(payload.get("realized_net_usdc") or 0.0)
        side = str(row["side"] or "").upper()
        qty = float(row["qty"] or 0.0)
        if side == "BUY":
            curr.buy_qty += qty
        elif side == "SELL":
            curr.sell_qty += qty

    for row in settlement_rows:
        payload = json.loads(row["payload_json"] or "{}")
        slug = str(payload.get("slug") or payload.get("market_slug") or "")
        if not slug:
            continue
        curr = by_slug.setdefault(
            slug,
            MainBotMarketRow(
                slug=slug,
                fill_count=0,
                buy_qty=0.0,
                sell_qty=0.0,
                fill_realized_pnl=0.0,
                settlement_pnl=0.0,
                outcome="",
            ),
        )
        curr.settlement_pnl += float(payload.get("settlement_pnl_usdc") or 0.0)
        curr.outcome = str(payload.get("outcome") or curr.outcome)

    return by_slug


def _load_shadow_rows(db_path: Path, cutoff: Optional[datetime]) -> Dict[str, ShadowRow]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT ts, payload_json
        FROM strategy_events
        WHERE event_type in ('SHADOW_PAPER_TRADE_SETTLEMENT','SHADOW_PAPER_TRADE_EXIT')
        ORDER BY ts
        """
    ).fetchall()
    conn.close()

    by_slug: Dict[str, ShadowRow] = {}
    for row in rows:
        ts = _parse_ts(str(row["ts"]))
        if cutoff and ts < cutoff:
            continue
        payload = json.loads(row["payload_json"] or "{}")
        slug = str(payload.get("slug") or "")
        side = str(payload.get("side") or "")
        if not slug or side not in {"BUY_UP", "BUY_DOWN"}:
            continue
        by_slug[slug] = ShadowRow(
            slug=slug,
            ts=ts,
            side=side,
            entry_qty=float(payload.get("entry_qty") or 1.0),
            entry_price=float(payload.get("entry_price") or 0.0),
            exit_price=float(payload.get("exit_price") or 0.0),
            pnl_per_share=float(payload.get("realized_pnl") or 0.0),
            edge=float(payload.get("candidate_edge") or 0.0),
            shadow_score=float(payload.get("shadow_score") or 0.0),
            close_kind=str(payload.get("close_kind") or "settlement"),
            exit_reason=str(payload.get("exit_reason") or "settlement"),
            outcome=str(payload.get("settlement_outcome") or ""),
        )
    return by_slug


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare worst main-bot markets against shadow paper trades for veto design")
    ap.add_argument("--shadow-db", default="./logs/shadow_probe.db", help="SQLite DB path for shadow probe")
    ap.add_argument("--trade-db", default="./logs/trade_journal.db", help="SQLite DB path for main bot journal")
    ap.add_argument("--hours", type=int, default=24, help="Lookback hours; <=0 means all rows")
    ap.add_argument("--top", type=int, default=12, help="How many worst main markets to print")
    ap.add_argument("--loss-threshold", type=float, default=-0.50, help="Only show veto candidates at or below this main combined pnl")
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
    main_by_slug = _load_main_bot_markets(trade_db, cutoff)
    shadow_by_slug = _load_shadow_rows(shadow_db, cutoff)

    main_rows = sorted(main_by_slug.values(), key=lambda row: row.combined_pnl)

    print("=" * 112)
    print("Shadow Veto Report")
    print("=" * 112)
    print(f"trade_db: {trade_db}")
    print(f"shadow_db: {shadow_db}")
    print(f"lookback_hours: {args.hours if args.hours > 0 else 'ALL'}")
    if cutoff:
        print(f"cutoff_utc: {cutoff.isoformat()}")
    print(f"main_markets: {len(main_rows)}")
    print(f"shadow_markets: {len(shadow_by_slug)}")
    print()

    print("[Worst Main Markets]")
    for row in main_rows[: args.top]:
        shadow = shadow_by_slug.get(row.slug)
        shadow_desc = "NONE"
        if shadow:
            shadow_desc = (
                f"{shadow.side} entry={_fmt(shadow.entry_price)} qty={_fmt(shadow.entry_qty)} "
                f"pnl/share={_fmt(shadow.pnl_per_share)} total={_fmt(shadow.total_pnl)} "
                f"edge={_fmt(shadow.edge)} score={_fmt(shadow.shadow_score)} "
                f"close={shadow.close_kind}:{shadow.exit_reason}"
            )
        print(
            f"{row.slug} main={_fmt(row.combined_pnl)} "
            f"(fill={_fmt(row.fill_realized_pnl)} settle={_fmt(row.settlement_pnl)} fills={row.fill_count} "
            f"buy_qty={_fmt(row.buy_qty)} pnl/share={_fmt(row.pnl_per_buy_share)} outcome={row.outcome or '-'}) "
            f"shadow={shadow_desc}"
        )

    candidates = [row for row in main_rows if row.combined_pnl <= args.loss_threshold]
    print()
    print(f"[Veto Candidates <= {args.loss_threshold:.2f}]")
    if not candidates:
        print("none")
        return 0

    improved = 0
    skipped = 0
    worse = 0
    for row in candidates:
        shadow = shadow_by_slug.get(row.slug)
        if shadow is None:
            skipped += 1
            verdict = "SHADOW_SKIPPED"
            detail = "no shadow trade"
        elif row.pnl_per_buy_share is not None and shadow.pnl_per_share > row.pnl_per_buy_share:
            improved += 1
            verdict = "SHADOW_BETTER"
            detail = (
                f"{shadow.side} entry={_fmt(shadow.entry_price)} qty={_fmt(shadow.entry_qty)} "
                f"pnl/share={_fmt(shadow.pnl_per_share)} total={_fmt(shadow.total_pnl)} "
                f"edge={_fmt(shadow.edge)} score={_fmt(shadow.shadow_score)}"
            )
        else:
            worse += 1
            verdict = "SHADOW_WORSE"
            detail = (
                f"{shadow.side} entry={_fmt(shadow.entry_price)} qty={_fmt(shadow.entry_qty)} "
                f"pnl/share={_fmt(shadow.pnl_per_share)} total={_fmt(shadow.total_pnl)} "
                f"edge={_fmt(shadow.edge)} score={_fmt(shadow.shadow_score)}"
            )
        print(
            f"{row.slug} {verdict} main={_fmt(row.combined_pnl)} "
            f"(fill={_fmt(row.fill_realized_pnl)} settle={_fmt(row.settlement_pnl)} buy_qty={_fmt(row.buy_qty)} "
            f"pnl/share={_fmt(row.pnl_per_buy_share)} outcome={row.outcome or '-'}) "
            f"{detail}"
        )

    print()
    print(
        f"summary: candidates={len(candidates)} "
        f"shadow_better={improved} shadow_skipped={skipped} shadow_worse={worse}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
