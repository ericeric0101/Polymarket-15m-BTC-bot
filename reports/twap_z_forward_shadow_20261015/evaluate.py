"""Pre-registered forward shadow evaluation of the TWAP-difficulty z rule (see plan.md).

Read-only over the forward checkout's P_paths exports and the standalone price
recorder. Enforces the stopping rule: until the primary (ZC) policy has N >= 50
markets, or 21 days have elapsed, only coverage is reported -- no PnL.

    .venv/bin/python reports/twap_z_forward_shadow_20261015/evaluate.py --fetch-labels
    .venv/bin/python reports/twap_z_forward_shadow_20261015/evaluate.py            # coverage / final
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

O = Path(__file__).resolve().parent
R = O.parents[1]
FORWARD_EXPORTS = Path("/Users/cheng-kaihuang/Polymarket-BTC-entry-v3-forward/data/research_export")
POPULATION_START = datetime(2026, 10, 15, 0, 15, tzinfo=timezone.utc).timestamp()
PRIMARY_N = 50
MAX_DAYS = 21
YR3 = 3 * 365 * 86400
CHECKPOINTS = tuple(range(55, 0, -5))
ASOF_SEC = 3.0
BASIS_WINDOW_SEC = 3600.0
BASIS_MIN_PAIRS = 30
BOOT_REPS, BOOT_SEED = 10_000, 20261015
LABELS = O / "labels" / "forward_labels.json"


# --- loading ------------------------------------------------------------------------------

def complete_export_days(exports_root: Path) -> list[str]:
    days = []
    for f in sorted((exports_root / "manifests").glob("P_*.json")):
        m = json.loads(f.read_text())
        day = str(m.get("date_utc"))
        if m.get("complete_day") and datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() + 86400 > POPULATION_START:
            days.append(day)
    return days


def load_paths(exports_root: Path, days: list[str]) -> pd.DataFrame:
    frames = []
    for day in days:
        for name in ("paths_native_v2.parquet", "paths_historical_pre_v2.parquet"):
            f = exports_root / "P_paths" / day / name
            if f.exists():
                x = pd.read_parquet(f)
                if len(x):
                    x["priority"] = int("native" in name)
                    frames.append(x)
    if not frames:
        return pd.DataFrame()
    x = pd.concat(frames, ignore_index=True)
    for c in ["snapshot_ts", "time_left_sec", "strike", "sigma_ex_market", "required_future_avg_to_flip",
              "remaining_final_window_sec", "best_bid_up", "best_ask_up", "best_bid_down", "best_ask_down"]:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    x = x.sort_values(["snapshot_ts", "priority"]).drop_duplicates(["market_slug", "snapshot_ts"], keep="last")
    x["start"] = x.market_slug.str.rsplit("-", n=1).str[-1].astype(float)
    return x[(x.snapshot_ts >= x.start) & (x.snapshot_ts < x.start + 900) & (x.start >= POPULATION_START)].copy()


def load_recorder(root: Path) -> pd.DataFrame:
    files = sorted(glob.glob(str(root / "*" / "*.parquet")))
    if not files:
        return pd.DataFrame({"source": pd.Series(dtype=str), "ts_sec": pd.Series(dtype="int64"),
                             "close": pd.Series(dtype=float), "last_received_ms": pd.Series(dtype="int64"),
                             "t": pd.Series(dtype=float)})
    rec = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    rec["t"] = rec.last_received_ms / 1000.0
    return rec.sort_values("t")


# --- grid and z ---------------------------------------------------------------------------

def build_grid(x: pd.DataFrame) -> pd.DataFrame:
    markets = x[["market_slug", "start"]].drop_duplicates()
    grid = []
    for h in CHECKPOINTS:
        g = markets.copy()
        g["checkpoint"] = h
        g["decision_ts"] = g.start + 900 - h
        grid.append(pd.merge_asof(g.sort_values("decision_ts"), x.drop(columns="start").sort_values("snapshot_ts"),
                                  left_on="decision_ts", right_on="snapshot_ts", by="market_slug",
                                  direction="backward", tolerance=ASOF_SEC))
    d = pd.concat(grid, ignore_index=True)
    for side in ("up", "down"):
        d[f"valid_{side}"] = (d.market_mid_fresh.eq(True) & d[f"best_bid_{side}"].ge(0)
                              & d[f"best_ask_{side}"].between(.001, 1) & d[f"best_bid_{side}"].le(d[f"best_ask_{side}"]))
    d["eligible"] = (d.required_move_mode.eq("EXACT_FINAL_WINDOW_BOUNDARY") & d.twap_fresh.eq(True)
                     & d.sigma_ex_market_fresh.eq(True) & d.sigma_ex_market.gt(0)
                     & d.required_future_avg_to_flip.gt(0) & d.remaining_final_window_sec.gt(0)
                     & (d.valid_up | d.valid_down))
    d["mid"] = np.where(d.valid_up, (d.best_bid_up + d.best_ask_up) / 2, 1 - (d.best_bid_down + d.best_ask_down) / 2)
    return d


def asof_source(d: pd.DataFrame, rec: pd.DataFrame, source: str, col: str) -> pd.DataFrame:
    r = rec[rec.source.eq(source)][["t", "close"]].rename(columns={"t": f"{col}_t", "close": col})
    d = pd.merge_asof(d.sort_values("decision_ts"), r.sort_values(f"{col}_t"), left_on="decision_ts",
                      right_on=f"{col}_t", direction="backward", tolerance=ASOF_SEC)
    assert (d[f"{col}_t"].dropna() <= d.loc[d[f"{col}_t"].notna(), "decision_ts"]).all()
    return d


def rolling_basis(rec: pd.DataFrame, times: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Median Binance-minus-Chainlink bps over [t-3600, t), pairs completed before t."""
    p = rec[rec.source.isin(["binance_btcusdt", "chainlink_spot"])]
    if p.empty:
        return np.full(len(times), np.nan), np.zeros(len(times), int)
    w = p.pivot_table(index="ts_sec", columns="source", values=["close", "t"], aggfunc="last")
    if not {"binance_btcusdt", "chainlink_spot"} <= set(w["close"].columns):
        return np.full(len(times), np.nan), np.zeros(len(times), int)
    w = w.dropna()
    if w.empty:
        return np.full(len(times), np.nan), np.zeros(len(times), int)
    pair_t = w["t"].max(axis=1).to_numpy()
    bps = ((w["close"]["binance_btcusdt"] - w["close"]["chainlink_spot"]) / w["close"]["chainlink_spot"] * 1e4).to_numpy()
    order = np.argsort(pair_t)
    pair_t, bps = pair_t[order], bps[order]
    out, cnt = np.full(len(times), np.nan), np.zeros(len(times), int)
    for i, t in enumerate(times):
        lo, hi = np.searchsorted(pair_t, t - BASIS_WINDOW_SEC, "left"), np.searchsorted(pair_t, t, "left")
        cnt[i] = hi - lo
        if cnt[i] >= BASIS_MIN_PAIRS:
            out[i] = np.median(bps[lo:hi])
    return out, cnt


def add_z(d: pd.DataFrame, rec: pd.DataFrame) -> pd.DataFrame:
    for source, col in (("chainlink_spot", "cl_spot"), ("binance_btcusdt", "bn_spot"), ("coinbase_btcusd", "cb_spot")):
        d = asof_source(d, rec, source, col)
    d["basis_bps"], d["basis_pairs"] = rolling_basis(rec, d.decision_ts.to_numpy())
    d["bn_adj"] = d.bn_spot * (1 - d.basis_bps / 1e4)

    def z(s):
        return (s - d.required_future_avg_to_flip) / (s * d.sigma_ex_market * np.sqrt(d.remaining_final_window_sec / YR3))
    d["zc"], d["zb"], d["zk"] = z(d.cl_spot), z(d.bn_adj), z(d.cb_spot)
    return d


# --- policy --------------------------------------------------------------------------------

def select(d: pd.DataFrame, zcol: str, lo: float) -> pd.DataFrame:
    g = d[d.eligible & d[zcol].notna() & d.mid.between(lo, 1 - lo) & d[zcol].abs().ge(2)].copy()
    g["side"] = np.where(g[zcol] > 0, "up", "down")
    g = g[np.where(g.side.eq("up"), g.valid_up, g.valid_down)]
    g = g.sort_values("decision_ts").drop_duplicates("market_slug").copy()
    g["ask"] = np.where(g.side.eq("up"), g.best_ask_up, g.best_ask_down)
    return g


def settle(g: pd.DataFrame, labels: dict[str, str]) -> pd.DataFrame:
    g = g.copy()
    g["official_outcome"] = g.market_slug.map(labels)
    g = g[g.official_outcome.isin(["UP", "DOWN"])].copy()
    g["win"] = np.where(g.side.eq("up"), g.official_outcome.eq("UP"), g.official_outcome.eq("DOWN")).astype(int)
    g["fee"] = .07 * g.ask * (1 - g.ask)
    g["net"] = g.win - g.ask - g.fee
    g["day_utc"] = pd.to_datetime(g.start, unit="s", utc=True).dt.date.astype(str)
    return g


def delayed_net(g: pd.DataFrame, x: pd.DataFrame, lag: int) -> pd.Series:
    out = pd.Series(np.nan, index=g.index)
    for side in ("up", "down"):
        q = x[x.market_mid_fresh.eq(True) & x[f"best_bid_{side}"].ge(0) & x[f"best_ask_{side}"].between(.001, 1)
              & x[f"best_bid_{side}"].le(x[f"best_ask_{side}"])][["market_slug", "snapshot_ts", f"best_ask_{side}"]]
        q = q.rename(columns={"snapshot_ts": "qts", f"best_ask_{side}": "later_ask"}).sort_values("qts")
        s = g[g.side.eq(side)].copy()
        if s.empty:
            continue
        s["lookup"] = s.decision_ts + lag
        s = pd.merge_asof(s.reset_index().sort_values("lookup"), q, left_on="lookup", right_on="qts",
                          by="market_slug", direction="backward", tolerance=2)
        ok = s.qts.ge(s.decision_ts) & s.lookup.lt(s.start + 900)
        a = s.later_ask.where(ok)
        out.loc[s["index"]] = (s.win - a - .07 * a * (1 - a)).to_numpy()
    return out


def bootstrap_lb(net: np.ndarray) -> float:
    rng = np.random.default_rng(BOOT_SEED)
    means = net[rng.integers(0, len(net), size=(BOOT_REPS, len(net)))].mean(axis=1)
    return float(np.quantile(means, .05))


def verdict(g: pd.DataFrame) -> dict:
    n = len(g)
    if n == 0:
        return {"verdict": "NO_DATA", "n": 0}
    m, total = float(g.net.mean()), float(g.net.sum())
    lb = bootstrap_lb(g.net.to_numpy())
    drop_best = float(g.net.sort_values().iloc[:-1].sum()) if n > 1 else float("nan")
    by_day = g.groupby("day_utc").net.sum()
    max_day_share = float(by_day.max() / total) if total > 0 else None
    up_share = float(g.side.eq("up").mean())
    d1 = g["net_delay1"].dropna()
    checks = {
        "mean_and_lb_positive": m > 0 and lb > 0,
        "drop_best_positive": drop_best > 0,
        "no_day_over_50pct": max_day_share is not None and max_day_share <= .5,
        "both_sides_ge_25pct": .25 <= up_share <= .75,
        "delay1_mean_positive": len(d1) > 0 and float(d1.mean()) > 0,
    }
    result = "FAIL" if m <= 0 else "PASS" if all(checks.values()) else "INCONCLUSIVE"
    return {"verdict": result, "n": n, "wins": int(g.win.sum()), "mean_net": m, "total_net": total,
            "bootstrap_lb95": lb, "drop_best_total": drop_best, "max_day_share": max_day_share,
            "up_share": up_share, "delay1_available": int(len(d1)),
            "delay1_mean_net": float(d1.mean()) if len(d1) else None, "checks": checks,
            "mean_ask": float(g.ask.mean())}


# --- labels --------------------------------------------------------------------------------

def load_labels() -> dict[str, str]:
    if not LABELS.exists():
        return {}
    return {r["slug"]: r.get("official_outcome") for r in json.loads(LABELS.read_text())}


def fetch_labels(slugs: list[str]) -> None:
    """Fetch official outcomes for ended markets not yet resolved in the cache (public Gamma API)."""
    import httpx
    sys.path.insert(0, str(R / "scripts"))
    from fetch_official_resolutions import fetch_one  # noqa: E402

    cache = {r["slug"]: r for r in (json.loads(LABELS.read_text()) if LABELS.exists() else [])}
    now = datetime.now(timezone.utc).timestamp()
    todo = [s for s in slugs if cache.get(s, {}).get("official_outcome") not in ("UP", "DOWN")
            and float(s.rsplit("-", 1)[-1]) + 900 + 600 < now]
    with httpx.Client(timeout=20) as client:
        for slug in todo:
            cache[slug] = fetch_one(client, "https://gamma-api.polymarket.com", slug)
    LABELS.parent.mkdir(parents=True, exist_ok=True)
    tmp = LABELS.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(cache.values(), key=lambda r: r["slug"]), indent=1))
    tmp.replace(LABELS)
    print(f"labels: fetched {len(todo)}, cached {len(cache)}")


# --- main ----------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exports-root", type=Path, default=FORWARD_EXPORTS)
    ap.add_argument("--recorder-root", type=Path, default=R / "data" / "price_sources_1s")
    ap.add_argument("--fetch-labels", action="store_true")
    args = ap.parse_args(argv)

    days = complete_export_days(args.exports_root)
    x = load_paths(args.exports_root, days)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = O / "runs" / stamp
    out.mkdir(parents=True, exist_ok=True)
    if x.empty:
        (out / "status.json").write_text(json.dumps({"status": "NO_COMPLETE_EXPORT_DAYS_IN_POPULATION",
                                                       "export_days": days}, indent=2))
        print(f"no complete export days in population yet (complete days seen: {days}); wrote {out}")
        return 0
    if args.fetch_labels:
        fetch_labels(sorted(x.market_slug.unique()))
    rec = load_recorder(args.recorder_root)
    d = add_z(build_grid(x), rec)
    labels = load_labels()

    cov = d.assign(day_utc=pd.to_datetime(d.start, unit="s", utc=True).dt.date.astype(str)).groupby("day_utc").agg(
        markets=("market_slug", "nunique"), grid_rows=("market_slug", "size"), eligible_rows=("eligible", "sum"),
        cl_available=("cl_spot", lambda s: float(s.notna().mean())),
        bn_available=("bn_spot", lambda s: float(s.notna().mean())),
        cb_available=("cb_spot", lambda s: float(s.notna().mean())),
        basis_available=("basis_bps", lambda s: float(s.notna().mean())))
    cov.to_csv(out / "coverage.csv")
    primary = settle(select(d, "zc", .10), labels)
    elapsed_days = (datetime.now(timezone.utc).timestamp() - POPULATION_START) / 86400
    final = len(primary) >= PRIMARY_N or elapsed_days >= MAX_DAYS
    status = {"run_utc": stamp, "export_days": days, "population_markets": int(d.market_slug.nunique()),
              "primary_selected_with_label": int(len(primary)), "elapsed_days": round(elapsed_days, 2),
              "analysis_allowed": bool(final), "labels_cached": len(labels)}
    if not final:
        status["status"] = "COVERAGE_ONLY (stopping rule: N < 50 and < 21 days; PnL withheld)"
        (out / "status.json").write_text(json.dumps(status, indent=2))
        print(json.dumps(status, indent=2))
        print(cov.round(3).to_string())
        return 0

    rows, cands = [], []
    for zcol in ("zc", "zb", "zk"):
        for lo in (.10, .05):
            g = settle(select(d, zcol, lo), labels)
            g["net_delay1"] = delayed_net(g, x, 1)
            g["net_delay3"] = delayed_net(g, x, 3)
            g["variant"], g["mid_lo"] = zcol, lo
            cands.append(g)
            rows.append({"variant": zcol, "mid_lo": lo, **{k: v for k, v in verdict(g).items() if k != "checks"}})
    pd.DataFrame(rows).to_csv(out / "policy_summary.csv", index=False)
    keep = ["variant", "mid_lo", "market_slug", "day_utc", "checkpoint", "decision_ts", "mid", "zc", "zb", "zk",
            "basis_bps", "side", "ask", "official_outcome", "win", "fee", "net", "net_delay1", "net_delay3"]
    pd.concat(cands, ignore_index=True)[keep].to_csv(out / "candidates.csv", index=False)
    pg = cands[0]
    status.update({"status": "FINAL", "small_sample": len(pg) < PRIMARY_N, "primary": verdict(pg)})
    (out / "verdict.json").write_text(json.dumps(status, indent=2, default=str))
    print(json.dumps(status, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
