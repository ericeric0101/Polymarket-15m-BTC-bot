"""Flip rate by required_move_sigma bin vs naive diffusion baseline Phi(-z); market-level, per tier, cutoffs pooled ONLY within a market? No: one row per market per cutoff; cutoffs reported separately."""
import csv, math
from collections import defaultdict
from pathlib import Path
EV=Path(__file__).resolve().parent
man={m["market_id"]:m for m in csv.DictReader((EV.parents[1]/"data_validity_manifest.csv").open())}
cut=list(csv.DictReader((EV/"cutoff_leaders.csv").open()))
Phi=lambda x:0.5*(1+math.erf(x/math.sqrt(2)))
bins=[(0,0.5),(0.5,1),(1,2),(2,4),(4,1e9)]
def wil(k,n,z=1.96):
    if not n: return None
    p=k/n; d=1+z*z/n; c=(p+z*z/(2*n))/d; h=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d; return (round(c-h,3),round(c+h,3))
for tier in ("1","2"):
  for k in ("300","180","120"):
    acc=defaultdict(lambda:[0,0,0.0,set()])
    miss=0
    for r in cut:
        m=man[r["market_id"]]
        if m["tier"]!=tier or r["cutoff_sec"]!=k or r["leader"] not in("UP","DOWN"): continue
        try: z=float(r["required_move_sigma"])
        except: miss+=1; continue
        for lo,hi in bins:
            if lo<=z<hi:
                a=acc[(lo,hi)]; a[0]+=int(r["flip"]); a[1]+=1; a[2]+=Phi(-z); a[3].add(m["date_utc"])
    print(f"tier={tier} T-{k} (missing z={miss}):", "; ".join(f"z[{lo},{hi if hi<1e8 else 'inf'}) flips={a[0]}/{a[1]} wilson={wil(a[0],a[1])} meanPhi(-z)={a[2]/a[1]:.3f} N_days={len(a[3])}" for (lo,hi),a in sorted(acc.items())))
