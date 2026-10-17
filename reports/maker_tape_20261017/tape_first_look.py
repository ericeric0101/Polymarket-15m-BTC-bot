"""First look at the Polymarket trade tape for the maker (liquidity-provision) direction. Exploratory.

For every trade print in the final 120 s of a settled 5m/15m Up/Down market:
  taker bought `token` at price p (side=BUY) -> the maker SOLD it at p;
  side=SELL -> the maker BOUGHT it at p.
Maker settlement PnL per share = (p - win) if the maker sold, (win - p) if the maker bought,
where win = 1 if the token's outcome won (reconstructed Chainlink settlement). Makers pay no fee
here; the taker's fee is shown separately. A positive maker PnL means liquidity provision
in that slice earned money on average (the takers lost).

    .venv/bin/python -I reports/maker_tape_20261017/tape_first_look.py
"""
import glob, sys, time
from pathlib import Path
import numpy as np, pandas as pd

R = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(R), str(R / "reports" / "twap_z_dev_pnl_20261017")]
import dev_pnl as D  # noqa: E402

fs = sorted(glob.glob(str(R / "data/polymarket_trades/*/*.parquet")))
if not fs:
    sys.exit("no trade files yet")
t = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True).drop_duplicates("tx_hash")
t["start"] = t.market_slug.str.rsplit("-", n=1).str[-1].astype(int)
t["dur"] = np.where(t.market_slug.str.contains("-5m-"), 300, 900)
t["end"] = t.start + t.dur
t["tte"] = t.end - t.trade_ms / 1000.0
now = time.time()
t = t[t.end + 30 < now].copy()                                   # settled markets only

# Official outcomes (Gamma). The price store writes completed hours only, so the current
# hour's Chainlink data is not on disk yet; official labels avoid that gap.
import json, httpx
sys.path.insert(0, str(R / "scripts"))
from fetch_official_resolutions import fetch_one  # noqa: E402
cache_path = Path(__file__).resolve().parent / "official_labels.json"
cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
with httpx.Client(timeout=20) as c:
    for slug in t.market_slug.unique():
        if cache.get(slug) not in ("UP", "DOWN"):
            cache[slug] = fetch_one(c, "https://gamma-api.polymarket.com", slug).get("official_outcome")
cache_path.write_text(json.dumps(cache, indent=1))
outcome = {s: ("Up" if v == "UP" else "Down") for s, v in cache.items() if v in ("UP", "DOWN")}
t["winner"] = t.market_slug.map(outcome)
t = t[t.winner.notna()].copy()
t["win"] = (t.outcome == t.winner).astype(float)
t["maker_sold"] = t.side.str.upper() == "BUY"
t["maker_pnl"] = np.where(t.maker_sold, t.price - t.win, t.win - t.price)
t["notional"] = t.price * t["size"]
t["taker_fee"] = .07 * t.price * (1 - t.price)

print(f"settled markets with prints: {t.market_slug.nunique()} | prints: {len(t)} | "
      f"window {pd.to_datetime(t.trade_ms.min(), unit='ms')} .. {pd.to_datetime(t.trade_ms.max(), unit='ms')} UTC")
w = lambda g: pd.Series({"prints": len(g), "shares": g["size"].sum(), "usdc": g.notional.sum(),
                         "maker_pnl_per_share_vw": (g.maker_pnl * g["size"]).sum() / g["size"].sum(),
                         "maker_pnl_per_print": g.maker_pnl.mean(), "taker_buy_share": g.maker_sold.mean(),
                         "mean_price": g.price.mean()})
print("\nby time to end (all streams):")
print(t.groupby(pd.cut(t.tte, [0, 10, 30, 60, 120, 900]), observed=True).apply(w).round(4).to_string())
late = t[t.tte <= 120]
print("\nlast 120 s, by stream:")
print(late.groupby(late.market_slug.str.rsplit("-", n=1).str[0]).apply(w).round(4).to_string())
print("\nlast 120 s, by trade price (maker's counterparty price):")
print(late.groupby(pd.cut(late.price, [0, .1, .3, .5, .7, .9, .99, 1.0]), observed=True).apply(w).round(4).to_string())
print("\nlast 120 s, by maker side:")
print(late.groupby(np.where(late.maker_sold, "maker SOLD (taker bought)", "maker BOUGHT (taker sold)")).apply(w).round(4).to_string())
