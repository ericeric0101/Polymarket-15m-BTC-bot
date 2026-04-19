#!/usr/bin/env python3
"""
Econ-gate attribution report.

For each NO_TRADE_ECON_GATE event, align the nearest shadow snapshot and the
final market settlement, then estimate a simple hold-to-redeem counterfactual:
"if we had bought one share at the blocked ask, what would the payoff be?"
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Dict, List, Optional

from realized_edge_report import (
    _bucket_fair_edge,
    _bucket_time_left,
    _bucket_token_price,
    _fmt,
    _load_settlements,
    _norm_outcome,
    _parse_ts_epoch,
    _print_bucket_summary,
    _safe_json_loads,
    _signed_spot_minus_strike,
    _to_iso_utc,
)
from score_momentum_report import ShadowSnapshot, _load_shadow_snapshots


def _bucket_gap(x: Optional[float]) -> str:
    if x is None:
        return "missing"
    if x < -0.20:
        return "<-0.20"
    if x < -0.10:
        return "-0.20--0.10"
    if x < -0.05:
        return "-0.10--0.05"
    if x < 0.0:
        return "-0.05-0"
    if x < 0.05:
        return "0-0.05"
    return ">=0.05"


def _bucket_penalty(x: Optional[float]) -> str:
    if x is None:
        return "missing"
    if x < 0.02:
        return "<0.02"
    if x < 0.03:
        return "0.02-0.03"
    if x < 0.04:
        return "0.03-0.04"
    if x < 0.05:
        return "0.04-0.05"
    return ">=0.05"


def _bucket_expected(x: Optional[float]) -> str:
    if x is None:
        return "missing"
    if x < -0.10:
        return "<-0.10"
    if x < -0.03:
        return "-0.10--0.03"
    if x < 0.0:
        return "-0.03-0"
    if x < 0.01:
        return "0-0.01"
    if x < 0.03:
        return "0.01-0.03"
    return ">=0.03"


def _bucket_robust(x: Optional[float]) -> str:
    if x is None:
        return "missing"
    if x < -0.10:
        return "<-0.10"
    if x < -0.05:
        return "-0.10--0.05"
    if x < -0.02:
        return "-0.05--0.02"
    if x < 0.0:
        return "-0.02-0"
    return ">=0"


def _extract_float(pattern: str, text: str) -> Optional[float]:
    m = re.search(pattern, text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


@dataclass
class EconGateEvent:
    ts: str
    ts_epoch: float
    slug: str
    instrument_id: str
    fair: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    primary_reason: str
    expected_net: Optional[float]
    exec_penalty: Optional[float]
    robust_net: Optional[float]


def _load_econ_gate_events(
    conn: sqlite3.Connection,
    run_id: Optional[str],
    cutoff_iso: Optional[str],
) -> Dict[str, List[EconGateEvent]]:
    where: List[str] = ["event_type='NO_TRADE_ECON_GATE'"]
    params: List[object] = []
    if run_id:
        where.append("run_id=?")
        params.append(run_id)
    if cutoff_iso:
        where.append("ts>=?")
        params.append(cutoff_iso)
    sql = f"SELECT ts,payload_json FROM strategy_events WHERE {' AND '.join(where)} ORDER BY ts ASC"
    out: Dict[str, List[EconGateEvent]] = {}
    for row in conn.execute(sql, tuple(params)).fetchall():
        payload = _safe_json_loads(row["payload_json"])
        if not isinstance(payload, dict):
            continue
        slug = str(payload.get("slug") or payload.get("market_slug") or "")
        if not slug:
            continue
        primary_reason = str(payload.get("primary_reason") or "")
        event = EconGateEvent(
            ts=str(row["ts"]),
            ts_epoch=_parse_ts_epoch(str(row["ts"])),
            slug=slug,
            instrument_id=str(payload.get("instrument_id") or ""),
            fair=float(payload["fair"]) if payload.get("fair") is not None else None,
            bid=float(payload["bid"]) if payload.get("bid") is not None else None,
            ask=float(payload["ask"]) if payload.get("ask") is not None else None,
            primary_reason=primary_reason,
            expected_net=_extract_float(r"expected_net=(-?\d+\.\d+)", primary_reason),
            exec_penalty=_extract_float(r"exec_penalty=(-?\d+\.\d+)", primary_reason),
            robust_net=_extract_float(r"robust_net=(-?\d+\.\d+)", primary_reason),
        )
        out.setdefault(slug, []).append(event)
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


def _counterfactual_edge(token_outcome: str, settlement_outcome: str, ask: Optional[float]) -> Optional[float]:
    if token_outcome not in {"UP", "DOWN"} or settlement_outcome not in {"UP", "DOWN"} or ask is None:
        return None
    return (1.0 - ask) if token_outcome == settlement_outcome else (-ask)


def _aggregate_market_rows(rows: List[dict]) -> List[dict]:
    grouped: Dict[str, List[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row["slug"]), []).append(row)

    out: List[dict] = []
    for slug, rs in grouped.items():
        rs = sorted(rs, key=lambda r: r["ts"])
        # first trigger is the earliest blocked opportunity in the market.
        first = rs[0]
        hyp = first["counterfactual_edge_1sh"]
        out.append(
            {
                "slug": slug,
                "first_no_trade_ts": first["ts"],
                "samples": len(rs),
                "token_outcome": first["token_outcome"],
                "settlement_outcome": first["settlement_outcome"],
                "would_have_won": first["would_have_won"],
                "avg_fair": mean(float(r["fair"]) for r in rs if r["fair"] is not None),
                "avg_ask": mean(float(r["ask"]) for r in rs if r["ask"] is not None),
                "avg_fair_minus_ask": mean(float(r["fair_minus_ask"]) for r in rs if r["fair_minus_ask"] is not None),
                "avg_exec_penalty": mean(float(r["exec_penalty"]) for r in rs if r["exec_penalty"] is not None),
                "avg_expected_net": mean(float(r["expected_net"]) for r in rs if r["expected_net"] is not None),
                "avg_robust_net": mean(float(r["robust_net"]) for r in rs if r["robust_net"] is not None),
                "side_score": first["side_score"],
                "time_left_sec": first["time_left_sec"],
                "time_left_bucket": first["time_left_bucket"],
                "token_price_bucket": first["token_price_bucket"],
                "fair_minus_ask_bucket": first["fair_minus_ask_bucket"],
                "exec_penalty_bucket": first["exec_penalty_bucket"],
                "expected_net_bucket": first["expected_net_bucket"],
                "robust_net_bucket": first["robust_net_bucket"],
                "signed_spot_minus_strike": first["signed_spot_minus_strike"],
                "counterfactual_edge_1sh": hyp,
            }
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="NO_TRADE_ECON_GATE attribution report")
    ap.add_argument("--db", default="./logs/trade_journal.db")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--csv-out", default="./logs/reports/econ_gate_events.csv")
    ap.add_argument("--market-csv-out", default="./logs/reports/econ_gate_markets.csv")
    args = ap.parse_args()

    run_id = args.run_id.strip() or None
    cutoff_iso = _to_iso_utc(args.hours)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    econ_by_slug = _load_econ_gate_events(conn, run_id, cutoff_iso)
    settlements = _load_settlements(conn, run_id, cutoff_iso)
    shadows = _load_shadow_snapshots(conn, run_id, cutoff_iso)

    rows: List[dict] = []
    missing_settlement = 0
    missing_shadow = 0
    unresolved_side = 0

    for slug, events in econ_by_slug.items():
        settlement = settlements.get(slug)
        if settlement is None:
            missing_settlement += len(events)
            continue
        for e in events:
            shadow = _latest_shadow_before(shadows, slug, e.ts_epoch, 120.0)
            if shadow is None:
                missing_shadow += 1
                continue
            token_outcome = _norm_outcome(shadow.candidate_side) or _norm_outcome(shadow.active_side)
            if token_outcome not in {"UP", "DOWN"}:
                unresolved_side += 1
                continue
            signed_sms = _signed_spot_minus_strike(token_outcome, shadow.spot_minus_strike)
            fair_minus_ask = (e.fair - e.ask) if e.fair is not None and e.ask is not None else None
            cf_edge = _counterfactual_edge(token_outcome, settlement.outcome, e.ask)
            rows.append(
                {
                    "ts": e.ts,
                    "slug": slug,
                    "token_outcome": token_outcome,
                    "settlement_outcome": settlement.outcome,
                    "would_have_won": int(token_outcome == settlement.outcome),
                    "fair": e.fair,
                    "bid": e.bid,
                    "ask": e.ask,
                    "fair_minus_ask": fair_minus_ask,
                    "exec_penalty": e.exec_penalty,
                    "expected_net": e.expected_net,
                    "robust_net": e.robust_net,
                    "primary_reason": e.primary_reason,
                    "side_score": shadow.main_score,
                    "time_left_sec": shadow.time_left_sec,
                    "time_left_bucket": _bucket_time_left(shadow.time_left_sec),
                    "signed_spot_minus_strike": signed_sms,
                    "ret_30_bps": shadow.ret_30_bps,
                    "breakout_persistence_60s": shadow.breakout_persistence_60s,
                    "token_price_bucket": _bucket_token_price(e.ask),
                    "fair_minus_ask_bucket": _bucket_gap(fair_minus_ask),
                    "exec_penalty_bucket": _bucket_penalty(e.exec_penalty),
                    "expected_net_bucket": _bucket_expected(e.expected_net),
                    "robust_net_bucket": _bucket_robust(e.robust_net),
                    "counterfactual_edge_1sh": cf_edge,
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
    market_out = Path(args.market_csv_out)
    market_out.parent.mkdir(parents=True, exist_ok=True)
    if market_rows:
        with market_out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(market_rows[0].keys()))
            writer.writeheader()
            writer.writerows(market_rows)

    print("=" * 100)
    print("Econ Gate Attribution Report")
    print("=" * 100)
    print(f"db: {args.db}")
    print(f"lookback_hours: {args.hours if args.hours > 0 else '(all time)'}")
    print(f"rows: {len(rows)}")
    print(f"markets: {len(market_rows)}")
    print(f"missing_settlement: {missing_settlement}")
    print(f"missing_shadow: {missing_shadow}")
    print(f"unresolved_side: {unresolved_side}")
    print(f"csv_out: {out_path}")
    print(f"market_csv_out: {market_out}")
    print("")

    if rows:
        wins = sum(int(r["would_have_won"]) for r in rows)
        cf_rows = [r for r in rows if r["counterfactual_edge_1sh"] is not None]
        cf_sum = sum(float(r["counterfactual_edge_1sh"]) for r in cf_rows)
        print(
            f"event-level would_have_won: {wins}/{len(rows)} "
            f"(win_rate={(wins/len(rows))*100.0:.2f}%) "
            f"counterfactual_1sh_sum={cf_sum:.4f}"
        )
        print("")
        _print_bucket_summary(rows, "fair_minus_ask_bucket", "By fair-ask bucket:", pnl_key="counterfactual_edge_1sh")
        _print_bucket_summary(rows, "exec_penalty_bucket", "By exec_penalty bucket:", pnl_key="counterfactual_edge_1sh")
        _print_bucket_summary(rows, "expected_net_bucket", "By expected_net bucket:", pnl_key="counterfactual_edge_1sh")
        _print_bucket_summary(rows, "robust_net_bucket", "By robust_net bucket:", pnl_key="counterfactual_edge_1sh")
        _print_bucket_summary(rows, "token_price_bucket", "By token-price bucket:", pnl_key="counterfactual_edge_1sh")
        _print_bucket_summary(rows, "time_left_bucket", "By time-left bucket:", pnl_key="counterfactual_edge_1sh")

    if market_rows:
        wins = sum(int(r["would_have_won"]) for r in market_rows)
        cf_rows = [r for r in market_rows if r["counterfactual_edge_1sh"] is not None]
        cf_sum = sum(float(r["counterfactual_edge_1sh"]) for r in cf_rows)
        print(
            f"market-level would_have_won: {wins}/{len(market_rows)} "
            f"(win_rate={(wins/len(market_rows))*100.0:.2f}%) "
            f"counterfactual_1sh_sum={cf_sum:.4f}"
        )
        print("")
        _print_bucket_summary(market_rows, "fair_minus_ask_bucket", "Market-level by fair-ask bucket:", pnl_key="counterfactual_edge_1sh")
        _print_bucket_summary(market_rows, "exec_penalty_bucket", "Market-level by exec_penalty bucket:", pnl_key="counterfactual_edge_1sh")
        _print_bucket_summary(market_rows, "expected_net_bucket", "Market-level by expected_net bucket:", pnl_key="counterfactual_edge_1sh")
        _print_bucket_summary(market_rows, "robust_net_bucket", "Market-level by robust_net bucket:", pnl_key="counterfactual_edge_1sh")
        _print_bucket_summary(market_rows, "token_price_bucket", "Market-level by token-price bucket:", pnl_key="counterfactual_edge_1sh")
        _print_bucket_summary(market_rows, "time_left_bucket", "Market-level by time-left bucket:", pnl_key="counterfactual_edge_1sh")

        worst = sorted(market_rows, key=lambda r: float(r["counterfactual_edge_1sh"]))[:10]
        best = sorted(market_rows, key=lambda r: float(r["counterfactual_edge_1sh"]), reverse=True)[:10]
        print("Worst skipped-market examples:")
        for r in worst:
            print(
                f"- {r['slug']} cf={float(r['counterfactual_edge_1sh']):+.4f} "
                f"fair-ask={float(r['avg_fair_minus_ask']):+.4f} "
                f"penalty={float(r['avg_exec_penalty']):.4f} "
                f"expected={float(r['avg_expected_net']):+.4f} robust={float(r['avg_robust_net']):+.4f} "
                f"side={r['token_outcome']}->{r['settlement_outcome']}"
            )
        print("")
        print("Best skipped-market examples:")
        for r in best:
            print(
                f"- {r['slug']} cf={float(r['counterfactual_edge_1sh']):+.4f} "
                f"fair-ask={float(r['avg_fair_minus_ask']):+.4f} "
                f"penalty={float(r['avg_exec_penalty']):.4f} "
                f"expected={float(r['avg_expected_net']):+.4f} robust={float(r['avg_robust_net']):+.4f} "
                f"side={r['token_outcome']}->{r['settlement_outcome']}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
