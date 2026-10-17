"""DEV, descriptive plan_v2 section 7 'determined settlement' policy on all streams (causal books).
First second in T-10..T-1 at which every settlement-window second is already received; buy the
determined side at its ask if |margin| >= 0.5 bps. Official labels verify the outcome."""
import json, sys
from pathlib import Path
import numpy as np, pandas as pd
O = Path(__file__).resolve().parent
sys.path.insert(0, str(O))
import dev_pnl as D  # noqa: E402

def main():
    import time
    now = time.time()
    books = D.load("data/polymarket_book_1s", ["ts_sec", "market_slug", "outcome", "best_bid", "best_ask", "ask_size",
                                               "last_update_received_ms", "sample_ms"])
    if "sample_ms" not in books: books["sample_ms"] = np.nan
    slugs = [s for s in books.market_slug.unique()
             if int(s.rsplit("-", 1)[-1]) >= (D.V2_START if s.startswith("btc-updown-15m-") else D.V3_START)
             and int(s.rsplit("-", 1)[-1]) < D.T_U and int(s.rsplit("-", 1)[-1]) + D.dur(s) + 600 < now]
    bx = D.BookIndex(books[books.ts_sec >= min(int(s.rsplit('-', 1)[-1]) for s in slugs) - 10])
    prices = D.load("data/price_sources_1s")
    series = {}
    for a, (sp, tw) in D.SRC.items():
        q = prices[prices.source.isin([sp, tw])].copy(); q["source"] = q.source.map({sp: "chainlink_spot", tw: "chainlink_twap60"})
        series[a] = D.ChainlinkSeries(q, window_offset=-62)
    rows = []
    for slug in slugs:
        a, start = slug.split("-")[0], int(slug.rsplit("-", 1)[-1]); end = start + D.dur(slug); cs = series[a]
        strike = cs.strike(start, end - 10)
        if strike is None: continue
        for h in range(10, 0, -1):
            t = end - h
            px = cs.determined_settlement(end, t)
            if px is None: continue
            margin = (px - strike) / strike * 1e4
            side = "Up" if px >= strike else "Down"
            b = bx.asof(slug, side, t)
            ask = float(b.best_ask) if b is not None and pd.notna(b.best_ask) and t - float(b.ts_sec) <= 1.0 else np.nan
            size = float(b.ask_size) if b is not None and pd.notna(b.ask_size) else np.nan
            later = bx.asof(slug, side, t + 1, not_before=t)
            rows.append({"market_slug": slug, "stream": slug.rsplit("-", 1)[0], "end": end, "lock_h": h, "margin_bps": margin,
                         "side": side, "ask": ask, "ask_size": size,
                         "ask_plus1": float(later.best_ask) if later is not None and pd.notna(later.best_ask) else np.nan})
            break
    L = pd.DataFrame(rows)
    L["eligible"] = L.margin_bps.abs().ge(.5) & L.ask.notna()
    cand = L[L.eligible & (L.ask < .995)]
    cache = {x["slug"]: x for x in (json.loads(D.LABELS.read_text()) if D.LABELS.exists() else [])}
    import httpx
    from fetch_official_resolutions import fetch_one
    todo = [s for s in cand.market_slug if cache.get(s, {}).get("official_outcome") not in ("UP", "DOWN")]
    with httpx.Client(timeout=20) as c:
        for s in todo: cache[s] = fetch_one(c, "https://gamma-api.polymarket.com", s)
    D.LABELS.write_text(json.dumps(sorted(cache.values(), key=lambda r: r["slug"]), indent=1))
    L["official"] = L.market_slug.map({s: r.get("official_outcome") for s, r in cache.items()})
    L["win"] = np.where(L.official.isin(["UP", "DOWN"]), (L.side.str.upper() == L.official).astype(float), np.nan)
    L["net"] = L.win - L.ask - D.FEE(L.ask)
    L["net_plus1"] = L.win - L.ask_plus1 - D.FEE(L.ask_plus1)
    L.to_csv(O / "dev_lock.csv", index=False)
    e = L[L.eligible]
    print("markets with lock row:", len(L), " eligible(|margin|>=0.5bps & ask seen):", len(e))
    print("lock second (T-h) distribution:", e.lock_h.value_counts().sort_index().to_dict())
    print("ask at lock:", e.ask.describe(percentiles=[.1, .5, .9]).round(4).to_dict())
    print("share ask<=0.99:", round(float((e.ask <= .99).mean()), 3), " ask<=0.98:", round(float((e.ask <= .98).mean()), 3))
    c = L[L.eligible & (L.ask < .995) & L.win.notna()]
    print(f"buyable (ask<0.995) with official label: n={len(c)} wins={int(c.win.sum())} mean_ask={c.ask.mean():.4f} "
          f"mean_net={c.net.mean():+.4f} total={c.net.sum():+.3f} mean_net_plus1s={c.net_plus1.mean():+.4f}  "
          f"ask_size_med={c.ask_size.median():.1f}")
    print(c.groupby("stream").agg(n=("net", "size"), wins=("win", "sum"), ask=("ask", "mean"), net=("net", "mean"),
                                  net_p1=("net_plus1", "mean"), size=("ask_size", "median")).round(4).to_string())
    print("losses:", c[c.win == 0][["market_slug", "lock_h", "margin_bps", "side", "ask", "official"]].to_string(index=False))

main()
