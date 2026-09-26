#!/usr/bin/env python3
"""Offline report over live entry research snapshots and their trade outcomes."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from bot.live_entry_research import build_shadow_labels, edge_semantics

DEFAULT_DB = ROOT / "data/trading/trade_journal.db"
DEFAULT_OUTPUT = ROOT / "reports/live_entry_quality"


def _number(value: Any) -> float | None:
    try:
        out = float(value)
        return out if out == out and abs(out) != float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


def _epoch(value: Any) -> float | None:
    numeric = _number(value)
    if numeric is not None:
        return numeric
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _payload(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(dict.fromkeys(k for row in rows for k in row)) or ["status"]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _bucket(value: float | None, edges: tuple[tuple[float, str], ...], *, missing="unavailable") -> str:
    if value is None:
        return missing
    for upper, label in edges:
        if value < upper:
            return label
    return edges[-1][1]


def _aggregate(rows: list[dict[str, Any]], key_field: str, buckets: list[tuple[str, str]]) -> list[dict[str, Any]]:
    output=[]
    for key, label in buckets:
        subset=[r for r in rows if r.get(key_field)==key]
        fills=[r for r in subset if r.get("filled")]
        settled=[r for r in fills if r.get("settlement_pnl_usdc") is not None]
        directional_settled=[r for r in fills if r.get("settlement_correct_if_taken") is not None]
        markouts=defaultdict(list)
        for row in subset:
            for horizon in (1,5,10,30):
                value=row.get(f"markout_{horizon}s")
                if value is not None: markouts[horizon].append(float(value))
        output.append({
            "bucket":label,"candidate_count":len(subset),"submitted_count":sum(bool(r.get("submitted")) for r in subset),
            "fill_count":len(fills),"fill_rate":len(fills)/len(subset) if subset else None,
            "settled_fill_count":len(settled),"directional_settlement_count":len(directional_settled),
            "settlement_win_rate":sum(bool(r.get("settlement_correct_if_taken")) for r in directional_settled)/len(directional_settled) if directional_settled else None,
            "mean_settlement_pnl_usdc":sum(float(r["settlement_pnl_usdc"]) for r in settled)/len(settled) if settled else None,
            "mean_markout_1s_ps":sum(markouts[1])/len(markouts[1]) if markouts[1] else None,
            "mean_markout_5s_ps":sum(markouts[5])/len(markouts[5]) if markouts[5] else None,
            "mean_markout_10s_ps":sum(markouts[10])/len(markouts[10]) if markouts[10] else None,
            "mean_markout_30s_ps":sum(markouts[30])/len(markouts[30]) if markouts[30] else None,
            "independent_markets":len({r.get("slug") for r in subset if r.get("slug")}),
        })
    return output


def _shadow_reject_counterfactual(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Settlement-direction proxy only; it does not imply a candidate was fillable."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("shadow_reject"):
            grouped[str(row.get("shadow_reject_bucket") or "unspecified")].append(row)
    output=[]
    for reason, subset in sorted(grouped.items()):
        settled=[r for r in subset if r.get("winner") in {"UP", "DOWN"}]
        scored=[]
        for row in settled:
            side=str(row.get("outcome_side") or (row.get("wanted_side") if row.get("entry_source")=="outcome_fast_follow" else "") or "").upper()
            price=_number(row.get("entry_price")); qty=_number(row.get("planned_quantity"))
            if side not in {"UP", "DOWN"} or price is None or qty is None:
                continue
            correct=side==str(row["winner"]).upper()
            # This is a no-fee, no-queue, no-fill-probability proxy. For maker
            # candidates the passive quote is not proof the price was executable.
            scored.append({"correct":correct, "pnl":qty*((1.0 if correct else 0.0)-price)})
        output.append({
            "shadow_reject_reason":reason,
            "candidate_count":len(subset),
            "settled_candidate_count":len(settled),
            "priced_directional_counterfactual_count":len(scored),
            "counterfactual_direction_accuracy":sum(x["correct"] for x in scored)/len(scored) if scored else None,
            "gross_resolution_pnl_if_fully_filled_usdc":sum(x["pnl"] for x in scored) if scored else None,
            "mean_gross_resolution_pnl_if_fully_filled_usdc":sum(x["pnl"] for x in scored)/len(scored) if scored else None,
            "counterfactual_basis":"settlement payout minus snapshot price times snapshot quantity; excludes fees, fill probability, queue position, impact and exits",
        })
    return output


def load_candidates(db_path: Path, *, return_metrics: bool = False):
    if not db_path.exists():
        raise FileNotFoundError(f"trade journal not found: {db_path}")
    uri=db_path.resolve().as_uri()+"?mode=ro&immutable=1"
    with sqlite3.connect(uri,uri=True) as conn:
        conn.row_factory=sqlite3.Row
        strategy_rows=conn.execute("SELECT id,ts,event_type,payload_json FROM strategy_events WHERE event_type IN ('ENTRY_DECISION_TRACE','ENTRY_RESEARCH_CANDIDATE_TERMINAL','ENTRY_RESEARCH_TELEMETRY_SUMMARY','FAST_FOLLOW_QUOTE_HANDOFF','FAST_FOLLOW_ENTRY_BLOCKED') ORDER BY id").fetchall()
        order_rows=conn.execute("SELECT id,ts,event_type,client_order_id,side,price,qty,status,expected_net_usdc,payload_json FROM order_events WHERE event_type IN ('ORDER_MAKER_INTENT','ORDER_SUBMIT','ORDER_FAST_FOLLOW_INTENT','ORDER_FAST_FOLLOW_SUBMIT','ORDER_DRY_RUN_SUBMITTED','ORDER_FILLED','FILL_MARKOUT') ORDER BY id").fetchall()
    candidates: dict[str,dict[str,Any]]={}
    snapshot_event_count = 0
    snapshot_ts_values=[]
    candidate_start_counts=defaultdict(int)
    summary_counters: dict[str, float] = {}
    for row in strategy_rows:
        payload=_payload(row["payload_json"])
        if row["event_type"] == "ENTRY_RESEARCH_TELEMETRY_SUMMARY":
            for key, value in payload.items():
                parsed = _number(value)
                if parsed is not None:
                    summary_counters[key] = parsed
            continue
        if row["event_type"] == "ENTRY_RESEARCH_CANDIDATE_TERMINAL":
            cid = str(payload.get("research_candidate_id") or "")
            if cid in candidates:
                candidates[cid].update({
                    "candidate_status": payload.get("candidate_status"),
                    "candidate_episode_end": _number(payload.get("candidate_episode_end")),
                    "candidate_update_count": _number(payload.get("candidate_update_count")) or candidates[cid].get("candidate_update_count", 0),
                })
            continue
        snap=payload.get("research_snapshot")
        if not isinstance(snap,dict):
            continue
        cid=str(snap.get("candidate_id") or payload.get("research_candidate_id") or "")
        if not cid:
            continue
        item=candidates.setdefault(cid,{})
        snapshot_event_count += 1
        timestamp=_epoch(row["ts"])
        if timestamp is not None:
            snapshot_ts_values.append(timestamp)
        if snap.get("candidate_started") is True:
            candidate_start_counts[cid] += 1
        item["snapshot_event_count"] = int(item.get("snapshot_event_count", 0)) + 1
        # Prefer later enriched records while preserving non-null first observations.
        for key,value in snap.items():
            if value is not None or key not in item:
                item[key]=value
        economics=payload.get("economics_context")
        if isinstance(economics,dict):
            item["_recompute_shadow_from_economics_context"]=True
            for source,target in (("fair_price","estimated_probability"),("limit_price","entry_price"),
                                  ("quantity","planned_quantity"),("resolution_ev_usdc","resolution_ev_usdc"),
                                  ("taker_fee_usdc","fee_usdc"),("execution_penalty_usdc","execution_penalty_usdc"),
                                  ("expected_net_usdc","robust_net_usdc")):
                value=_number(economics.get(source))
                if value is not None: item[target]=value
            item["fee_per_share"]=(item.get("fee_usdc")/item.get("planned_quantity") if item.get("fee_usdc") is not None and item.get("planned_quantity") else item.get("fee_per_share"))
            item["execution_penalty_per_share"]=(item.get("execution_penalty_usdc")/item.get("planned_quantity") if item.get("execution_penalty_usdc") is not None and item.get("planned_quantity") else item.get("execution_penalty_per_share"))
        item.update({"candidate_id":cid,"decision_event_type":row["event_type"],"decision_ts":_epoch(row["ts"]),
                     "decision_state":payload.get("state"),"decision_reason":payload.get("final_reason") or payload.get("reason"),
                     "entry_source":snap.get("entry_source") or ("outcome_fast_follow" if "FAST_FOLLOW" in row["event_type"] else "normal_maker")})
        item.setdefault("candidate_episode_start", _number(snap.get("candidate_episode_start")) or _epoch(row["ts"]))
        item.setdefault("candidate_status", snap.get("candidate_status") or "candidate_active")
        item["candidate_update_count"] = max(int(_number(item.get("candidate_update_count")) or 0),
            int(_number(snap.get("candidate_update_count")) or item["snapshot_event_count"]))
    join_metrics = {"submit_rows": 0, "submit_rows_with_candidate_id": 0, "submitted_candidates": 0,
                    "submit_rows_joined": 0, "orphan_submit_count": 0,
                    "fill_rows": 0, "fill_rows_with_candidate_id": 0, "filled_candidates": 0,
                    "fill_rows_joined": 0, "orphan_fill_count": 0}
    orders_by_id={}
    candidate_id_by_order_id={}
    submit_candidate_ids=set()
    fill_candidate_ids=set()
    pending_markouts=[]
    pending_fills=[]
    for row in order_rows:
        p=_payload(row["payload_json"])
        cid=str(p.get("research_candidate_id") or p.get("research_snapshot",{}).get("candidate_id") or "")
        typ=str(row["event_type"])
        if typ in {"ORDER_MAKER_INTENT","ORDER_SUBMIT","ORDER_FAST_FOLLOW_SUBMIT","ORDER_FAST_FOLLOW_INTENT","ORDER_DRY_RUN_SUBMITTED"}:
            actual_submit=typ in {"ORDER_SUBMIT","ORDER_FAST_FOLLOW_SUBMIT","ORDER_DRY_RUN_SUBMITTED"}
            if actual_submit:
                join_metrics["submit_rows"] += 1
                if cid:
                    join_metrics["submit_rows_with_candidate_id"] += 1
                    if cid in candidates:
                        join_metrics["submit_rows_joined"] += 1
                        submit_candidate_ids.add(cid)
                    else:
                        join_metrics["orphan_submit_count"] += 1
                else:
                    join_metrics["orphan_submit_count"] += 1
            item=candidates.get(cid) if cid else None
            research_snapshot=p.get("research_snapshot")
            if item is not None and isinstance(research_snapshot,dict):
                for key,value in research_snapshot.items():
                    if value is not None or key not in item:
                        item[key]=value
            economics=p.get("economics_context")
            if item is not None and isinstance(economics,dict):
                item["_recompute_shadow_from_economics_context"]=True
                for source,target in (("fair_price","estimated_probability"),("limit_price","entry_price"),
                                      ("quantity","planned_quantity"),("resolution_ev_usdc","resolution_ev_usdc"),
                                      ("taker_fee_usdc","fee_usdc"),("execution_penalty_usdc","execution_penalty_usdc"),
                                      ("expected_net_usdc","robust_net_usdc")):
                    value=_number(economics.get(source))
                    if value is not None: item[target]=value
                qty_value=_number(item.get("planned_quantity"))
                if qty_value:
                    if item.get("fee_usdc") is not None: item["fee_per_share"]=item["fee_usdc"]/qty_value
                    if item.get("execution_penalty_usdc") is not None: item["execution_penalty_per_share"]=item["execution_penalty_usdc"]/qty_value
            if row["client_order_id"]:
                order_id=str(row["client_order_id"])
                if cid:
                    candidate_id_by_order_id[order_id]=cid
                if item is not None:
                    orders_by_id[order_id]=item
            if item is not None:
                if actual_submit:
                    item["submitted"]=True
                    item["submit_event_type"]=typ
                    item["submit_ts"]=_epoch(row["ts"])
                    item["submitted_price"]=_number(row["price"])
                    item["submitted_qty"]=_number(row["qty"])
                    item["submitted_expected_net_usdc"]=_number(row["expected_net_usdc"])
                item["client_order_id"]=row["client_order_id"]
        elif typ=="ORDER_FILLED":
            join_metrics["fill_rows"] += 1
            order_id=str(row["client_order_id"] or "")
            cid=cid or candidate_id_by_order_id.get(order_id, "")
            if cid:
                join_metrics["fill_rows_with_candidate_id"] += 1
                if cid in candidates:
                    join_metrics["fill_rows_joined"] += 1
                    fill_candidate_ids.add(cid)
                else:
                    join_metrics["orphan_fill_count"] += 1
            else:
                join_metrics["orphan_fill_count"] += 1
            item=candidates.get(cid) if cid else orders_by_id.get(order_id)
            if item is None:
                pending_fills.append((cid, order_id, row))
                continue
            item["filled"]=True; item["fill_price"]=_number(row["price"]); item["fill_qty"]=_number(row["qty"]); item["fill_ts"]=_epoch(row["ts"])
            orders_by_id[order_id]=item
        elif typ=="FILL_MARKOUT":
            fill_id=str(p.get("fill_id") or row["client_order_id"] or "")
            target=orders_by_id.get(fill_id)
            if target is not None:
                horizon=_number(p.get("horizon_sec"))
                markout=_number(p.get("signed_markout_ps"))
                if horizon in {1.0,5.0,10.0,30.0} and markout is not None:
                    target[f"markout_{int(horizon)}s"]=markout
            else:
                pending_markouts.append((fill_id, p))
    for cid, order_id, row in pending_fills:
        item=candidates.get(cid) if cid else orders_by_id.get(order_id)
        if item is not None:
            item["filled"]=True; item["fill_price"]=_number(row["price"])
            item["fill_qty"]=_number(row["qty"]); item["fill_ts"]=_epoch(row["ts"])
            orders_by_id[order_id]=item
    # Resolve marks even if a journal import reordered them ahead of submit.
    # This remains linear in journal rows rather than scanning all candidates per mark.
    for fill_id, p in pending_markouts:
        target=orders_by_id.get(fill_id)
        if target is None:
            continue
        horizon=_number(p.get("horizon_sec")); markout=_number(p.get("signed_markout_ps"))
        if horizon in {1.0,5.0,10.0,30.0} and markout is not None:
            target[f"markout_{int(horizon)}s"]=markout
    with sqlite3.connect(uri,uri=True) as conn:
        conn.row_factory=sqlite3.Row
        settlements=conn.execute("SELECT payload_json FROM strategy_events WHERE event_type='MARKET_SETTLEMENT' ORDER BY id").fetchall()
    settlement_by_slug={}
    for row in settlements:
        p=_payload(row["payload_json"]); slug=str(p.get("slug") or "")
        if slug: settlement_by_slug[slug]=p
    filled_candidate_count_by_slug=defaultdict(int)
    for candidate in candidates.values():
        if candidate.get("filled") and candidate.get("slug"):
            filled_candidate_count_by_slug[str(candidate["slug"])] += 1
    for item in candidates.values():
        if item.get("filled"):
            item["candidate_status"] = "candidate_filled"
        elif item.get("submitted") and item.get("candidate_status") not in {"candidate_invalidated","candidate_expired","market_rolled"}:
            item["candidate_status"] = "candidate_submitted"
        item.setdefault("candidate_episode_end", _number(item.get("fill_ts")) or _number(item.get("submit_ts")) or _number(item.get("decision_ts")))
        item["settled"] = bool(settlement_by_slug.get(str(item.get("slug") or "")))
        settle=settlement_by_slug.get(str(item.get("slug") or ""),{})
        item["settled"]=bool(settle)
        item["winner"]=settle.get("outcome")
        market_pnl=_number(settle.get("settlement_pnl_usdc"))
        item["market_settlement_pnl_usdc"]=market_pnl
        if item.get("filled") and filled_candidate_count_by_slug.get(str(item.get("slug") or ""))==1:
            item["settlement_pnl_usdc"]=market_pnl
            item["pnl_attribution_reason"]="single_filled_candidate_in_market; market-level settlement PnL"
        else:
            item["settlement_pnl_usdc"]=None
            item["pnl_attribution_reason"]=("multiple_filled_candidates_in_market" if item.get("filled") else "candidate_not_filled")
        outcome_side=str(item.get("outcome_side") or item.get("wanted_side") or "").upper()
        if outcome_side in {"UP","DOWN"} and item.get("winner") in {"UP","DOWN"}:
            item["settlement_correct_if_taken"]=outcome_side==item["winner"]
        else: item["settlement_correct_if_taken"]=None
        quantity=_number(item.get("planned_quantity"))
        top_ask=_number(item.get("top_ask_size"))
        nearby_ask=_number(item.get("nearby_ask_depth"))
        depth=top_ask if top_ask is not None else nearby_ask
        depth_ok=(depth >= quantity) if depth is not None and quantity is not None else None
        source=str(item.get("entry_source") or "normal_maker")
        edge_data=edge_semantics(
            probability=_number(item.get("estimated_probability") if item.get("estimated_probability") is not None else item.get("fair_probability")),
            entry_price=_number(item.get("entry_price")),
            fee_per_share=_number(item.get("fee_per_share")),
            execution_penalty_per_share=_number(item.get("execution_penalty_per_share")),
            method=("fast_follow_resolution_ev_minus_fee_minus_markout" if source=="outcome_fast_follow" else "maker_quote_economics"),
        )
        item.update(edge_data)
        item["maker_gross_probability_edge_ps"] = edge_data["gross_probability_edge_ps"] if source=="normal_maker" else None
        item["maker_robust_net_usdc"] = _number(item.get("robust_net_usdc")) if source=="normal_maker" else None
        item["maker_expected_net_usdc"] = _number(item.get("expected_net_usdc")) if source=="normal_maker" else None
        item["directional_edge_ps"] = edge_data["net_directional_edge_ps"]
        if item.pop("_recompute_shadow_from_economics_context",False) or (
            not item.get("shadow_edge_verdict") and item.get("shadow_size_multiplier") is None
        ):
            shadow=build_shadow_labels(
                edge_ps=edge_data["net_directional_edge_ps"],
                robust_net_usdc=_number(item.get("robust_net_usdc")),
                fair=_number(item.get("estimated_probability") if item.get("estimated_probability") is not None else item.get("fair_probability")),
                entry_price=_number(item.get("entry_price")),
                abs_distance_bps=_number(item.get("abs_distance_bps")),
                safety_sigma=_number(item.get("safety_sigma")),
                crossings_last_120s=_number(item.get("crossings_last_120s")),
                weekend=(True if item.get("weekday_weekend")=="weekend" else False if item.get("weekday_weekend")=="weekday" else None),
                depth_adequate=depth_ok,
                gross_edge_ps=edge_data["gross_probability_edge_ps"],
                edge_cost_complete=edge_data["edge_cost_complete"],
            )
            item.update(shadow)
        elif not edge_data["edge_cost_complete"]:
            # Preserve prior non-edge shadow labels, but never misstate a gross
            # or incomplete-cost estimate as a complete net-edge verdict.
            item["shadow_edge_verdict"]="SHADOW_EDGE_UNAVAILABLE"
        multiplier=_number(item.get("shadow_size_multiplier"))
        item["shadow_size_counterfactual_pnl_usdc"]=(item["settlement_pnl_usdc"]*multiplier if item.get("settlement_pnl_usdc") is not None and multiplier is not None else None)
        item["candidate_market_key"]=item.get("slug")
        edge=_number(item.get("net_directional_edge_ps"))
        item["edge_bucket"]=_bucket(edge,((0,"<0"),(.01,"0–1c"),(.02,"1–2c"),(.05,"2–5c"),(float("inf"),">5c")))
        gross_edge=_number(item.get("gross_probability_edge_ps"))
        item["gross_edge_bucket"]=_bucket(gross_edge,((0,"<0"),(.01,"0–1c"),(.02,"1–2c"),(.05,"2–5c"),(float("inf"),">5c")))
        robust=_number(item.get("robust_net_usdc"))
        item["robust_net_bucket"]=_bucket(robust,((0,"<0"),(.1,"0–0.10"),(.5,"0.10–0.50"),(float("inf"),">0.50")))
        price=_number(item.get("entry_price"))
        item["price_bucket"]=_bucket(price,((.6,"<0.60"),(.7,"0.60–0.70"),(.75,"0.70–0.75"),(.8,"0.75–0.80"),(.85,"0.80–0.85"),(.9,"0.85–0.90"),(float("inf"),">0.90")))
        dist=_number(item.get("abs_distance_bps"))
        item["distance_bucket"]=_bucket(dist,((1,"<1bps"),(2,"1–2bps"),(5,"2–5bps"),(10,"5–10bps"),(20,"10–20bps"),(float("inf"),">20bps")))
        sigma=_number(item.get("safety_sigma"))
        item["safety_sigma_bucket"]=_bucket(sigma,((.5,"<0.5"),(1,"0.5–1"),(2,"1–2"),(float("inf"),">2")))
        crossings=_number(item.get("crossings_last_120s"))
        item["crossing_bucket"]="unavailable" if crossings is None else "0" if crossings==0 else "1" if crossings==1 else "2+"
        reason=str(item.get("shadow_reject_reason") or "not_shadow_rejected")
        item["shadow_reject_bucket"]=reason
    duplicate_candidate_count=sum(max(0,count-1) for count in candidate_start_counts.values())
    snapshot_timestamps=snapshot_ts_values
    duration_hours=max((max(snapshot_timestamps)-min(snapshot_timestamps))/3600.0, 1/60) if snapshot_timestamps else None
    comparable_edge_candidates=sum(
        r.get("entry_source")=="outcome_fast_follow"
        and _number(r.get("estimated_probability") if r.get("estimated_probability") is not None else r.get("fair_probability")) is not None
        and _number(r.get("entry_price")) is not None
        for r in candidates.values()
    )
    complete_edge_candidates=sum(bool(r.get("edge_cost_complete")) for r in candidates.values() if r.get("entry_source")=="outcome_fast_follow")
    join_metrics.update({
        "submitted_candidates": len(submit_candidate_ids), "filled_candidates": len(fill_candidate_ids),
        "submit_to_candidate_join_rate": join_metrics["submit_rows_joined"]/join_metrics["submit_rows"] if join_metrics["submit_rows"] else None,
        "fill_to_candidate_join_rate": join_metrics["fill_rows_joined"]/join_metrics["fill_rows"] if join_metrics["fill_rows"] else None,
        "submit_id_coverage_rate": join_metrics["submit_rows_with_candidate_id"]/join_metrics["submit_rows"] if join_metrics["submit_rows"] else None,
        "fill_id_coverage_rate": join_metrics["fill_rows_with_candidate_id"]/join_metrics["fill_rows"] if join_metrics["fill_rows"] else None,
        "duplicate_candidate_count": duplicate_candidate_count,
        "orphan_submit_count": join_metrics["orphan_submit_count"], "orphan_fill_count": join_metrics["orphan_fill_count"],
        "snapshot_event_count": snapshot_event_count, "edge_cost_comparable_candidates": comparable_edge_candidates,
        "edge_cost_complete_candidates": complete_edge_candidates,
        "edge_cost_complete_rate": complete_edge_candidates/comparable_edge_candidates if comparable_edge_candidates else None,
        "estimated_event_rate_per_hour": snapshot_event_count/duration_hours if duration_hours else None,
        "measurement_scope": "post_hardening_candidates_present" if candidates else "no_candidate_snapshots_historical_join_only",
        **summary_counters,
    })
    result=list(candidates.values())
    return (result,join_metrics) if return_metrics else result


def _entry_trace_count(db_path: Path) -> int:
    uri=db_path.resolve().as_uri()+"?mode=ro&immutable=1"
    with sqlite3.connect(uri,uri=True) as conn:
        row=conn.execute("SELECT COUNT(*) FROM strategy_events WHERE event_type='ENTRY_DECISION_TRACE'").fetchone()
    return int(row[0] or 0)


def generate_report(db_path: Path, output: Path) -> int:
    rows,join_metrics=load_candidates(db_path,return_metrics=True)
    entry_trace_count=_entry_trace_count(db_path)
    for candidate in rows:
        candidate.setdefault("submitted", False)
        candidate.setdefault("filled", False)
        candidate.setdefault("settled", False)
    output.mkdir(parents=True,exist_ok=True)
    _write_csv(output/"candidate_level.csv",rows)
    for filename,key,buckets in (
        ("edge_buckets.csv","edge_bucket",[(x,x) for x in ("<0","0–1c","1–2c","2–5c",">5c","unavailable")]),
        ("gross_edge_buckets.csv","gross_edge_bucket",[(x,x) for x in ("<0","0–1c","1–2c","2–5c",">5c","unavailable")]),
        ("price_buckets.csv","price_bucket",[(x,x) for x in ("<0.60","0.60–0.70","0.70–0.75","0.75–0.80","0.80–0.85","0.85–0.90",">0.90","unavailable")]),
        ("strike_distance_buckets.csv","distance_bucket",[(x,x) for x in ("<1bps","1–2bps","2–5bps","5–10bps","10–20bps",">20bps","unavailable")]),
        ("safety_sigma_buckets.csv","safety_sigma_bucket",[(x,x) for x in ("<0.5","0.5–1","1–2",">2","unavailable")]),
        ("crossing_buckets.csv","crossing_bucket",[(x,x) for x in ("0","1","2+","unavailable")]),
        ("weekday_weekend.csv","weekday_weekend",[(x,x) for x in ("weekday","weekend","unavailable")]),
        ("hour_blocks.csv","hour_block_et",[(x,x) for x in ("00-06","06-12","12-18","18-24","unavailable")]),
        ("shadow_size_counterfactual.csv","shadow_entry_risk_level",[(x,x) for x in ("LOW","MEDIUM","HIGH")]),
    ):
        _write_csv(output/filename,_aggregate(rows,key,buckets))
    _write_csv(output/"shadow_reject_counterfactual.csv",_shadow_reject_counterfactual(rows))
    candidate_updates=[int(_number(r.get("candidate_update_count")) or 0) for r in rows]
    sorted_updates=sorted(candidate_updates)
    p90=sorted_updates[min(len(sorted_updates)-1,int(0.9*(len(sorted_updates)-1)))] if sorted_updates else None
    join_rate=join_metrics.get("submit_to_candidate_join_rate")
    fill_rate=join_metrics.get("fill_to_candidate_join_rate")
    quality_row={
        "research_candidates":len(rows), "snapshot_events":join_metrics.get("snapshot_event_count",0),
        "updates_per_candidate_mean":sum(candidate_updates)/len(candidate_updates) if candidate_updates else None,
        "updates_per_candidate_p90":p90, "submit_join_rate":join_rate, "fill_join_rate":fill_rate,
        "orphan_submit":join_metrics.get("orphan_submit_count",0), "orphan_fill":join_metrics.get("orphan_fill_count",0),
        "snapshot_suppressed":join_metrics.get("research_snapshots_suppressed"),
        "estimated_event_rate_per_hour":join_metrics.get("estimated_event_rate_per_hour"),
        "net_edge_completeness":join_metrics.get("edge_cost_complete_rate"),
    }
    _write_csv(output/"candidate_join_quality.csv",[join_metrics])
    _write_csv(output/"telemetry_health.csv",[quality_row])
    settled=[r for r in rows if r.get("settlement_pnl_usdc") is not None]
    fill_count=sum(bool(r.get("filled")) for r in rows)
    lines=["# Live Entry Quality / Shadow Risk 報告","",
           f"- Journal: `{db_path}`",f"- Logical research candidates: {len(rows)}; snapshots: {join_metrics.get('snapshot_event_count',0)}; unique markets: {len({r.get('slug') for r in rows if r.get('slug')})}; submitted: {sum(bool(r.get('submitted')) for r in rows)}; filled: {fill_count}; settled filled candidates: {len(settled)}.",
           f"- `ENTRY_DECISION_TRACE` rows in the selected journal: {entry_trace_count}.",
           "- This is observation-only. Shadow reject and size-down columns are counterfactual labels; they do not alter trading authority.",
           (f"- Post-hardening candidate join quality: not yet measurable; the selected journal has no research candidate snapshots. Legacy unmatched submit/fill rows={join_metrics.get('orphan_submit_count',0)}/{join_metrics.get('orphan_fill_count',0)}."
            if not rows else f"- Candidate join quality: submit={join_rate if join_rate is not None else 'not yet measurable'}; fill={fill_rate if fill_rate is not None else 'not yet measurable'}; orphan submits={join_metrics.get('orphan_submit_count',0)}; orphan fills={join_metrics.get('orphan_fill_count',0)}."),
           f"- Net edge completeness: {join_metrics.get('edge_cost_complete_rate') if join_metrics.get('edge_cost_complete_rate') is not None else 'not yet measurable'}; snapshot suppression={join_metrics.get('research_snapshots_suppressed','not yet measurable')}; estimated event rate/hour={join_metrics.get('estimated_event_rate_per_hour','not yet measurable')}.",
           "- Research review threshold: at least 30 independent markets per bucket for screening; policy review should prefer 50–100+ independent markets over multiple weeks and weekday/weekend coverage.",
           "- Maker fill-to-candidate attribution uses the durable research candidate ID. Historical rows without it are not force-matched. Settlement PnL is market-level settlement telemetry, not an isolated per-order realized PnL.",
           "- Markouts are signed per-share follow-ups where available; missing horizons remain null. Public/venue fills and BBO are not synthesized.",
           "", "## Coverage and caveats", "",
           "See `candidate_level.csv` for source, freshness, strike provenance, BBO/depth, economics components, candidate status, fills, markouts and settlement join fields. A missing strike/spot/volatility value remains unavailable, not zero.",
           "- If this report says zero snapshots, the selected journal predates the new `research_snapshot` payload or is not the live journal; it is not evidence that no historical candidates existed.",
           "- Normal-maker `fair` comes from the configured pricer (normally digital when canonical spot/strike are available). The legacy `calibrated_probability` field copies this fair value; its name alone does not prove empirical calibration. Qualified strong-directional regimes use measured historical win rates.",
           "- Normal maker quotes are passive at the quote plan's BUY price. Outcome fast-follow uses a target-token executable ask and a separate FOK economics calculation.",
           "- `shadow_reject_counterfactual.csv` is a settlement-direction/full-fill gross proxy at the recorded candidate price and quantity, not realizable PnL; maker snapshots are passive prices and may never have been executable. It excludes fees, fills, queue position, impact, exits and sizing constraints.",
           "", "## Bucket reports", "",
           "`edge_buckets.csv` groups only complete net directional edge; `gross_edge_buckets.csv` is separate. `candidate_join_quality.csv` and `telemetry_health.csv` expose attribution and event-rate health. Other bucket CSVs report candidate/fill/settlement/markout counts; empty buckets are retained.",
           "", "## Live economics formula audit", "",
           "- Maker passive quote: spread-capture expected net is `expected_spread_capture + expected_rebate - adverse_selection_buffer`; the quote engine also computes `robust_net = expected_net - execution_penalty`. The normal maker path intentionally does not universally subtract the imported markout penalty from its admission gate; strong-directional regime eligibility gates on measured `resolution_ev >= min_expected_net`, while markout-adjusted robust net is telemetry. See final audit in task response.",
           "- Outcome FOK: `resolution_ev = qty × (fair_probability_for_outcome - limit_price)`; `robust_net = resolution_ev - taker_fee - empirical_adverse_markout_penalty`; the FOK allow/reject compares expected net to the configured minimum.",
           "- Operator command: `.venv/bin/python scripts/live_entry_quality_report.py --db data/trading/trade_journal.db --output reports/live_entry_quality`.",
           "- No live parameters or behavior are changed by this report generator.", ""]
    warnings=[]
    if not rows and join_metrics.get("submit_rows",0):
        warnings.append("no research candidate snapshots are present, so post-hardening join quality is not yet measurable; legacy submits/fills remain unmatched")
    else:
        if join_rate is not None and join_rate < .95: warnings.append("submit-to-candidate join rate is below 95%")
        if fill_rate is not None and fill_rate < .95: warnings.append("fill-to-candidate join rate is below 95%")
    completeness=join_metrics.get("edge_cost_complete_rate")
    if completeness is not None and completeness < .95: warnings.append("net edge cost completeness is below 95%")
    if p90 is not None and p90 > 100: warnings.append("updates per candidate p90 is unusually high (>100)")
    if warnings:
        lines[2:2]=["**DATA QUALITY WARNING**", "", *[f"- {warning}." for warning in warnings], ""]
    (output/"summary.md").write_text("\n".join(lines),encoding="utf-8")
    print(f"candidates={len(rows)} submits_joined={join_metrics.get('submit_rows_joined')}/{join_metrics.get('submit_rows')} fills_joined={join_metrics.get('fill_rows_joined')}/{join_metrics.get('fill_rows')} filled={fill_count} settled={len(settled)} output={output}")
    return 0


def main(argv: list[str] | None=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db",type=Path,default=DEFAULT_DB)
    parser.add_argument("--output",type=Path,default=DEFAULT_OUTPUT)
    args=parser.parse_args(argv)
    return generate_report(args.db,args.output)


if __name__=="__main__":
    raise SystemExit(main())
