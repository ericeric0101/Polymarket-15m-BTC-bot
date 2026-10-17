"""DEV (unblinded, amendment A1.3) PnL of the frozen TWAP-z rule on all recorder data before T_U.

Rule = plan_v2/v3 section 4 (ZC, T-55..T-5, 0.10<=mid<=0.90, |z|>=2, first match, buy at ask,
hold to official settlement, fee 0.07 p (1-p)). Books use the causal as-of of amendment A1.1.
Exploratory only: these markets can no longer serve as confirmatory evidence.

    .venv/bin/python -I reports/twap_z_dev_pnl_20261017/dev_pnl.py [--no-fetch]
"""
import glob, json, sys, time
from pathlib import Path

import numpy as np
import pandas as pd

R = Path(__file__).resolve().parents[2]
O = Path(__file__).resolve().parent
sys.path[:0] = [str(R), str(R / "reports" / "twap_z_forward_shadow_20261015"), str(R / "scripts")]
from recorder_mechanics import ChainlinkSeries  # noqa: E402
from evaluate_v2 import BookIndex, _valid  # noqa: E402  (causal as-of, amendment A1.1)
from bot.research.twap_z_executor import CHECKPOINTS, YR3  # noqa: E402

V2_START, V3_START = 1792026000, 1792119600          # 2026-10-15T01:00Z, 2026-10-16T03:00Z
T_U = 1792207800                                      # 2026-10-17T03:30Z
FEE = lambda a: .07 * a * (1 - a)
SRC = {"btc": ("chainlink_spot", "chainlink_twap60")}
SRC.update({a: (f"chainlink_spot:{a}", f"chainlink_twap60:{a}") for a in ("eth", "sol", "xrp")})
LABELS = O / "official_labels.json"


def load(root, cols=None):
    """Concat hourly files; columns missing from older files (e.g. sample_ms) come back as NaN."""
    import pyarrow.parquet as pq
    fs = sorted(glob.glob(str(R / root / "2026-10-1[5-7]" / "*.parquet")))
    parts = []
    for f in fs:
        have = set(pq.read_schema(f).names)
        parts.append(pd.read_parquet(f, columns=[c for c in cols if c in have] if cols else None))
    return pd.concat(parts, ignore_index=True)


def dur(slug):
    return 300 if "-5m-" in slug else 900


def main():
    now = time.time()
    books = load("data/polymarket_book_1s", ["ts_sec", "market_slug", "outcome", "best_bid", "best_ask",
                                             "ask_size", "last_update_received_ms", "sample_ms"])
    if "sample_ms" not in books:
        books["sample_ms"] = np.nan
    slugs = []
    for s in books.market_slug.unique():
        st, d = int(s.rsplit("-", 1)[-1]), dur(s)
        pop = V2_START if s.startswith("btc-updown-15m-") else V3_START
        if st >= pop and st < T_U and st + d + 600 < now:
            slugs.append(s)
    lo = min(int(s.rsplit("-", 1)[-1]) for s in slugs) - 400
    books = books[books.ts_sec >= lo]
    bx = BookIndex(books)
    prices = load("data/price_sources_1s")
    series = {}
    for a, (sp, tw) in SRC.items():
        q = prices[prices.source.isin([sp, tw])].copy()
        q["source"] = q.source.map({sp: "chainlink_spot", tw: "chainlink_twap60"})
        series[a] = ChainlinkSeries(q, window_offset=-62)

    rows = []
    for slug in sorted(slugs):
        a, start = slug.split("-")[0], int(slug.rsplit("-", 1)[-1])
        end = start + dur(slug)
        cs = series[a]
        for h in CHECKPOINTS:
            t = end - h
            up, dn = bx.asof(slug, "Up", t), bx.asof(slug, "Down", t)
            vu, vd = _valid(up), _valid(dn)
            strike = cs.strike(start, t)
            req, rem, _ = cs.required_future_avg(end, strike, t) if strike else (None, 0, 0)
            sig, spot = cs.sigma(t), cs.spot_asof(t)
            mid = (up.best_bid + up.best_ask) / 2 if vu else (1 - (dn.best_bid + dn.best_ask) / 2) if vd else np.nan
            z = np.nan
            if strike and req is not None and rem > 0 and sig and spot and (vu or vd):
                z = (spot - req) / (spot * sig * np.sqrt(rem / YR3))
            rows.append({"market_slug": slug, "stream": slug.rsplit("-", 1)[0], "start": start, "end": end,
                         "checkpoint": h, "t": t, "z": z, "mid": mid, "vu": vu, "vd": vd,
                         "ask_up": up.best_ask if vu else np.nan, "ask_dn": dn.best_ask if vd else np.nan,
                         "size_up": up.ask_size if vu else np.nan, "size_dn": dn.ask_size if vd else np.nan,
                         "book_seen": (up is not None) or (dn is not None)})
    g = pd.DataFrame(rows)
    cov = g.groupby("market_slug").book_seen.all().rename("book_covered")
    sel = g[g.z.notna() & g.mid.between(.10, .90) & g.z.abs().ge(2)].copy()
    sel["side"] = np.where(sel.z > 0, "up", "down")
    sel = sel[np.where(sel.side == "up", sel.vu, sel.vd)]
    sel = sel.sort_values("t").drop_duplicates("market_slug").copy()
    sel["ask"] = np.where(sel.side == "up", sel.ask_up, sel.ask_dn)
    sel["ask_size"] = np.where(sel.side == "up", sel.size_up, sel.size_dn)
    for lag in (1, 3):
        vals = []
        for r in sel.itertuples():
            row = bx.asof(r.market_slug, "Up" if r.side == "up" else "Down", r.t + lag, not_before=r.t)
            vals.append(float(row.best_ask) if row is not None and _valid(row) and r.t + lag < r.end else np.nan)
        sel[f"ask_d{lag}"] = vals

    cache = {x["slug"]: x for x in (json.loads(LABELS.read_text()) if LABELS.exists() else [])}
    if "--no-fetch" not in sys.argv:
        import httpx
        from fetch_official_resolutions import fetch_one
        todo = [s for s in sel.market_slug if cache.get(s, {}).get("official_outcome") not in ("UP", "DOWN")]
        with httpx.Client(timeout=20) as client:
            for s in todo:
                cache[s] = fetch_one(client, "https://gamma-api.polymarket.com", s)
        LABELS.write_text(json.dumps(sorted(cache.values(), key=lambda r: r["slug"]), indent=1))
    lab = {s: r.get("official_outcome") for s, r in cache.items()}
    sel["official"] = sel.market_slug.map(lab)
    sel = sel[sel.official.isin(["UP", "DOWN"])].copy()
    sel["win"] = np.where(sel.side == "up", sel.official == "UP", sel.official == "DOWN").astype(int)
    sel["net"] = sel.win - sel.ask - FEE(sel.ask)
    for lag in (1, 3):
        sel[f"net_d{lag}"] = sel.win - sel[f"ask_d{lag}"] - FEE(sel[f"ask_d{lag}"])
    sel["day_utc"] = pd.to_datetime(sel.start, unit="s", utc=True).dt.date.astype(str)
    sel["exec_5_5"] = sel.ask_size >= 5.5
    sel.to_csv(O / "dev_trades.csv", index=False)

    rng = np.random.default_rng(20261017)
    def cluster_ci(df, n_boot=10000):
        groups = df.groupby("end").net.agg(["sum", "count"])
        s, c = groups["sum"].to_numpy(), groups["count"].to_numpy()
        idx = rng.integers(0, len(s), size=(n_boot, len(s)))
        means = s[idx].sum(1) / c[idx].sum(1)
        return float(np.quantile(means, .025)), float(np.quantile(means, .975)), float(np.quantile(means, .05))
    def summarize(df):
        if df.empty:
            return {"n": 0}
        lo95, hi95, lb90 = cluster_ci(df)
        by_end = df.groupby("end").net.sum()
        tot = float(df.net.sum())
        return {"n": int(len(df)), "clusters": int(df.end.nunique()), "wins": int(df.win.sum()),
                "win_rate": round(float(df.win.mean()), 3), "mean_ask": round(float(df.ask.mean()), 3),
                "mean_net": round(float(df.net.mean()), 4), "total_net": round(tot, 3),
                "ci95": [round(lo95, 4), round(hi95, 4)], "lb95_one_sided": round(lb90, 4),
                "drop_best_cluster_total": round(tot - float(by_end.max()), 3),
                "max_day_share": round(float(df.groupby("day_utc").net.sum().max() / tot), 3) if tot > 0 else None,
                "up_share": round(float((df.side == "up").mean()), 3),
                "delay1_mean_net": round(float(df.net_d1.mean()), 4) if df.net_d1.notna().any() else None,
                "delay1_n": int(df.net_d1.notna().sum()),
                "delay3_mean_net": round(float(df.net_d3.mean()), 4) if df.net_d3.notna().any() else None,
                "exec_5_5_share": round(float(df.exec_5_5.mean()), 3)}
    out = {"population": {"v2_btc15m_from": "2026-10-15T01:00Z", "v3_from": "2026-10-16T03:00Z",
                          "until_start_before": "2026-10-17T03:30Z (T_U)"},
           "markets": int(len(slugs)), "markets_book_covered": int(cov.sum()),
           "pooled": summarize(sel),
           "pooled_exec_5_5_only": summarize(sel[sel.exec_5_5]),
           "by_length": {k: summarize(v) for k, v in sel.groupby(np.where(sel.stream.str.contains("-5m"), "5m", "15m"))},
           "by_stream": {k: summarize(v) for k, v in sel.groupby("stream")},
           "stream_share_of_total": {k: round(float(v / sel.net.sum()), 3) for k, v in sel.groupby("stream").net.sum().items()}
           if sel.net.sum() > 0 else None,
           "by_ask_bucket": {str(k): summarize(v) for k, v in sel.groupby(pd.cut(sel.ask, [0, .7, .8, .9, .95, 1.0]), observed=True)},
           "by_absz_bucket": {str(k): summarize(v) for k, v in sel.groupby(pd.cut(sel.z.abs(), [2, 3, 5, 1e9]), observed=True)},
           "markets_by_stream": {k: int(v) for k, v in pd.Series([s.rsplit('-', 1)[0] for s in slugs]).value_counts().items()},
           "covered_by_stream": {k: int(v) for k, v in cov[cov].index.to_series().str.rsplit("-", n=1).str[0].value_counts().items()}}
    (O / "dev_summary.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
