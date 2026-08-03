#!/usr/bin/env python3
"""
Recent BUY fill diagnostics for trade_journal.db.

Focus:
- BUY fill price
- main score at fill
- score change after 10s / 30s / 60s
- heuristic chase-entry label

This is a diagnostics report, not a trading rule. The chase label is a
transparent heuristic intended to highlight potentially overextended entries.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Dict, List, Optional

from realized_edge_report import (
    FillRow,
    SettlementRow,
    _infer_token_outcome,
    _iter_buy_fills,
    _norm_outcome,
    _parse_ts_epoch,
    _signed_spot_minus_strike,
    _snapshot_best_prices,
    _to_iso_utc,
)


SIGNAL_EVENT_TYPES = (
    "SIDE_DECISION_OBSERVATION",
    "LIVE_SIGNAL_COMPARE",
    "MAIN_SIGNAL_CANDIDATE_LIVE",
    "SHADOW_SIGNAL_CANDIDATE_LIVE",
    "NO_TRADE_ACTIVE_SIDE_NONE",
    "SIDE_MODE_CHANGED",
)


@dataclass
class SignalSnapshot:
    ts: str
    ts_epoch: float
    slug: str
    event_type: str
    side_score: Optional[float]
    main_score: Optional[float]
    shadow_score: Optional[float]
    candidate_side: str
    active_side: str
    time_left_sec: Optional[float]
    spot_minus_strike: Optional[float]
    ret_30_bps: Optional[float]
    breakout_persistence_60s: Optional[float]
    bid_up: Optional[float]
    ask_up: Optional[float]
    bid_down: Optional[float]
    ask_down: Optional[float]


@dataclass
class SubmitContext:
    client_order_id: str
    entry_mode: str
    size_multiplier: Optional[float]
    entry_quality_quote_price_cap: Optional[float]
    entry_quality_label: Optional[str]
    entry_quality_risk: Optional[float]
    entry_quality_post_entry_decay_risk: Optional[float]
    entry_quality_size_multiplier: Optional[float]
    entry_quality_suggested_size_multiplier: Optional[float]
    entry_quality_min_expected_uplift_usdc: Optional[float]
    entry_quality_quote_placement_mode: Optional[str]
    entry_quality_reasons: str


def _safe_json_loads(s: object) -> object:
    if s is None:
        return None
    try:
        return json.loads(str(s))
    except Exception:
        return None


def _fmt(x: Optional[float], digits: int = 4) -> str:
    if x is None:
        return "NA"
    return f"{x:.{digits}f}"


def _load_signal_snapshots(
    conn: sqlite3.Connection,
    run_id: Optional[str],
    cutoff_iso: Optional[str],
) -> Dict[str, List[SignalSnapshot]]:
    where: List[str] = [f"event_type IN ({','.join(['?'] * len(SIGNAL_EVENT_TYPES))})"]
    params: List[object] = list(SIGNAL_EVENT_TYPES)
    if run_id:
        where.append("run_id=?")
        params.append(run_id)
    if cutoff_iso:
        where.append("ts>=?")
        params.append(cutoff_iso)

    sql = f"""
        SELECT ts, event_type, payload_json
        FROM strategy_events
        WHERE {" AND ".join(where)}
        ORDER BY ts ASC
    """
    out: Dict[str, List[SignalSnapshot]] = {}
    for row in conn.execute(sql, tuple(params)).fetchall():
        payload = _safe_json_loads(row["payload_json"])
        if not isinstance(payload, dict):
            continue
        slug = str(payload.get("slug") or payload.get("market_slug") or "")
        if not slug:
            continue
        side_score = payload.get("main_score")
        if side_score is None:
            side_score = payload.get("side_score")
        snap = SignalSnapshot(
            ts=str(row["ts"]),
            ts_epoch=_parse_ts_epoch(str(row["ts"])),
            slug=slug,
            event_type=str(row["event_type"]),
            side_score=(float(side_score) if side_score is not None else None),
            main_score=(float(payload["main_score"]) if payload.get("main_score") is not None else None),
            shadow_score=(float(payload["shadow_score"]) if payload.get("shadow_score") is not None else None),
            candidate_side=_norm_outcome(payload.get("main_candidate_side")),
            active_side=_norm_outcome(payload.get("main_active_side") or payload.get("active_side")),
            time_left_sec=(
                float(payload["time_left_sec"])
                if payload.get("time_left_sec") is not None
                else None
            ),
            spot_minus_strike=(
                float(payload["spot_minus_strike"])
                if payload.get("spot_minus_strike") is not None
                else None
            ),
            ret_30_bps=(
                float(payload["ret_30_bps"])
                if payload.get("ret_30_bps") is not None
                else None
            ),
            breakout_persistence_60s=(
                float(payload["breakout_persistence_60s"])
                if payload.get("breakout_persistence_60s") is not None
                else None
            ),
            bid_up=(float(payload["bid_up"]) if payload.get("bid_up") is not None else None),
            ask_up=(float(payload["ask_up"]) if payload.get("ask_up") is not None else None),
            bid_down=(float(payload["bid_down"]) if payload.get("bid_down") is not None else None),
            ask_down=(float(payload["ask_down"]) if payload.get("ask_down") is not None else None),
        )
        out.setdefault(slug, []).append(snap)
    return out


def _load_settlements_compat(
    conn: sqlite3.Connection,
    run_id: Optional[str],
    cutoff_iso: Optional[str],
) -> Dict[str, SettlementRow]:
    where: List[str] = ["event_type='MARKET_SETTLEMENT'"]
    params: List[object] = []
    if run_id:
        where.append("run_id=?")
        params.append(run_id)
    if cutoff_iso:
        where.append("ts>=?")
        params.append(cutoff_iso)

    sql = f"SELECT payload_json FROM strategy_events WHERE {' AND '.join(where)}"
    out: Dict[str, SettlementRow] = {}
    for row in conn.execute(sql, tuple(params)).fetchall():
        payload = _safe_json_loads(row["payload_json"])
        if not isinstance(payload, dict):
            continue
        slug = str(payload.get("slug") or payload.get("market_slug") or "")
        spot = (float(payload["spot"]) if payload.get("spot") is not None else None)
        strike = (float(payload["strike"]) if payload.get("strike") is not None else None)
        outcome = _norm_outcome(payload.get("outcome"))
        if outcome not in {"UP", "DOWN"} and spot is not None and strike is not None:
            outcome = "UP" if spot >= strike else "DOWN"
        if outcome not in {"UP", "DOWN"}:
            outcome = _norm_outcome(payload.get("active_side"))
        if not slug or outcome not in {"UP", "DOWN"}:
            continue
        out[slug] = SettlementRow(
            slug=slug,
            outcome=outcome,
            spot=spot,
            strike=strike,
            inventory_side=_norm_outcome(payload.get("inventory_side") or payload.get("active_side")),
            instrument_id=str(payload.get("instrument_id") or ""),
            settlement_pnl_usdc=(
                float(payload["settlement_pnl_usdc"])
                if payload.get("settlement_pnl_usdc") is not None
                else None
            ),
        )
    return out


def _load_submit_contexts(
    conn: sqlite3.Connection,
    run_id: Optional[str],
    cutoff_iso: Optional[str],
) -> Dict[str, SubmitContext]:
    where: List[str] = ["event_type='ORDER_SUBMIT'"]
    params: List[object] = []
    if run_id:
        where.append("run_id=?")
        params.append(run_id)
    if cutoff_iso:
        where.append("ts>=?")
        params.append(cutoff_iso)

    sql = f"""
        SELECT client_order_id, payload_json
        FROM order_events
        WHERE {" AND ".join(where)}
        ORDER BY id ASC
    """
    out: Dict[str, SubmitContext] = {}
    for row in conn.execute(sql, tuple(params)).fetchall():
        client_order_id = str(row["client_order_id"] or "")
        if not client_order_id:
            continue
        payload = _safe_json_loads(row["payload_json"])
        if not isinstance(payload, dict):
            continue
        quality = payload.get("entry_quality")
        quality_d = quality if isinstance(quality, dict) else {}
        reasons = quality_d.get("entry_quality_reasons")
        if isinstance(reasons, list):
            reasons_txt = "|".join(str(x) for x in reasons)
        else:
            reasons_txt = str(reasons or "")
        out[client_order_id] = SubmitContext(
            client_order_id=client_order_id,
            entry_mode=str(payload.get("entry_mode") or ""),
            size_multiplier=(
                float(payload["size_multiplier"])
                if payload.get("size_multiplier") is not None
                else None
            ),
            entry_quality_quote_price_cap=(
                float(payload["entry_quality_quote_price_cap"])
                if payload.get("entry_quality_quote_price_cap") is not None
                else None
            ),
            entry_quality_label=(
                str(quality_d.get("entry_quality_label"))
                if quality_d.get("entry_quality_label") is not None
                else None
            ),
            entry_quality_risk=(
                float(quality_d["entry_quality_risk"])
                if quality_d.get("entry_quality_risk") is not None
                else None
            ),
            entry_quality_post_entry_decay_risk=(
                float(quality_d["entry_quality_post_entry_decay_risk"])
                if quality_d.get("entry_quality_post_entry_decay_risk") is not None
                else None
            ),
            entry_quality_size_multiplier=(
                float(quality_d["entry_quality_size_multiplier"])
                if quality_d.get("entry_quality_size_multiplier") is not None
                else None
            ),
            entry_quality_suggested_size_multiplier=(
                float(quality_d["entry_quality_suggested_size_multiplier"])
                if quality_d.get("entry_quality_suggested_size_multiplier") is not None
                else None
            ),
            entry_quality_min_expected_uplift_usdc=(
                float(quality_d["entry_quality_min_expected_uplift_usdc"])
                if quality_d.get("entry_quality_min_expected_uplift_usdc") is not None
                else None
            ),
            entry_quality_quote_placement_mode=(
                str(quality_d.get("entry_quality_quote_placement_mode"))
                if quality_d.get("entry_quality_quote_placement_mode") is not None
                else None
            ),
            entry_quality_reasons=reasons_txt,
        )
    return out


def _latest_snapshot_before(
    snapshots_by_slug: Dict[str, List[SignalSnapshot]],
    slug: str,
    target_ts_epoch: float,
    max_age_sec: float,
) -> Optional[SignalSnapshot]:
    snaps = snapshots_by_slug.get(slug)
    if not snaps:
        return None
    epochs = [s.ts_epoch for s in snaps]
    idx = bisect.bisect_right(epochs, target_ts_epoch) - 1
    if idx < 0:
        return None
    snap = snaps[idx]
    if target_ts_epoch - snap.ts_epoch > max_age_sec:
        return None
    return snap


def _score_value(snapshot: Optional[SignalSnapshot]) -> Optional[float]:
    if snapshot is None:
        return None
    if snapshot.main_score is not None:
        return snapshot.main_score
    return snapshot.side_score


def _bucket_post_decay(delta: Optional[float]) -> str:
    if delta is None:
        return "missing"
    if delta <= -0.30:
        return "<=-0.30"
    if delta <= -0.10:
        return "-0.30--0.10"
    if delta < 0.10:
        return "-0.10-0.10"
    return ">=0.10"


def _compute_chase_score(
    *,
    fill_price: float,
    main_score_abs: Optional[float],
    signed_sms_now: Optional[float],
    delta_score_60s_pre: Optional[float],
) -> tuple[int, List[str]]:
    score = 0
    reasons: List[str] = []

    if fill_price >= 0.85:
        score += 2
        reasons.append("price>=0.85")
    elif fill_price >= 0.75:
        score += 1
        reasons.append("price>=0.75")

    if signed_sms_now is not None and abs(signed_sms_now) >= 60.0:
        score += 1
        reasons.append("|signed_sms|>=60")

    if main_score_abs is not None and main_score_abs >= 0.55:
        score += 1
        reasons.append("|main_score|>=0.55")

    if delta_score_60s_pre is not None:
        if delta_score_60s_pre >= 0.40:
            score += 1
            reasons.append("delta_score_60s>=0.40")
        elif 0.20 <= delta_score_60s_pre < 0.40:
            reasons.append("delta_score_60s mid-band")

    return score, reasons


def _is_chase_entry(chase_score: int, fill_price: float, signed_sms_now: Optional[float]) -> bool:
    if chase_score >= 3:
        return True
    return fill_price >= 0.85 and signed_sms_now is not None and abs(signed_sms_now) >= 60.0


def _post_entry_decay_label(delta_30: Optional[float], delta_60: Optional[float]) -> str:
    if delta_60 is not None and delta_60 <= -0.25:
        return "severe"
    if delta_30 is not None and delta_30 <= -0.10:
        return "moderate"
    if delta_60 is not None and delta_60 >= 0.10:
        return "stable_plus"
    if delta_30 is None and delta_60 is None:
        return "missing"
    return "stable"


def main() -> int:
    ap = argparse.ArgumentParser(description="Recent BUY fill score-decay report from trade_journal.db")
    ap.add_argument("--db", default="./logs/trade_journal.db", help="SQLite DB path")
    ap.add_argument("--run-id", default=None, help="Optional run_id filter")
    ap.add_argument("--hours", type=int, default=48, help="Lookback hours")
    ap.add_argument(
        "--csv-out",
        default="./logs/reports/recent_buy_fill_report.csv",
        help="CSV output path",
    )
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found: {db_path}")
        return 1

    cutoff_iso = _to_iso_utc(args.hours)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    fills = list(_iter_buy_fills(conn, args.run_id, cutoff_iso))
    settlements = _load_settlements_compat(conn, args.run_id, cutoff_iso)
    snapshots_by_slug = _load_signal_snapshots(conn, args.run_id, cutoff_iso)
    submit_contexts = _load_submit_contexts(conn, args.run_id, cutoff_iso)

    rows_out: List[dict] = []
    for fill in fills:
        settlement = settlements.get(fill.slug)
        if settlement is None:
            continue
        submit_ctx = submit_contexts.get(fill.client_order_id)

        snap_now = _latest_snapshot_before(snapshots_by_slug, fill.slug, fill.ts_epoch, 45.0)
        snap_prev_60 = _latest_snapshot_before(snapshots_by_slug, fill.slug, fill.ts_epoch - 60.0, 45.0)
        snap_post_10 = _latest_snapshot_before(snapshots_by_slug, fill.slug, fill.ts_epoch + 10.0, 45.0)
        snap_post_30 = _latest_snapshot_before(snapshots_by_slug, fill.slug, fill.ts_epoch + 30.0, 45.0)
        snap_post_60 = _latest_snapshot_before(snapshots_by_slug, fill.slug, fill.ts_epoch + 60.0, 45.0)

        token_outcome = _infer_token_outcome(fill, settlement, snap_now)
        if token_outcome not in {"UP", "DOWN"}:
            continue

        bid_now, ask_now = _snapshot_best_prices(snap_now, token_outcome)
        score_now = _score_value(snap_now)
        score_prev_60 = _score_value(snap_prev_60)
        score_post_10 = _score_value(snap_post_10)
        score_post_30 = _score_value(snap_post_30)
        score_post_60 = _score_value(snap_post_60)
        signed_sms_now = _signed_spot_minus_strike(token_outcome, snap_now.spot_minus_strike if snap_now else None)
        delta_score_60s_pre = (
            (score_now - score_prev_60)
            if score_now is not None and score_prev_60 is not None
            else None
        )
        delta_score_post_10 = (
            (score_post_10 - score_now)
            if score_post_10 is not None and score_now is not None
            else None
        )
        delta_score_post_30 = (
            (score_post_30 - score_now)
            if score_post_30 is not None and score_now is not None
            else None
        )
        delta_score_post_60 = (
            (score_post_60 - score_now)
            if score_post_60 is not None and score_now is not None
            else None
        )
        chase_score, chase_reasons = _compute_chase_score(
            fill_price=fill.price,
            main_score_abs=(abs(score_now) if score_now is not None else None),
            signed_sms_now=signed_sms_now,
            delta_score_60s_pre=delta_score_60s_pre,
        )
        is_chase = _is_chase_entry(chase_score, fill.price, signed_sms_now)
        won = int(token_outcome == settlement.outcome)

        rows_out.append(
            {
                "fill_id": fill.id,
                "fill_ts": fill.ts,
                "slug": fill.slug,
                "token_outcome": token_outcome,
                "settlement_outcome": settlement.outcome,
                "won": won,
                "fill_price": fill.price,
                "qty": fill.qty,
                "main_score_now": score_now,
                "main_score_60s_pre": score_prev_60,
                "delta_score_60s_pre": delta_score_60s_pre,
                "main_score_post_10s": score_post_10,
                "main_score_post_30s": score_post_30,
                "main_score_post_60s": score_post_60,
                "delta_score_post_10s": delta_score_post_10,
                "delta_score_post_30s": delta_score_post_30,
                "delta_score_post_60s": delta_score_post_60,
                "post_decay_bucket_10s": _bucket_post_decay(delta_score_post_10),
                "post_decay_bucket_30s": _bucket_post_decay(delta_score_post_30),
                "post_decay_bucket_60s": _bucket_post_decay(delta_score_post_60),
                "shadow_score_now": (snap_now.shadow_score if snap_now else None),
                "signed_spot_minus_strike_now": signed_sms_now,
                "ret_30_bps_now": (snap_now.ret_30_bps if snap_now else None),
                "breakout_persistence_60s_now": (
                    snap_now.breakout_persistence_60s if snap_now else None
                ),
                "submit_entry_mode": (submit_ctx.entry_mode if submit_ctx else None),
                "submit_size_multiplier": (submit_ctx.size_multiplier if submit_ctx else None),
                "entry_quality_label": (submit_ctx.entry_quality_label if submit_ctx else None),
                "entry_quality_risk": (submit_ctx.entry_quality_risk if submit_ctx else None),
                "entry_quality_post_entry_decay_risk": (
                    submit_ctx.entry_quality_post_entry_decay_risk if submit_ctx else None
                ),
                "entry_quality_size_multiplier": (
                    submit_ctx.entry_quality_size_multiplier if submit_ctx else None
                ),
                "entry_quality_suggested_size_multiplier": (
                    submit_ctx.entry_quality_suggested_size_multiplier if submit_ctx else None
                ),
                "entry_quality_min_expected_uplift_usdc": (
                    submit_ctx.entry_quality_min_expected_uplift_usdc if submit_ctx else None
                ),
                "entry_quality_quote_placement_mode": (
                    submit_ctx.entry_quality_quote_placement_mode if submit_ctx else None
                ),
                "entry_quality_quote_price_cap": (
                    submit_ctx.entry_quality_quote_price_cap if submit_ctx else None
                ),
                "entry_quality_reasons": (
                    submit_ctx.entry_quality_reasons if submit_ctx else ""
                ),
                "bid_now": bid_now,
                "ask_now": ask_now,
                "time_left_sec": (snap_now.time_left_sec if snap_now else None),
                "chase_score": chase_score,
                "is_chase_entry": int(is_chase),
                "chase_reasons": "|".join(chase_reasons),
                "post_entry_decay_label": _post_entry_decay_label(
                    delta_score_post_30, delta_score_post_60
                ),
            }
        )

    csv_out = Path(args.csv_out)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    if rows_out:
        with csv_out.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
            writer.writeheader()
            writer.writerows(rows_out)

    print("=" * 100)
    print("Recent BUY Fill Report")
    print("=" * 100)
    print(f"db: {db_path}")
    print(f"lookback_hours: {args.hours}")
    print(f"rows: {len(rows_out)}")
    print(f"csv_out: {csv_out}")
    if not rows_out:
        return 0

    chase_rows = [r for r in rows_out if int(r["is_chase_entry"]) == 1]
    print(f"chase_entries: {len(chase_rows)} / {len(rows_out)}")
    print(
        "avg_fill_price="
        f"{_fmt(mean(float(r['fill_price']) for r in rows_out))} "
        "avg_main_score_abs="
        f"{_fmt(mean(abs(float(r['main_score_now'])) for r in rows_out if r['main_score_now'] is not None))}"
    )
    print("")

    rows_sorted = sorted(rows_out, key=lambda r: (r["fill_ts"], r["fill_id"]), reverse=True)
    print("Recent fills:")
    for r in rows_sorted[:20]:
        print(
            f"- {r['fill_ts']} slug={r['slug']} outcome={r['token_outcome']} "
            f"fill={_fmt(float(r['fill_price']), 2)} score={_fmt(r['main_score_now'])} "
            f"d10={_fmt(r['delta_score_post_10s'])} d30={_fmt(r['delta_score_post_30s'])} "
            f"d60={_fmt(r['delta_score_post_60s'])} "
            f"won={r['won']} chase={r['is_chase_entry']} reasons={r['chase_reasons'] or '-'} "
            f"eq={r['entry_quality_label'] or '-'} risk={_fmt(r['entry_quality_risk'])} "
            f"decay={r['post_entry_decay_label']} "
            f"mult={_fmt(r['submit_size_multiplier'])}/{_fmt(r['entry_quality_suggested_size_multiplier'])} "
            f"place={r['entry_quality_quote_placement_mode'] or '-'}"
        )

    print("")
    print("Chase subset:")
    for r in sorted(chase_rows, key=lambda x: (float(x["fill_price"]), x["fill_ts"]), reverse=True)[:20]:
        print(
            f"- {r['fill_ts']} slug={r['slug']} outcome={r['token_outcome']} "
            f"fill={_fmt(float(r['fill_price']), 2)} score={_fmt(r['main_score_now'])} "
            f"sms={_fmt(r['signed_spot_minus_strike_now'], 2)} "
            f"pre60={_fmt(r['delta_score_60s_pre'])} "
            f"d10={_fmt(r['delta_score_post_10s'])} d30={_fmt(r['delta_score_post_30s'])} "
            f"d60={_fmt(r['delta_score_post_60s'])} won={r['won']}"
        )

    print("")
    print("Diagnostics:")
    for label, subset in (
        ("all", rows_out),
        ("chase_only", chase_rows),
        ("non_chase", [r for r in rows_out if int(r["is_chase_entry"]) == 0]),
    ):
        if not subset:
            continue
        win_rate = 100.0 * sum(int(r["won"]) for r in subset) / len(subset)
        avg_fill = mean(float(r["fill_price"]) for r in subset)
        med_fill = median(float(r["fill_price"]) for r in subset)
        avg_d10 = mean(float(r["delta_score_post_10s"]) for r in subset if r["delta_score_post_10s"] is not None)
        avg_d30 = mean(float(r["delta_score_post_30s"]) for r in subset if r["delta_score_post_30s"] is not None)
        avg_d60 = mean(float(r["delta_score_post_60s"]) for r in subset if r["delta_score_post_60s"] is not None)
        print(
            f"- {label:<10} n={len(subset):<3d} win_rate={win_rate:>6.2f}% "
            f"avg_fill={_fmt(avg_fill, 3)} med_fill={_fmt(med_fill, 3)} "
            f"avg_d10={_fmt(avg_d10)} avg_d30={_fmt(avg_d30)} avg_d60={_fmt(avg_d60)}"
        )

    decay_labels = ("severe", "moderate", "stable", "stable_plus", "missing")
    print("")
    print("Post-entry decay:")
    for decay_label in decay_labels:
        subset = [r for r in rows_out if r["post_entry_decay_label"] == decay_label]
        if not subset:
            continue
        win_rate = 100.0 * sum(int(r["won"]) for r in subset) / len(subset)
        print(
            f"- {decay_label:<10} n={len(subset):<3d} win_rate={win_rate:>6.2f}% "
            f"avg_fill={_fmt(mean(float(r['fill_price']) for r in subset), 3)}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
