"""Stage 5A: P(final adverse | state at first stop candidate) and stop-minus-hold by state.

Uses the committed loaders and StopCandidateShadow (same replay as scripts/research_stop_analysis.py),
but keeps the candidate records returned by observe() so their state fields can be tabulated.
DRY-RUN shadow positions only; official outcomes; day = market UTC day."""
import csv, json, sqlite3, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, "scripts"); sys.path.insert(0, ".")
import research_stop_analysis as rsa
from bot.research.stop_shadow import StopCandidateShadow

D = Path(sys.argv[1])
official = {r["market_slug"]: r["outcome_used"] for r in csv.DictReader(open(
    "data/research_export/outcome_provenance/market_outcomes_5356cf95f81e.csv")) if r["outcome_used"] in ("UP", "DOWN")}
paths = rsa.load_paths(Path("data/research_export"))
conn = sqlite3.connect("file:logs/trade_journal.db?mode=ro", uri=True)
fills = {}
for (pj,) in conn.execute("SELECT payload_json FROM order_events WHERE event_type='SHADOW_SIM_ENTRY_FILLED'"):
    d = json.loads(pj or "{}"); fills[d.get("slug")] = d
conn.close()
cands = defaultdict(list)
for r in csv.DictReader(open(D / "stage2_official/tables/shadow_sim_markets.csv")):
    f = fills.get(r["slug"]); series = paths.get(("NATIVE_V2", r["slug"])) or paths.get(("HISTORICAL_PRE_V2", r["slug"]))
    if not f or not series or r["slug"] not in official:
        continue
    fill_ts = float(f.get("filled_ts") or f.get("created_ts")); end = int(r["slug"].rsplit("-", 1)[-1]) + 900
    pos = {"simulation_id": f.get("simulation_id"), "slug": r["slug"], "side": r["side"], "entry_price": float(r["price"]),
           "qty": float(f["qty"]), "filled_ts": fill_ts}
    sh = StopCandidateShadow(db=None, run_id="x", hard_loss_usdc=2.0, hard_loss_min_hold_sec=60)
    hold = pos["qty"] * ((1.0 if official[r["slug"]] == r["side"] else 0.0) - pos["entry_price"])
    side_l = r["side"].lower()
    for s in series:
        ts = float(s["snapshot_ts"])
        if ts < fill_ts or s.get(f"best_bid_{side_l}") is None or str(s.get("twap_fresh")).lower() in ("false", "0"):
            continue
        for c in sh.observe(position=pos, bid=s[f"best_bid_{side_l}"], ask=s.get(f"best_ask_{side_l}"), now_ts=ts,
                            twap=s.get("official_twap"), strike=s.get("strike"), time_left_sec=end - ts,
                            score=s.get("side_score"), legacy_sigma=s.get("required_move_sigma"),
                            z_diffusion=s.get("required_move_z_diffusion")):
            cands[c["stop_reason"]].append({**c, "day": r["day"], "hold": hold, "final_adverse": hold < 0})

def bucket(v, edges):
    if v is None: return "NA"
    for lo, hi in zip(edges, edges[1:]):
        if lo <= v < hi: return f"[{lo},{hi})"
    return f">={edges[-1]}"

out = {}
for reason, rows in cands.items():
    block = {"n_candidates": len(rows), "n_days": len({x["day"] for x in rows}),
             "p_final_adverse": round(sum(x["final_adverse"] for x in rows) / len(rows), 4),
             "mean_stop_minus_hold": round(st.mean(x["pnl_if_stop_now_usdc"] - x["hold"] for x in rows), 4)}
    for name, key, edges in (("by_tte_sec", "time_left_sec", (0, 120, 300, 480, 900)),
                             ("by_exit_bid", "current_exit_bid", (0, 0.2, 0.4, 0.6, 0.8)),
                             ("by_distance_bps", "distance_bps", (0, 2, 5, 10, 20)),
                             ("by_legacy_sigma", "legacy_sigma", (0, 0.5, 1, 2))):
        cells = defaultdict(list)
        for x in rows:
            cells[bucket(x.get(key), edges)].append(x)
        block[name] = {k: {"n": len(v), "n_days": len({x["day"] for x in v}),
                           "p_final_adverse": round(sum(x["final_adverse"] for x in v) / len(v), 3),
                           "mean_stop_minus_hold": round(st.mean(x["pnl_if_stop_now_usdc"] - x["hold"] for x in v), 3),
                           "descriptive_only": len({x["day"] for x in v}) < 3} for k, v in sorted(cells.items())}
    out[reason] = block
(D / "stage5" / "stop_state_breakdown.json").write_text(json.dumps(out, indent=1))
for reason, block in out.items():
    print("==", reason, {k: block[k] for k in ("n_candidates", "n_days", "p_final_adverse", "mean_stop_minus_hold")})
    for name in ("by_tte_sec", "by_exit_bid", "by_distance_bps", "by_legacy_sigma"):
        print("  ", name, {k: (v["n"], v["n_days"], v["p_final_adverse"], v["mean_stop_minus_hold"]) for k, v in block[name].items()})
