#!/usr/bin/env python3
"""
Pre-fill score acceleration + price momentum report.

This report complements realized_edge_report.py by focusing on how quickly
the decision score and signed spot-minus-strike moved in the final 30s/60s
before a BUY fill.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Dict, Iterable, List, Optional

from realized_edge_report import (
    FillRow,
    SettlementRow,
    _bucket_time_left,
    _fmt,
    _infer_token_outcome,
    _iter_buy_fills,
    _load_settlements,
    _norm_outcome,
    _parse_ts_epoch,
    _print_bucket_summary,
    _safe_json_loads,
    _signed_spot_minus_strike,
    _snapshot_best_prices,
    _to_iso_utc,
)


SHADOW_EVENT_TYPE = "SHADOW_SIGNAL_CANDIDATE_LIVE"


@dataclass
class ShadowSnapshot:
    ts: str
    ts_epoch: float
    slug: str
    main_score: Optional[float]
    active_side: str
    candidate_side: str
    time_left_sec: Optional[float]
    spot_minus_strike: Optional[float]
    ret_10_bps: Optional[float]
    ret_30_bps: Optional[float]
    breakout_persistence_60s: Optional[float]
    shadow_score: Optional[float]
    bid_up: Optional[float]
    ask_up: Optional[float]
    bid_down: Optional[float]
    ask_down: Optional[float]


def _bucket_delta_score(x: Optional[float]) -> str:
    if x is None:
        return "missing"
    if x < -0.20:
        return "<-0.20"
    if x < 0.0:
        return "-0.20-0"
    if x < 0.20:
        return "0-0.20"
    if x < 0.40:
        return "0.20-0.40"
    return ">=0.40"


def _bucket_delta_signed_sms(x: Optional[float]) -> str:
    if x is None:
        return "missing"
    if x < -20.0:
        return "<-20"
    if x < 0.0:
        return "-20-0"
    if x < 10.0:
        return "0-10"
    if x < 30.0:
        return "10-30"
    return ">=30"


def _bucket_ret_bps(x: Optional[float]) -> str:
    if x is None:
        return "missing"
    if x < -1.0:
        return "<-1"
    if x < 0.0:
        return "-1-0"
    if x < 1.0:
        return "0-1"
    return ">=1"


def _bucket_breakout(x: Optional[float]) -> str:
    if x is None:
        return "missing"
    if x < 0.25:
        return "<0.25"
    if x < 0.75:
        return "0.25-0.75"
    return ">=0.75"


def _load_shadow_snapshots(
    conn: sqlite3.Connection,
    run_id: Optional[str],
    cutoff_iso: Optional[str],
) -> Dict[str, List[ShadowSnapshot]]:
    where: List[str] = ["event_type=?"]
    params: List[object] = [SHADOW_EVENT_TYPE]
    if run_id:
        where.append("run_id=?")
        params.append(run_id)
    if cutoff_iso:
        where.append("ts>=?")
        params.append(cutoff_iso)

    sql = f"""
        SELECT ts, payload_json
        FROM strategy_events
        WHERE {" AND ".join(where)}
        ORDER BY ts ASC
    """
    out: Dict[str, List[ShadowSnapshot]] = {}
    for row in conn.execute(sql, tuple(params)).fetchall():
        payload = _safe_json_loads(row["payload_json"])
        if not isinstance(payload, dict):
            continue
        slug = str(payload.get("slug") or payload.get("market_slug") or "")
        if not slug:
            continue
        snap = ShadowSnapshot(
            ts=str(row["ts"]),
            ts_epoch=_parse_ts_epoch(str(row["ts"])),
            slug=slug,
            main_score=(float(payload["main_score"]) if payload.get("main_score") is not None else None),
            active_side=_norm_outcome(payload.get("main_active_side")),
            candidate_side=_norm_outcome(payload.get("main_candidate_side")),
            time_left_sec=(float(payload["time_left_sec"]) if payload.get("time_left_sec") is not None else None),
            spot_minus_strike=(
                float(payload["spot_minus_strike"]) if payload.get("spot_minus_strike") is not None else None
            ),
            ret_10_bps=(float(payload["ret_10_bps"]) if payload.get("ret_10_bps") is not None else None),
            ret_30_bps=(float(payload["ret_30_bps"]) if payload.get("ret_30_bps") is not None else None),
            breakout_persistence_60s=(
                float(payload["breakout_persistence_60s"])
                if payload.get("breakout_persistence_60s") is not None
                else None
            ),
            shadow_score=(float(payload["shadow_score"]) if payload.get("shadow_score") is not None else None),
            bid_up=(float(payload["bid_up"]) if payload.get("bid_up") is not None else None),
            ask_up=(float(payload["ask_up"]) if payload.get("ask_up") is not None else None),
            bid_down=(float(payload["bid_down"]) if payload.get("bid_down") is not None else None),
            ask_down=(float(payload["ask_down"]) if payload.get("ask_down") is not None else None),
        )
        out.setdefault(slug, []).append(snap)
    return out


def _latest_shadow_before(
    shadows_by_slug: Dict[str, List[ShadowSnapshot]],
    slug: str,
    ts_epoch: float,
    max_age_sec: float,
) -> Optional[ShadowSnapshot]:
    rows = shadows_by_slug.get(slug)
    if not rows:
        return None
    epochs = [r.ts_epoch for r in rows]
    idx = bisect.bisect_right(epochs, ts_epoch) - 1
    if idx < 0:
        return None
    row = rows[idx]
    if ts_epoch - row.ts_epoch > max_age_sec:
        return None
    return row


def _latest_shadow_before_offset(
    shadows_by_slug: Dict[str, List[ShadowSnapshot]],
    slug: str,
    ts_epoch: float,
    offset_sec: float,
    tolerance_sec: float = 45.0,
) -> Optional[ShadowSnapshot]:
    target_epoch = ts_epoch - offset_sec
    return _latest_shadow_before(shadows_by_slug, slug, target_epoch, tolerance_sec)


def _is_explosive_accel(delta_score_60s: Optional[float], delta_signed_sms_60s: Optional[float]) -> int:
    if delta_score_60s is None or delta_signed_sms_60s is None:
        return 0
    return int(delta_score_60s >= 0.25 and delta_signed_sms_60s >= 10.0)


def _aggregate_market_rows(rows: List[dict]) -> List[dict]:
    grouped: Dict[str, List[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row["slug"]), []).append(row)

    market_rows: List[dict] = []
    for slug, rs in grouped.items():
        rs = sorted(rs, key=lambda r: (r["fill_ts"], r["fill_id"]))
        first = rs[0]
        total_realized = sum(float(r["realized_edge_usdc"]) for r in rs)
        market_rows.append(
            {
                "slug": slug,
                "fill_count": len(rs),
                "first_fill_ts": first["fill_ts"],
                "token_outcome": first["token_outcome"],
                "settlement_outcome": first["settlement_outcome"],
                "won": first["won"],
                "avg_fill_price": mean(float(r["fill_price"]) for r in rs),
                "market_realized_edge_usdc": total_realized,
                "time_left_sec": first["time_left_sec"],
                "time_left_bucket": first["time_left_bucket"],
                "side_score_now": first["side_score_now"],
                "side_score_30s_ago": first["side_score_30s_ago"],
                "side_score_60s_ago": first["side_score_60s_ago"],
                "delta_score_30s": first["delta_score_30s"],
                "delta_score_60s": first["delta_score_60s"],
                "delta_score_60s_bucket": first["delta_score_60s_bucket"],
                "signed_spot_minus_strike_now": first["signed_spot_minus_strike_now"],
                "delta_signed_sms_30s": first["delta_signed_sms_30s"],
                "delta_signed_sms_60s": first["delta_signed_sms_60s"],
                "delta_signed_sms_60s_bucket": first["delta_signed_sms_60s_bucket"],
                "ret_10_bps_now": first["ret_10_bps_now"],
                "ret_30_bps_now": first["ret_30_bps_now"],
                "ret_30_bps_bucket": first["ret_30_bps_bucket"],
                "breakout_persistence_60s_now": first["breakout_persistence_60s_now"],
                "breakout_bucket": first["breakout_bucket"],
                "shadow_score_now": first["shadow_score_now"],
                "explosive_accel_flag": first["explosive_accel_flag"],
            }
        )
    return market_rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Score acceleration + momentum report from trade_journal.db")
    ap.add_argument("--db", default="./logs/trade_journal.db")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--hours", type=int, default=8)
    ap.add_argument("--csv-out", default="./logs/reports/score_momentum_trades.csv")
    ap.add_argument("--market-csv-out", default="./logs/reports/score_momentum_markets.csv")
    args = ap.parse_args()

    run_id = args.run_id.strip() or None
    cutoff_iso = _to_iso_utc(args.hours)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    fills = list(_iter_buy_fills(conn, run_id, cutoff_iso))
    settlements = _load_settlements(conn, run_id, cutoff_iso)
    shadow_by_slug = _load_shadow_snapshots(conn, run_id, cutoff_iso)

    rows: List[dict] = []
    missing_settlement = 0
    missing_shadow = 0

    for fill in fills:
        settlement = settlements.get(fill.slug)
        if settlement is None:
            missing_settlement += 1
            continue

        now = _latest_shadow_before(shadow_by_slug, fill.slug, fill.ts_epoch, 120.0)
        if now is None:
            missing_shadow += 1
            continue
        prev30 = _latest_shadow_before_offset(shadow_by_slug, fill.slug, fill.ts_epoch, 30.0)
        prev60 = _latest_shadow_before_offset(shadow_by_slug, fill.slug, fill.ts_epoch, 60.0)

        token_outcome = _infer_token_outcome(fill, settlement, None) or _norm_outcome(now.candidate_side) or _norm_outcome(now.active_side)
        if token_outcome not in {"UP", "DOWN"}:
            continue

        signed_now = _signed_spot_minus_strike(token_outcome, now.spot_minus_strike)
        signed_30 = _signed_spot_minus_strike(token_outcome, prev30.spot_minus_strike if prev30 else None)
        signed_60 = _signed_spot_minus_strike(token_outcome, prev60.spot_minus_strike if prev60 else None)
        delta_score_30 = (now.main_score - prev30.main_score) if now.main_score is not None and prev30 and prev30.main_score is not None else None
        delta_score_60 = (now.main_score - prev60.main_score) if now.main_score is not None and prev60 and prev60.main_score is not None else None
        delta_signed_30 = (signed_now - signed_30) if signed_now is not None and signed_30 is not None else None
        delta_signed_60 = (signed_now - signed_60) if signed_now is not None and signed_60 is not None else None

        won = int(token_outcome == settlement.outcome)
        realized = float(settlement.settlement_pnl_usdc or 0.0) - float(fill.commission_usdc or 0.0)
        _, ask_px = _snapshot_best_prices(None, token_outcome)

        rows.append(
            {
                "slug": fill.slug,
                "fill_id": fill.id,
                "fill_ts": fill.ts,
                "token_outcome": token_outcome,
                "settlement_outcome": settlement.outcome,
                "won": won,
                "fill_price": fill.price,
                "qty": fill.qty,
                "commission_usdc": fill.commission_usdc,
                "realized_edge_usdc": realized,
                "time_left_sec": now.time_left_sec,
                "time_left_bucket": _bucket_time_left(now.time_left_sec),
                "side_score_now": now.main_score,
                "side_score_30s_ago": prev30.main_score if prev30 else None,
                "side_score_60s_ago": prev60.main_score if prev60 else None,
                "delta_score_30s": delta_score_30,
                "delta_score_60s": delta_score_60,
                "delta_score_60s_bucket": _bucket_delta_score(delta_score_60),
                "signed_spot_minus_strike_now": signed_now,
                "signed_spot_minus_strike_30s_ago": signed_30,
                "signed_spot_minus_strike_60s_ago": signed_60,
                "delta_signed_sms_30s": delta_signed_30,
                "delta_signed_sms_60s": delta_signed_60,
                "delta_signed_sms_60s_bucket": _bucket_delta_signed_sms(delta_signed_60),
                "ret_10_bps_now": now.ret_10_bps,
                "ret_30_bps_now": now.ret_30_bps,
                "ret_30_bps_bucket": _bucket_ret_bps(now.ret_30_bps),
                "breakout_persistence_60s_now": now.breakout_persistence_60s,
                "breakout_bucket": _bucket_breakout(now.breakout_persistence_60s),
                "shadow_score_now": now.shadow_score,
                "explosive_accel_flag": _is_explosive_accel(delta_score_60, delta_signed_60),
            }
        )

    out_path = Path(args.csv_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    market_rows = _aggregate_market_rows(rows)
    market_out_path = Path(args.market_csv_out)
    market_out_path.parent.mkdir(parents=True, exist_ok=True)
    if market_rows:
        with market_out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(market_rows[0].keys()))
            writer.writeheader()
            writer.writerows(market_rows)

    print("=" * 100)
    print("Score + Momentum Report")
    print("=" * 100)
    print(f"db: {args.db}")
    print(f"lookback_hours: {args.hours if args.hours > 0 else '(all time)'}")
    print(f"rows: {len(rows)}")
    print(f"markets: {len(market_rows)}")
    print(f"missing_settlement: {missing_settlement}")
    print(f"missing_shadow: {missing_shadow}")
    print(f"csv_out: {out_path}")
    print(f"market_csv_out: {market_out_path}")
    print("")

    if rows:
        wins = sum(int(r["won"]) for r in rows)
        print(
            f"fills wins/losses: {wins}/{len(rows)-wins} "
            f"(win_rate={(wins/len(rows))*100.0:.2f}%) sum_edge_usdc={sum(float(r['realized_edge_usdc']) for r in rows):.4f}"
        )
        print("")
        _print_bucket_summary(rows, "delta_score_60s_bucket", "By delta_score_60s bucket:")
        _print_bucket_summary(rows, "delta_signed_sms_60s_bucket", "By delta_signed_spot_minus_strike_60s bucket:")
        _print_bucket_summary(rows, "ret_30_bps_bucket", "By ret_30_bps bucket:")
        _print_bucket_summary(rows, "breakout_bucket", "By breakout_persistence_60s bucket:")
        explosive_rows = [r for r in rows if int(r["explosive_accel_flag"]) == 1]
        print("Custom acceleration buckets:")
        for label, subset in [("explosive_accel_flag", explosive_rows)]:
            n = len(subset)
            wins = sum(int(r["won"]) for r in subset)
            win_rate = (wins / n) * 100.0 if n else 0.0
            avg_edge = mean(float(r["realized_edge_usdc"]) for r in subset) if n else 0.0
            sum_edge = sum(float(r["realized_edge_usdc"]) for r in subset)
            print(
                f"- {label:<24} n={n:<4d} win_rate={win_rate:>6.2f}% "
                f"avg_edge_usdc={_fmt(avg_edge):>8} sum_edge_usdc={_fmt(sum_edge):>9}"
            )
        print("")

    if market_rows:
        wins = sum(int(r["won"]) for r in market_rows)
        print(
            f"market wins/losses: {wins}/{len(market_rows)-wins} "
            f"(win_rate={(wins/len(market_rows))*100.0:.2f}%) "
            f"sum_market_edge_usdc={sum(float(r['market_realized_edge_usdc']) for r in market_rows):.4f}"
        )
        print("")
        _print_bucket_summary(market_rows, "delta_score_60s_bucket", "Market-level by delta_score_60s bucket:", pnl_key="market_realized_edge_usdc")
        _print_bucket_summary(market_rows, "delta_signed_sms_60s_bucket", "Market-level by delta_signed_spot_minus_strike_60s bucket:", pnl_key="market_realized_edge_usdc")
        _print_bucket_summary(market_rows, "ret_30_bps_bucket", "Market-level by ret_30_bps bucket:", pnl_key="market_realized_edge_usdc")
        _print_bucket_summary(market_rows, "breakout_bucket", "Market-level by breakout_persistence_60s bucket:", pnl_key="market_realized_edge_usdc")
        explosive_rows = [r for r in market_rows if int(r["explosive_accel_flag"]) == 1]
        print("Market-level custom acceleration buckets:")
        for label, subset in [("explosive_accel_flag", explosive_rows)]:
            n = len(subset)
            wins = sum(int(r["won"]) for r in subset)
            win_rate = (wins / n) * 100.0 if n else 0.0
            avg_edge = mean(float(r["market_realized_edge_usdc"]) for r in subset) if n else 0.0
            sum_edge = sum(float(r["market_realized_edge_usdc"]) for r in subset)
            print(
                f"- {label:<24} n={n:<4d} win_rate={win_rate:>6.2f}% "
                f"avg_edge_usdc={_fmt(avg_edge):>8} sum_edge_usdc={_fmt(sum_edge):>9}"
            )
        print("")

        losers = sorted(market_rows, key=lambda r: float(r["market_realized_edge_usdc"]))[:5]
        print("Worst market examples:")
        for r in losers:
            print(
                f"- {r['slug']} pnl={float(r['market_realized_edge_usdc']):+.4f} "
                f"score60={r['delta_score_60s']} signed_sms60={r['delta_signed_sms_60s']} "
                f"ret30={r['ret_30_bps_now']} breakout={r['breakout_persistence_60s_now']} "
                f"fill={r['avg_fill_price']:.2f} outcome={r['token_outcome']}->{r['settlement_outcome']}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
