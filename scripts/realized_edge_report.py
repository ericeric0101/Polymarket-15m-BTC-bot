#!/usr/bin/env python3
"""
Realized edge calibration report from trade_journal.db.

This report is designed for hold-to-redeem style analysis. It aligns:
- BUY fills from order_events
- MARKET_SETTLEMENT rows from strategy_events
- nearest pre-fill live signal snapshots from strategy_events

It then reports which fill contexts actually produced positive realized edge.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Dict, Iterable, List, Optional


SIGNAL_EVENT_TYPES = (
    "MAIN_SIGNAL_CANDIDATE_LIVE",
    "LIVE_SIGNAL_COMPARE",
    "SIDE_DECISION",
    "NO_TRADE_ACTIVE_SIDE_NONE",
)
REGIME_EVENT_TYPE = "ENTRY_REGIME_OBSERVATION"


def _to_iso_utc(hours: int) -> Optional[str]:
    if hours <= 0:
        return None
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _safe_json_loads(s: object) -> object:
    if s is None:
        return None
    try:
        return json.loads(str(s))
    except Exception:
        return None


def _parse_ts_epoch(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return 0.0


def _norm_side(side_val: object) -> str:
    txt = str(side_val or "").strip().lower()
    if txt in {"1", "buy", "bid"} or "buy" in txt:
        return "buy"
    if txt in {"2", "sell", "ask"} or "sell" in txt:
        return "sell"
    return ""


def _norm_outcome(x: object) -> str:
    txt = str(x or "").strip().upper()
    if txt in {"UP", "YES", "BUY_UP"}:
        return "UP"
    if txt in {"DOWN", "NO", "BUY_DOWN"}:
        return "DOWN"
    return txt


def _bucket_side_score_abs(x: Optional[float]) -> str:
    if x is None:
        return "missing"
    ax = abs(x)
    if ax < 0.10:
        return "<0.10"
    if ax < 0.20:
        return "0.10-0.20"
    if ax < 0.35:
        return "0.20-0.35"
    return ">=0.35"


def _bucket_fair_edge(x: Optional[float]) -> str:
    if x is None:
        return "missing"
    if x < 0.0:
        return "<0"
    if x < 0.01:
        return "0-0.01"
    if x < 0.03:
        return "0.01-0.03"
    if x < 0.05:
        return "0.03-0.05"
    return ">=0.05"


def _bucket_time_left(x: Optional[float]) -> str:
    if x is None:
        return "missing"
    if x < 120:
        return "<120s"
    if x < 300:
        return "120-300s"
    if x < 600:
        return "300-600s"
    return ">=600s"


def _bucket_token_price(x: Optional[float]) -> str:
    if x is None:
        return "missing"
    if x < 0.20:
        return "<0.20"
    if x < 0.40:
        return "0.20-0.40"
    if x < 0.60:
        return "0.40-0.60"
    if x < 0.80:
        return "0.60-0.80"
    return ">=0.80"


def _bucket_spread(x: Optional[float]) -> str:
    if x is None:
        return "missing"
    if x <= 0.01:
        return "<=0.01"
    if x <= 0.02:
        return "0.01-0.02"
    return ">0.02"


def _bucket_signed_spot_minus_strike(x: Optional[float]) -> str:
    if x is None:
        return "missing"
    if x < 0:
        return "<0"
    if x < 10:
        return "0-10"
    if x < 30:
        return "10-30"
    if x < 60:
        return "30-60"
    return ">=60"


def _is_high_price_continuation(
    token_price: Optional[float],
    signed_spot_minus_strike: Optional[float],
    side_score: Optional[float],
) -> bool:
    if token_price is None or signed_spot_minus_strike is None or side_score is None:
        return False
    return (
        token_price >= 0.75
        and abs(signed_spot_minus_strike) >= 30.0
        and abs(side_score) >= 0.35
    )


def _is_near_strike_moderate_price(
    token_price: Optional[float],
    signed_spot_minus_strike: Optional[float],
    side_score: Optional[float],
) -> bool:
    if token_price is None or signed_spot_minus_strike is None or side_score is None:
        return False
    return (
        0.35 <= token_price <= 0.60
        and abs(signed_spot_minus_strike) <= 10.0
        and 0.20 <= abs(side_score) <= 0.35
    )


@dataclass
class FillRow:
    id: int
    ts: str
    ts_epoch: float
    run_id: str
    client_order_id: str
    slug: str
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


@dataclass
class SettlementRow:
    slug: str
    outcome: str
    spot: Optional[float]
    strike: Optional[float]
    inventory_side: str
    instrument_id: str
    settlement_pnl_usdc: Optional[float]


@dataclass
class SignalSnapshot:
    ts: str
    ts_epoch: float
    slug: str
    event_type: str
    active_side: str
    side_locked: Optional[bool]
    side_score: Optional[float]
    candidate_side: str
    time_left_sec: Optional[float]
    spot_minus_strike: Optional[float]
    bid_up: Optional[float]
    ask_up: Optional[float]
    bid_down: Optional[float]
    ask_down: Optional[float]


@dataclass
class RegimeObservation:
    ts: str
    ts_epoch: float
    slug: str
    regime_tag: str
    main_candidate_outcome: str
    main_score: Optional[float]
    main_score_abs: Optional[float]
    time_left_sec: Optional[float]
    spot_minus_strike: Optional[float]
    signed_spot_minus_strike: Optional[float]
    token_price: Optional[float]


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
    for row in conn.execute(sql, tuple(params)).fetchall():
        side = _norm_side(row["side"])
        if side != "buy":
            continue
        price = float(row["price"] or 0.0)
        qty = float(row["qty"] or 0.0)
        if price <= 0.0 or qty <= 0.0:
            continue
        payload = _safe_json_loads(row["payload_json"])
        payload_d = payload if isinstance(payload, dict) else {}
        slug = str(payload_d.get("slug") or payload_d.get("market_slug") or "")
        if not slug:
            continue
        yield FillRow(
            id=int(row["id"]),
            ts=str(row["ts"]),
            ts_epoch=_parse_ts_epoch(str(row["ts"])),
            run_id=str(row["run_id"]),
            client_order_id=str(row["client_order_id"] or ""),
            slug=slug,
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


def _load_settlements(conn: sqlite3.Connection, run_id: Optional[str], cutoff_iso: Optional[str]) -> Dict[str, SettlementRow]:
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
        outcome = _norm_outcome(payload.get("outcome"))
        if not slug or outcome not in {"UP", "DOWN"}:
            continue
        out[slug] = SettlementRow(
            slug=slug,
            outcome=outcome,
            spot=(float(payload["spot"]) if payload.get("spot") is not None else None),
            strike=(float(payload["strike"]) if payload.get("strike") is not None else None),
            inventory_side=_norm_outcome(payload.get("inventory_side")),
            instrument_id=str(payload.get("instrument_id") or ""),
            settlement_pnl_usdc=(
                float(payload["settlement_pnl_usdc"])
                if payload.get("settlement_pnl_usdc") is not None
                else None
            ),
        )
    return out


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
            active_side=_norm_outcome(payload.get("main_active_side") or payload.get("active_side")),
            side_locked=(
                bool(payload.get("main_side_locked"))
                if payload.get("main_side_locked") is not None
                else (bool(payload.get("side_locked")) if payload.get("side_locked") is not None else None)
            ),
            side_score=(float(side_score) if side_score is not None else None),
            candidate_side=_norm_outcome(payload.get("main_candidate_side")),
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
            bid_up=(float(payload["bid_up"]) if payload.get("bid_up") is not None else None),
            ask_up=(float(payload["ask_up"]) if payload.get("ask_up") is not None else None),
            bid_down=(float(payload["bid_down"]) if payload.get("bid_down") is not None else None),
            ask_down=(float(payload["ask_down"]) if payload.get("ask_down") is not None else None),
        )
        out.setdefault(slug, []).append(snap)
    return out


def _load_regime_observations(
    conn: sqlite3.Connection,
    run_id: Optional[str],
    cutoff_iso: Optional[str],
) -> Dict[str, List[RegimeObservation]]:
    where: List[str] = ["event_type=?"]
    params: List[object] = [REGIME_EVENT_TYPE]
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
    out: Dict[str, List[RegimeObservation]] = {}
    for row in conn.execute(sql, tuple(params)).fetchall():
        payload = _safe_json_loads(row["payload_json"])
        if not isinstance(payload, dict):
            continue
        slug = str(payload.get("slug") or payload.get("market_slug") or "")
        regime_tag = str(payload.get("regime_tag") or "")
        if not slug or not regime_tag:
            continue
        out.setdefault(slug, []).append(
            RegimeObservation(
                ts=str(row["ts"]),
                ts_epoch=_parse_ts_epoch(str(row["ts"])),
                slug=slug,
                regime_tag=regime_tag,
                main_candidate_outcome=_norm_outcome(payload.get("main_candidate_outcome")),
                main_score=(float(payload["main_score"]) if payload.get("main_score") is not None else None),
                main_score_abs=(
                    float(payload["main_score_abs"]) if payload.get("main_score_abs") is not None else None
                ),
                time_left_sec=(
                    float(payload["time_left_sec"]) if payload.get("time_left_sec") is not None else None
                ),
                spot_minus_strike=(
                    float(payload["spot_minus_strike"]) if payload.get("spot_minus_strike") is not None else None
                ),
                signed_spot_minus_strike=(
                    float(payload["signed_spot_minus_strike"])
                    if payload.get("signed_spot_minus_strike") is not None
                    else None
                ),
                token_price=(float(payload["token_price"]) if payload.get("token_price") is not None else None),
            )
        )
    return out


def _latest_snapshot_before(
    snapshots_by_slug: Dict[str, List[SignalSnapshot]],
    slug: str,
    fill_ts_epoch: float,
    max_age_sec: float,
) -> Optional[SignalSnapshot]:
    snaps = snapshots_by_slug.get(slug)
    if not snaps:
        return None
    epochs = [s.ts_epoch for s in snaps]
    idx = bisect.bisect_right(epochs, fill_ts_epoch) - 1
    if idx < 0:
        return None
    snap = snaps[idx]
    if fill_ts_epoch - snap.ts_epoch > max_age_sec:
        return None
    return snap


def _latest_regime_observation_before(
    observations_by_slug: Dict[str, List[RegimeObservation]],
    slug: str,
    fill_ts_epoch: float,
    max_age_sec: float,
) -> Optional[RegimeObservation]:
    observations = observations_by_slug.get(slug)
    if not observations:
        return None
    epochs = [o.ts_epoch for o in observations]
    idx = bisect.bisect_right(epochs, fill_ts_epoch) - 1
    if idx < 0:
        return None
    obs = observations[idx]
    if fill_ts_epoch - obs.ts_epoch > max_age_sec:
        return None
    return obs


def _infer_token_outcome(fill: FillRow, settlement: SettlementRow, snapshot: Optional[SignalSnapshot]) -> str:
    if snapshot is not None:
        if snapshot.candidate_side in {"UP", "DOWN"}:
            return snapshot.candidate_side
        if snapshot.active_side in {"UP", "DOWN"}:
            return snapshot.active_side
    if settlement.instrument_id and fill.instrument_id and settlement.instrument_id == fill.instrument_id:
        if settlement.inventory_side in {"UP", "DOWN"}:
            return settlement.inventory_side
    if settlement.inventory_side == "UP":
        return "DOWN"
    if settlement.inventory_side == "DOWN":
        return "UP"
    return ""


def _snapshot_best_prices(snapshot: Optional[SignalSnapshot], token_outcome: str) -> tuple[Optional[float], Optional[float]]:
    if snapshot is None:
        return (None, None)
    if token_outcome == "UP":
        return (snapshot.bid_up, snapshot.ask_up)
    if token_outcome == "DOWN":
        return (snapshot.bid_down, snapshot.ask_down)
    return (None, None)


def _signed_spot_minus_strike(token_outcome: str, spot_minus_strike: Optional[float]) -> Optional[float]:
    if spot_minus_strike is None:
        return None
    if token_outcome == "UP":
        return spot_minus_strike
    if token_outcome == "DOWN":
        return -spot_minus_strike
    return None


def _fmt(x: float, digits: int = 4) -> str:
    return f"{x:.{digits}f}"


def _print_bucket_summary(
    rows: List[dict],
    key: str,
    title: str,
    pnl_key: str = "realized_edge_usdc",
) -> None:
    grouped: Dict[str, List[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row.get(key) or "missing"), []).append(row)
    print(title)
    for bucket in sorted(grouped.keys()):
        bucket_rows = grouped[bucket]
        n = len(bucket_rows)
        wins = sum(1 for r in bucket_rows if float(r[pnl_key]) > 0)
        win_rate = (wins / n) * 100.0 if n else 0.0
        avg_edge = sum(float(r[pnl_key]) for r in bucket_rows) / n if n else 0.0
        sum_edge = sum(float(r[pnl_key]) for r in bucket_rows)
        print(
            f"- {bucket:<12} n={n:<4d} win_rate={win_rate:>6.2f}% "
            f"avg_edge_usdc={_fmt(avg_edge):>8} sum_edge_usdc={_fmt(sum_edge):>9}"
        )
    print("")


def _aggregate_market_rows(rows_out: List[dict]) -> List[dict]:
    grouped: Dict[str, List[dict]] = {}
    for row in rows_out:
        grouped.setdefault(str(row["slug"]), []).append(row)

    market_rows: List[dict] = []
    for slug, rows in grouped.items():
        rows = sorted(rows, key=lambda r: (r["ts"], r["fill_id"]))
        first = rows[0]
        total_qty = sum(float(r["qty"]) for r in rows)
        total_cost = sum(float(r["fill_price"]) * float(r["qty"]) for r in rows)
        total_commission = sum(float(r["commission_usdc"]) for r in rows)
        total_realized_edge_usdc = sum(float(r["realized_edge_usdc"]) for r in rows)
        avg_fill_price = (total_cost / total_qty) if total_qty > 0 else 0.0
        market_rows.append(
            {
                "slug": slug,
                "fill_count": len(rows),
                "first_fill_ts": first["ts"],
                "token_outcome": first["token_outcome"],
                "settlement_outcome": first["settlement_outcome"],
                "won": first["won"],
                "total_qty": total_qty,
                "avg_fill_price": avg_fill_price,
                "total_commission_usdc": total_commission,
                "market_realized_edge_usdc": total_realized_edge_usdc,
                "side_score": first["side_score"],
                "active_side": first["active_side"],
                "side_locked": first["side_locked"],
                "time_left_sec": first["time_left_sec"],
                "spot_minus_strike": first["spot_minus_strike"],
                "signed_spot_minus_strike": first["signed_spot_minus_strike"],
                "best_bid": first["best_bid"],
                "best_ask": first["best_ask"],
                "spread": first["spread"],
                "p_fair_submit": first["p_fair_submit"],
                "fair_minus_entry": (
                    first["p_fair_submit"] - avg_fill_price
                    if first["p_fair_submit"] is not None
                    else None
                ),
                "directional_edge_ps_submit": first["directional_edge_ps_submit"],
                "directional_edge_usdc_submit": first["directional_edge_usdc_submit"],
                "expected_net_usdc": sum(float(r["expected_net_usdc"] or 0.0) for r in rows),
                "robust_net_usdc_submit": sum(float(r["robust_net_usdc_submit"] or 0.0) for r in rows),
                "signal_event_type": first["signal_event_type"],
                "side_score_bucket": first["side_score_bucket"],
                "fair_minus_entry_bucket": _bucket_fair_edge(
                    (first["p_fair_submit"] - avg_fill_price) if first["p_fair_submit"] is not None else None
                ),
                "time_left_bucket": first["time_left_bucket"],
                "signed_spot_minus_strike_bucket": first["signed_spot_minus_strike_bucket"],
                "token_price_bucket": _bucket_token_price(avg_fill_price),
                "spread_bucket": first["spread_bucket"],
                "is_high_price_continuation": int(
                    _is_high_price_continuation(
                        token_price=avg_fill_price,
                        signed_spot_minus_strike=(
                            float(first["signed_spot_minus_strike"])
                            if first["signed_spot_minus_strike"] is not None
                            else None
                        ),
                        side_score=(float(first["side_score"]) if first["side_score"] is not None else None),
                    )
                ),
                "is_near_strike_moderate_price": int(
                    _is_near_strike_moderate_price(
                        token_price=avg_fill_price,
                        signed_spot_minus_strike=(
                            float(first["signed_spot_minus_strike"])
                            if first["signed_spot_minus_strike"] is not None
                            else None
                        ),
                        side_score=(float(first["side_score"]) if first["side_score"] is not None else None),
                    )
                ),
                "observed_target_regime": int(first.get("observed_target_regime", 0)),
                "observed_regime_tag": first.get("observed_regime_tag") or "",
                "observed_regime_ts": first.get("observed_regime_ts") or "",
                "observed_regime_time_left_sec": first.get("observed_regime_time_left_sec"),
                "observed_regime_signed_spot_minus_strike": first.get("observed_regime_signed_spot_minus_strike"),
                "observed_regime_token_price": first.get("observed_regime_token_price"),
            }
        )
    return sorted(market_rows, key=lambda r: r["first_fill_ts"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Realized edge calibration report from trade_journal.db")
    parser.add_argument("--db", default="./logs/trade_journal.db", help="SQLite DB path")
    parser.add_argument("--run-id", default=None, help="Optional run_id filter")
    parser.add_argument("--hours", type=int, default=0, help="Lookback hours (<=0 means all time)")
    parser.add_argument(
        "--csv-out",
        default="./logs/reports/realized_edge_trades.csv",
        help="Output CSV path for per-fill rows",
    )
    parser.add_argument(
        "--market-csv-out",
        default="./logs/reports/realized_edge_markets.csv",
        help="Output CSV path for per-market rows",
    )
    parser.add_argument(
        "--regime-csv-out",
        default="./logs/reports/regime_attribution_markets.csv",
        help="Output CSV path for markets that hit observation regimes",
    )
    parser.add_argument(
        "--snapshot-max-age-sec",
        type=float,
        default=180.0,
        help="Maximum age for nearest pre-fill signal snapshot",
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
    snapshots_by_slug = _load_signal_snapshots(conn, args.run_id, cutoff_iso)
    regime_observations_by_slug = _load_regime_observations(conn, args.run_id, cutoff_iso)
    fills = list(_iter_buy_fills(conn, args.run_id, cutoff_iso))
    conn.close()

    if not settlements:
        print("No MARKET_SETTLEMENT rows found in selected scope.")
        return 1
    if not fills:
        print("No BUY fills found in selected scope.")
        return 1

    rows_out: List[dict] = []
    missing_settlement = 0
    missing_snapshot = 0
    unresolved_token_side = 0
    for fill in fills:
        settlement = settlements.get(fill.slug)
        if settlement is None:
            missing_settlement += 1
            continue
        snapshot = _latest_snapshot_before(
            snapshots_by_slug=snapshots_by_slug,
            slug=fill.slug,
            fill_ts_epoch=fill.ts_epoch,
            max_age_sec=float(args.snapshot_max_age_sec),
        )
        regime_observation = _latest_regime_observation_before(
            observations_by_slug=regime_observations_by_slug,
            slug=fill.slug,
            fill_ts_epoch=fill.ts_epoch,
            max_age_sec=float(args.snapshot_max_age_sec),
        )
        if snapshot is None:
            missing_snapshot += 1
        token_outcome = _infer_token_outcome(fill, settlement, snapshot)
        if token_outcome not in {"UP", "DOWN"}:
            unresolved_token_side += 1
            continue
        best_bid, best_ask = _snapshot_best_prices(snapshot, token_outcome)
        spread = (
            (best_ask - best_bid)
            if best_bid is not None and best_ask is not None
            else None
        )
        fair_minus_entry = (
            fill.p_fair_submit - fill.price
            if fill.p_fair_submit is not None
            else None
        )
        fee_per_share = fill.commission_usdc / fill.qty if fill.qty > 0 else 0.0
        payoff_per_share = 1.0 if token_outcome == settlement.outcome else 0.0
        realized_edge_per_share = payoff_per_share - fill.price - fee_per_share
        realized_edge_usdc = realized_edge_per_share * fill.qty
        signed_sms = _signed_spot_minus_strike(token_outcome, snapshot.spot_minus_strike if snapshot else None)
        is_high_price_cont = _is_high_price_continuation(
            token_price=fill.price,
            signed_spot_minus_strike=signed_sms,
            side_score=(snapshot.side_score if snapshot else None),
        )
        is_near_strike_mod = _is_near_strike_moderate_price(
            token_price=fill.price,
            signed_spot_minus_strike=signed_sms,
            side_score=(snapshot.side_score if snapshot else None),
        )
        rows_out.append(
            {
                "fill_id": fill.id,
                "ts": fill.ts,
                "run_id": fill.run_id,
                "slug": fill.slug,
                "client_order_id": fill.client_order_id,
                "instrument_id": fill.instrument_id,
                "token_outcome": token_outcome,
                "settlement_outcome": settlement.outcome,
                "won": int(token_outcome == settlement.outcome),
                "fill_price": fill.price,
                "qty": fill.qty,
                "commission_usdc": fill.commission_usdc,
                "fee_per_share": fee_per_share,
                "payoff_per_share": payoff_per_share,
                "realized_edge_per_share": realized_edge_per_share,
                "realized_edge_usdc": realized_edge_usdc,
                "settlement_spot": settlement.spot,
                "settlement_strike": settlement.strike,
                "settlement_pnl_usdc_market": settlement.settlement_pnl_usdc,
                "side_score": snapshot.side_score if snapshot else None,
                "active_side": snapshot.active_side if snapshot else "",
                "side_locked": snapshot.side_locked if snapshot else None,
                "time_left_sec": snapshot.time_left_sec if snapshot else None,
                "spot_minus_strike": snapshot.spot_minus_strike if snapshot else None,
                "signed_spot_minus_strike": signed_sms,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": spread,
                "p_fair_submit": fill.p_fair_submit,
                "fair_minus_entry": fair_minus_entry,
                "directional_edge_ps_submit": fill.directional_edge_ps_submit,
                "directional_edge_usdc_submit": fill.directional_edge_usdc_submit,
                "expected_net_usdc": fill.expected_net_usdc,
                "robust_net_usdc_submit": fill.robust_net_usdc_submit,
                "fee_ps_submit": fill.fee_ps_submit,
                "other_cost_ps_submit": fill.other_cost_ps_submit,
                "exec_penalty_usdc_submit": fill.exec_penalty_usdc_submit,
                "signal_event_type": snapshot.event_type if snapshot else "",
                "side_score_bucket": _bucket_side_score_abs(snapshot.side_score if snapshot else None),
                "fair_minus_entry_bucket": _bucket_fair_edge(fair_minus_entry),
                "time_left_bucket": _bucket_time_left(snapshot.time_left_sec if snapshot else None),
                "signed_spot_minus_strike_bucket": _bucket_signed_spot_minus_strike(signed_sms),
                "token_price_bucket": _bucket_token_price(fill.price),
                "spread_bucket": _bucket_spread(spread),
                "is_high_price_continuation": int(is_high_price_cont),
                "is_near_strike_moderate_price": int(is_near_strike_mod),
                "observed_target_regime": int(regime_observation is not None),
                "observed_regime_tag": regime_observation.regime_tag if regime_observation is not None else "",
                "observed_regime_ts": regime_observation.ts if regime_observation is not None else "",
                "observed_regime_time_left_sec": (
                    regime_observation.time_left_sec if regime_observation is not None else None
                ),
                "observed_regime_signed_spot_minus_strike": (
                    regime_observation.signed_spot_minus_strike if regime_observation is not None else None
                ),
                "observed_regime_token_price": (
                    regime_observation.token_price if regime_observation is not None else None
                ),
            }
        )

    out_path = Path(args.csv_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if rows_out:
        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
            writer.writeheader()
            writer.writerows(rows_out)
    market_rows = _aggregate_market_rows(rows_out)
    market_out_path = Path(args.market_csv_out)
    market_out_path.parent.mkdir(parents=True, exist_ok=True)
    if market_rows:
        with market_out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(market_rows[0].keys()))
            writer.writeheader()
            writer.writerows(market_rows)
    regime_rows = [r for r in market_rows if int(r.get("observed_target_regime", 0)) == 1]
    regime_out_path = Path(args.regime_csv_out)
    regime_out_path.parent.mkdir(parents=True, exist_ok=True)
    if regime_rows:
        with regime_out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(regime_rows[0].keys()))
            writer.writeheader()
            writer.writerows(regime_rows)

    if not rows_out:
        print("No aligned fill rows after settlement/signal matching.")
        print(f"missing_settlement={missing_settlement} missing_snapshot={missing_snapshot} unresolved_token_side={unresolved_token_side}")
        return 1

    total = len(rows_out)
    wins = sum(int(r["won"]) for r in rows_out)
    losses = total - wins
    sum_edge = sum(float(r["realized_edge_usdc"]) for r in rows_out)
    avg_edge = sum_edge / total
    win_rate = (wins / total) * 100.0

    print("=" * 100)
    print("Realized Edge Calibration Report")
    print("=" * 100)
    print(f"db: {db_path}")
    print(f"run_id: {args.run_id or '(all)'}")
    print(f"lookback_hours: {args.hours if args.hours > 0 else '(all time)'}")
    if cutoff_iso:
        print(f"cutoff_utc: {cutoff_iso}")
    print(f"rows: {total}")
    print(f"markets: {len(market_rows)}")
    print(f"wins/losses: {wins}/{losses} (win_rate={win_rate:.2f}%)")
    print(f"sum_realized_edge_usdc: {_fmt(sum_edge)}")
    print(f"avg_realized_edge_usdc: {_fmt(avg_edge)}")
    print(f"csv_out: {out_path}")
    print(f"market_csv_out: {market_out_path}")
    print(f"regime_csv_out: {regime_out_path}")
    print("")
    print(
        f"alignment: missing_settlement={missing_settlement} "
        f"missing_snapshot={missing_snapshot} unresolved_token_side={unresolved_token_side}"
    )
    print("")

    _print_bucket_summary(rows_out, "side_score_bucket", "By |side_score| bucket:")
    _print_bucket_summary(rows_out, "fair_minus_entry_bucket", "By fair-entry bucket:")
    _print_bucket_summary(rows_out, "time_left_bucket", "By time-left bucket:")
    _print_bucket_summary(rows_out, "signed_spot_minus_strike_bucket", "By signed spot-strike bucket:")
    _print_bucket_summary(rows_out, "token_price_bucket", "By token-price bucket:")
    _print_bucket_summary(rows_out, "spread_bucket", "By spread bucket:")

    high_price_rows = [r for r in rows_out if int(r["is_high_price_continuation"]) == 1]
    near_strike_rows = [r for r in rows_out if int(r["is_near_strike_moderate_price"]) == 1]
    observed_regime_rows = [r for r in rows_out if int(r["observed_target_regime"]) == 1]

    print("Custom regime buckets:")
    for label, subset in [
        ("high_price_continuation", high_price_rows),
        ("near_strike_moderate_price", near_strike_rows),
        ("observed_target_regime", observed_regime_rows),
    ]:
        n = len(subset)
        wins = sum(int(r["won"]) for r in subset)
        win_rate = (wins / n) * 100.0 if n else 0.0
        avg_edge = sum(float(r["realized_edge_usdc"]) for r in subset) / n if n else 0.0
        sum_edge = sum(float(r["realized_edge_usdc"]) for r in subset)
        print(
            f"- {label:<28} n={n:<4d} win_rate={win_rate:>6.2f}% "
            f"avg_edge_usdc={_fmt(avg_edge):>8} sum_edge_usdc={_fmt(sum_edge):>9}"
        )
    print("")

    market_total = len(market_rows)
    if market_total > 0:
        market_wins = sum(int(r["won"]) for r in market_rows)
        market_sum = sum(float(r["market_realized_edge_usdc"]) for r in market_rows)
        market_avg = market_sum / market_total
        market_win_rate = (market_wins / market_total) * 100.0
        print("Market-level summary:")
        print(
            f"- wins/losses: {market_wins}/{market_total - market_wins} "
            f"(win_rate={market_win_rate:.2f}%)"
        )
        print(f"- sum_market_realized_edge_usdc: {_fmt(market_sum)}")
        print(f"- avg_market_realized_edge_usdc: {_fmt(market_avg)}")
        print("")

        _print_bucket_summary(market_rows, "side_score_bucket", "Market-level by |side_score| bucket:", pnl_key="market_realized_edge_usdc")
        _print_bucket_summary(market_rows, "fair_minus_entry_bucket", "Market-level by fair-entry bucket:", pnl_key="market_realized_edge_usdc")
        _print_bucket_summary(market_rows, "time_left_bucket", "Market-level by time-left bucket:", pnl_key="market_realized_edge_usdc")
        _print_bucket_summary(market_rows, "signed_spot_minus_strike_bucket", "Market-level by signed spot-strike bucket:", pnl_key="market_realized_edge_usdc")
        _print_bucket_summary(market_rows, "token_price_bucket", "Market-level by token-price bucket:", pnl_key="market_realized_edge_usdc")
        _print_bucket_summary(market_rows, "spread_bucket", "Market-level by spread bucket:", pnl_key="market_realized_edge_usdc")

        market_high_price_rows = [r for r in market_rows if int(r["is_high_price_continuation"]) == 1]
        market_near_strike_rows = [r for r in market_rows if int(r["is_near_strike_moderate_price"]) == 1]
        market_observed_regime_rows = [r for r in market_rows if int(r["observed_target_regime"]) == 1]
        print("Market-level custom regime buckets:")
        for label, subset in [
            ("high_price_continuation", market_high_price_rows),
            ("near_strike_moderate_price", market_near_strike_rows),
            ("observed_target_regime", market_observed_regime_rows),
        ]:
            n = len(subset)
            wins = sum(int(r["won"]) for r in subset)
            win_rate = (wins / n) * 100.0 if n else 0.0
            avg_edge = mean(float(r["market_realized_edge_usdc"]) for r in subset) if n else 0.0
            sum_edge = sum(float(r["market_realized_edge_usdc"]) for r in subset)
            print(
                f"- {label:<28} n={n:<4d} win_rate={win_rate:>6.2f}% "
                f"avg_edge_usdc={_fmt(avg_edge):>8} sum_edge_usdc={_fmt(sum_edge):>9}"
            )
        print("")

        if market_observed_regime_rows:
            _print_bucket_summary(
                market_observed_regime_rows,
                "fair_minus_entry_bucket",
                "Observed regime markets by fair-entry bucket:",
                pnl_key="market_realized_edge_usdc",
            )
            _print_bucket_summary(
                market_observed_regime_rows,
                "side_score_bucket",
                "Observed regime markets by |side_score| bucket:",
                pnl_key="market_realized_edge_usdc",
            )
            _print_bucket_summary(
                market_observed_regime_rows,
                "token_price_bucket",
                "Observed regime markets by token-price bucket:",
                pnl_key="market_realized_edge_usdc",
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
