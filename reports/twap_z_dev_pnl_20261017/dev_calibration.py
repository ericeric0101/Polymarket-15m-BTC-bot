"""DEV diagnostics (prices only, every market x checkpoint): z-model calibration and exchange lead over Chainlink.
Outcome = reconstructed settlement side (validated ~exact vs official in mechanics). Exploratory."""
import sys
from math import erf, sqrt
from pathlib import Path
import numpy as np, pandas as pd
O = Path(__file__).resolve().parent
sys.path.insert(0, str(O))
import dev_pnl as D  # noqa: E402

Phi = np.vectorize(lambda x: 0.5 * (1 + erf(x / sqrt(2))))
prices = D.load("data/price_sources_1s")
rows = []
for a, (sp, tw) in D.SRC.items():
    q = prices[prices.source.isin([sp, tw])].copy()
    q["source"] = q.source.map({sp: "chainlink_spot", tw: "chainlink_twap60"})
    cs = D.ChainlinkSeries(q, window_offset=-62)
    s0 = (D.V3_START if a != "btc" else D.V2_START)
    last = int(q.last_source_ms.max() // 1000) - 700
    for dur in (300, 900):
        for start in range(s0 + (-s0 % dur), last, dur):
            end = start + dur
            k = cs.strike(start, end - 55)
            settle = cs.settlement(end)
            if k is None or settle is None:
                continue
            up = settle >= k
            for h in D.CHECKPOINTS:
                t = end - h
                req, rem, _ = cs.required_future_avg(end, k, t)
                sig, spot = cs.sigma(t), cs.spot_asof(t)
                if req is None or rem <= 0 or not sig or not spot:
                    continue
                sd = spot * sig * np.sqrt(rem / D.YR3)
                # realised average of the unobserved window seconds
                known_last = int(cs.known_spot(t).index.max())
                fut = cs.spot_px.reindex([s for s in cs.window(end) if s > known_last])
                rows.append({"asset": a, "dur": dur, "h": h, "z": (spot - req) / sd, "up": up,
                             "u": (fut.mean() - spot) / sd if fut.notna().all() and len(fut) else np.nan})
g = pd.DataFrame(rows)
g["p"] = Phi(g.z)
g["win_side_up"] = g.z > 0
g["absz"] = g.z.abs()
g["correct"] = np.where(g.z > 0, g.up, ~g.up)
print(f"checkpoints={len(g)} markets~{len(g)//11}")
print("calibration by |z| (model P = Phi(|z|) vs realised):")
b = g.groupby(pd.cut(g.absz, [0, .5, 1, 1.5, 2, 2.5, 3, 4, 6, 1e9]), observed=True).agg(
    n=("correct", "size"), model=("absz", lambda x: float(Phi(x).mean())), realised=("correct", "mean")).round(3)
print(b.to_string())
u = g.u.dropna()
print(f"standardised future-average move u: n={len(u)} sd={u.std():.2f} (model assumes 1.00) "
      f"| P(|u|>2)={np.mean(np.abs(u) > 2):.3f} (normal 0.046) | P(|u|>3)={np.mean(np.abs(u) > 3):.3f} (normal 0.003)")
print("u sd by asset/len/checkpoint band:")
g["band"] = pd.cut(g.h, [0, 20, 40, 55])
print(g.groupby(["dur", "band"], observed=True).u.agg(["count", "std"]).round(2).to_string())
print(g.groupby("asset").u.agg(["count", "std"]).round(2).to_string())

# exchange lead: Chainlink BTC 1s return vs Binance / Coinbase 1s returns k seconds earlier (local receipt clock)
cl = prices[prices.source == "chainlink_spot"].assign(t=lambda d: (d.last_received_ms // 1000).astype(int)).groupby("t").close.last()
for src in ("binance_btcusdt", "coinbase_btcusd"):
    ex = prices[prices.source == src].assign(t=lambda d: (d.last_received_ms // 1000).astype(int)).groupby("t").close.last()
    idx = range(max(cl.index.min(), ex.index.min()), min(cl.index.max(), ex.index.max()))
    c = np.log(cl.reindex(idx).ffill()).diff()
    e = np.log(ex.reindex(idx).ffill()).diff()
    cors = {k: round(float(c.corr(e.shift(k))), 3) for k in range(-2, 6)}
    print(f"corr(Chainlink ret[t], {src} ret[t-k]) by k:", cors)
