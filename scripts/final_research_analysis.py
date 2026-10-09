#!/usr/bin/env python3
"""Pre-live research synthesis for entry and stop-loss decisions (read-only inputs).

Inputs : logs/trade_journal.db (mode=ro), data/research_export/{A_market_summary,B_decisions,P_paths}
Outputs: <out>/tables/*.csv, <out>/results.json   (report text is written separately)

Unit of analysis is the market (one 15-minute window); intervals are Wilson at
market level plus a day-clustered bootstrap where at least 3 days exist.
Native freshness-v2 and historical pre-v2 path rows are always reported separately.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import glob
import json
import math
import random
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

LIVE_END = "2026-10-01"


# ------------------------------------------------------------------ helpers
def wilson(k, n, z=1.959964):
    if not n:
        return [None, None]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 4), round(c + h, 4)]


def day_bootstrap(rows, stat, B=2000, seed=11):
    """rows: list of (day, value...). stat: fn(list rows)->float. Resample days."""
    days = sorted({r[0] for r in rows})
    if len(days) < 3:
        return None
    by = defaultdict(list)
    for r in rows:
        by[r[0]].append(r)
    rng = random.Random(seed)
    vals = []
    for _ in range(B):
        sample = [r for d in (rng.choice(days) for _ in days) for r in by[d]]
        try:
            vals.append(stat(sample))
        except ZeroDivisionError:
            continue
    vals.sort()
    return [round(vals[int(.025 * len(vals))], 4), round(vals[int(.975 * len(vals))], 4)] if vals else None


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def slug_start(slug: str) -> int:
    return int(str(slug).rsplit("-", 1)[-1])


def bucket(value, edges):
    if value is None:
        return "NA"
    for lo, hi in zip(edges, edges[1:]):
        if lo <= value < hi:
            return f"[{lo},{hi})"
    return f">={edges[-1]}" if value >= edges[-1] else f"<{edges[0]}"


def summarize(rows, key_fn, *, win="won", price="price", pnl="pnl"):
    """Group market-level rows -> n, n_days, win rate (Wilson), avg price, edge, pnl."""
    groups = defaultdict(list)
    for r in rows:
        groups[key_fn(r)].append(r)
    out = []
    for key, rs in sorted(groups.items(), key=lambda kv: str(kv[0])):
        n = len(rs)
        k = sum(1 for r in rs if r[win])
        avg_price = statistics.mean(r[price] for r in rs) if rs and rs[0].get(price) is not None else None
        out.append({"group": key, "n_markets": n, "n_days": len({r["day"] for r in rs}),
                    "win_rate": round(k / n, 4) if n else None, "win_wilson95": wilson(k, n),
                    "avg_price": round(avg_price, 4) if avg_price is not None else None,
                    "edge_win_minus_price": round(k / n - avg_price, 4) if n and avg_price is not None else None,
                    "pnl_total": round(sum(r.get(pnl) or 0 for r in rs), 3) if pnl else None,
                    "pnl_per_market": round(sum(r.get(pnl) or 0 for r in rs) / n, 4) if pnl and n else None})
    return out


def write_csv(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", newline="") as handle:
        w = csv.DictWriter(handle, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in r.items()})


# ------------------------------------------------------------------ settlements
def load_settlements(provenance_csv: Path, labels: str = "official"):
    """Outcome labels from the provenance dataset (scripts/build_outcome_provenance.py).

    labels="official": outcome_used (Polymarket official > canonical TWAP; journal never primary).
    labels="journal" : the runtime journal outcome, for diagnostic comparison only.
    """
    rows = list(csv.DictReader(provenance_csv.open()))
    key = "outcome_used" if labels == "official" else "journal_outcome"
    settle = {r["market_slug"]: r[key] for r in rows if r[key] in ("UP", "DOWN")}
    meta = {"provenance_csv": provenance_csv.name, "provenance_csv_sha256": hashlib.sha256(provenance_csv.read_bytes()).hexdigest(), "labels": labels,
            "official_cache_content_sha256": rows[0]["official_cache_content_sha256"] if rows else None,
            "markets_labelled": len(settle),
            "outcome_source_used": dict(Counter(r["outcome_source_used"] for r in rows)),
            "journal_mismatch": sum(1 for r in rows if r["journal_mismatch"] == "1"),
            "journal_compared": sum(1 for r in rows if r["journal_mismatch"] != ""),
            "twap_mismatch": sum(1 for r in rows if r["twap_mismatch"] == "1"),
            "twap_compared": sum(1 for r in rows if r["twap_mismatch"] != "")}
    return settle, meta


# ------------------------------------------------------------------ part 1: live trades
def live_trades(conn, settle):
    live_runs = {r[0] for r in conn.execute("SELECT run_id FROM strategy_runs WHERE mode='LIVE'")}
    markout = {}
    for (pj,) in conn.execute("SELECT payload_json FROM order_events WHERE event_type='FILL_MARKOUT' AND side='BUY'"):
        d = json.loads(pj or "{}")
        markout.setdefault(d.get("fill_id"), d)
    exits = {r[0]: r[1] for r in conn.execute(
        "SELECT client_order_id, reason FROM order_events WHERE event_type='ORDER_TAKER_EXIT_SUBMIT'")}
    redeemed = defaultdict(float)  # on-chain redemption evidence (partial coverage)
    for (pj,) in conn.execute("SELECT payload_json FROM strategy_events WHERE event_type='REDEEM_EXECUTED'"):
        d = json.loads(pj or "{}")
        if d.get("slug") and d.get("status") == 1:
            redeemed[d["slug"]] += float(d.get("redeem_position_size_shares") or 0)
    fills = conn.execute("SELECT run_id, ts, client_order_id, side, price, qty, instrument_id, payload_json "
                         "FROM order_events WHERE event_type='ORDER_FILLED' AND ts < ? ORDER BY id", (LIVE_END,)).fetchall()
    positions = {}
    for run, ts, coid, side, price, qty, inst, pj in fills:
        if run not in live_runs or not price or not qty:
            continue
        d = json.loads(pj or "{}")
        slug = d.get("slug") or d.get("market_slug")
        inst = inst or d.get("instrument_id")
        if not slug:
            continue
        pos = positions.setdefault((slug, inst), {"slug": slug, "instrument_id": inst, "buys": [], "sells": [],
                                                  "fees": 0.0, "first_ts": ts})
        pos["fees"] += float(d.get("effective_fee_usdc") or 0)
        (pos["buys"] if side == "BUY" else pos["sells"]).append((float(price), float(qty), coid))
        if side == "BUY" and coid in markout:
            pos.setdefault("markout", markout[coid])
    rows = []
    for (slug, inst), p in positions.items():
        if not p["buys"] or slug not in settle or "markout" not in p:
            continue
        m = p["markout"]
        held_side = m.get("entry_outcome_side")
        if held_side not in ("UP", "DOWN"):
            continue
        qty_b = sum(q for _, q, _ in p["buys"])
        cost = sum(px * q for px, q, _ in p["buys"])
        sold = [(px, q, c) for px, q, c in p["sells"]]
        qty_s = min(qty_b, sum(q for _, q, _ in sold))
        proceeds = sum(px * q for px, q, _ in sold)
        win = settle[slug] == held_side
        residual_value = (qty_b - qty_s) * (1.0 if win else 0.0)
        pnl = proceeds + residual_value - cost - p["fees"]
        hold_pnl = qty_b * (1.0 if win else 0.0) - cost - p["fees"]
        kinds = {("STOP" if "TAKER-EXIT" in c and exits.get(c) in ("stop_loss", "invalidation_recovery", "offside_near_close")
                  else "TAKER_OTHER" if "TAKER-EXIT" in c else "URGENT" if "URGENT" in c else "MAKER_SELL")
                 for _, _, c in sold}
        exit_kind = "HELD_TO_SETTLEMENT" if not sold else "+".join(sorted(kinds))
        rows.append({"slug": slug, "day": p["first_ts"][:10], "side": held_side, "settlement": settle[slug], "won": win,
                     "price": cost / qty_b, "qty": qty_b, "pnl": pnl, "hold_pnl": hold_pnl,
                     "cost_usdc": cost, "sell_proceeds_usdc": proceeds, "fees_usdc": p["fees"],
                     "residual_qty": qty_b - qty_s, "settlement_payoff_usdc": residual_value,
                     "redeem_shares_recorded": redeemed.get(slug),
                     "exit_kind": exit_kind, "exit_minus_hold": pnl - hold_pnl,
                     "time_left_sec": m.get("entry_time_left_sec"), "side_score": m.get("entry_side_score"),
                     "signed_spot_distance": m.get("entry_signed_spot_distance"), "weekend": m.get("entry_is_weekend_utc"),
                     "spread": m.get("entry_bbo_spread"), "model_probability": m.get("model_probability")})
    return rows


# ------------------------------------------------------------------ part 2: dry-run shadow sims
def shadow_sims(conn, settle):
    created = {}
    for (pj,) in conn.execute("SELECT payload_json FROM order_events WHERE event_type='SHADOW_SIM_ENTRY_FILLED'"):
        d = json.loads(pj or "{}")
        created[d.get("simulation_id")] = d
    rows = []
    for ts, pj in conn.execute("SELECT ts, payload_json FROM strategy_events WHERE event_type='SHADOW_SIM_CYCLE_RESULT' ORDER BY id"):
        d = json.loads(pj or "{}")
        f = created.get(d.get("simulation_id"), {})
        start = slug_start(d["slug"])
        filled_ts = f.get("filled_ts") or f.get("created_ts")
        outcome = settle.get(d["slug"], d["outcome"])
        won = outcome == d["side"]
        qty, price = float(d["qty"]), float(d["entry_price"])
        pnl = qty * (1.0 if won else 0.0) - qty * price  # relabelled with the official resolution
        rows.append({"slug": d["slug"], "day": day_of(start), "side": d["side"], "settlement": outcome,
                     "journal_outcome": d["outcome"], "won": won, "price": price, "pnl": pnl,
                     "pnl_per_share": pnl / qty if qty else None,
                     "elapsed_at_fill_sec": (filled_ts - start) if filled_ts else None,
                     "spot_minus_strike": (d["spot"] - d["strike"]) if d.get("spot") and d.get("strike") else None,
                     "weekend": datetime.fromtimestamp(start, timezone.utc).weekday() >= 5})
    return rows


# ------------------------------------------------------------------ part 3: gate counterfactual
def gate_counterfactual(export_root: Path, settle):
    first = {}
    for path in sorted(glob.glob(str(export_root / "B_decisions" / "*" / "entry_decisions.parquet"))):
        t = pq.read_table(path)
        cols = set(t.column_names)
        need = [c for c in ("slug", "state", "final_reason", "snap_outcome_side", "entry_price", "time_left_sec", "ts") if c in cols]
        for r in t.select(need).to_pylist():
            side, slug = r.get("snap_outcome_side"), r.get("slug")
            if side not in ("UP", "DOWN") or slug not in settle or r.get("entry_price") is None:
                continue
            reason = r.get("final_reason") or ("eligible" if r.get("state") == "ALLOW" else "unknown")
            reason = reason.split(":")[0].split(" entry=")[0]
            key = (slug, side, reason)
            if key not in first:  # first decision per market/side/reason: no pseudo-replication
                first[key] = {"slug": slug, "day": day_of(slug_start(slug)), "side": side, "reason": reason,
                              "state": r.get("state"), "price": float(r["entry_price"]),
                              "won": settle[slug] == side, "time_left_sec": r.get("time_left_sec")}
    return list(first.values())


# ------------------------------------------------------------------ part 4/5: path calibration & side accuracy
CUTOFFS = (600, 300, 180, 120, 60, 30)


def path_rows(export_root: Path, settle):
    out = []
    for path in sorted(glob.glob(str(export_root / "P_paths" / "*" / "paths_*.parquet"))):
        native = "native" in Path(path).name
        t = pq.read_table(path)
        if t.num_rows == 0 or "market_slug" not in t.column_names:
            continue
        cols = [c for c in ("market_slug", "snapshot_ts", "best_bid_up", "best_ask_up", "best_bid_down", "best_ask_down",
                            "market_mid_up", "market_mid_down", "official_twap", "strike", "twap_fresh",
                            "required_move_sigma", "required_move_z_diffusion", "active_side", "side_score",
                            "market_mid_fresh", "twap_recomputation_class") if c in t.column_names]
        by = defaultdict(list)
        for r in t.select(cols).to_pylist():
            by[r["market_slug"]].append(r)
        for slug, rs in by.items():
            if slug not in settle:
                continue
            start = slug_start(slug)
            rs.sort(key=lambda r: float(r["snapshot_ts"]))
            for k in CUTOFFS:
                target = start + 900 - k
                cand = [r for r in rs if target - 5 <= float(r["snapshot_ts"]) <= target]
                if not native:
                    cand = [r for r in cand if str(r.get("twap_recomputation_class") or "").startswith("FRESH")]
                cand = [r for r in cand if r.get("market_mid_up") is not None and r.get("market_mid_down") is not None
                        and str(r.get("market_mid_fresh")).lower() not in ("false", "0")]
                if not cand:
                    continue
                r = cand[-1]
                out.append({"slug": slug, "day": day_of(start), "cutoff": k, "provenance": "NATIVE_V2" if native else "HISTORICAL_PRE_V2",
                            "settlement": settle[slug], **{c: r.get(c) for c in cols if c != "market_slug"}})
    return out


def calibration(rows):
    """Per cutoff: the cheaper (market-losing) token. Selling it at its bid is +EV iff bid > P(win)."""
    table = []
    for prov in ("NATIVE_V2", "HISTORICAL_PRE_V2"):
        for k in CUTOFFS:
            for lo, hi in ((0.0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5)):
                sel = []
                for r in rows:
                    if r["provenance"] != prov or r["cutoff"] != k:
                        continue
                    up, down = float(r["market_mid_up"]), float(r["market_mid_down"])
                    side, mid = ("UP", up) if up < down else ("DOWN", down)
                    bid = r.get(f"best_bid_{side.lower()}")
                    if lo <= mid < hi and bid is not None:
                        sel.append((r["day"], mid, float(bid), r["settlement"] == side))
                n = len(sel)
                if not n:
                    continue
                k_win = sum(1 for s in sel if s[3])
                table.append({"provenance": prov, "cutoff_sec": k, "losing_side_mid_bucket": f"[{lo},{hi})",
                              "n_markets": n, "n_days": len({s[0] for s in sel}),
                              "avg_mid": round(statistics.mean(s[1] for s in sel), 4),
                              "avg_bid": round(statistics.mean(s[2] for s in sel), 4),
                              "realized_win_rate": round(k_win / n, 4), "win_wilson95": wilson(k_win, n),
                              "sell_at_bid_edge_per_share": round(statistics.mean(s[2] for s in sel) - k_win / n, 4),
                              "day_bootstrap95_sell_edge": day_bootstrap(
                                  [(s[0], s[2], s[3]) for s in sel],
                                  lambda rs: statistics.mean(x[1] for x in rs) - sum(1 for x in rs if x[2]) / len(rs))})
    return table


def z_flip(rows):
    """Settlement flip of the TWAP leader by cutoff and difficulty bucket (native: diffusion z)."""
    table = []
    for prov, field, edges in (("NATIVE_V2", "required_move_z_diffusion", (0, .5, 1, 1.5, 2, 3)),
                               ("NATIVE_V2", "required_move_sigma", (0, .5, 1, 2, 4)),
                               ("HISTORICAL_PRE_V2", "required_move_sigma", (0, .5, 1, 2, 4))):
        for k in (300, 180, 120, 60):
            groups = defaultdict(list)
            for r in rows:
                if r["provenance"] != prov or r["cutoff"] != k or r.get("official_twap") is None or r.get("strike") is None:
                    continue
                if str(r.get("twap_fresh")).lower() in ("false", "0"):
                    continue
                twap, strike = float(r["official_twap"]), float(r["strike"])
                if twap == strike or r.get(field) is None:
                    continue
                leader = "UP" if twap > strike else "DOWN"
                groups[bucket(float(r[field]), edges)].append((r["day"], leader != r["settlement"]))
            for b, sel in sorted(groups.items()):
                n, f = len(sel), sum(1 for s in sel if s[1])
                table.append({"provenance": prov, "metric": field, "cutoff_sec": k, "bucket": b, "n_markets": n,
                              "n_days": len({s[0] for s in sel}), "flip_rate": round(f / n, 4), "flip_wilson95": wilson(f, n)})
    return table


def side_accuracy(rows):
    """Does the bot's chosen side beat the price of that side at each cutoff?"""
    table = []
    for prov in ("NATIVE_V2", "HISTORICAL_PRE_V2"):
        for k in CUTOFFS:
            sel = []
            for r in rows:
                side = str(r.get("active_side") or "").upper()
                if r["provenance"] != prov or r["cutoff"] != k or side not in ("UP", "DOWN"):
                    continue
                ask = r.get(f"best_ask_{side.lower()}")
                mid = r.get(f"market_mid_{side.lower()}")
                if ask is None or mid is None:
                    continue
                sel.append((r["day"], float(mid), float(ask), r["settlement"] == side,
                            abs(float(r.get("side_score") or 0))))
            for label, part in (("all", sel), ("score>=0.30", [s for s in sel if s[4] >= .30]),
                                ("score<0.30", [s for s in sel if s[4] < .30])):
                n = len(part)
                if not n:
                    continue
                w = sum(1 for s in part if s[3])
                table.append({"provenance": prov, "cutoff_sec": k, "subset": label, "n_markets": n,
                              "n_days": len({s[0] for s in part}), "chosen_side_win_rate": round(w / n, 4),
                              "win_wilson95": wilson(w, n), "avg_mid": round(statistics.mean(s[1] for s in part), 4),
                              "avg_ask": round(statistics.mean(s[2] for s in part), 4),
                              "edge_vs_mid": round(w / n - statistics.mean(s[1] for s in part), 4),
                              "edge_vs_ask_taker": round(w / n - statistics.mean(s[2] for s in part), 4),
                              "day_bootstrap95_edge_vs_mid": day_bootstrap(
                                  [(s[0], s[1], s[3]) for s in part],
                                  lambda rs: sum(1 for x in rs if x[2]) / len(rs) - statistics.mean(x[1] for x in rs))})
    return table


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default="logs/trade_journal.db")
    ap.add_argument("--export", default="data/research_export")
    ap.add_argument("--out", required=True)
    ap.add_argument("--provenance", required=True, help="market_outcomes_<hash>.csv from build_outcome_provenance.py")
    ap.add_argument("--labels", choices=("official", "journal"), default="official")
    a = ap.parse_args()
    out, export_root = Path(a.out), Path(a.export)
    conn = sqlite3.connect(f"file:{Path(a.journal).resolve()}?mode=ro", uri=True, timeout=30)
    settle, settle_meta = load_settlements(Path(a.provenance), a.labels)
    live = live_trades(conn, settle)
    sims = shadow_sims(conn, settle)
    gates = gate_counterfactual(export_root, settle)
    paths = path_rows(export_root, settle)
    conn.close()

    res = {"settlement_sources": settle_meta, "generated_at_utc": datetime.now(timezone.utc).isoformat()}
    res["live_overall"] = summarize(live, lambda r: "all")
    res["live_by_exit_kind"] = summarize(live, lambda r: r["exit_kind"])
    res["live_by_price"] = summarize(live, lambda r: bucket(r["price"], (0, .5, .6, .7, .8, .9, 1.0)))
    res["live_by_time_left"] = summarize(live, lambda r: bucket(r["time_left_sec"], (0, 120, 300, 480, 600, 900)))
    res["live_by_score"] = summarize(live, lambda r: bucket(abs(r["side_score"]) if r["side_score"] is not None else None, (0, .2, .3, .4, .6, 1.01)))
    res["live_by_weekend"] = summarize(live, lambda r: "weekend" if r["weekend"] else "weekday")
    stops = [r for r in live if "STOP" in r["exit_kind"]]
    res["live_stop_vs_hold"] = {"n_markets": len(stops), "n_days": len({r["day"] for r in stops}),
                                "stops_where_held_side_won": sum(1 for r in stops if r["won"]),
                                "realized_pnl": round(sum(r["pnl"] for r in stops), 3),
                                "hold_to_settlement_pnl": round(sum(r["hold_pnl"] for r in stops), 3),
                                "stop_minus_hold": round(sum(r["exit_minus_hold"] for r in stops), 3)}
    tps = [r for r in live if r["exit_kind"] == "MAKER_SELL"]
    res["live_maker_sell_vs_hold"] = {"n_markets": len(tps), "realized_pnl": round(sum(r["pnl"] for r in tps), 3),
                                      "hold_to_settlement_pnl": round(sum(r["hold_pnl"] for r in tps), 3),
                                      "sell_minus_hold": round(sum(r["exit_minus_hold"] for r in tps), 3)}
    res["live_day_bootstrap95_pnl_per_market"] = day_bootstrap([(r["day"], r["pnl"]) for r in live],
                                                               lambda rs: statistics.mean(x[1] for x in rs))
    res["sim_overall"] = summarize(sims, lambda r: "all")
    res["sim_by_price"] = summarize(sims, lambda r: bucket(r["price"], (0, .5, .6, .7, .8, .9, 1.0)))
    res["sim_by_elapsed"] = summarize(sims, lambda r: bucket(r["elapsed_at_fill_sec"], (0, 300, 420, 540, 660, 780, 900)))
    res["sim_by_weekend"] = summarize(sims, lambda r: "weekend" if r["weekend"] else "weekday")
    res["sim_by_side"] = summarize(sims, lambda r: r["side"])
    res["sim_day_bootstrap95_pnl_per_market"] = day_bootstrap([(r["day"], r["pnl"]) for r in sims],
                                                              lambda rs: statistics.mean(x[1] for x in rs))
    res["gate_counterfactual"] = summarize(gates, lambda r: r["reason"], pnl=None)
    res["calibration_losing_side"] = calibration(paths)
    res["flip_by_difficulty"] = z_flip(paths)
    res["chosen_side_accuracy"] = side_accuracy(paths)
    res["counts"] = {"live_positions": len(live), "live_days": len({r["day"] for r in live}),
                     "sim_markets": len(sims), "sim_days": len({r["day"] for r in sims}),
                     "gate_rows": len(gates), "path_cutoff_rows": len(paths)}
    tables = out / "tables"
    for name, data in (("live_positions", live), ("shadow_sim_markets", sims), ("gate_counterfactual_rows", gates),
                       ("path_cutoff_rows", paths)):
        write_csv(tables / f"{name}.csv", data)
    for key, val in res.items():
        if isinstance(val, list) and val and isinstance(val[0], dict):
            write_csv(tables / f"{key}.csv", val)
    (out / "results.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res["counts"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
