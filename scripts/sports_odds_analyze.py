"""S1 v1 analysis (reports/sports_odds_research_20261017/prereg.md sections 4 and 6).

    .venv/bin/python scripts/sports_odds_analyze.py --t0 2026-10-17T12:00:00Z           # blind: counts only
    .venv/bin/python scripts/sports_odds_analyze.py --t0 ... --days 14 --unblind        # full results

Blind mode never reads closing-snapshot prices or settlement values: it prints the
number of snapshots, valid rows, events in range, closing coverage (existence only)
and H2 entry counts, which the extension rule (prereg section 2) needs.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import numpy as np
import pandas as pd

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.research.sports_odds import EXPLORE_LEAGUES, LEAGUES, fair_from_odds  # noqa: E402

OUT_ROOT = REPO_ROOT / "data" / "sports_odds"
THETA = 0.01
THETAS_SECONDARY = (0.005, 0.02, 0.03)
BUCKETS = (("A", 72, 168), ("B", 24, 72), ("C", 6, 24), ("D", 1, 6))
CLOSE_WINDOW_MIN = (1, 30)
N_BOOT = 10_000
SEED = 20261017
ALT_BOOKS = ("betfair_ex_eu", "matchbook")
SOCCER = {k for k, lg in LEAGUES.items() if lg.three_way}


# --- fees -------------------------------------------------------------------------------------

def fee_main(p, rate, exponent):
    """Prereg section 3: rate * (p(1-p))^exponent per share."""
    p = np.clip(p, 0.0, 1.0)
    return rate * (p * (1 - p)) ** exponent


def fee_alt(p, rate, exponent):
    """Section 6 sensitivity: execution/rebate_model.py form, p * rate * (p(1-p))^exponent."""
    return np.clip(p, 0.0, 1.0) * fee_main(p, rate, exponent)


# --- loading ----------------------------------------------------------------------------------

def load_rows(t0: datetime, end: datetime) -> pd.DataFrame:
    frames = []
    for path in sorted((OUT_ROOT / "scans").glob("*/*_tokens.csv")):
        if "_replay" in path.name:
            continue
        df = pd.read_csv(path)
        df["row_order"] = np.arange(len(df))
        df["file"] = path.name
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    for c in ("scan_utc", "start_utc"):
        df[c] = pd.to_datetime(df[c], utc=True)
    df = df[(df["scan_utc"] >= t0) & (df["start_utc"] >= t0 + timedelta(days=1)) & (df["start_utc"] <= end)]
    df = df.copy()
    df["minutes_to_start"] = (df["start_utc"] - df["scan_utc"]).dt.total_seconds() / 60
    for c in ("suspect", "odds_stale", "has_book", "accepting_orders"):
        df[c] = df[c].astype(str).str.lower() == "true"
    df["valid"] = (~df["suspect"] & ~df["odds_stale"] & df["has_book"] & df["accepting_orders"]
                   & df["bid"].notna() & df["ask"].notna())
    df["bucket"] = None
    hrs = df["minutes_to_start"] / 60
    for name, lo, hi in BUCKETS:
        df.loc[(hrs >= lo) & (hrs < hi), "bucket"] = name
    return df


def entries(df: pd.DataFrame, theta: float, fee: Callable = fee_main) -> pd.DataFrame:
    """H2/H3 entry: first valid baseline snapshot (>= 1h out) with fair - ask - fee(ask) >= theta."""
    base = df[(df["kind"] == "baseline") & df["valid"] & df["bucket"].notna()].copy()
    base["edge"] = base["fair"] - base["ask"] - fee(base["ask"], base["fee_rate"], base["fee_exponent"])
    hit = base[base["edge"] >= theta].sort_values("scan_utc")
    return hit.groupby("token_id", as_index=False).first()


def closing_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Last valid snapshot 1-30 min before kickoff, per token (any kind)."""
    lo, hi = CLOSE_WINDOW_MIN
    c = df[df["valid"] & (df["minutes_to_start"] >= lo) & (df["minutes_to_start"] <= hi)]
    return c.sort_values("scan_utc").groupby("token_id").last()


def load_resolutions() -> dict[str, float]:
    """token_id -> settlement value, from the last record per market."""
    last: dict[str, dict] = {}
    path = OUT_ROOT / "resolutions.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                last[rec["market_id"]] = rec
    out: dict[str, float] = {}
    for rec in last.values():
        if rec.get("closed"):
            out.update({k: float(v) for k, v in (rec.get("token_values") or {}).items()})
    return out


# --- statistics -------------------------------------------------------------------------------

def cluster_bootstrap(values: np.ndarray, clusters: np.ndarray, n: int = N_BOOT, seed: int = SEED):
    """Mean and 95% CI, resampling whole clusters (events) with replacement."""
    if len(values) == 0:
        return float("nan"), float("nan"), float("nan")
    uniq, inv = np.unique(clusters, return_inverse=True)
    sums = np.bincount(inv, weights=values)
    counts = np.bincount(inv).astype(float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(uniq), size=(n, len(uniq)))
    means = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(values.mean()), float(lo), float(hi)


def logit(p):
    p = np.clip(np.asarray(p, dtype=float), 0.01, 0.99)
    return np.log(p / (1 - p))


def cluster_logit(y: np.ndarray, X: np.ndarray, clusters: np.ndarray, iters: int = 50):
    """Logistic regression (Newton) with cluster-robust sandwich SE (CR1)."""
    beta = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ beta))
        H = X.T @ (X * (p * (1 - p))[:, None])
        step = np.linalg.solve(H, X.T @ (y - p))
        beta += step
        if np.max(np.abs(step)) < 1e-10:
            break
    p = 1 / (1 + np.exp(-X @ beta))
    bread = np.linalg.inv(X.T @ (X * (p * (1 - p))[:, None]))
    scores = X * (y - p)[:, None]
    uniq, inv = np.unique(clusters, return_inverse=True)
    meat = np.zeros((X.shape[1], X.shape[1]))
    for g in range(len(uniq)):
        s = scores[inv == g].sum(axis=0)
        meat += np.outer(s, s)
    G = len(uniq)
    V = bread @ meat @ bread * (G / (G - 1) if G > 1 else 1.0)
    return beta, np.sqrt(np.diag(V))


def one_sided_p(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


# --- report pieces ----------------------------------------------------------------------------

def fmt_ci(t) -> str:
    m, lo, hi = t
    return f"{m:+.4f} [{lo:+.4f}, {hi:+.4f}]"


def verdict(t) -> str:
    m, lo, _ = t
    return "PASS" if (m > 0 and lo > 0) else "fail"


def blind_counts(df: pd.DataFrame) -> list[str]:
    ent = entries(df, THETA)
    close_exists = set(df[(df["minutes_to_start"] >= CLOSE_WINDOW_MIN[0])
                          & (df["minutes_to_start"] <= CLOSE_WINDOW_MIN[1]) & df["valid"]]["token_id"])
    return [
        f"snapshot files: {df['file'].nunique()} (baseline {df[df.kind == 'baseline']['file'].nunique()}, "
        f"closing {df[df.kind == 'closing']['file'].nunique()})",
        f"rows: {len(df)}  valid: {int(df['valid'].sum())}",
        f"events in range: {df['event_id'].nunique()}  tokens: {df['token_id'].nunique()}",
        f"tokens with a closing snapshot: {len(close_exists)}",
        f"H2 entries (theta={THETA}): {len(ent)} from {ent['event_id'].nunique() if len(ent) else 0} events; "
        f"with closing snapshot: {int(ent['token_id'].isin(close_exists).sum()) if len(ent) else 0}",
        "entries by bucket: " + (", ".join(f"{k}={v}" for k, v in ent["bucket"].value_counts().sort_index().items())
                                 if len(ent) else "-"),
        "entries by league: " + (", ".join(f"{k}={v}" for k, v in ent["league"].value_counts().items())
                                 if len(ent) else "-"),
    ]


def clv_table(df, close, theta, fee, res=None) -> dict:
    ent = entries(df, theta, fee).set_index("token_id")
    j = ent.join(close[["fair", "bid"]].rename(columns={"fair": "fair_close", "bid": "bid_close"}), how="inner")
    out = {"n_entries": len(ent), "n_with_close": len(j)}
    if len(j) == 0:
        return out
    cost = j["ask"] + fee(j["ask"], j["fee_rate"], j["fee_exponent"])
    j["clv"] = j["fair_close"] - cost
    j["rt"] = j["bid_close"] - cost - fee(j["bid_close"], j["fee_rate"], j["fee_exponent"])
    j["pin_move"] = j["fair_close"] - j["fair"]
    out["frame"] = j
    out["H2"] = cluster_bootstrap(j["clv"].to_numpy(), j["event_id"].astype(str).to_numpy())
    out["H3"] = cluster_bootstrap(j["rt"].to_numpy(), j["event_id"].astype(str).to_numpy())
    if res is not None:
        h = ent[ent.index.isin(res.keys())].copy()
        h["y"] = [res[t] for t in h.index]
        h = h[h["y"].isin([0.0, 1.0])]
        h["hold"] = h["y"] - h["ask"] - fee(h["ask"], h["fee_rate"], h["fee_exponent"])
        out["hold_n"] = len(h)
        out["hold"] = cluster_bootstrap(h["hold"].to_numpy(), h["event_id"].astype(str).to_numpy())
    return out


def h1(df: pd.DataFrame, res: dict) -> list[str]:
    c = df[(df["kind"] == "baseline") & df["valid"] & (df["bucket"] == "C")].copy()
    # One row per event x outcome: soccer YES tokens; two-way markets: first-listed outcome.
    yes = c[c["side"] == "yes"]
    two = c[c["side"] == "outcome"].sort_values(["scan_utc", "row_order"])
    first_tok = two.groupby("market_id")["token_id"].first()
    two = two[two["token_id"].isin(set(first_tok))]
    sel = pd.concat([yes, two]).sort_values("scan_utc").groupby("token_id").last()
    sel = sel[sel.index.isin(res.keys())].copy()
    sel["y"] = [res[t] for t in sel.index]
    sel = sel[sel["y"].isin([0.0, 1.0])]
    lines = [f"H1 rows: {len(sel)} from {sel['event_id'].nunique()} events (bucket C, latest snapshot)"]
    if len(sel) < 20 or sel["y"].nunique() < 2:
        return lines + ["H1: not enough rows"]
    y = sel["y"].to_numpy()
    X = np.column_stack([np.ones(len(sel)), logit(sel["mid"]), logit(sel["fair"])])
    beta, se = cluster_logit(y, X, sel["event_id"].astype(str).to_numpy())
    z = beta[2] / se[2]
    p = one_sided_p(z)
    brier = {k: float(np.mean((sel[k] - y) ** 2)) for k in ("mid", "fair")}
    ll = {k: float(-np.mean(y * np.log(np.clip(sel[k], 1e-6, 1)) + (1 - y) * np.log(np.clip(1 - sel[k], 1e-6, 1))))
          for k in ("mid", "fair")}
    lines += [f"logit(mid) coef {beta[1]:+.3f} (se {se[1]:.3f}); logit(fair) coef {beta[2]:+.3f} (se {se[2]:.3f}), "
              f"one-sided p = {p:.4f} -> {'PASS' if beta[2] > 0 and p < 0.05 else 'fail'}",
              f"Brier: PM {brier['mid']:.4f}  Pinnacle {brier['fair']:.4f}; "
              f"log loss: PM {ll['mid']:.4f}  Pinnacle {ll['fair']:.4f}"]
    return lines


def alt_book_fairs(df: pd.DataFrame, book: str) -> pd.Series:
    """Fair from another book for each row, re-read from the stored raw odds."""
    cache: dict[tuple, dict] = {}
    vals = []
    for _, r in df.iterrows():
        stamp = r["scan_utc"].strftime("%Y-%m-%d/%H%M%SZ")
        key = (stamp, r["league"])
        if key not in cache:
            path = OUT_ROOT / "raw" / stamp / f"odds_{r['league']}.json.gz"
            cache[key] = ({str(e["id"]): e for e in json.load(gzip.open(path, "rt"))} if path.exists() else {})
        ev = cache[key].get(str(r["book_event_id"]))
        f = fair_from_odds(ev, LEAGUES[r["league"]].three_way, book=book) if ev else None
        p = f.probs.get(r["book_outcome"]) if f else None
        vals.append(np.nan if p is None else (1 - p if r["side"] == "no" else p))
    return pd.Series(vals, index=df.index)


def unblind_report(df: pd.DataFrame) -> list[str]:
    close = closing_rows(df)
    res = load_resolutions()
    L = ["## Primary", ""]
    L += h1(df, res)
    main = clv_table(df, close, THETA, fee_main, res)
    L += ["", f"H2/H3 entries theta={THETA}: {main['n_entries']}, with closing snapshot: {main['n_with_close']}"]
    if "H2" in main:
        L += [f"H2 CLV per share: {fmt_ci(main['H2'])} -> {verdict(main['H2'])}",
              f"H3 PM round trip per share: {fmt_ci(main['H3'])} -> {verdict(main['H3'])}"]

    L += ["", "## Secondary (descriptive)", ""]
    for th in THETAS_SECONDARY:
        t = clv_table(df, close, th, fee_main)
        if "H2" in t:
            L.append(f"theta={th}: n={t['n_with_close']} H2 {fmt_ci(t['H2'])} H3 {fmt_ci(t['H3'])}")
    t = clv_table(df, close, THETA, fee_alt, res)
    if "H2" in t:
        L.append(f"alt fee (p*rate*p(1-p)): n={t['n_with_close']} H2 {fmt_ci(t['H2'])} H3 {fmt_ci(t['H3'])}")
    if "hold" in main:
        L.append(f"hold to settlement: n={main['hold_n']} {fmt_ci(main['hold'])}")
    if "frame" in main:
        j = main["frame"]
        for col, label in (("bucket", "bucket"), ("league", "league")):
            L.append("")
            for k, g in j.groupby(col):
                L.append(f"{label} {k}: n={len(g)} H2 {fmt_ci(cluster_bootstrap(g['clv'].to_numpy(), g['event_id'].astype(str).to_numpy()))}"
                         f" H3 {fmt_ci(cluster_bootstrap(g['rt'].to_numpy(), g['event_id'].astype(str).to_numpy()))}")
        L += ["", f"Pinnacle move fair_close - fair_entry: mean {j['pin_move'].mean():+.4f} "
                  f"median {j['pin_move'].median():+.4f}",
              f"capacity: +EV $ at entry median {j['plus_ev_usdc'].median():.0f}, "
              f"p90 {j['plus_ev_usdc'].quantile(0.9):.0f}; per entry day sum median "
              f"{j.groupby(j['scan_utc'].dt.date)['plus_ev_usdc'].sum().median():.0f}"]

    # Soccer heavy favourite (former H4).
    fav = df[(df["kind"] == "baseline") & df["valid"] & df["league"].isin(SOCCER) & (df["side"] == "yes")
             & (df["book_outcome"] != "Draw") & (df["fair"] >= 0.65) & (df["minutes_to_start"] >= 24 * 60)]
    fav = fav.sort_values("scan_utc").groupby("token_id", as_index=False).first()
    if len(fav):
        L += ["", f"soccer favourite YES (fair>=0.65, >=24h): n={len(fav)} mean(mid - fair) "
                  f"{fmt_ci(cluster_bootstrap((fav['mid'] - fav['fair']).to_numpy(), fav['event_id'].astype(str).to_numpy()))}"]

    # Direction B: explore vs main leagues.
    v = df[(df["kind"] == "baseline") & df["valid"]].copy()
    v["gap"] = (v["fair"] - v["mid"]).abs()
    L.append("")
    for grp, mask in (("main", ~v["league"].isin(EXPLORE_LEAGUES)), ("explore", v["league"].isin(EXPLORE_LEAGUES))):
        g = v[mask]
        if len(g):
            ent = entries(df[df["league"].isin(g["league"].unique())], THETA)
            L.append(f"{grp}: rows {len(g)} |fair-mid| median {g['gap'].median():.4f} p90 {g['gap'].quantile(0.9):.4f};"
                     f" entries {len(ent)} per 1000 rows {1000 * len(ent) / len(g):.1f}")

    # Maker: descriptive only.
    L.append(f"maker fair - bid (valid baseline rows): median {(v['fair'] - v['bid']).median():+.4f}, "
             f"share >= 0.01: {((v['fair'] - v['bid']) >= 0.01).mean():.3f}")

    # Other books as fair source (entries re-scored against their own close).
    if "frame" in main:
        for book in ALT_BOOKS:
            sub = df[(df["kind"] == "baseline") & df["valid"] & df["bucket"].notna()].copy()
            sub["fair"] = alt_book_fairs(sub, book)
            sub = sub[sub["fair"].notna()]
            c2 = df[df["valid"] & (df["minutes_to_start"] >= CLOSE_WINDOW_MIN[0])
                    & (df["minutes_to_start"] <= CLOSE_WINDOW_MIN[1])].copy()
            c2["fair"] = alt_book_fairs(c2, book)
            c2 = c2[c2["fair"].notna()].sort_values("scan_utc").groupby("token_id").last()
            t = clv_table(sub, c2, THETA, fee_main)
            if "H2" in t:
                L.append(f"fair source {book}: n={t['n_with_close']} H2 {fmt_ci(t['H2'])} H3 {fmt_ci(t['H3'])}")
            else:
                L.append(f"fair source {book}: no entries with close")
    return L


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="S1 v1 analysis")
    ap.add_argument("--t0", required=True, help="freeze commit time, e.g. 2026-10-17T12:00:00Z")
    ap.add_argument("--days", type=int, default=14, choices=(14, 28))
    ap.add_argument("--unblind", action="store_true")
    args = ap.parse_args(argv)
    t0 = datetime.fromisoformat(args.t0.replace("Z", "+00:00")).astimezone(timezone.utc)
    end = t0 + timedelta(days=args.days)
    df = load_rows(t0, end)
    print(f"# S1 v1 analysis  T0={t0.isoformat()}  events starting [{(t0 + timedelta(days=1)).date()}, {end.isoformat()}]")
    if df.empty:
        print("no rows")
        return 0
    print("\n".join(blind_counts(df)))
    if not args.unblind:
        print("(blind: no closing prices or settlements read)")
        return 0
    print()
    print("\n".join(unblind_report(df)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
