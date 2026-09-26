#!/usr/bin/env python3
"""Offline report over live entry research snapshots and their trade outcomes."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from bot.live_entry_research import build_shadow_labels

DEFAULT_DB = ROOT / "data/trading/trade_journal.db"
DEFAULT_OUTPUT = ROOT / "reports/live_entry_quality"


def _number(value: Any) -> float | None:
    try:
        out = float(value)
        return out if out == out and abs(out) != float("inf") else None
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


def load_candidates(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        raise FileNotFoundError(f"trade journal not found: {db_path}")
    uri=db_path.resolve().as_uri()+"?mode=ro&immutable=1"
    with sqlite3.connect(uri,uri=True) as conn:
        conn.row_factory=sqlite3.Row
        strategy_rows=conn.execute("SELECT id,ts,event_type,payload_json FROM strategy_events WHERE event_type IN ('ENTRY_DECISION_TRACE','FAST_FOLLOW_QUOTE_HANDOFF','FAST_FOLLOW_ENTRY_BLOCKED') ORDER BY id").fetchall()
        order_rows=conn.execute("SELECT id,ts,event_type,client_order_id,side,price,qty,status,expected_net_usdc,payload_json FROM order_events WHERE event_type IN ('ORDER_MAKER_INTENT','ORDER_SUBMIT','ORDER_FAST_FOLLOW_INTENT','ORDER_FAST_FOLLOW_SUBMIT','ORDER_DRY_RUN_SUBMITTED','ORDER_FILLED','FILL_MARKOUT') ORDER BY id").fetchall()
    candidates: dict[str,dict[str,Any]]={}
    for row in strategy_rows:
        payload=_payload(row["payload_json"])
        snap=payload.get("research_snapshot")
        if not isinstance(snap,dict):
            continue
        cid=str(snap.get("candidate_id") or payload.get("research_candidate_id") or "")
        if not cid:
            continue
        item=candidates.setdefault(cid,{})
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
            if item.get("estimated_probability") is not None and item.get("entry_price") is not None:
                item["directional_edge_ps"]=item["estimated_probability"]-item["entry_price"]-(item.get("fee_per_share") or 0)-(item.get("execution_penalty_per_share") or 0)
        item.update({"candidate_id":cid,"decision_event_type":row["event_type"],"decision_ts":row["ts"],
                     "decision_state":payload.get("state"),"decision_reason":payload.get("final_reason") or payload.get("reason"),
                     "entry_source":snap.get("entry_source") or ("outcome_fast_follow" if "FAST_FOLLOW" in row["event_type"] else "normal_maker")})
    orders_by_id={}
    pending_markouts=[]
    for row in order_rows:
        p=_payload(row["payload_json"])
        cid=str(p.get("research_candidate_id") or p.get("research_snapshot",{}).get("candidate_id") or "")
        typ=str(row["event_type"])
        if typ in {"ORDER_MAKER_INTENT","ORDER_SUBMIT","ORDER_FAST_FOLLOW_SUBMIT","ORDER_FAST_FOLLOW_INTENT","ORDER_DRY_RUN_SUBMITTED"}:
            if not cid or cid not in candidates:
                continue
            item=candidates[cid]
            research_snapshot=p.get("research_snapshot")
            if isinstance(research_snapshot,dict):
                for key,value in research_snapshot.items():
                    if value is not None or key not in item:
                        item[key]=value
            economics=p.get("economics_context")
            if isinstance(economics,dict):
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
                if item.get("estimated_probability") is not None and item.get("entry_price") is not None:
                    item["directional_edge_ps"]=item["estimated_probability"]-item["entry_price"]-(item.get("fee_per_share") or 0)-(item.get("execution_penalty_per_share") or 0)
            item["submitted"]=typ not in {"ORDER_MAKER_INTENT","ORDER_FAST_FOLLOW_INTENT"} or bool(row["status"]=="SUBMITTED")
            item["submit_event_type"]=typ
            item["client_order_id"]=row["client_order_id"]
            item["submit_ts"]=row["ts"]
            item["submitted_price"]=_number(row["price"])
            item["submitted_qty"]=_number(row["qty"])
            item["submitted_expected_net_usdc"]=_number(row["expected_net_usdc"])
            if row["client_order_id"]:
                orders_by_id[str(row["client_order_id"])]=item
        elif typ=="ORDER_FILLED":
            item=candidates.get(cid) if cid else orders_by_id.get(str(row["client_order_id"] or ""))
            if item is None:
                continue
            item["filled"]=True; item["fill_price"]=_number(row["price"]); item["fill_qty"]=_number(row["qty"]); item["fill_ts"]=row["ts"]
            orders_by_id[str(row["client_order_id"] or "")]=item
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
        if item.pop("_recompute_shadow_from_economics_context",False):
            shadow=build_shadow_labels(
                edge_ps=_number(item.get("directional_edge_ps")),
                robust_net_usdc=_number(item.get("robust_net_usdc")),
                fair=_number(item.get("estimated_probability") or item.get("fair_probability")),
                entry_price=_number(item.get("entry_price")),
                abs_distance_bps=_number(item.get("abs_distance_bps")),
                safety_sigma=_number(item.get("safety_sigma")),
                crossings_last_120s=_number(item.get("crossings_last_120s")),
                weekend=(True if item.get("weekday_weekend")=="weekend" else False if item.get("weekday_weekend")=="weekday" else None),
                depth_adequate=depth_ok,
            )
            item.update(shadow)
        multiplier=_number(item.get("shadow_size_multiplier"))
        item["shadow_size_counterfactual_pnl_usdc"]=(item["settlement_pnl_usdc"]*multiplier if item.get("settlement_pnl_usdc") is not None and multiplier is not None else None)
        item["candidate_market_key"]=item.get("slug")
        edge=_number(item.get("directional_edge_ps"))
        item["edge_bucket"]=_bucket(edge,((0,"<0"),(.01,"0–1c"),(.02,"1–2c"),(.05,"2–5c"),(float("inf"),">5c")))
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
    return list(candidates.values())


def _entry_trace_count(db_path: Path) -> int:
    uri=db_path.resolve().as_uri()+"?mode=ro&immutable=1"
    with sqlite3.connect(uri,uri=True) as conn:
        row=conn.execute("SELECT COUNT(*) FROM strategy_events WHERE event_type='ENTRY_DECISION_TRACE'").fetchone()
    return int(row[0] or 0)


def generate_report(db_path: Path, output: Path) -> int:
    rows=load_candidates(db_path)
    entry_trace_count=_entry_trace_count(db_path)
    output.mkdir(parents=True,exist_ok=True)
    _write_csv(output/"candidate_level.csv",rows)
    for filename,key,buckets in (
        ("edge_buckets.csv","edge_bucket",[(x,x) for x in ("<0","0–1c","1–2c","2–5c",">5c","unavailable")]),
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
    settled=[r for r in rows if r.get("settlement_pnl_usdc") is not None]
    fill_count=sum(bool(r.get("filled")) for r in rows)
    lines=["# Live Entry Quality / Shadow Risk 報告","",
           f"- Journal: `{db_path}`",f"- Research snapshots: {len(rows)}; unique markets: {len({r.get('slug') for r in rows if r.get('slug')})}; submitted: {sum(bool(r.get('submitted')) for r in rows)}; filled: {fill_count}; settled filled candidates: {len(settled)}.",
           f"- `ENTRY_DECISION_TRACE` rows in the selected journal: {entry_trace_count}.",
           "- This is observation-only. Shadow reject and size-down columns are counterfactual labels; they do not alter trading authority.",
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
           "`edge_buckets.csv`, `price_buckets.csv`, `strike_distance_buckets.csv`, `safety_sigma_buckets.csv`, `crossing_buckets.csv`, `weekday_weekend.csv`, `hour_blocks.csv`, `shadow_reject_counterfactual.csv`, and `shadow_size_counterfactual.csv` report candidate/fill/settlement/markout counts. Empty buckets are retained so missing evidence is visible.",
           "", "## Live economics formula audit", "",
           "- Maker passive quote: spread-capture expected net is `expected_spread_capture + expected_rebate - adverse_selection_buffer`; the quote engine also computes `robust_net = expected_net - execution_penalty`. The normal maker path intentionally does not universally subtract the imported markout penalty from its admission gate; strong-directional regime eligibility gates on measured `resolution_ev >= min_expected_net`, while markout-adjusted robust net is telemetry. See final audit in task response.",
           "- Outcome FOK: `resolution_ev = qty × (fair_probability_for_outcome - limit_price)`; `robust_net = resolution_ev - taker_fee - empirical_adverse_markout_penalty`; the FOK allow/reject compares expected net to the configured minimum.",
           "- No live parameters or behavior are changed by this report generator.", ""]
    (output/"summary.md").write_text("\n".join(lines),encoding="utf-8")
    print(f"snapshots={len(rows)} filled={fill_count} settled={len(settled)} output={output}")
    return 0


def main(argv: list[str] | None=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db",type=Path,default=DEFAULT_DB)
    parser.add_argument("--output",type=Path,default=DEFAULT_OUTPUT)
    args=parser.parse_args(argv)
    return generate_report(args.db,args.output)


if __name__=="__main__":
    raise SystemExit(main())
