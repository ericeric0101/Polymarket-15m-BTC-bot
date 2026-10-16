"""Stage 2: same-market comparison of journal vs official labels (offline, reads stage2_* tables)."""
import csv, json, statistics as st, sys
from collections import defaultdict
from pathlib import Path
D = Path(sys.argv[1])
def load(name, labels):
    return list(csv.DictReader(open(D / f"stage2_{labels}" / "tables" / name)))
f = lambda x: None if x in ("", "None", None) else float(x)
def bucket(v, edges):
    if v is None: return "NA"
    for lo, hi in zip(edges, edges[1:]):
        if lo <= v < hi: return f"[{lo},{hi})"
    return f">={edges[-1]}"
def stats(rows):
    n = len(rows)
    if not n: return {"n_markets": 0}
    pnl = [f(r["pnl"]) for r in rows]; w = sum(r["won"] == "True" for r in rows); price = st.mean(f(r["price"]) for r in rows)
    return {"n_markets": n, "n_days": len({r["day"] for r in rows}), "mean_pnl": round(st.mean(pnl), 4),
            "median_pnl": round(st.median(pnl), 4), "total_pnl": round(sum(pnl), 3), "win_rate": round(w / n, 4),
            "edge_win_minus_price": round(w / n - price, 4)}
out = {}
for table, key_fns in (("live_positions.csv", {"overall": lambda r: "all", "exit_kind": lambda r: r["exit_kind"],
                         "price": lambda r: bucket(f(r["price"]), (0, .5, .6, .7, .75, .8, .9, 1.0)),
                         "score": lambda r: bucket(abs(f(r["side_score"])) if f(r["side_score"]) is not None else None, (0, .2, .3, .4, .6, 1.01)),
                         "tte": lambda r: bucket(f(r["time_left_sec"]), (0, 120, 300, 480, 600, 900))}),
                       ("shadow_sim_markets.csv", {"overall": lambda r: "all",
                         "price": lambda r: bucket(f(r["price"]), (0, .5, .6, .7, .75, .8, .9, 1.0)),
                         "elapsed": lambda r: bucket(f(r["elapsed_at_fill_sec"]), (0, 300, 420, 540, 660, 900))})):
    off = {r["slug"] + r.get("side", ""): r for r in load(table, "official")}
    jou = {r["slug"] + r.get("side", ""): r for r in load(table, "journal")}
    common = sorted(set(off) & set(jou))
    cohort = "LIVE" if table.startswith("live") else "DRY_RUN"
    for dim, fn in key_fns.items():
        groups = defaultdict(lambda: ([], []))
        for k in common:
            groups[fn(off[k])][0].append(off[k]); groups[fn(jou[k])][1].append(jou[k])
        for g, (o, j) in sorted(groups.items()):
            so, sj = stats(o), stats(j)
            changed = (so.get("mean_pnl") is not None and sj.get("mean_pnl") is not None
                       and (so["mean_pnl"] > 0) != (sj["mean_pnl"] > 0))
            out.setdefault(cohort, []).append({"dimension": dim, "group": g, "official": so, "journal": sj,
                                               "mean_pnl_sign_changed": changed})
    out[cohort + "_full_official"] = stats(list(off.values()))
    out[cohort + "_common_markets"] = len(common)
    out[cohort + "_label_flips_in_common"] = sum(off[k]["won"] != jou[k]["won"] for k in common)
for labels in ("official", "journal"):
    r = json.load(open(D / f"stage2_{labels}" / "results.json"))
    out[f"stop_vs_hold_{labels}"] = r["live_stop_vs_hold"]; out[f"maker_sell_vs_hold_{labels}"] = r["live_maker_sell_vs_hold"]
json.dump(out, open(D / "stage2_comparison.json", "w"), indent=1)
print(json.dumps({k: v for k, v in out.items() if not isinstance(v, list)}, indent=1))
for cohort in ("LIVE", "DRY_RUN"):
    print("==", cohort)
    for row in out[cohort]:
        o, j = row["official"], row["journal"]
        print(f"  {row['dimension']:9s} {row['group']:12s} n={o.get('n_markets',0):3d} | official mean={o.get('mean_pnl')} win={o.get('win_rate')} | journal mean={j.get('mean_pnl')} win={j.get('win_rate')} {'<-- SIGN CHANGED' if row['mean_pnl_sign_changed'] else ''}")
