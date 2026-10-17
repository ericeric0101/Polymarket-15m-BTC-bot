"""MB1 rolling-validation mechanics check (no PnL is read or printed).

    .venv/bin/python -I reports/maker_bid_20261017/check_rolling.py data/maker_bid_dryrun/rolling_r1
"""
import glob, json, sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

R = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(R))
from bot.research.maker_bid import LATENCY_SEC, SIZE, WINDOW_END, WINDOW_START  # noqa: E402

d = Path(sys.argv[1])
ev = [json.loads(l) for f in sorted(d.glob("events_*.jsonl")) for l in open(f)]
ev = [e for e in ev if e["event"] != "SETTLED"]            # settlement rows carry PnL: not read here
df = pd.DataFrame(ev)
if df.empty:
    sys.exit("no events")
df["end"] = df.market_slug.str.rsplit("-", n=1).str[-1].astype(int) + df.market_slug.str.contains("-5m-").map({True: 300, False: 900})
issues = []
print("events:", dict(Counter(df.event)))
print("markets:", df.market_slug.nunique(), "| streams:", sorted(df.market_slug.str.rsplit("-", n=1).str[0].unique()))

# 1. placement window and latency
pl = df[df.event == "PLACE_SENT"]
tte = pl.end - pl.t
bad = pl[(tte > WINDOW_START + 0.2) | (tte < WINDOW_END - 0.2)]
if len(bad): issues.append(f"{len(bad)} placements outside T-{WINDOW_START}..T-{WINDOW_END}")
lv = df[df.event == "LIVE"]
lat = (lv.live_ts - lv.sent_ts).round(3)
if not (lat == round(LATENCY_SEC, 3)).all(): issues.append(f"live latency not {LATENCY_SEC}: {lat.unique()[:5]}")
meas = (lv.queue_measured_ts - lv.live_ts)
print(f"queue measured after live: p50 {meas.median():.3f}s max {meas.max():.3f}s | queue_ahead p50 {lv.queue_ahead.median():.1f} p90 {lv.queue_ahead.quantile(.9):.1f}")
if meas.max() > 0.25: issues.append(f"queue measured up to {meas.max():.2f}s after live (tick lag)")

# 2. nothing live after the window end (cancel effective by T-10 + latency + tick)
ce = df[df.event == "CANCEL_EFFECTIVE"]
last_eff = ce.groupby("order_id").t.max()
orders = pl.set_index("order_id")
for oid, o in orders.iterrows():
    if oid in last_eff.index:
        if last_eff[oid] > o.end - WINDOW_END + LATENCY_SEC + 0.3:
            issues.append(f"order {oid} cancel effective {last_eff[oid] - o.end:+.2f}s vs end")
    elif not ((df.event == "FILL") & (df.order_id == oid)).any() and o.end < df.t.max():
        issues.append(f"order {oid} ({o.market_slug}) never cancelled nor filled")

# 3. fills must match real SELL prints in the recorder tape (independent capture)
fills = df[df.event == "FILL"]
tape = pd.concat([pd.read_parquet(f) for f in glob.glob(str(R / "data/polymarket_trades/*/*.parquet"))], ignore_index=True)
tape = tape.drop_duplicates("tx_hash")
matched = 0
for f in fills.itertuples():
    m = tape[(tape.tx_hash == f.print_tx) & (tape.token_id == f.token_id)]
    if len(m) and (m.side.str.upper() == "SELL").all() and (m.price <= f.price + 1e-9).all():
        matched += 1
print(f"fills: {len(fills)} | matched to recorder tape (tx, token, SELL, price<=bid): {matched}"
      f" (tape may lag by one 10-min part file)")

# 4. per-token one fill, size never exceeded
per_order = fills.groupby("order_id").fill_shares.sum()
if (per_order > SIZE + 1e-9).any(): issues.append("an order filled above its size")
ftok = fills.groupby(["market_slug", "token_id"]).order_id.nunique()
for (slug, tok), n in ftok[ftok > 1].items():
    ff = fills[(fills.market_slug == slug) & (fills.token_id == tok)].sort_values("t")
    first_t = ff.t.iloc[0]
    late = ff[(ff.order_id != ff.order_id.iloc[0]) & (ff.t > first_t + LATENCY_SEC + 0.15)]
    if len(late):
        issues.append(f"{slug} {tok[:8]}: second order filled after the sibling cancel was effective")
    else:
        print(f"note: {slug} double fill inside cancel latency (realistic, counted)")

# 5. queue consistency: SELL volume at our price after measurement must exceed queue before a non-sweep fill
for f in fills.itertuples():
    o = lv[lv.order_id == f.order_id]
    if o.empty or f.print_price < f.price - 1e-9:
        continue
    o = o.iloc[0]
    vol = tape[(tape.token_id == f.token_id) & (tape.side.str.upper() == "SELL") & (abs(tape.price - f.price) < 1e-9)
               & (tape.received_ms / 1000 > o.queue_measured_ts - 0.5) & (tape.received_ms / 1000 <= f.t + 0.5)]["size"].sum()
    # (recorder and dry-run use separate sockets: their receipt clocks differ by a few ms)
    if len(tape) and vol + 1e-6 < o.queue_ahead:
        issues.append(f"fill {f.order_id}: tape SELL volume {vol:.1f} < queue_ahead {o.queue_ahead:.1f}")

# 6. descriptive
cs = df[df.event == "CANCEL_SENT"]
print("cancel reasons:", dict(Counter(cs.reason.str.replace(r"_[+-].*", "", regex=True))))
churn = pl.groupby(["market_slug", "token_id"]).size()
print(f"placements per token-market: p50 {churn.median():.0f} p90 {churn.quantile(.9):.0f} max {churn.max()}")
print(f"orders placed {len(pl)}, went live {len(lv)}, filled orders {fills.order_id.nunique()}, "
      f"picked-off fills {int(fills.picked_off.sum())}")
print("\nISSUES:" if issues else "\nno mechanics issues found")
for i in issues[:30]:
    print(" -", i)
