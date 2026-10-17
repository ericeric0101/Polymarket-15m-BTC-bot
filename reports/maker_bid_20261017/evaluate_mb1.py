"""MB1 confirmatory evaluation (plan_mb1.md section 4). Written before any confirmatory PnL was read.

    .venv/bin/python -I reports/maker_bid_20261017/evaluate_mb1.py           # coverage only until the stop rule
    .venv/bin/python -I reports/maker_bid_20261017/evaluate_mb1.py --final   # verdict (only at N>=200 fills or 7 days)
"""
import json, sys, time
from pathlib import Path
import numpy as np, pandas as pd

R = Path(__file__).resolve().parents[2]
D = R / "data" / "maker_bid_dryrun" / "confirm"
START = 1792223834.0           # 2026-10-17T07:57:14Z: confirmatory dry-run start (addendum A2)
N_PRIMARY, MAX_DAYS = 200, 7

ev = [json.loads(l) for f in sorted(D.glob("events_*.jsonl")) for l in open(f)]
df = pd.DataFrame(ev)
fills = df[(df.event == "FILL") & (df.t >= START)]
st = df[(df.event == "SETTLED") & (df.t >= START)].drop_duplicates(["market_slug", "token_id"])
placed = df[(df.event == "PLACE_SENT") & (df.t >= START)]
days = (time.time() - START) / 86400
final_allowed = len(st) >= N_PRIMARY or days >= MAX_DAYS
print(f"days {days:.2f} | placed {len(placed)} | filled orders {fills.order_id.nunique()} | settled token-positions {len(st)} "
      f"| picked-off fills {int(fills.picked_off.sum())} | streams {fills.market_slug.str.rsplit('-', n=1).str[0].value_counts().to_dict()}")
if "--final" not in sys.argv or not final_allowed:
    print(f"PnL hidden (stop rule: >= {N_PRIMARY} settled positions or {MAX_DAYS} days; now {len(st)}, {days:.2f} d)")
    sys.exit(0)

st = st.assign(pnl_per_share=st.pnl / st.shares, day=pd.to_datetime(st.end_ts, unit="s", utc=True).dt.date.astype(str),
               stream=st.market_slug.str.rsplit("-", n=1).str[0])
m = float(st.pnl.sum() / st.shares.sum())
g = st.groupby("end_ts").agg(pnl=("pnl", "sum"), sh=("shares", "sum"))
rng = np.random.default_rng(20261017)
idx = rng.integers(0, len(g), size=(10000, len(g)))
boot = g.pnl.to_numpy()[idx].sum(1) / g.sh.to_numpy()[idx].sum(1)
lb = float(np.quantile(boot, 0.05))
tot = float(st.pnl.sum())
checks = {"mean_and_lb_positive": m > 0 and lb > 0,
          "drop_best_cluster_positive": tot - float(g.pnl.max()) > 0,
          "no_day_over_50pct": tot > 0 and float(st.groupby("day").pnl.sum().max() / tot) <= .5,
          "no_stream_over_50pct": tot > 0 and float(st.groupby("stream").pnl.sum().max() / tot) <= .5}
verdict = "FAIL" if m <= 0 else ("PASS" if all(checks.values()) else "INCONCLUSIVE")
print(json.dumps({"verdict": verdict, "n_positions": len(st), "mean_pnl_per_share": round(m, 4), "lb95_one_sided": round(lb, 4),
                  "total_pnl_usdc": round(tot, 3), "checks": checks,
                  "by_stream": st.groupby("stream").agg(n=("pnl", "size"), pnl=("pnl", "sum")).round(3).to_dict("index")}, indent=1))
