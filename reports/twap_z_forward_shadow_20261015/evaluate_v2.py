"""Pre-registered forward shadow v2 (plan_v2.md): recorder-only evaluation of the TWAP z rule.

Everything comes from the standalone recorder (Chainlink/Binance/Coinbase prices and
Polymarket books) plus official outcomes; no trading-bot export is read. Selection,
settlement, verdict and the stopping rule are shared with v1 (evaluate.py).

    .venv/bin/python reports/twap_z_forward_shadow_20261015/evaluate_v2.py --fetch-labels
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

O = Path(__file__).resolve().parent
R = O.parents[1]
sys.path.insert(0, str(O))
import evaluate as v1  # noqa: E402  (shared rule, settlement, verdict, labels)
from recorder_mechanics import ChainlinkSeries, load_parquet_dir  # noqa: E402

POPULATION_START = datetime(2026, 10, 15, 1, 0, tzinfo=timezone.utc).timestamp()
# TWAP stamped at second S = mean of Chainlink spot seconds [S-62, S-3] (plan_v2.md section 1.2).
WINDOW_OFFSET = -62
LOCK_SECONDS = range(10, 0, -1)       # T-10 ... T-1, 1 s steps (descriptive "determined" policy)
LOCK_MIN_MARGIN_BPS = 0.5             # above the observed reconstruction error (max 0.21 bps)
LOCK_BOOK_MAX_AGE = 1.0
PRIMARY_N, MAX_DAYS, ASOF_SEC = v1.PRIMARY_N, v1.MAX_DAYS, v1.ASOF_SEC
YR3 = v1.YR3
LABEL_DELAY_SEC = 600


class BookIndex:
    """Per (market, outcome) book rows sorted by sample second, for fast as-of lookups."""

    def __init__(self, books: pd.DataFrame) -> None:
        self._g = {k: (g.ts_sec.to_numpy(), g.reset_index(drop=True))
                   for k, g in books.sort_values("ts_sec").groupby(["market_slug", "outcome"])}

    def asof(self, slug: str, outcome: str, t: float, *, not_before: float | None = None):
        hit = self._g.get((slug, outcome))
        if hit is None:
            return None
        ts, rows = hit
        i = int(np.searchsorted(ts, t, side="right")) - 1
        lo = not_before if not_before is not None else t - ASOF_SEC
        return rows.iloc[i] if i >= 0 and ts[i] >= lo else None


def _valid(row) -> bool:
    if row is None:
        return False
    bid, ask = row.best_bid, row.best_ask
    return pd.notna(bid) and pd.notna(ask) and 0 <= bid <= ask and .001 <= ask <= 1


class ExchangeIndex:
    def __init__(self, prices: pd.DataFrame) -> None:
        self._g = {src: (g.t.to_numpy(), g.close.to_numpy()) for src, g in prices.sort_values("t").groupby("source")}

    def asof(self, source: str, t: float) -> float | None:
        hit = self._g.get(source)
        if hit is None:
            return None
        ts, px = hit
        i = int(np.searchsorted(ts, t, side="right")) - 1
        return float(px[i]) if i >= 0 and ts[i] >= t - ASOF_SEC else None


def build_grid(prices: pd.DataFrame, books: pd.DataFrame, cs: ChainlinkSeries, now_ts: float) -> pd.DataFrame:
    prices = prices.assign(t=prices.last_received_ms / 1000.0).sort_values("t")
    bx, ex = BookIndex(books), ExchangeIndex(prices)
    slugs = sorted(s for s in books.market_slug.unique()
                   if POPULATION_START <= float(s.rsplit("-", 1)[-1]) and float(s.rsplit("-", 1)[-1]) + 900 <= now_ts)
    # Basis pairs on one local clock: Chainlink re-keyed by receipt second (v1.rolling_basis pairs by ts_sec).
    rb = prices[prices.source.isin(["binance_btcusdt", "chainlink_spot"])].copy()
    rb["ts_sec"] = (rb.last_received_ms // 1000).astype("int64")
    rows = []
    for slug in slugs:
        start = int(slug.rsplit("-", 1)[-1])
        end = start + 900
        for h in v1.CHECKPOINTS:
            t = end - h
            up, dn = bx.asof(slug, "Up", t), bx.asof(slug, "Down", t)
            vu, vd = _valid(up), _valid(dn)
            strike = cs.strike(start, t)
            req, rem, missing = cs.required_future_avg(end, strike, t) if strike else (None, 0, 0)
            sigma = cs.sigma(t)
            rows.append({
                "market_slug": slug, "start": start, "checkpoint": h, "decision_ts": t,
                "valid_up": vu, "valid_down": vd,
                "best_bid_up": up.best_bid if up is not None else np.nan, "best_ask_up": up.best_ask if up is not None else np.nan,
                "best_bid_down": dn.best_bid if dn is not None else np.nan, "best_ask_down": dn.best_ask if dn is not None else np.nan,
                "strike": strike, "required_future_avg_to_flip": req, "remaining_final_window_sec": rem,
                "observed_missing_filled": missing, "sigma_ex_market": sigma,
                "cl_spot": cs.spot_asof(t), "bn_spot": ex.asof("binance_btcusdt", t),
                "cb_spot": ex.asof("coinbase_btcusd", t),
            })
    d = pd.DataFrame(rows)
    if d.empty:
        return d
    d["mid"] = np.where(d.valid_up, (d.best_bid_up + d.best_ask_up) / 2, 1 - (d.best_bid_down + d.best_ask_down) / 2)
    d["eligible"] = (d.strike.notna() & d.required_future_avg_to_flip.notna() & d.remaining_final_window_sec.gt(0)
                     & d.sigma_ex_market.gt(0) & (d.valid_up | d.valid_down))
    d["basis_bps"], d["basis_pairs"] = v1.rolling_basis(rb, d.decision_ts.to_numpy())
    d["bn_adj"] = d.bn_spot * (1 - d.basis_bps / 1e4)
    for col, spot in (("zc", "cl_spot"), ("zb", "bn_adj"), ("zk", "cb_spot")):
        s = d[spot].astype(float)
        d[col] = (s - d.required_future_avg_to_flip) / (s * d.sigma_ex_market * np.sqrt(d.remaining_final_window_sec / YR3))
    return d


def delayed_net(g: pd.DataFrame, bx: BookIndex, lag: int) -> pd.Series:
    out = pd.Series(np.nan, index=g.index)
    for i, r in g.iterrows():
        row = bx.asof(r.market_slug, "Up" if r.side == "up" else "Down", r.decision_ts + lag,
                        not_before=r.decision_ts)
        if row is not None and _valid(row) and r.decision_ts + lag < r.start + 900:
            a = float(row.best_ask)
            out[i] = r.win - a - .07 * a * (1 - a)
    return out


def lock_policy(d_slugs: list[str], cs: ChainlinkSeries, bx: BookIndex, labels: dict[str, str]) -> pd.DataFrame:
    """Descriptive: first second in T-10..T-1 at which the settlement is already fully observed."""
    rows = []
    for slug in d_slugs:
        start = int(slug.rsplit("-", 1)[-1])
        end = start + 900
        strike = cs.strike(start, end - 10)
        if strike is None:
            continue
        for h in LOCK_SECONDS:
            t = end - h
            settle_px = cs.determined_settlement(end, t)
            if settle_px is None:
                continue
            margin = (settle_px - strike) / strike * 1e4
            side = "Up" if settle_px >= strike else "Down"
            book = bx.asof(slug, side, t)
            fresh = book is not None and t - float(book.ts_sec) <= LOCK_BOOK_MAX_AGE
            ask = float(book.best_ask) if fresh and pd.notna(book.best_ask) else None
            later = bx.asof(slug, side, t + 1, not_before=t)
            official = labels.get(slug)
            rows.append({"market_slug": slug, "seconds_before_end": h, "decision_ts": t, "margin_bps": margin,
                         "side": side, "ask": ask, "ask_plus1s": float(later.best_ask) if later is not None and pd.notna(later.best_ask) else None,
                         "eligible": abs(margin) >= LOCK_MIN_MARGIN_BPS and ask is not None and ask <= 1,
                         "official_outcome": official,
                         "win": None if official not in ("UP", "DOWN") else int(side.upper() == official)})
            break
    r = pd.DataFrame(rows)
    if len(r):
        r["net"] = np.where(r.eligible & r.win.notna(), r.win - r.ask - .07 * r.ask * (1 - r.ask), np.nan)
    return r


def label_agreement(d: pd.DataFrame, cs: ChainlinkSeries, labels: dict[str, str]) -> dict:
    """Reconstructed side (settlement TWAP >= strike, ties UP) vs official outcome: data-quality check."""
    agree = total = 0
    for slug in d.market_slug.unique():
        start = int(slug.rsplit("-", 1)[-1])
        strike, settle_px = cs.strike(start, start + 900), cs.settlement(start + 900)
        official = labels.get(slug)
        if strike is None or settle_px is None or official not in ("UP", "DOWN"):
            continue
        total += 1
        agree += ("UP" if settle_px >= strike else "DOWN") == official
    return {"markets": total, "agree": agree, "rate": agree / total if total else None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recorder-root", type=Path, default=R / "data" / "price_sources_1s")
    ap.add_argument("--book-root", type=Path, default=R / "data" / "polymarket_book_1s")
    ap.add_argument("--fetch-labels", action="store_true")
    args = ap.parse_args(argv)
    if WINDOW_OFFSET is None:
        raise SystemExit("WINDOW_OFFSET not set: run the mechanics validation and freeze plan_v2.md first")

    now = datetime.now(timezone.utc).timestamp()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = O / "runs_v2" / stamp
    out.mkdir(parents=True, exist_ok=True)
    prices, books = load_parquet_dir(args.recorder_root), load_parquet_dir(args.book_root)
    if prices.empty or books.empty:
        (out / "status.json").write_text(json.dumps({"status": "NO_RECORDER_DATA"}, indent=2))
        print("no recorder data yet")
        return 0
    cs = ChainlinkSeries(prices, window_offset=WINDOW_OFFSET)
    d = build_grid(prices, books, cs, now - LABEL_DELAY_SEC)
    if d.empty:
        (out / "status.json").write_text(json.dumps({"status": "NO_COMPLETED_MARKETS_IN_POPULATION"}, indent=2))
        print("no completed markets in population yet")
        return 0
    if args.fetch_labels:
        v1.fetch_labels(sorted(d.market_slug.unique()))
    labels = v1.load_labels()

    d["day_utc"] = pd.to_datetime(d.start, unit="s", utc=True).dt.date.astype(str)
    cov = d.groupby("day_utc").agg(
        markets=("market_slug", "nunique"), grid_rows=("market_slug", "size"), eligible_rows=("eligible", "sum"),
        strike_available=("strike", lambda s: float(s.notna().mean())),
        required_avg_available=("required_future_avg_to_flip", lambda s: float(s.notna().mean())),
        book_valid_any=("valid_up", lambda s: float((s | d.loc[s.index, "valid_down"]).mean())),
        cl_available=("cl_spot", lambda s: float(s.notna().mean())),
        bn_available=("bn_spot", lambda s: float(s.notna().mean())),
        cb_available=("cb_spot", lambda s: float(s.notna().mean())),
        basis_available=("basis_bps", lambda s: float(s.notna().mean())))
    cov.to_csv(out / "coverage.csv")
    primary = v1.settle(v1.select(d, "zc", .10), labels)
    elapsed_days = (now - POPULATION_START) / 86400
    final = len(primary) >= PRIMARY_N or elapsed_days >= MAX_DAYS
    status = {"run_utc": stamp, "population_markets": int(d.market_slug.nunique()),
              "primary_selected_with_label": int(len(primary)), "elapsed_days": round(elapsed_days, 2),
              "analysis_allowed": bool(final), "labels_cached": len(labels),
              "settlement_reconstruction_vs_official": label_agreement(d, cs, labels)}
    if not final:
        status["status"] = "COVERAGE_ONLY (stopping rule: N < 50 and < 21 days; PnL withheld)"
        (out / "status.json").write_text(json.dumps(status, indent=2))
        print(json.dumps(status, indent=2))
        print(cov.round(3).to_string())
        return 0

    bx = BookIndex(books)
    rows, cands = [], []
    for zcol in ("zc", "zb", "zk"):
        for lo in (.10, .05):
            g = v1.settle(v1.select(d, zcol, lo), labels)
            g["net_delay1"], g["net_delay3"] = delayed_net(g, bx, 1), delayed_net(g, bx, 3)
            g["variant"], g["mid_lo"] = zcol, lo
            cands.append(g)
            rows.append({"variant": zcol, "mid_lo": lo, **{k: v for k, v in v1.verdict(g).items() if k != "checks"}})
    pd.DataFrame(rows).to_csv(out / "policy_summary.csv", index=False)
    lock = lock_policy(sorted(d.market_slug.unique()), cs, bx, labels)
    lock.to_csv(out / "lock_policy.csv", index=False)
    e = lock[lock.eligible & lock.win.notna()] if len(lock) else lock
    status["lock_policy_descriptive"] = {
        "markets_determined_by_T-1": int(len(lock)), "eligible_with_ask": int(len(e)),
        "wins": int(e.win.sum()) if len(e) else 0, "ask_lt_0_99": int((e.ask < .99).sum()) if len(e) else 0,
        "mean_ask": float(e.ask.mean()) if len(e) else None, "total_net": float(e.net.sum()) if len(e) else None,
        "median_seconds_before_end": float(lock.seconds_before_end.median()) if len(lock) else None}
    keep = ["variant", "mid_lo", "market_slug", "day_utc", "checkpoint", "decision_ts", "mid", "zc", "zb", "zk",
            "basis_bps", "side", "ask", "official_outcome", "win", "fee", "net", "net_delay1", "net_delay3"]
    pd.concat(cands, ignore_index=True)[keep].to_csv(out / "candidates.csv", index=False)
    status.update({"status": "FINAL", "small_sample": len(cands[0]) < PRIMARY_N, "primary": v1.verdict(cands[0])})
    (out / "verdict.json").write_text(json.dumps(status, indent=2, default=str))
    print(json.dumps(status, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
