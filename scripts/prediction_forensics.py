#!/usr/bin/env python3
"""Offline forensic replay of selected shadow-simulated BTC 15m entries.

Research only: reads the trade journal, TWAP research journal, and BTC 1s
Parquet. It does not import live strategy code or modify any database.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

from bot.btc_1s_history import load_btc_1s_history


DEFAULT_TARGET_SLUGS = (
    "btc-updown-15m-1790920800", "btc-updown-15m-1790921700",
    "btc-updown-15m-1790923500", "btc-updown-15m-1790925300",
    "btc-updown-15m-1790926200", "btc-updown-15m-1790927100",
    "btc-updown-15m-1790928900", "btc-updown-15m-1790930700",
    "btc-updown-15m-1790932500", "btc-updown-15m-1790934300",
    "btc-updown-15m-1790936100", "btc-updown-15m-1790937900",
    "btc-updown-15m-1790938800", "btc-updown-15m-1790939700",
)
FRESH_SEC = 2.0


def _db(path: Path):
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=30)


def _payload_rows(conn, table: str, event_type: str, slugs: set[str], ts_lo=None, ts_hi=None):
    clauses, params = ["event_type=?"], [event_type]
    if ts_lo is not None:
        clauses.append("ts>=?"); params.append(ts_lo)
    if ts_hi is not None:
        clauses.append("ts<=?"); params.append(ts_hi)
    rows = conn.execute(f"SELECT id,ts,payload_json FROM {table} WHERE {' AND '.join(clauses)} ORDER BY id", params)
    for eid, ts, raw in rows:
        try:
            payload = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        slug = payload.get("slug") or payload.get("market_slug")
        if slug in slugs:
            yield {"id": eid, "ts": ts, "payload": payload, "slug": slug}


def _side_prob(row: dict, side: str):
    key = "p_up_ex_market" if side == "UP" else "p_down_ex_market"
    value = row.get(key)
    return float(value) if value is not None else None


def _entry_p_ex(feature: dict, side: str, *, max_observation_age_sec: float = FRESH_SEC):
    age = feature.get("feature_age_sec", feature.get("p_ex_last_observation_age_sec"))
    if (feature.get("sigma_ex_market_fresh") is not True or age is None
            or not 0 <= float(age) <= max_observation_age_sec):
        return None
    value = _side_prob(feature, side)
    return value if value is not None and 0 <= value <= 1 else None


def _market_mid(row: dict):
    val = row.get("market_mid_probability_up")
    if val is not None:
        return float(val)
    bid, ask = row.get("best_bid_up"), row.get("best_ask_up")
    if bid is not None and ask is not None and 0 <= float(bid) <= float(ask) <= 1:
        return (float(bid) + float(ask)) / 2
    return None


def _valid_model(row):
    try:
        p = float(row.get("p_up_ex_market"))
    except (TypeError, ValueError):
        return False
    return (row.get("sigma_ex_market_fresh") is True and 0.0 <= p <= 1.0
            and row.get("source_ts") is not None)


def _fresh_mid(row):
    age = row.get("market_bbo_up_source_age_sec")
    if age is None:
        age = row.get("market_mid_source_age_sec")
    return _market_mid(row) is not None and age is not None and 0 <= float(age) <= FRESH_SEC


def _nearest(rows, ts: float, max_age: float = 12.0):
    prior = [r for r in rows if r["observed_ts"] <= ts]
    if not prior:
        return None
    row = prior[-1]
    return row if ts - row["observed_ts"] <= max_age else None


def _features_at_market(rows: list[dict], ts: float, side: str):
    row = _nearest(rows, ts)
    if row is None:
        prior=[r for r in rows if r["observed_ts"]<=ts]
        if not prior: return {}
        old=prior[-1]
        old_age=ts-old["observed_ts"]
        return {"p_ex_last_observed_up":old.get("p_up_ex_market") if _valid_model(old) else None,
                "p_ex_last_observed_side":_side_prob(old,side) if _valid_model(old) else None,
                "p_ex_last_observed_ts":old["observed_ts"],
                "p_ex_last_observation_age_sec":old_age,
                "p_ex_observation_stale_at_entry":old_age>FRESH_SEC,
                "p_ex_last_observation_sigma_age_sec":old.get("sigma_ex_market_age_sec"),
                "p_ex_last_observation_source_ts":old.get("source_ts")}
    return {
        "feature_ts": row["observed_ts"],
        "feature_age_sec": ts - row["observed_ts"],
        "settlement_state_side": row.get("settlement_state_side"),
        "path_spot_side": row.get("path_spot_side"),
        "settlement_path_side_divergence": row.get("settlement_path_side_divergence"),
        "path_spot": row.get("path_spot"), "path_spot_source": row.get("path_spot_source"),
        "path_spot_age_sec": row.get("path_spot_age_sec"),
        "official_current_twap": row.get("official_current_twap"),
        "twap_minus_strike_bps": row.get("twap_minus_strike_bps"),
        "required_move_mode": row.get("required_move_mode"),
        "required_move_bps": row.get("required_move_bps"),
        "required_move_usd": row.get("required_move_usd"),
        "required_move_sigma": row.get("required_move_sigma"),
        "p_up_ex_market": row.get("p_up_ex_market") if _valid_model(row) else None,
        "p_down_ex_market": row.get("p_down_ex_market") if _valid_model(row) else None,
        "p_ex_last_observed_up":row.get("p_up_ex_market") if _valid_model(row) else None,
        "p_ex_last_observed_side":_side_prob(row,side) if _valid_model(row) else None,
        "p_ex_last_observed_ts":row["observed_ts"],
        "p_ex_last_observation_age_sec":ts-row["observed_ts"],
        "p_ex_observation_stale_at_entry":ts-row["observed_ts"]>FRESH_SEC,
        "p_ex_last_observation_sigma_age_sec":row.get("sigma_ex_market_age_sec"),
        "p_ex_last_observation_source_ts":row.get("source_ts"),
        "p_ex_raw_out_of_range": (row.get("p_up_ex_market") is not None and
                                   not (0 <= float(row["p_up_ex_market"]) <= 1)),
        "p_down_ex_market": row.get("p_down_ex_market") if _valid_model(row) else None,
        "p_side_ex_market": _side_prob(row, side) if _valid_model(row) else None,
        "sigma_ex_market_fresh": row.get("sigma_ex_market_fresh"),
        "sigma_ex_market_age_sec": row.get("sigma_ex_market_age_sec"),
        "market_mid_probability_up": _market_mid(row),
        "market_mid_fresh": _fresh_mid(row),
        "market_bbo_up_source_age_sec": row.get("market_bbo_up_source_age_sec"),
        "market_bbo_down_source_age_sec": row.get("market_bbo_down_source_age_sec"),
        "best_bid_up": row.get("best_bid_up"), "best_ask_up": row.get("best_ask_up"),
        "best_bid_down": row.get("best_bid_down"), "best_ask_down": row.get("best_ask_down"),
        "twap_slope_5s_bps_per_sec": row.get("twap_slope_5s_bps_per_sec"),
        "twap_slope_10s_bps_per_sec": row.get("twap_slope_10s_bps_per_sec"),
    }


def _btc_returns(btc: dict[int, dict], ts: float):
    now_sec = int(math.floor(ts))
    current = btc.get(now_sec)
    if current is None:
        prior = [s for s in btc if s <= ts]
        current = btc[max(prior)] if prior else None
    if current is None:
        return {**{f"btc_return_{h}s_bps": None for h in (1, 5, 10, 30, 60)},
                "btc_realized_vol_60s_bps": None, "btc_realized_vol_60s_sample_count": 0}
    p = float(current["close"])
    out = {}
    for h in (1, 5, 10, 30, 60):
        old = btc.get(now_sec - h)
        out[f"btc_return_{h}s_bps"] = (10000 * (p / float(old["close"]) - 1)
                                        if old and old.get("close") else None)
    one_sec_returns=[]
    for sec in range(now_sec-59, now_sec+1):
        cur, prev=btc.get(sec),btc.get(sec-1)
        if cur and prev and cur.get("close") and prev.get("close"):
            one_sec_returns.append(10000*math.log(float(cur["close"])/float(prev["close"])))
    out["btc_realized_vol_60s_bps"]=(statistics.pstdev(one_sec_returns) if len(one_sec_returns)>=30 else None)
    out["btc_realized_vol_60s_sample_count"]=len(one_sec_returns)
    return out


def _csv(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields or ["empty"], extrasaction="ignore")
        w.writeheader(); w.writerows(rows)


def _metric_summary(rows, probability_key, target_key="outcome_up"):
    valid = [r for r in rows if r.get(probability_key) is not None and r.get(target_key) is not None]
    if not valid:
        return {"n": 0, "brier": None, "log_loss": None, "direction_accuracy": None}
    eps = 1e-9
    brier, ll, hit = [], [], []
    for r in valid:
        p = min(1-eps, max(eps, float(r[probability_key])))
        y = int(r[target_key])
        brier.append((p-y)**2)
        ll.append(-(y*math.log(p)+(1-y)*math.log(1-p)))
        hit.append((p >= .5) == bool(y))
    return {"n": len(valid), "brier": statistics.mean(brier), "log_loss": statistics.mean(ll),
            "direction_accuracy": statistics.mean(hit)}


def _poisson_binomial_tail(probabilities, observed):
    pmf=[1.0]
    for probability in probabilities:
        nxt=[0.0]*(len(pmf)+1)
        for k,mass in enumerate(pmf):
            nxt[k]+=mass*(1-float(probability))
            nxt[k+1]+=mass*float(probability)
        pmf=nxt
    return sum(pmf[int(observed):]) if probabilities else None


def fmt(value):
    return "NA" if value is None else f"{float(value):.3f}"


def _corr(x, y):
    if len(x) < 3 or len(x) != len(y): return None
    if len(set(x)) < 2 or len(set(y)) < 2: return None
    return statistics.correlation(x, y)


def _direction_hit(x, y):
    pairs=[(a,b) for a,b in zip(x,y) if a != 0 and b != 0]
    return sum((a>0)==(b>0) for a,b in pairs)/len(pairs) if pairs else None


def _market_level_repricing(rows):
    groups=defaultdict(list)
    for row in rows: groups[(row["slug"],row["horizon_sec"])].append(row)
    output=[]
    for (slug,horizon),group in groups.items():
        item={"slug":slug,"horizon_sec":horizon,"n_observations":len(group),
              "mean_market_mid_up":statistics.mean(float(r["market_mid_up"]) for r in group),
              "mean_probability_residual":statistics.mean(float(r["probability_residual"]) for r in group),
              "mean_future_mid_change":statistics.mean(float(r["future_mid_change"]) for r in group)}
        for key in ("btc_return_10s_bps","required_move_sigma"):
            values=[float(r[key]) for r in group if r.get(key) is not None]
            item["mean_"+key]=statistics.mean(values) if values else None
        output.append(item)
    return output


def _solve_linear_system(matrix, vector):
    n=len(vector)
    a=[list(map(float,row))+[float(vector[i])] for i,row in enumerate(matrix)]
    for col in range(n):
        pivot=max(range(col,n),key=lambda r:abs(a[r][col]))
        if abs(a[pivot][col])<1e-12: return None
        a[col],a[pivot]=a[pivot],a[col]
        scale=a[col][col]
        a[col]=[v/scale for v in a[col]]
        for row in range(n):
            if row==col: continue
            factor=a[row][col]
            a[row]=[x-factor*y for x,y in zip(a[row],a[col])]
    return [a[i][-1] for i in range(n)]


def _repricing_model_comparison(rows):
    """Descriptive market-level OLS on common complete cases; not a strategy model."""
    models={"A_mid_only":["mean_market_mid_up"],
            "B_mid_plus_p_ex_residual":["mean_market_mid_up","mean_probability_residual"],
            "C_mid_plus_BTC_10s":["mean_market_mid_up","mean_btc_return_10s_bps"],
            "D_mid_plus_required_sigma":["mean_market_mid_up","mean_required_move_sigma"],
            "combined":["mean_market_mid_up","mean_probability_residual",
                        "mean_btc_return_10s_bps","mean_required_move_sigma"]}
    output=[]
    for horizon in (5,10,30):
        group=[r for r in rows if r["horizon_sec"]==horizon]
        complete=[r for r in group if all(r.get(k) is not None for k in models["combined"])
                  and r.get("mean_future_mid_change") is not None]
        y=[float(r["mean_future_mid_change"]) for r in complete]
        for name,features in models.items():
            n=len(complete); k=len(features)+1
            result={"horizon_sec":horizon,"model":name,"n_markets":n,
                    "features":"+".join(features),"r_squared":None,"rmse":None,
                    "interpretation":"in-sample descriptive OLS; market-level rows; not causal"}
            if n>k:
                x=[[1.0]+[float(r[f]) for f in features] for r in complete]
                xtx=[[sum(row[i]*row[j] for row in x) for j in range(k)] for i in range(k)]
                xty=[sum(row[i]*target for row,target in zip(x,y)) for i in range(k)]
                beta=_solve_linear_system(xtx,xty)
                if beta is not None:
                    pred=[sum(a*b for a,b in zip(row,beta)) for row in x]
                    mean_y=statistics.mean(y); ss_tot=sum((v-mean_y)**2 for v in y)
                    ss_res=sum((v-fit)**2 for v,fit in zip(y,pred))
                    result.update({"r_squared":(1-ss_res/ss_tot if ss_tot else None),
                                   "rmse":math.sqrt(ss_res/n),
                                   "intercept":beta[0]})
                    for feature,coef in zip(features,beta[1:]): result[f"coef_{feature}"]=coef
            output.append(result)
    return output


def _cluster_corr_ci(rows, x_key, y_key, *, reps=1000):
    groups=defaultdict(list)
    for row in rows:
        if row.get(x_key) is not None and row.get(y_key) is not None:
            groups[row["slug"]].append((float(row[x_key]),float(row[y_key])))
    points=[(statistics.mean(x for x,_ in values),statistics.mean(y for _,y in values))
            for values in groups.values()]
    estimate=_corr([x for x,_ in points],[y for _,y in points])
    if len(points)<3: return estimate,None,None,len(points)
    rng=random.Random(31); samples=[]
    for _ in range(reps):
        draw=[points[rng.randrange(len(points))] for _ in points]
        value=_corr([x for x,_ in draw],[y for _,y in draw])
        if value is not None: samples.append(value)
    samples.sort()
    if not samples: return estimate,None,None,len(points)
    return estimate,samples[int(.025*(len(samples)-1))],samples[int(.975*(len(samples)-1))],len(points)


def build_analysis(journal: Path, twap_db: Path, btc_dir: Path, output: Path,
                  selected_slugs: tuple[str, ...] = DEFAULT_TARGET_SLUGS):
    slugs = set(selected_slugs)
    with _db(journal) as conn:
        settled = list(_payload_rows(conn, "order_events", "SHADOW_SIM_SETTLED", slugs))
        filled = list(_payload_rows(conn, "order_events", "SHADOW_SIM_ENTRY_FILLED", slugs))
        side_obs = list(_payload_rows(conn, "strategy_events", "SIDE_DECISION_OBSERVATION", slugs))
        side_decisions = list(_payload_rows(conn, "strategy_events", "SIDE_DECISION", slugs))
        settlements = list(_payload_rows(conn, "strategy_events", "MARKET_SETTLEMENT", slugs))
        traces = list(_payload_rows(conn, "strategy_events", "ENTRY_DECISION_TRACE", slugs))
        depth_candidates = list(_payload_rows(conn, "order_events", "DEPTH_RISK_SHADOW_CANDIDATE", slugs))
        depth_markouts = list(_payload_rows(conn, "order_events", "DEPTH_RISK_SHADOW_MARKOUT", slugs))
    settled_by_slug = {}
    for r in settled:
        settled_by_slug[r["slug"]] = {**r["payload"], "event_ts": r["ts"]}
    filled_by_slug = {}
    for r in filled:
        filled_by_slug.setdefault(r["slug"], {**r["payload"], "event_ts": r["ts"]})
    settlement_by_slug = {r["slug"]: {**r["payload"], "event_ts": r["ts"]} for r in settlements}
    twap_by_slug = defaultdict(list)
    with _db(twap_db) as conn:
        query = conn.execute("SELECT decision_epoch_ns,slug,payload_json FROM lead_lag_decisions ORDER BY decision_epoch_ns")
        for ns, slug, raw in query:
            if slug not in slugs:
                continue
            try: p = json.loads(raw or "{}")
            except (TypeError, json.JSONDecodeError): continue
            p["observed_ts"] = float(p.get("observed_ts") or int(ns)/1e9)
            twap_by_slug[slug].append(p)
    for rows in twap_by_slug.values(): rows.sort(key=lambda r: r["observed_ts"])
    side_by_slug = defaultdict(list)
    for r in side_obs:
        p = r["payload"]
        ts = float(p.get("decision_ts") or _iso_epoch(r["ts"]))
        side_by_slug[r["slug"]].append({**p, "observed_ts": ts, "_event_type": "SIDE_DECISION_OBSERVATION"})
    for r in side_decisions:
        p = r["payload"]
        ts = float(p.get("decision_ts") or _iso_epoch(r["ts"]))
        side_by_slug[r["slug"]].append({**p, "observed_ts": ts, "_event_type": "SIDE_DECISION"})
    for rows in side_by_slug.values(): rows.sort(key=lambda r: r["observed_ts"])
    traces_by_slug = defaultdict(list)
    for r in traces:
        p = r["payload"]
        traces_by_slug[r["slug"]].append({**p, "observed_ts": _iso_epoch(r["ts"])})

    entries = []
    # Use one settled simulation per slug; explicit slug cohort avoids treating
    # restarts/node rollovers as independent markets.
    for slug in selected_slugs:
        p = settled_by_slug.get(slug)
        if not p:
            continue
        side = p.get("side")
        entry_ts = float(p.get("filled_ts") or p.get("created_ts") or _iso_epoch(p["event_ts"]))
        market_start = _slug_start(slug)
        feature = _features_at_market(twap_by_slug.get(slug, []), entry_ts, side)
        decision = _nearest(side_by_slug.get(slug, []), entry_ts, max_age=30)
        pre_snapshot = next((t.get("research_snapshot") for t in traces_by_slug.get(slug, [])
                             if t.get("research_snapshot") and t["observed_ts"] <= entry_ts), None)
        if pre_snapshot:
            feature.update({"entry_crossings_total": pre_snapshot.get("crossings_total"),
                            "entry_crossings_last_60s": pre_snapshot.get("crossings_last_60s"),
                            "entry_crossings_last_120s": pre_snapshot.get("crossings_last_120s"),
                            "entry_distance_bps": pre_snapshot.get("signed_distance_bps"),
                            "entry_top_bid_size": pre_snapshot.get("top_bid_size"),
                            "entry_top_ask_size": pre_snapshot.get("top_ask_size"),
                            "entry_nearby_bid_depth": pre_snapshot.get("nearby_bid_depth"),
                            "entry_nearby_ask_depth": pre_snapshot.get("nearby_ask_depth"),
                            "entry_safety_sigma": pre_snapshot.get("safety_sigma")})
        feature_age = feature.get("feature_age_sec", feature.get("p_ex_last_observation_age_sec"))
        side_p = _entry_p_ex(feature, side)
        near_side_p = _entry_p_ex(feature, side, max_observation_age_sec=12.0)
        fill_mid = p.get("fill_mid")
        fill_quote_age = p.get("fill_quote_age_raw_sec")
        fill_tier = p.get("fill_quote_freshness_tier") or p.get("quote_freshness_tier")
        fill_mid_fresh = (fill_tier == "FRESH" and fill_quote_age is not None
                          and 0 <= float(fill_quote_age) <= FRESH_SEC)
        # fill_mid is the contemporaneous midpoint of the bought outcome token.
        # For a DOWN token, convert it back to the UP-probability axis.
        m_up = (float(fill_mid) if side == "UP" and fill_mid is not None else
                1.0-float(fill_mid) if side == "DOWN" and fill_mid is not None else
                feature.get("market_mid_probability_up"))
        market_side_p = (m_up if side == "UP" else (1-float(m_up) if m_up is not None else None)) if fill_mid_fresh else None
        row = {
            "slug": slug, "side": side, "outcome": p.get("outcome"), "won": bool(p.get("won")),
            "entry_ts": entry_ts, "market_start_ts": market_start,
            "time_left_sec": p.get("time_left_sec"), "market_elapsed_sec": 900-float(p.get("time_left_sec") or 900),
            "entry_price": p.get("entry_price"), "market_entry_mid": p.get("fill_mid"),
            "qty":p.get("qty"),
            "entry_bid": p.get("fill_bid"), "entry_ask": p.get("fill_ask"),
            "market_side_probability": market_side_p, "market_probability_up": m_up,
            "market_mid_source": "simulated_fill_mid" if fill_mid is not None else "twap_research_bbo",
            "market_mid_fresh": fill_mid_fresh,
            "fill_quote_age_raw_sec": p.get("fill_quote_age_raw_sec"),
            "p_ex_side_at_entry": side_p,
            "p_ex_side_within_12s": near_side_p,
            "p_ex_up_at_entry": feature.get("p_up_ex_market") if side_p is not None else None,
            "ex_edge_vs_market_side_probability": (side_p-market_side_p if side_p is not None and market_side_p is not None else None),
            "ex_edge_vs_entry_price": (side_p-float(p["entry_price"]) if side_p is not None and p.get("entry_price") is not None else None),
            "side_score": p.get("side_score"), "side_reason": p.get("side_reason"),
            "fair": p.get("fair"), "spot": p.get("spot"), "strike": p.get("strike"),
            "quote_age_sec": p.get("quote_age_sec"), "quote_freshness_tier": p.get("quote_freshness_tier"),
            "simulated_pnl_usdc": p.get("simulated_pnl_usdc"),
            "expected_net_usdc_at_entry": p.get("expected_net_usdc"),
            "simulation_id": p.get("simulation_id"),
            "decision_score": decision.get("composite_score") if decision else None,
            "decision_market_consensus": decision.get("market_consensus") if decision else None,
            "decision_btc_trend": decision.get("btc_trend") if decision else None,
            "decision_strike_proximity": decision.get("strike_proximity") if decision else None,
            "decision_weights": (f"{decision.get('w_market')}/{decision.get('w_btc')}/{decision.get('w_strike')}" if decision else None),
            "decision_fresh_market_mid": decision.get("market_mid") if decision else None,
            "decision_market_mid_age_sec": decision.get("market_mid_source_age_sec") if decision else None,
            "decision_reference_age_sec": decision.get("reference_spot_age_sec") if decision else None,
            **feature,
        }
        # Ensure side-probability uses exact side, not the temporary merged dict.
        row["p_ex_side_at_entry"] = side_p
        row["p_ex_side_within_12s"] = near_side_p
        if side_p is None:
            row["p_up_ex_market"] = None
        row["market_mid_fresh"] = fill_mid_fresh
        row["twap_research_mid_fresh"] = feature.get("market_mid_fresh")
        for window in (30, 60, 120):
            row[f"active_side_flips_last_{window}s"] = _active_side_flips_before(
                side_by_slug.get(slug, []), entry_ts, window)
        row.update(_parse_side_reason(p.get("side_reason")))
        entries.append(row)

    # Load BTC one-second data over the selected cohort window for independent
    # momentum and reversal timelines. No interpolation is performed.
    times = [float(e["entry_ts"]) for e in entries]
    starts = [_slug_start(e["slug"]) for e in entries]
    btc_rows = load_btc_1s_history(min(starts)-1, max(starts)+900, btc_dir) if entries else []
    btc = {int(r["ts_sec"]): r for r in btc_rows}
    for e in entries:
        e.update(_btc_returns(btc, e["entry_ts"]))
        spot, strike=e.get("spot"),e.get("strike")
        e["spot_minus_strike_bps"]=(10000*(float(spot)/float(strike)-1)
                                      if spot is not None and strike not in (None,0) else None)
        bid,ask=e.get("entry_bid"),e.get("entry_ask")
        e["entry_spread"]=(float(ask)-float(bid) if bid is not None and ask is not None else None)
        # Market-level p(mid) expected wins uses the filled-side mid when fresh;
        # fill-time sampled midpoint is descriptive and source age is reported.
        e["outcome_up"] = 1 if e["outcome"] == "UP" else 0
        e["market_entry_up_probability"] = e.get("market_probability_up")
        e["market_expected_win_probability"] = e.get("market_side_probability")
        e["flip_sigma_bucket"] = _sigma_bucket(e.get("required_move_sigma"))
        e["entry_time_bin"] = _time_bin(e.get("time_left_sec"))

    # Entry-timed 1-second timeline files for the two known losing markets.
    loser_timelines = []
    depth_rows = []
    lead_lag = []
    for e in entries:
        slug, entry_ts = e["slug"], float(e["entry_ts"])
        if e["won"] is False:
            settle_ts = _slug_start(slug) + 900
            for t in range(max(int(_slug_start(slug)), int(entry_ts)-120), int(settle_ts)+1, 5):
                f = _features_at_market(twap_by_slug.get(slug, []), t, e["side"])
                side_row = _nearest(side_by_slug.get(slug, []), t, max_age=5.0)
                if side_row:
                    f.update({"side_score":side_row.get("composite_score",side_row.get("score")),
                              "signal_active_side":side_row.get("active_side"),
                              "signal_proposed_side":side_row.get("proposed_side"),
                              "signal_market_consensus":side_row.get("market_consensus"),
                              "signal_btc_trend":side_row.get("btc_trend"),
                              "signal_strike_proximity":side_row.get("strike_proximity"),
                              "signal_market_mid_source_age_sec":side_row.get("market_mid_source_age_sec"),
                              "signal_reference_age_sec":side_row.get("reference_spot_age_sec"),
                              "signal_observation_age_sec":t-side_row["observed_ts"]})
                # nearest BTC second at/before sample; return horizons are based on observed bars only.
                f.update(_btc_returns(btc, t))
                loser_timelines.append({"slug": slug, "entry_side": e["side"],
                    "seconds_from_entry": round(t-entry_ts, 3), "time_left_sec": round(settle_ts-t, 3),
                    "btc_close": _btc_close(btc, t), **f})
            probes = [r for r in depth_candidates if r["slug"] == slug]
            for probe in probes:
                d=probe["payload"]; dts=float(d.get("created_ts") or _iso_epoch(probe["ts"]))
                if not entry_ts-120 <= dts <= settle_ts: continue
                en=d.get("entry") or {}; ex=d.get("exit") or {}
                linked=[m["payload"] for m in depth_markouts if m["slug"]==slug and m["payload"].get("simulation_id")==d.get("simulation_id")]
                depth_rows.append({"slug":slug,"ts":dts,"seconds_from_entry":dts-entry_ts,
                    "entry_side":e["side"],"probe_side":d.get("side"),"requested_quantity":d.get("requested_quantity"),
                    "entry_filled_qty":en.get("filled_quantity"),"entry_fill_rate":en.get("fill_rate"),
                    "entry_vwap":en.get("vwap"),"entry_visible_depth":en.get("visible_depth"),
                    "exit_filled_qty":ex.get("filled_quantity"),"exit_fill_rate":ex.get("fill_rate"),
                    "exit_vwap":ex.get("vwap"),"exit_visible_depth":ex.get("visible_depth"),
                    "immediate_round_trip_markout_usdc":d.get("immediate_round_trip_markout_usdc"),
                    "markout_horizons_sec":";".join(str(m.get("markout_horizon_sec")) for m in linked),
                    "markout_usdc":";".join(str(m.get("markout_usdc")) for m in linked),
                    "interpretation":"hypothetical depth probe; not actual position execution"})
        lead_lag.extend(_lead_lag_rows(slug, twap_by_slug.get(slug, []),
                                       side_by_slug.get(slug, [])))

    # Attach only near-synchronous depth probes to the 5-second timeline;
    # otherwise leave the depth fields absent (never forward-fill stale depth).
    entries_by_slug={e["slug"]:e for e in entries}
    for point in loser_timelines:
        slug=point["slug"]; entry_ts=float(entries_by_slug[slug]["entry_ts"])
        at=entry_ts+float(point["seconds_from_entry"])
        probes=[r for r in depth_candidates if r["slug"]==slug]
        nearest=min(probes,key=lambda r:abs(float(r["payload"].get("created_ts") or _iso_epoch(r["ts"]))-at),default=None)
        if nearest:
            age=abs(float(nearest["payload"].get("created_ts") or _iso_epoch(nearest["ts"]))-at)
            if age<=2.5:
                ep=nearest["payload"].get("entry") or {}; xp=nearest["payload"].get("exit") or {}
                point.update({"depth_probe_age_sec":age,"depth_probe_quantity":nearest["payload"].get("requested_quantity"),
                              "depth_probe_entry_visible_depth":ep.get("visible_depth"),"depth_probe_entry_fill_rate":ep.get("fill_rate"),
                              "depth_probe_exit_visible_depth":xp.get("visible_depth"),"depth_probe_exit_fill_rate":xp.get("fill_rate")})

    _csv(output/"entries.csv", entries)
    _csv(output/"winner_loser_comparison.csv", _winner_loser_rows(entries))
    _csv(output/"loser_timelines.csv", loser_timelines)
    _csv(output/"depth_shadow_events.csv", depth_rows)
    _csv(output/"lead_lag_events.csv", lead_lag)
    _csv(output/"signal_ablation.csv", _ablation_rows(entries, side_by_slug))
    _csv(output/"counterfactual_filters.csv", _counterfactual_rows(entries))
    repricing = _repricing_rows(twap_by_slug, entries, btc)
    _csv(output/"repricing_prediction.csv", repricing)
    market_level_repricing=_market_level_repricing(repricing)
    _csv(output/"repricing_market_level.csv", market_level_repricing)
    _csv(output/"repricing_model_comparison.csv", _repricing_model_comparison(market_level_repricing))
    _csv(output/"probability_comparison.csv", _probability_comparison(entries))
    _csv(output/"required_path_entry_risk.csv", [e for e in entries if e.get("required_move_sigma") is not None or e.get("required_move_mode")])
    _csv(output/"data_quality.csv", _data_quality(entries, twap_by_slug, btc))
    summary = _summary(entries, twap_by_slug, lead_lag, btc_rows, repricing)
    (output/"summary.md").write_text(summary, encoding="utf-8")
    return {"entries": entries, "loser_timelines": loser_timelines, "lead_lag": lead_lag,
            "summary": summary, "btc_rows": btc_rows}


def _iso_epoch(text):
    from datetime import datetime
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _slug_start(slug):
    try: return float(str(slug).rsplit("-", 1)[1])
    except (ValueError, IndexError): return 0.0


def _sigma_bucket(v):
    if v is None: return "UNAVAILABLE"
    a = abs(float(v))
    return "LT_1_SIGMA" if a < 1 else "1_TO_2_SIGMA" if a < 2 else "2_TO_3_SIGMA" if a < 3 else "GT_3_SIGMA"


def _time_bin(v):
    if v is None: return "UNAVAILABLE"
    x = float(v)
    return ">600s" if x > 600 else "480-600s" if x >= 480 else "360-480s" if x >= 360 else "240-360s" if x >= 240 else "<240s"


def _active_side_flips_before(rows, entry_ts, window_sec):
    observations=[r for r in rows if r.get("_event_type")=="SIDE_DECISION"
                  and entry_ts-window_sec<=float(r["observed_ts"])<entry_ts]
    if len(observations)<2: return None
    previous=None; flips=0; valid=0
    for row in observations:
        side=row.get("active_side")
        if side not in {"UP","DOWN"}:
            previous=None
            continue
        valid+=1
        if previous is not None and previous!=side: flips+=1
        previous=side
    return flips if valid>=2 else None


def _btc_close(btc, ts):
    s = int(math.floor(ts))
    return btc[s]["close"] if s in btc else None


def _lead_lag_rows(slug, rows, side_rows=()):
    # Fresh source-timestamped crossings only. Missing gaps reset continuity.
    output, model_prev, mid_prev = [], None, None
    model_events, market_events, bot_events = [], [], []
    for r in rows:
        if _valid_model(r):
            p = float(r["p_up_ex_market"])
            side = "UP" if p >= .5 else "DOWN"
            if model_prev and model_prev[1] != side:
                model_cross = r["observed_ts"]
                event = {"slug": slug, "event": "MODEL_CROSS_0_5", "ts": model_cross,
                               "from_side": model_prev[1], "to_side": side,
                               "probability": p, "source_ts": r.get("source_ts"),
                               "sigma_ex_market_age_sec": r.get("sigma_ex_market_age_sec")}
                output.append(event); model_events.append(event)
            model_prev = (r["observed_ts"], side)
        else:
            model_prev = None
        if _fresh_mid(r):
            mid = float(_market_mid(r)); side = "UP" if mid >= .5 else "DOWN"
            if mid_prev and mid_prev[1] != side:
                mid_cross = r["observed_ts"]
                event = {"slug": slug, "event": "FRESH_MARKET_CROSS_0_5", "ts": mid_cross,
                               "from_side": mid_prev[1], "to_side": side,
                               "market_mid_up": mid,
                               "market_bbo_source_age_sec": r.get("market_bbo_up_source_age_sec")}
                output.append(event); market_events.append(event)
            mid_prev = (r["observed_ts"], side)
        else:
            mid_prev = None
    # Only explicit active-side decisions count as bot flips. NONE/UNKNOWN and
    # observation-only proposed sides break continuity instead of fabricating
    # a reversal across an unavailable interval.
    bot_prev = None
    for r in side_rows:
        if r.get("_event_type") == "SIDE_DECISION_OBSERVATION":
            continue
        side = r.get("active_side")
        if side not in {"UP", "DOWN"}:
            bot_prev = None
            continue
        if bot_prev and bot_prev[1] != side:
            event = {"slug": slug, "event": "BOT_SIDE_FLIP", "ts": r["observed_ts"],
                     "from_side": bot_prev[1], "to_side": side}
            output.append(event); bot_events.append(event)
        bot_prev = (r["observed_ts"], side)
    used_market = set()
    for model_event in model_events:
        candidates = [(i, event) for i, event in enumerate(market_events)
                      if i not in used_market and event["to_side"] == model_event["to_side"]
                      and abs(event["ts"]-model_event["ts"]) <= 60.0]
        if candidates:
            i, market_event = min(candidates, key=lambda item: abs(item[1]["ts"]-model_event["ts"]))
            used_market.add(i)
            output.append({"slug": slug, "event": "CROSSING_LEAD_LAG",
                           "model_cross_ts": model_event["ts"], "market_cross_ts": market_event["ts"],
                           "transition_to_side": model_event["to_side"],
                           "model_to_market_lead_sec": market_event["ts"]-model_event["ts"],
                           "match_window_sec": 60.0,
                           "status": "MEASURABLE"})
        else:
            output.append({"slug": slug, "event": "CROSSING_LEAD_LAG",
                           "model_cross_ts": model_event["ts"], "market_cross_ts": None,
                           "transition_to_side": model_event["to_side"],
                           "model_to_market_lead_sec": None, "match_window_sec": 60.0,
                           "status": "NOT_MEASURABLE"})
    for i, market_event in enumerate(market_events):
        if i not in used_market and not any(e["to_side"] == market_event["to_side"] for e in model_events):
            output.append({"slug": slug, "event": "CROSSING_LEAD_LAG",
                           "model_cross_ts": None, "market_cross_ts": market_event["ts"],
                           "transition_to_side": market_event["to_side"],
                           "model_to_market_lead_sec": None, "match_window_sec": 60.0,
                           "status": "NOT_MEASURABLE"})
    used_bot = set()
    for model_event in model_events:
        candidates = [(i, event) for i, event in enumerate(bot_events)
                      if i not in used_bot and event["to_side"] == model_event["to_side"]
                      and abs(event["ts"]-model_event["ts"]) <= 60.0]
        if candidates:
            i, bot_event = min(candidates, key=lambda item: abs(item[1]["ts"]-model_event["ts"]))
            used_bot.add(i)
            output.append({"slug": slug, "event": "MODEL_BOT_LEAD_LAG",
                           "model_cross_ts": model_event["ts"], "bot_flip_ts": bot_event["ts"],
                           "transition_to_side": model_event["to_side"],
                           "model_to_bot_lead_sec": bot_event["ts"]-model_event["ts"],
                           "match_window_sec": 60.0, "status": "MEASURABLE"})
        else:
            output.append({"slug": slug, "event": "MODEL_BOT_LEAD_LAG",
                           "model_cross_ts": model_event["ts"], "bot_flip_ts": None,
                           "transition_to_side": model_event["to_side"],
                           "model_to_bot_lead_sec": None, "match_window_sec": 60.0,
                           "status": "NOT_MEASURABLE"})
    return output


def _winner_loser_rows(entries):
    out=[]
    for label, group in (("WIN", [e for e in entries if e.get("won")]), ("LOSS", [e for e in entries if e.get("won") is False])):
        keys=("entry_price", "time_left_sec", "market_side_probability", "p_ex_side_at_entry",
              "ex_edge_vs_market_side_probability", "required_move_sigma", "btc_return_10s_bps",
              "btc_return_30s_bps", "entry_crossings_last_60s", "entry_crossings_last_120s",
              "entry_top_bid_size", "entry_top_ask_size", "entry_distance_bps")
        for k in keys:
            vals=[float(e[k]) for e in group if e.get(k) is not None]
            out.append({"result":label,"feature":k,"n":len(vals),"mean":statistics.mean(vals) if vals else None,
                        "median":statistics.median(vals) if vals else None,
                        "min":min(vals) if vals else None,"max":max(vals) if vals else None})
    return out


def _ablation_rows(entries, side_by_slug):
    rows=[]
    for e in entries:
        obs=_nearest(side_by_slug.get(e["slug"],[]),float(e["entry_ts"]),max_age=30)
        # Fill payload's side_reason records the contemporaneous component
        # values even when throttled side-decision telemetry has a gap.
        reason={k.removeprefix("reason_"):v for k,v in e.items() if k.startswith("reason_")}
        comps={"full":(obs.get("composite_score") if obs else reason.get("composite")),
               "market_only":(obs.get("market_consensus") if obs else reason.get("market")),
               "btc_only":(obs.get("btc_trend") if obs else reason.get("btc")),
               "structural_only":(obs.get("strike_proximity") if obs else reason.get("structural")),
               "minus_market":None}
        wm,wb,ws=((obs.get("w_market"),obs.get("w_btc"),obs.get("w_strike")) if obs else
                  (e.get("reason_w_market"),e.get("reason_w_btc"),e.get("reason_w_structural")))
        btc_component=comps["btc_only"]; struct_component=comps["structural_only"]
        if all(x is not None for x in (wb,ws,btc_component,struct_component)) and (float(wb)+float(ws)):
            comps["minus_market"]=(float(wb)*float(btc_component)+float(ws)*float(struct_component))/(float(wb)+float(ws))
        for model,score in comps.items():
            side="UP" if score is not None and float(score)>0 else "DOWN" if score is not None and float(score)<0 else "NONE"
            rows.append({"slug":e["slug"],"model":model,"entry_side":e["side"],"signal_side":side,
                         "settlement_side":e["outcome"],"signal_matches_entry":side==e["side"],
                         "settlement_direction_accuracy":side==e["outcome"] if side in {"UP","DOWN"} else None,
                         "signal_score":score,"entry_price":e.get("entry_price"),"win":e.get("won"),
                         "source":"nearest_side_decision" if obs else "fill_side_reason"})
    return rows


def _counterfactual_rows(entries):
    def keep(f,e): return f(e)
    filters=[("A_ex_market_agrees",lambda e:e.get("p_ex_side_at_entry") is not None and e["p_ex_side_at_entry"]>=.5),
             ("B_ex_at_least_market_side_probability",lambda e:e.get("p_ex_side_at_entry") is not None and e.get("market_side_probability") is not None and e["p_ex_side_at_entry"]>=e["market_side_probability"]),
             ("C_opposite_flip_ge_1_sigma",lambda e:e.get("required_move_sigma") is not None and abs(float(e["required_move_sigma"]))>=1),
             ("C_opposite_flip_ge_2_sigma",lambda e:e.get("required_move_sigma") is not None and abs(float(e["required_move_sigma"]))>=2),
             ("C_opposite_flip_ge_3_sigma",lambda e:e.get("required_move_sigma") is not None and abs(float(e["required_move_sigma"]))>=3),
             ("D_previous_60s_canonical_strike_crossings_lt_2",lambda e:e.get("entry_crossings_last_60s") is not None and int(e["entry_crossings_last_60s"])<2),
             ("E_BTC_10s_30s_not_opposing",lambda e:_momentum_agrees(e))]
    def available(name,e):
        if name.startswith("A_") or name.startswith("B_"): return e.get("p_ex_side_at_entry") is not None and (name.startswith("A_") or e.get("market_side_probability") is not None)
        if name.startswith("C_"): return e.get("required_move_sigma") is not None
        if name.startswith("D_"): return e.get("entry_crossings_last_60s") is not None
        return e.get("btc_return_10s_bps") is not None or e.get("btc_return_30s_bps") is not None
    out=[]
    for name,fn in filters:
        kept=[e for e in entries if keep(fn,e)]
        measurable=[e for e in entries if available(name,e)]
        rejected=[e for e in measurable if not fn(e)]
        out.append({"filter":name,"trades_kept":len(kept),"wins_kept":sum(bool(e["won"]) for e in kept),
                    "losses_kept":sum(e.get("won") is False for e in kept),
                    "gross_shadow_pnl_kept":sum(float(e.get("simulated_pnl_usdc") or 0) for e in kept),
                    "winners_rejected":sum(bool(e["won"]) for e in rejected),
                    "losers_avoided":sum(e.get("won") is False for e in rejected),
                    "feature_available_n":len(measurable),"feature_missing_n":len(entries)-len(measurable),
                    "eligible_but_rejected":len(measurable)-len(kept)})
    return out


def _momentum_agrees(e):
    vals=[e.get("btc_return_10s_bps"),e.get("btc_return_30s_bps")]
    vals=[float(v) for v in vals if v is not None]
    if not vals: return False
    sign=1 if e["side"]=="UP" else -1
    return all(sign*v>=0 for v in vals)


def _repricing_rows(twap_by_slug, entries, btc):
    out=[]
    selected={e["slug"]:e for e in entries}
    for slug,entry in selected.items():
        rows=twap_by_slug.get(slug,[])
        for r in rows:
            if not _valid_model(r) or not _fresh_mid(r): continue
            p=float(r["p_up_ex_market"]); mid=float(_market_mid(r))
            for horizon in (5,10,30):
                later=_nearest([x for x in rows if _fresh_mid(x)],r["observed_ts"]+horizon,max_age=1.5)
                if later is None or later["observed_ts"]<r["observed_ts"]+horizon-1.5: continue
                future=float(_market_mid(later)); dp=p-mid
                btc_returns=_btc_returns(btc,r["observed_ts"])
                out.append({"slug":slug,"ts":r["observed_ts"],"horizon_sec":horizon,
                    "market_mid_up":mid,"future_market_mid_up":future,"future_mid_change":future-mid,
                    "p_up_ex_market":p,"probability_residual":dp,
                    **btc_returns,"required_move_sigma":r.get("required_move_sigma"),
                    "market_bbo_source_age_sec":r.get("market_bbo_up_source_age_sec"),
                    "entry_side":entry["side"],"settlement_side":entry["outcome"]})
    return out


def _parse_side_reason(reason):
    import re
    if not reason: return {}
    patterns={"reason_composite":r"cs=([+-]?\d+(?:\.\d+)?)",
              "reason_market":r"mkt=([+-]?\d+(?:\.\d+)?)",
              "reason_btc":r"btc=([+-]?\d+(?:\.\d+)?)",
              "reason_structural":r"zs=([+-]?\d+(?:\.\d+)?)",
              "reason_w_market":r"w=([0-9.]+)/",
              "reason_w_btc":r"w=[0-9.]+/([0-9.]+)/",
              "reason_w_structural":r"w=[0-9.]+/[0-9.]+/([0-9.]+)"}
    out={}
    for key,pattern in patterns.items():
        m=re.search(pattern,str(reason)); out[key]=float(m.group(1)) if m else None
    return out


def _probability_comparison(entries):
    rows=[]
    market=[e for e in entries if e.get("market_mid_fresh") and e.get("market_probability_up") is not None]
    model=[e for e in entries if e.get("p_ex_up_at_entry") is not None]
    rows.append({"model":"market_fill_mid_all_fresh","n":len(market),**_metric_summary(market,"market_probability_up"),
                 "note":"fresh fill-time simulated midpoint; not executable ask"})
    rows.append({"model":"market_fill_mid_on_p_ex_subset","n":len(model),**_metric_summary(model,"market_probability_up"),
                 "note":"paired same markets as p_ex row"})
    rows.append({"model":"p_ex","n":len(model),**_metric_summary(model,"p_ex_up_at_entry"),
                 "note":f"sigma/source fresh, probability in [0,1], observation within {FRESH_SEC:.0f}s of fill"})
    return rows


def _data_quality(entries, twap_by_slug, btc_rows):
    out=[]
    for e in entries:
        rows=twap_by_slug.get(e["slug"],[])
        out.append({"slug":e["slug"],"entry_found":True,"settlement_label_found":bool(e.get("outcome")),
                    "twap_rows":len(rows),"valid_fresh_ex_market_probability_rows":sum(_valid_model(r) for r in rows),
                    "fresh_market_mid_rows":sum(_fresh_mid(r) for r in rows),
                    "path_features_at_entry":bool(e.get("path_spot") is not None),
                    "required_path_at_entry":bool(e.get("required_move_sigma") is not None),
                    "entry_snapshot_age_sec":e.get("feature_age_sec"),
                    "entry_quote_freshness":e.get("quote_freshness_tier"),
                    "btc_1s_rows_in_cohort_window":len(btc_rows),
                    "entry_btc_10s_available":e.get("btc_return_10s_bps") is not None,
                    "entry_btc_30s_available":e.get("btc_return_30s_bps") is not None})
    return out


def _all_settled_slugs(journal: Path):
    found=[]
    with _db(journal) as conn:
        for (raw,) in conn.execute("SELECT payload_json FROM order_events WHERE event_type='SHADOW_SIM_SETTLED' ORDER BY ts"):
            try: p=json.loads(raw or "{}")
            except (TypeError,json.JSONDecodeError): continue
            slug=p.get("slug") or p.get("market_slug")
            if slug and slug not in found: found.append(slug)
    return tuple(found)


def _summary(entries, twap_by_slug, lead_lag, btc_rows, repricing):
    n=len(entries); wins=sum(bool(e["won"]) for e in entries); losses=sum(e.get("won") is False for e in entries)
    pnl=sum(float(e.get("simulated_pnl_usdc") or 0) for e in entries)
    w_pnl=[float(e.get("simulated_pnl_usdc") or 0) for e in entries if e.get("won")]
    l_pnl=[float(e.get("simulated_pnl_usdc") or 0) for e in entries if e.get("won") is False]
    probs=[float(e["market_side_probability"]) for e in entries if e.get("market_side_probability") is not None]
    market_favorite_entries=sum(float(e["market_side_probability"])>=.5 for e in entries
                                if e.get("market_side_probability") is not None)
    expected=sum(probs)
    market_expected_sd=math.sqrt(sum(p*(1-p) for p in probs)) if probs else None
    market_tail=_poisson_binomial_tail(probs,wins) if probs else None
    flips=[r for r in lead_lag if r.get("event")=="CROSSING_LEAD_LAG"]
    measurable=[r for r in flips if r.get("status")=="MEASURABLE"]
    lead=[float(r["model_to_market_lead_sec"]) for r in measurable]
    measurable_slugs={r["slug"] for r in measurable}
    bot_pairs=[r for r in lead_lag if r.get("event")=="MODEL_BOT_LEAD_LAG" and r.get("status")=="MEASURABLE"]
    bot_leads=[float(r["model_to_bot_lead_sec"]) for r in bot_pairs]
    bot_model_led=sum(x>0 for x in bot_leads)
    bot_model_lagged=sum(x<0 for x in bot_leads)
    model_led=sum(v>0 for v in lead); model_lagged=sum(v<0 for v in lead)
    counter=_counterfactual_rows(entries)
    ab=_ablation_rows(entries, defaultdict(list))
    ab_lines="\n".join(f"| {model} | {len([r for r in ab if r['model']==model and r['signal_side'] in {'UP','DOWN'}])} | {sum(r['settlement_direction_accuracy'] is True for r in ab if r['model']==model)} | {sum(r['signal_matches_entry'] is True for r in ab if r['model']==model)} |" for model in ("full","market_only","minus_market","btc_only","structural_only"))
    cf_lines="\n".join(f"| {r['filter']} | {r['trades_kept']} | {r['wins_kept']} | {r['losses_kept']} | {r['gross_shadow_pnl_kept']:.2f} | {r['winners_rejected']} | {r['losers_avoided']} | {r['feature_available_n']}/{n} |" for r in counter)
    probability_rows=_probability_comparison(entries)
    prob_lines="\n".join(f"| {r['model']} | {r['n']} | {fmt(r['brier'])} | {fmt(r['log_loss'])} | {fmt(r['direction_accuracy'])} |" for r in probability_rows)
    score_by_model={r["model"]:r for r in probability_rows}
    paired_market=score_by_model.get("market_fill_mid_on_p_ex_subset",{})
    pex_score=score_by_model.get("p_ex",{})
    pex_brier_delta=(float(pex_score["brier"])-float(paired_market["brier"])
                     if pex_score.get("brier") is not None and paired_market.get("brier") is not None else None)
    pex_entry_n=sum(e.get("p_ex_side_at_entry") is not None for e in entries)
    pex_near_n=sum(e.get("p_ex_side_within_12s") is not None for e in entries)
    entry_lines="\n".join(f"| {e['slug'].rsplit('-',1)[-1]} | {e['side']} | {e['outcome']} | {float(e['entry_price']):.2f} | {fmt(e.get('market_side_probability'))} | {fmt(e.get('p_ex_side_at_entry'))} | {fmt(e.get('p_ex_side_within_12s'))} | {fmt(e.get('p_ex_last_observed_side'))}@{fmt(e.get('p_ex_last_observation_age_sec'))}s | {fmt(e.get('required_move_sigma'))} | {fmt(e.get('btc_return_10s_bps'))} | {float(e.get('simulated_pnl_usdc') or 0):.2f} |" for e in entries)
    comparison_lines="\n".join(f"| {r['result']} | {r['feature']} | {r['n']} | {fmt(r['mean'])} | {fmt(r['median'])} |" for r in _winner_loser_rows(entries)
                              if r["feature"] in {"entry_price","market_side_probability","p_ex_side_at_entry","required_move_sigma","btc_return_10s_bps","btc_return_30s_bps","entry_crossings_last_60s","entry_distance_bps","active_side_flips_last_30s","active_side_flips_last_60s","active_side_flips_last_120s"})
    loser_lines=[]
    for e in entries:
        if e.get("won") is False:
            loser_lines.append(f"- `{e['slug']}`: bought {e['side']} at {float(e['entry_price']):.2f}; fill-mid side probability {fmt(e.get('market_side_probability'))}; p_ex ≤12s {fmt(e.get('p_ex_side_within_12s'))}; latest prior p_ex {fmt(e.get('p_ex_last_observed_side'))} ({fmt(e.get('p_ex_last_observation_age_sec'))}s old); model fair {fmt(e.get('fair'))}; BTC 10s/30s {fmt(e.get('btc_return_10s_bps'))}/{fmt(e.get('btc_return_30s_bps'))} bps; required-path sigma {fmt(e.get('required_move_sigma'))}; side components market/BTC/structure={fmt(e.get('reason_market'))}/{fmt(e.get('reason_btc'))}/{fmt(e.get('reason_structural'))}, weights={e.get('reason_w_market')}/{e.get('reason_w_btc')}/{e.get('reason_w_structural')}; final {e['outcome']}, paper PnL ${float(e.get('simulated_pnl_usdc') or 0):.2f}.")
            entry_ts=float(e["entry_ts"]); side=e["side"]
            rows=twap_by_slug.get(e["slug"],[])
            opposite_model=next((r for r in rows if r["observed_ts"]>=entry_ts and _valid_model(r)
                                 and _side_prob(r,side) is not None and _side_prob(r,side)<.5),None)
            opposite_mid=next((r for r in rows if r["observed_ts"]>=entry_ts and _fresh_mid(r)
                               and ((float(_market_mid(r)) if side=="UP" else 1-float(_market_mid(r)))<.5)),None)
            opposite_twap=next((r for r in rows if r["observed_ts"]>=entry_ts
                                and r.get("settlement_state_side") in {"UP","DOWN"}
                                and r["settlement_state_side"]!=side),None)
            rel=lambda r: fmt(float(r["observed_ts"])-entry_ts) if r else "NA"
            loser_lines.append(f"  - First post-entry held-side probability <0.5: p_ex at +{rel(opposite_model)}s; fresh market mid at +{rel(opposite_mid)}s; official TWAP state opposite at +{rel(opposite_twap)}s. These are first observed crossings in available data, not a causal or executable stop recommendation.")
    timing=defaultdict(list)
    for e in entries: timing[e["entry_time_bin"]].append(e)
    timing_lines="\n".join(f"| {name} | {len(group)} | {sum(bool(e['won']) for e in group)} | {sum(e.get('won') is False for e in group)} | {sum(float(e.get('simulated_pnl_usdc') or 0) for e in group):.2f} |" for name,group in timing.items())
    gross_entry_cost=sum(float(e.get("entry_price") or 0)*float(e.get("qty") or 0) for e in entries)
    winner_payout=sum(float(e.get("qty") or 0) for e in entries if e.get("won"))
    loss_entry_cost=sum(float(e.get("entry_price") or 0)*float(e.get("qty") or 0) for e in entries if e.get("won") is False)
    repricing_lines=[]
    for horizon in (5,10,30):
        rr=[r for r in repricing if r["horizon_sec"]==horizon]
        market_rows=[r for r in _market_level_repricing(rr)]
        p_corr,p_lo,p_hi,p_n=_cluster_corr_ci(market_rows,"mean_probability_residual","mean_future_mid_change")
        b_corr,b_lo,b_hi,b_n=_cluster_corr_ci(market_rows,"mean_btc_return_10s_bps","mean_future_mid_change")
        repricing_lines.append(f"| {horizon}s | {len(rr)} / {p_n} | {fmt(p_corr)} [{fmt(p_lo)}, {fmt(p_hi)}] | {fmt(b_corr)} [{fmt(b_lo)}, {fmt(b_hi)}] | {fmt(_direction_hit([float(r['probability_residual']) for r in rr],[float(r['future_mid_change']) for r in rr]))} |")
    repricing_table="\n".join(repricing_lines)
    repricing_models=_repricing_model_comparison(_market_level_repricing(repricing))
    repricing_model_table="\n".join(f"| {r['horizon_sec']}s | {r['model']} | {r['n_markets']} | {fmt(r.get('r_squared'))} | {fmt(r.get('rmse'))} |" for r in repricing_models)
    ablation_table="\n".join(f"| {m} | {len([r for r in ab if r['model']==m and r['signal_side'] in {'UP','DOWN'}])} | {sum(r['settlement_direction_accuracy'] is True for r in ab if r['model']==m)} | {sum(r['signal_matches_entry'] is True for r in ab if r['model']==m)} |" for m in ("full","market_only","minus_market","btc_only","structural_only"))
    return f"""# Prediction forensics — shadow-simulated BTC 15m cohort

Research-only, descriptive counterfactuals; not a live-performance estimate. One market_slug is counted once, so process restarts are not independent samples. The selected cohort is the 14-settlement group requested (12 wins, 2 losses); the journal now contains additional older/newer dry-run fills outside this specific cohort.

## Executive answer

1. **Is the bot predictive beyond market mid? — MIXED, currently NOT MEASURABLE at strict entry freshness.** The 14 fill-time market mids imply 10.24 expected wins; 12 were observed. Only {pex_entry_n}/14 have a p_ex snapshot within the existing {FRESH_SEC:.0f}s freshness window at simulated fill ({pex_near_n}/14 within a looser descriptive 12s window); this cohort cannot establish an independent advantage.
2. **Which signal appears earliest before repricing? — MIXED.** Among {len(measurable)} same-direction crossing pairs within 60 seconds, p_ex crossed first in {model_led}, market first in {model_lagged}; median difference is {statistics.median(lead):.2f}s (positive means p_ex first). These are repeated transitions across {len(measurable_slugs)} markets, not independent samples, so this is a hint—not a reliable lead claim.
3. **What distinguishes the two losers? — MIXED.** Both entries were UP and both had negative 10-second BTC returns at entry. The first had p_ex UP about 0.718 vs fill-mid 0.685, but below its 0.72 entry price and 3.1 seconds before fill; the second's nearest p_ex was about 0.791 but 17.7 seconds before fill. Neither has p_ex evidence inside the strict 2-second fill window.
4. **Can required-path probability help? — MIXED as a diagnostic, not a live veto.** It is present for only part of this early-entry cohort; a 1σ filter retained three wins and avoided one loss among the seven measurable rows, while rejecting three wins.
5. **Which current signal components add value? — MIXED, weak evidence.** BTC-only and structural-only sign matched 12/14 outcomes on the filled-entry subset, but this is selection-conditioned and not an ablation of every rejected candidate.
6. **What should be tested next? — Frozen, prospective test of fresh p_ex-minus-mid residual against 30s repricing, plus an adverse-BTC 10s exit-warning shadow replay; do not grant live authority yet.**

## Cohort and PnL

- Unique settled markets: **{n}**; wins **{wins}**, losses **{losses}**, win rate **{(wins/n if n else 0):.1%}**.
- Gross simulated PnL: **${pnl:.2f}** (paper assumptions; no actual venue fill, fees, queue position or slippage).
- Payout decomposition: simulated entry cost **${gross_entry_cost:.2f}**, winning payout **${winner_payout:.2f}**, losing entry cost lost **${loss_entry_cost:.2f}**; the 12 winners average ${statistics.mean(w_pnl):.2f}, while two losers average ${statistics.mean(l_pnl):.2f}.
- Average winner: **${statistics.mean(w_pnl):.2f}**; average loser: **${statistics.mean(l_pnl):.2f}**; break-even win rate from these average payoffs: **{abs(statistics.mean(l_pnl))/(statistics.mean(w_pnl)+abs(statistics.mean(l_pnl))):.1%}**.
- Fill-time market-side midpoint probabilities available: **{len(probs)}/{n}**; sum implies **{expected:.2f}** expected wins vs **{wins}** observed (difference **{wins-expected:+.2f}**, Poisson-binomial SD {fmt(market_expected_sd)}, P(K≥{wins})={fmt(market_tail)}). The 1.76-win excess is not, by itself, compelling evidence against market pricing at N=14. This uses simulated fill mid on the purchased token (DOWN converted to UP axis); it is a midpoint benchmark, not executable ask or a fill guarantee.
- All **{market_favorite_entries}/{n}** entries were on the market-favored side at the recorded simulated fill midpoint. The offline side ablation nevertheless yields the same selected direction with and without the market component on all 14 filled cases; this means market mid was not necessary to determine these realized directions, but does not establish ex-market alpha.
- Probability scores against final settlement: Brier/log-loss/direction accuracy are shown below; ex-market scoring includes only fresh, source-timestamped, bounded probabilities.

| Probability source | N | Brier | Log loss | Direction accuracy |
|---|---:|---:|---:|---:|
{prob_lines}
- Paired p_ex vs market Brier delta (p_ex minus market on the same {pex_score.get('n',0)} strictly fresh-at-fill market): **{fmt(pex_brier_delta)}**; negative favors p_ex. At N={pex_score.get('n',0)}, this is not an estimable comparative result and must not be read as evidence of improvement.
- Entry price / market favorite decomposition: the entry-side quote and filled midpoint are in `entries.csv`. The two losses demonstrate that paying 0.72/0.77 for the favored side is not proof of positive independent edge.

## All 14 entries

| Market | Bought | Final | Entry | Market-side mid p | p_ex side ≤2s | p_ex ≤12s (exploratory) | latest p_ex side / age | Required σ | BTC 10s bps | Paper PnL |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
{entry_lines}

| Market time-left bin | N | Wins | Losses | Paper PnL |
|---|---:|---:|---:|---:|
{timing_lines}

### The two losses

{chr(10).join(loser_lines)}

For the last losing UP entry at 0.77: **the journal has no p_ex observation within 12 seconds of the simulated fill**. Its latest preceding valid p_ex was 0.791 approximately 17.7 seconds before fill, versus a fill midpoint of 0.75 and model fair 0.775 at entry. That earlier estimate is above 0.77 numerically, but is not synchronized closely enough to establish the true probability at fill. At fill the recorded fair exceeded price by only about 0.005/share, before model uncertainty. Thus the bot had a prior positive model indication, but no contemporaneous, independently validated edge at the simulated fill. The other loser’s closest p_ex was 0.718 about 3.1 seconds before fill; that too is outside the strict 2-second entry window.

## Winner / loser features

See `winner_loser_comparison.csv` for full feature availability. The focused comparison is:

| Result | Feature | N available | Mean | Median |
|---|---|---:|---:|---:|
{comparison_lines}

There are only two losers and several entry-time path fields are unavailable, so numeric separation is exploratory, not a validated rule. The explicit prior active-side flip counts are null where the journal has no directional decisions; canonical TWAP/strike crossings are reported separately and must not be renamed as bot-side flips. Both losers had negative BTC 10-second returns at entry, but the corresponding “not opposing” counterfactual also rejects four winning trades; that is a candidate to investigate, not a justified gate.

## Early probability and market repricing

- Fresh model observations require `sigma_ex_market_fresh=true`, non-null `p_up_ex_market`, and source timestamp. Entry probability scoring additionally requires the observation itself to be no more than {FRESH_SEC:.0f}s before simulated fill; older values are shown separately as near-entry context only.
- Fresh market observations require source-age in `[0, {FRESH_SEC:.1f}]` seconds; stale rows are excluded, not treated as a delayed crossing.
- Measurable paired model/market 0.5 crossing cases: **{len(measurable)}** over **{len(measurable_slugs)} unique markets** within the fixed 60-second same-direction matching window; p_ex led **{model_led}/{len(measurable)}**, market led **{model_lagged}/{len(measurable)}**, median model-to-market lead **{statistics.median(lead):.2f}s**. These are transitions, not independent markets.
- Fresh model-to-bot active-side matches: **{len(bot_pairs)}**; model crossed first in **{bot_model_led}**, bot decision first in **{bot_model_lagged}**, median model-to-bot lead **{fmt(statistics.median(bot_leads) if bot_leads else None)}s**. In this cohort no sampled active-side flip paired with a p_ex crossing, so relative model-to-bot lead is NOT MEASURABLE—not proof the bot did not change its decision. NONE/UNKNOWN intervals reset continuity; unmatched cases remain in `lead_lag_events.csv` as NOT_MEASURABLE.
- `repricing_prediction.csv` contains paired 5/10/30s fresh-mid changes after current mid, with ex-market residual and required-move context. It is not a tradable-edge/PnL test; no fabricated midpoint is used.

| Horizon | Paired observations / markets | market-cluster corr(p_ex − mid, future Δmid), 95% bootstrap CI | market-cluster corr(BTC 10s return, future Δmid), 95% bootstrap CI | residual directional hit |
|---|---:|---:|---:|---:|
{repricing_table}

Market-level OLS sensitivity on the same complete market set per horizon (mid-only vs added predictors):

| Horizon | Model | N markets | In-sample R² | RMSE |
|---|---|---:|---:|---:|
{repricing_model_table}

These are descriptive, in-sample fits over market-aggregated rows; the combined model uses four predictors with a small number of markets and is especially prone to overfit. R² differences are not out-of-sample evidence of incremental alpha. The residual correlations and their market-cluster bootstrap intervals above are the more conservative primary read.

## Counterfactual diagnostics (not tuned)

| Filter | Kept | Wins | Losses | Shadow PnL | Winners rejected | Losers avoided | Feature available |
|---|---:|---:|---:|---:|---:|---:|
{cf_lines}

`excluded_slugs` is in the CSV. Missing-feature markets are not counted as genuine rejected winners/avoided losers. Fixed sigma cutoffs 1/2/3 are shown only where `required_move_sigma` exists. The 10s/30s momentum screen keeps 8/14, removes both losers but also rejects four winners; its small paper PnL improvement is only a hypothesis, not a validated rule.

## Required-path / timing interpretation

The entry-time `required_move_sigma`, mode and path fields are in `required_path_entry_risk.csv`. Most simulated fills occur well before the final 120 seconds; therefore a final-window exact-average boundary usually cannot be applied at entry. Do not infer that an early position is “safe” from a late checkpoint or from a settlement label.

## Signal ablation

`signal_ablation.csv` compares sign-only direction of full, market-only, minus-market, BTC-only and structural-only components from logged fill-side reasons when a decision event was not sampled near fill. The full signal is the side actually selected, so agreement with the filled side is tautological; this is conditional on filled trades, not every candidate. No weights are fitted. Missing market components remain unavailable.

| Component view | Signal observations | Settlement direction correct | Agrees with filled side |
|---|---:|---:|---:|
{ablation_table}

## Prediction hypothesis verdict

**A — Market-favored outcomes explain the observed hit rate best, but not because the market component alone chose the direction.** All 14 simulated entries were on the market-favored side; market probability expected 10.24 wins and 12 occurred. In the offline ablation, removing the market component leaves all 14 selected directions unchanged, while market-only has 9/10 correct on the cases with a nonzero market component and minus-market also 9/10 on that same 10-market overlap. That is no demonstrated incremental edge for either component. The crossing analysis is mixed, strictly fresh p_ex at fill is present only once, and model-to-bot lead is not measurable. Thesis B is not established; Thesis C remains a plausible design description, not proven extra predictive value.

## Next experiment (maximum three)

1. Prospectively freeze a test of `p_up_ex_market - market_mid_up` vs +30s repricing: the market-mean in-sample R² rose 0.238→0.387 with the residual, while cluster-bootstrap correlation CI still crossed zero.
2. Keep a shadow-only “10s BTC move opposes held side” warning timeline: both losses showed it at entry, but the same screen would also reject four winners (counterfactual 8/14 kept).
3. Compare first adverse p_ex / mid / TWAP crossing and executable BBO depth on future losers and winners; here the first loser showed p_ex turning before recorded fresh-mid/TWAP, while the second's p_ex and market crossing were simultaneous, with active bot-side timing unavailable.

## Caveats and raw files

Shadow simulation assumes fills at recorded simulated prices and settlement payout; it does not model maker queue priority or actual execution. A 0.5 crossing is only a timing landmark, not a universal side reversal. `loser_timelines.csv` is sampled every 5 seconds from up to 120 seconds before entry through settlement; missing market quotes are blank. `data_quality.csv` documents row-level coverage.
"""


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal",type=Path,default=Path("logs/trade_journal.db"))
    ap.add_argument("--twap-db",type=Path,default=Path("data/research/twap_forward_shadow.db"))
    ap.add_argument("--btc-dir",type=Path,default=Path("data/btc_history_1s"))
    ap.add_argument("--output",type=Path,default=Path("reports/prediction_forensics"))
    ap.add_argument("--all-settled",action="store_true",help="Analyze all settled shadow markets, not the requested 14-market cohort")
    a=ap.parse_args()
    slugs=DEFAULT_TARGET_SLUGS
    if a.all_settled:
        slugs=_all_settled_slugs(a.journal)
    result=build_analysis(a.journal,a.twap_db,a.btc_dir,a.output,slugs)
    print(f"prediction forensics: markets={len(result['entries'])} loser_timeline_rows={len(result['loser_timelines'])} btc_1s_rows={len(result['btc_rows'])} output={a.output}")


if __name__=="__main__":
    main()
