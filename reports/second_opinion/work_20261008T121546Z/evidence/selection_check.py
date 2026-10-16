"""Compare included (tier 1/2) vs excluded (tier 3) markets on observables available for all settled markets."""
import csv, statistics as st
from collections import defaultdict
from pathlib import Path
EV=Path(__file__).resolve().parent
man=list(csv.DictReader((EV.parents[1]/"data_validity_manifest.csv").open()))
cut=list(csv.DictReader((EV/"cutoff_leaders.csv").open()))
flip300={r["market_id"]:int(r["flip"]) for r in cut if r["cutoff_sec"]=="300" and r["leader"] in("UP","DOWN")}
g=defaultdict(list)
for m in man:
    if m["twap_cross_count"]=="" : continue
    grp=("weekend" if m["date_utc"] in("2026-10-03","2026-10-04") else "weekday", "included" if m["tier"] in("1","2") else "excluded")
    g[grp].append(m)
for k in sorted(g):
    ms=g[k]; cc=[int(m["twap_cross_count"]) for m in ms]
    fl=[flip300[m["market_id"]] for m in ms if m["market_id"] in flip300]
    print(k, "N_markets=",len(ms), "twap_cross_count mean=%.2f median=%s share>=2=%.2f"%(st.mean(cc), st.median(cc), sum(c>=2 for c in cc)/len(cc)),
          "T-300 flip (any fresh cutoff) = %d/%d"%(sum(fl),len(fl)))

print("\nSensitivity (LABELLED, NOT PRIMARY): all settled markets with a fresh-TWAP cutoff snapshot, any tier, per day")
mm={m["market_id"]:m for m in man}
for k in ("300","180","120","60","30"):
    byd=defaultdict(lambda:[0,0])
    for r in cut:
        if r["cutoff_sec"]!=k or r["leader"] not in("UP","DOWN") or not r["settlement_side"]: continue
        d=mm[r["market_id"]]["date_utc"]; byd[d][0]+=int(r["flip"]); byd[d][1]+=1
    print("T-"+k, {d:"%d/%d=%.3f"%(a,b,a/b) for d,(a,b) in sorted(byd.items())})
