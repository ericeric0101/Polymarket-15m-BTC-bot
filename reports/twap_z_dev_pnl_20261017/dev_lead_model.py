"""DEV (exploratory) B-study: exchange-lead nowcast + calibrated volatility for the TWAP late window.

Train : BTC 5m + 15m windows starting 2026-10-15T01:00Z .. 2026-10-16T12:00Z (prices only -> calibration).
Valid : BTC windows starting 2026-10-16T12:00Z .. T_U (2026-10-17T03:30Z); trading uses causal books.
Outcome: reconstructed settlement side (TWAP at end vs at start; exact vs official in mechanics checks).

Models (all causal at decision time t, local receipt clock):
  base : z = (S_c - req) / (S_c * sigma * sqrt(rem / 3yr))           S_c = latest Chainlink spot
  lead : N = S_c * B(t) / B(t - LAG)   (Binance return over the Chainlink lag applied to S_c)
         z = (N - req) / (N * sigma * sqrt(max(rem - LAG, 0.5) / 3yr))
  each : p_up = Phi(z / k), k fitted by max likelihood on Train only.
Rule (fixed before looking at Valid): first checkpoint T-55..T-5 where p - ask - fee >= MARGIN on either side.
"""
import json, sys
from math import erf, sqrt
from pathlib import Path
import numpy as np, pandas as pd

O = Path(__file__).resolve().parent
sys.path.insert(0, str(O))
import dev_pnl as D  # noqa: E402

LAG = 2                      # seconds; peak of corr(Chainlink ret[t], Binance ret[t-k]) on DEV
MARGIN = 0.03                # USDC/share, fixed a priori
SPLIT = 1792152000           # 2026-10-16T12:00Z
Phi = np.vectorize(lambda x: 0.5 * (1 + erf(x / sqrt(2))))
FEE = lambda a: .07 * a * (1 - a)


def build():
    prices = D.load("data/price_sources_1s")
    q = prices[prices.source.isin(["chainlink_spot", "chainlink_twap60"])]
    cs = D.ChainlinkSeries(q, window_offset=-62)
    bn = prices[prices.source == "binance_btcusdt"].assign(t=lambda d: d.last_received_ms / 1000.0).sort_values("t")
    bt, bp = bn.t.to_numpy(), bn.close.to_numpy()

    def b_at(t):
        i = int(np.searchsorted(bt, t, side="right")) - 1
        return float(bp[i]) if i >= 0 and t - bt[i] <= 3 else None

    rows = []
    last = int(q.last_source_ms.max() // 1000) - 700
    for dur in (300, 900):
        for start in range(D.V2_START + (-D.V2_START % dur), min(last, D.T_U), dur):
            end = start + dur
            k = cs.strike(start, end - 55)
            settle = cs.settlement(end)
            if k is None or settle is None:
                continue
            for h in D.CHECKPOINTS:
                t = end - h
                req, rem, _ = cs.required_future_avg(end, k, t)
                sig, sc = cs.sigma(t), cs.spot_asof(t)
                b_now, b_lag = b_at(t), b_at(t - LAG)
                if req is None or rem <= 0 or not sig or not sc:
                    continue
                n = sc * b_now / b_lag if b_now and b_lag else np.nan
                rows.append({"slug": f"btc-updown-{'5m' if dur == 300 else '15m'}-{start}", "start": start, "end": end,
                             "dur": dur, "h": h, "t": t, "up": settle >= k,
                             "z_base": (sc - req) / (sc * sig * np.sqrt(rem / D.YR3)),
                             "z_lead": (n - req) / (n * sig * np.sqrt(max(rem - LAG, 0.5) / D.YR3)) if n == n else np.nan})
    return pd.DataFrame(rows)


def fit_k(z, y):
    best = None
    for k in np.arange(0.8, 3.01, 0.02):
        p = np.clip(Phi(z / k), 1e-6, 1 - 1e-6)
        ll = float(np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
        if best is None or ll > best[1]:
            best = (round(float(k), 2), ll)
    return best[0]


def scores(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return {"logloss": round(float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))), 4),
            "brier": round(float(np.mean((p - y) ** 2)), 4)}


def main():
    g = build()
    g = g[g.z_lead.notna()].copy()
    g["y"] = g.up.astype(float)
    tr, va = g[g.start < SPLIT], g[g.start >= SPLIT]
    out = {"lag_sec": LAG, "margin": MARGIN, "n_train": len(tr), "n_valid": len(va)}
    PER_H = "--per-h" in sys.argv     # one k per checkpoint (calibration varies with time left)
    for m in ("base", "lead"):
        if PER_H:
            ks = {h: fit_k(tr.loc[tr.h == h, f"z_{m}"].to_numpy(), tr.loc[tr.h == h, "y"].to_numpy()) for h in D.CHECKPOINTS}
            g[f"p_{m}"] = Phi(g[f"z_{m}"] / g.h.map(ks))
            out[f"k_{m}"] = ks
            k = None
        else:
            k = fit_k(tr[f"z_{m}"].to_numpy(), tr.y.to_numpy())
            g[f"p_{m}"] = Phi(g[f"z_{m}"] / k)
            out[f"k_{m}"] = k
        out[f"valid_{m}"] = scores(g.loc[g.start >= SPLIT, f"p_{m}"].to_numpy(), va.y.to_numpy())
        out[f"valid_{m}_raw_k1"] = scores(Phi(va[f"z_{m}"]), va.y.to_numpy())
    va = g[g.start >= SPLIT].copy()
    # calibration table on Valid
    for m in ("base", "lead"):
        b = va.groupby(pd.cut(va[f"p_{m}"], [0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.0]), observed=True).agg(
            n=("y", "size"), pred=(f"p_{m}", "mean"), real=("y", "mean")).round(3)
        out[f"calib_valid_{m}"] = b.reset_index().astype(str).to_dict("records")

    # trading on Valid with causal books
    books = D.load("data/polymarket_book_1s", ["ts_sec", "market_slug", "outcome", "best_bid", "best_ask", "ask_size",
                                               "last_update_received_ms", "sample_ms"])
    if "sample_ms" not in books:
        books["sample_ms"] = np.nan
    books = books[books.market_slug.str.startswith("btc-updown-") & (books.ts_sec >= SPLIT - 100)]
    bx = D.BookIndex(books)
    trades = {}
    for m in ("base", "lead"):
        sel = []
        for slug, gg in va.sort_values("t").groupby("slug"):
            for r in gg.itertuples():
                up, dn = bx.asof(slug, "Up", r.t), bx.asof(slug, "Down", r.t)
                p = getattr(r, f"p_{m}")
                cands = []
                if D._valid(up):
                    a = float(up.best_ask); cands.append(("up", p - a - FEE(a), a, up.ask_size))
                if D._valid(dn):
                    a = float(dn.best_ask); cands.append(("down", (1 - p) - a - FEE(a), a, dn.ask_size))
                cands = [c for c in cands if c[2] <= 0.98 and c[1] >= MARGIN]
                if cands:
                    side, edge, a, sz = max(cands, key=lambda c: c[1])
                    later = bx.asof(slug, "Up" if side == "up" else "Down", r.t + 1, not_before=r.t)
                    a1 = float(later.best_ask) if later is not None and D._valid(later) else np.nan
                    win = float(r.up if side == "up" else not r.up)
                    sel.append({"slug": slug, "end": r.end, "h": r.h, "side": side, "p": p, "ask": a, "edge": edge,
                                "ask_size": sz, "win": win, "net": win - a - FEE(a),
                                "net_d1": win - a1 - FEE(a1) if a1 == a1 else np.nan, "dur": r.dur})
                    break
        t = pd.DataFrame(sel)
        trades[m] = t
        if len(t):
            rng = np.random.default_rng(1)
            gs = t.groupby("end").net.agg(["sum", "count"])
            idx = rng.integers(0, len(gs), size=(10000, len(gs)))
            means = gs["sum"].to_numpy()[idx].sum(1) / gs["count"].to_numpy()[idx].sum(1)
            out[f"trade_{m}"] = {"n": len(t), "win": round(float(t.win.mean()), 3), "mean_ask": round(float(t.ask.mean()), 3),
                                 "mean_pred_edge": round(float(t.edge.mean()), 4), "mean_net": round(float(t.net.mean()), 4),
                                 "ci95": [round(float(np.quantile(means, .025)), 4), round(float(np.quantile(means, .975)), 4)],
                                 "total": round(float(t.net.sum()), 3), "delay1_mean_net": round(float(t.net_d1.mean()), 4),
                                 "by_h": t.groupby(pd.cut(t.h, [0, 20, 40, 55]), observed=True).net.agg(["count", "mean"]).round(3).reset_index().astype(str).to_dict("records"),
                                 "by_dur": t.groupby("dur").net.agg(["count", "mean"]).round(3).reset_index().astype(str).to_dict("records")}
        t.to_csv(O / f"dev_lead_trades_{m}{'_perh' if '--per-h' in sys.argv else ''}.csv", index=False)
    # covered markets in Valid (books at all checkpoints are not required; a market counts if any row had a book)
    out["valid_markets"] = int(va.slug.nunique())
    (O / f"dev_lead_summary{'_perh' if PER_H else ''}.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
