"""P1 DRY-RUN report: executor live decisions vs recorder shadow (plan_v2/v3 rule), per stream.
Coverage and executability only -- no outcomes, labels or PnL are read."""
import glob, json, sys
from collections import defaultdict
from pathlib import Path
import numpy as np
import pandas as pd

R = Path(__file__).resolve().parents[2]
O = Path(__file__).resolve().parent
sys.path[:0] = [str(R), str(R / "reports" / "twap_z_forward_shadow_20261015")]
from recorder_mechanics import ChainlinkSeries  # noqa: E402
from evaluate_v2 import BookIndex, _valid  # noqa: E402
from bot.research.twap_z_executor import CHECKPOINTS, YR3, MID_LO, MID_HI, Z_MIN  # noqa: E402

EXEC_START, EXEC_STOP = 1792118498.0, 1792205833.0   # old executor process: 2026-10-16 10:41:38 -> 10-17 10:57:13 (Taipei)
if "--window" in sys.argv:                            # e.g. --window 1792206651 1792210260 (P1 1-hour extension)
    i = sys.argv.index("--window"); EXEC_START, EXEC_STOP = float(sys.argv[i + 1]), float(sys.argv[i + 2])
TAG = "_ext1h" if "--window" in sys.argv else ""
SRC = {"btc": ("chainlink_spot", "chainlink_twap60")}
SRC.update({a: (f"chainlink_spot:{a}", f"chainlink_twap60:{a}") for a in ("eth", "sol", "xrp")})

# ---- executor rows (dedupe overlap of two processes: first row per market/checkpoint)
ex = []
for f in sorted(glob.glob(str(R / "data/twap_z_executor/2026-10-1[67].jsonl"))):
    for line in open(f):
        r = json.loads(line)
        if r.get("stream") and EXEC_START <= r["eval_ts"] <= EXEC_STOP:
            ex.append(r)
ex = pd.DataFrame(ex).sort_values("eval_ts").drop_duplicates(["market_slug", "checkpoint"])
ex["start"] = ex.market_slug.str.rsplit("-", n=1).str[-1].astype(int)
ex["dur"] = np.where(ex.market_slug.str.contains("-5m-"), 300, 900)
ex["end"] = ex.start + ex.dur
full = ex.groupby("market_slug").checkpoint.nunique()
ex = ex[ex.market_slug.isin(full[full == len(CHECKPOINTS)].index)]   # markets fully inside the window
ex = ex[ex.start >= EXEC_START]          # the executor must have seen the market open (strike)

# ---- recorder data
lo, hi = int(ex.start.min()) - 400, int(ex.end.max()) + 5
def load(root, cols=None):
    fs = sorted(glob.glob(str(R / root / "2026-10-1[67]" / "*.parquet")))
    return pd.concat([pd.read_parquet(f, columns=cols) for f in fs])
prices = load("data/price_sources_1s")
books = load("data/polymarket_book_1s")
books = books[books.ts_sec.between(lo, hi)]
CAUSAL = "--causal" in sys.argv
class CausalBookIndex(BookIndex):
    """Latest per-second row whose state provably existed at t: last update received <= t."""
    def asof(self, slug, outcome, t, *, not_before=None):
        hit = self._g.get((slug, outcome))
        if hit is None:
            return None
        ts, rows = hit
        i = int(np.searchsorted(ts, t, side="right")) - 1
        lo = not_before if not_before is not None else t - 3.0
        while i >= 0 and ts[i] >= lo:
            r = rows.iloc[i]
            if pd.notna(r.last_update_received_ms) and r.last_update_received_ms <= t * 1000:
                return r
            i -= 1
        return None
bx = CausalBookIndex(books) if CAUSAL else BookIndex(books)
series = {}
for a, (sp, tw) in SRC.items():
    q = prices[prices.source.isin([sp, tw])].copy()
    q["source"] = q.source.map({sp: "chainlink_spot", tw: "chainlink_twap60"})
    series[a] = ChainlinkSeries(q, window_offset=-62)

# ---- shadow grid on the same markets/checkpoints
rows = []
for slug, g in ex.groupby("market_slug"):
    a, start, end = slug.split("-")[0], int(g.start.iloc[0]), int(g.end.iloc[0])
    cs = series[a]
    for h in CHECKPOINTS:
        t = end - h
        up, dn = bx.asof(slug, "Up", t), bx.asof(slug, "Down", t)
        vu, vd = _valid(up), _valid(dn)
        strike = cs.strike(start, t)
        req, rem, _m = cs.required_future_avg(end, strike, t) if strike else (None, 0, 0)
        sig, spot = cs.sigma(t), cs.spot_asof(t)
        mid = ((up.best_bid + up.best_ask) / 2 if vu else (1 - (dn.best_bid + dn.best_ask) / 2) if vd else None)
        z = None
        if strike and req is not None and rem > 0 and sig and spot and (vu or vd):
            z = (spot - req) / (spot * sig * np.sqrt(rem / YR3))
        side = None if z is None else ("up" if z > 0 else "down")
        side_ok = (vu if side == "up" else vd) if side else False
        match = z is not None and mid is not None and MID_LO <= mid <= MID_HI and abs(z) >= Z_MIN and side_ok
        rows.append({"market_slug": slug, "checkpoint": h, "sh_book_rows": (up is not None) + (dn is not None),
                     "sh_z": z, "sh_mid": mid, "sh_side": side, "sh_match": bool(match)})
sh = pd.DataFrame(rows)

def first_match(df, flag, side):
    m = df[df[flag]].sort_values("checkpoint", ascending=False).drop_duplicates("market_slug")
    return m.set_index("market_slug")[["checkpoint", side]]
ex["ex_match"] = ex.action.isin(["WOULD_SUBMIT", "RULE_MATCH_FAIL_CLOSED"])
E = first_match(ex, "ex_match", "side").rename(columns={"checkpoint": "ex_cp", "side": "ex_side"})
S = first_match(sh, "sh_match", "sh_side").rename(columns={"checkpoint": "sh_cp"})
cov = sh.groupby("market_slug").sh_book_rows.min().ge(2).rename("book_covered")   # recorder books at all 11 checkpoints
M = pd.DataFrame(index=sorted(ex.market_slug.unique())).join(cov).join(E).join(S)
M["stream"] = M.index.str.rsplit("-", n=1).str[0]
def cls(r):
    e, s = pd.notna(r.ex_cp), pd.notna(r.sh_cp)
    if not e and not s: return "neither"
    if e and s: return "same" if (r.ex_cp == r.sh_cp and r.ex_side == r.sh_side) else ("same_side_diff_time" if r.ex_side == r.sh_side else "diff_side")
    return "executor_only" if e else "shadow_only"
M["cls"] = M.apply(cls, axis=1)
M.to_csv(O / f"p1_market_comparison{'_causal' if CAUSAL else ''}{TAG}.csv")

# ---- per-stream summary
trig = ex[ex.action.eq("WOULD_SUBMIT")]
out = []
for st, g in M.groupby("stream"):
    gc = g[g.book_covered]
    either = gc[gc.cls != "neither"]
    t = trig[trig.market_slug.str.rsplit("-", n=1).str[0] == st]
    sz = t.ask_size.fillna(0)
    out.append({"stream": st, "markets": len(g), "covered": len(gc), "exec_triggers": int(g.ex_cp.notna().sum()),
                "covered_either": len(either), "same": int((either.cls == "same").sum()),
                "agree_pct": round(100 * (either.cls == "same").mean(), 1) if len(either) else None,
                "same_side_diff_time": int((either.cls == "same_side_diff_time").sum()),
                "executor_only": int((either.cls == "executor_only").sum()), "shadow_only": int((either.cls == "shadow_only").sum()),
                "diff_side": int((either.cls == "diff_side").sum()),
                "ask_med": round(float(t.ask.median()), 3) if len(t) else None,
                "ask_size_p10": round(float(sz.quantile(.1)), 1) if len(t) else None,
                "ask_size_med": round(float(sz.median()), 1) if len(t) else None,
                "ask_size_p90": round(float(sz.quantile(.9)), 1) if len(t) else None,
                "ask_size_min": round(float(sz.min()), 1) if len(t) else None,
                "n_ge_5_5": int((sz >= 5.5).sum()), "n_ge_20": int((sz >= 20).sum()), "n_ge_50": int((sz >= 50).sum())})
summ = pd.DataFrame(out)
summ.to_csv(O / f"p1_stream_summary{'_causal' if CAUSAL else ''}{TAG}.csv", index=False)
reasons = ex[ex.action.isin(["SKIP"])].reasons.explode().value_counts()
lat = ex.decision_latency_ms
res = {"window_taipei": ["2026-10-16 10:41:38", "2026-10-17 10:57:13"], "hours": round((EXEC_STOP - EXEC_START) / 3600, 2),
       "markets_full": int(len(M)), "markets_book_covered": int(M.book_covered.sum()),
       "exec_triggers": int(len(trig)), "rule_match_fail_closed": int((ex.action == "RULE_MATCH_FAIL_CLOSED").sum()),
       "latency_ms": {"p50": round(float(lat.median()), 2), "p99": round(float(lat.quantile(.99)), 2), "max": round(float(lat.max()), 2)},
       "agreement_covered": {k: int(v) for k, v in M[M.book_covered].cls.value_counts().items()},
       "skip_reason_top": {k: int(v) for k, v in reasons.head(12).items()}}
(O / f"p1_summary{'_causal' if CAUSAL else ''}{TAG}.json").write_text(json.dumps(res, indent=1))
print(json.dumps(res, indent=1)); print(summ.to_string(index=False))
