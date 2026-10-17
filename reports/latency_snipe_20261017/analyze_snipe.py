"""Stale-quote ("snipe") opportunity analysis on a ms_recorder tape. Reads data only; places nothing.

    python3 analyze_snipe.py TAPE_DIR --rtt-ms 12            # RTT from clob_rtt_probe.py on the same VM
    python3 analyze_snipe.py TAPE_DIR --rtt-ms 12 --fetch-outcomes   # adds hold-to-settlement PnL

Question: after a Binance move, how long does the Polymarket quote on the favoured side stay stale,
who takes it and how fast, and would an order that reaches the matching engine L ms after we saw
the move still get filled with positive value net of the taker fee?

Definitions (all times are VM receive times unless said otherwise):
- jump: Binance bookTicker mid moves >= J bps within W ms (one per asset per refractory period); t0 is
  the receive time of the tick that completes it. Favoured token = Up for an up-move, Down otherwise.
- A0: favoured best ask at t0 (book replayed from the PM market channel); size at A0 = Q0.
- pm_led: the favoured ask had already risen during [t0 - W, t0] (Polymarket moved first).
- stale_ms: time until the favoured best ask rises above A0 (or vanishes); capped at HORIZON_MS.
- snipers: taker BUYs of the favoured token at <= A0, and taker SELLs of the other token, after t0 - W.
  first_snipe_ms is the earliest one relative to t0 (negative = before we saw the move).
- engine time ~ receive time - d_pm (PM one-way delay, median of recv - server timestamp).
- capture at latency L (our order reaches the engine at t0 + L): the stale level is still there in engine
  time, and Q0 minus snipes that happened earlier in engine time still covers our size (we queue last).
- net per share = favoured mid at t0 + H - A0 - fee, fee = FEE_COEF * A0 * (1 - A0).
"""
from __future__ import annotations

import argparse
import gzip
import json
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

HORIZON_MS = 5000
FEE_COEF = 0.07
SIZE = 5.0
J_GRID_BPS = (2.0, 3.0, 5.0, 8.0)
PRIMARY_J_BPS = 3.0
W_MS = 1000
REFRACTORY_MS = 3000
L_GRID_MS = (0, 5, 10, 25, 50, 100, 200, 370)
MARKOUT_MS = (5000, 60000)
SERVER_D_GRID_MS = (0, 50, 100, 120, 150, 200, 300, 500)


def read_lines(d: Path):
    for f in sorted(d.glob("*.jsonl.gz")):
        with gzip.open(f, "rt") as fh:
            for line in fh:
                recv, _, raw = line.rstrip("\n").partition("\t")
                if not raw.startswith(("{", "[")):
                    continue
                try:
                    yield int(recv) / 1e6, json.loads(raw)          # recv in ms (float)
                except ValueError:
                    continue


def load_binance(tape: Path):
    """asset -> DataFrame(t, mid); plus one-way samples recv - aggTrade T (ms)."""
    rows, lat = defaultdict(list), []
    for t, m in read_lines(tape / "binance"):
        d, stream = m.get("data") or {}, str(m.get("stream") or "")
        asset = stream.split("usdt@")[0]
        if stream.endswith("@bookTicker"):
            rows[asset].append((t, (float(d["b"]) + float(d["a"])) / 2))
        elif stream.endswith("@aggTrade") and d.get("T"):
            lat.append(t - float(d["T"]))
    return {a: pd.DataFrame(v, columns=["t", "mid"]) for a, v in rows.items()}, lat


def load_binance_trades(tape: Path) -> dict:
    """asset -> DataFrame(t = Binance trade time T in ms, mid = trade price), in exchange time."""
    rows = defaultdict(list)
    for _, m in read_lines(tape / "binance"):
        d, stream = m.get("data") or {}, str(m.get("stream") or "")
        if stream.endswith("@aggTrade") and d.get("T"):
            rows[stream.split("usdt@")[0]].append((float(d["T"]), float(d["p"])))
    return {a: pd.DataFrame(sorted(v), columns=["t", "mid"]) for a, v in rows.items()}


REACTION_BINS_MS = (-1000, -500, -200, 0, 25, 50, 100, 200, 500, 1000, 2000)


def server_reaction(bn_trades: dict, trades: pd.DataFrame, meta: dict, j_bps: float) -> dict:
    """Location-free reaction of Polymarket takers to Binance moves, on exchange clocks only.

    For each Binance aggTrade jump (exchange time T0), Polymarket trades of that asset's live markets are
    timed by their server timestamp minus T0. Aligned = taker BUY of the favoured token or taker SELL of
    the other one; contra = the opposite. Aligned minus contra per delay bin is the informed reaction:
    where the excess appears is how fast the fastest takers act after Binance prints.
    """
    by_asset = defaultdict(list)
    for tok, m in meta.items():
        by_asset[m["asset"]].append(tok)
    tr = trades.dropna(subset=["server_t"])
    tr_by_tok = {tok: g for tok, g in tr.groupby("token", sort=False)}
    edges = np.array(REACTION_BINS_MS, dtype=float)
    al_n, co_n = np.zeros(len(edges) - 1), np.zeros(len(edges) - 1)
    al_sh, co_sh = np.zeros(len(edges) - 1), np.zeros(len(edges) - 1)
    first, n_jumps = [], 0
    for asset, df in bn_trades.items():
        for t0, sign, _ in find_jumps(df, j_bps):
            n_jumps += 1
            fa = []
            for tok in by_asset.get(asset, []):
                m = meta[tok]
                if not (m["start_ts"] * 1000 + 5000 <= t0 <= m["end_ts"] * 1000 - 5000):
                    continue
                g = tr_by_tok.get(tok)
                if g is None:
                    continue
                g = g[(g.server_t >= t0 + edges[0]) & (g.server_t < t0 + edges[-1])]
                fav = (m["outcome"].lower() == "up") == (sign > 0)
                aligned = (g.side == "BUY") == fav
                for mask, cnt, sh in ((aligned, al_n, al_sh), (~aligned, co_n, co_sh)):
                    d = (g.server_t[mask] - t0).to_numpy()
                    cnt += np.histogram(d, edges)[0]
                    sh += np.histogram(d, edges, weights=g["size"][mask].to_numpy())[0]
                fa += [x for x in (g.server_t[aligned] - t0) if x >= 0]
            if fa:
                first.append(min(fa))
    labels = [f"{int(a)}..{int(b)}" for a, b in zip(edges[:-1], edges[1:])]
    return {"jumps": n_jumps,
            "first_aligned_ms_p10_p50_p90": [round(float(np.quantile(first, q)), 1) for q in (.1, .5, .9)] if first else None,
            "aligned_minus_contra_trades": dict(zip(labels, (al_n - co_n).astype(int).tolist())),
            "aligned_minus_contra_shares": dict(zip(labels, np.round(al_sh - co_sh, 1).tolist())),
            "aligned_trades": dict(zip(labels, al_n.astype(int).tolist()))}


def load_markets(tape: Path) -> dict:
    meta = {}
    for line in open(tape / "markets.jsonl"):
        m = json.loads(line)
        for tok, outcome in m["tokens"].items():
            meta[tok] = {"slug": m["slug"], "asset": m["asset"], "outcome": outcome,
                         "start_ts": m["start_ts"], "end_ts": m["end_ts"], "duration": m["duration"]}
    return meta


def replay_pm(tape: Path, meta: dict, clock: str = "recv"):
    """Best-quote changes per token, trades, and one-way samples (recv - server timestamp).

    clock="server" stamps every book change and trade with the Polymarket server timestamp instead of the
    VM receive time (messages without one are applied but not re-stamped), for location-free simulation."""
    books: dict = {}
    best_rows, trades, lat = [], [], []
    last_best: dict = {}

    def emit(t, tok):
        b = books.get(tok)
        if b is None:
            return
        bb = max(b["bids"]) if b["bids"] else None
        ba = min(b["asks"]) if b["asks"] else None
        cur = (bb, b["bids"].get(bb) if bb is not None else None, ba, b["asks"].get(ba) if ba is not None else None)
        if last_best.get(tok) != cur:
            last_best[tok] = cur
            best_rows.append((t, tok) + cur)

    for d in sorted(p for p in tape.glob("pm_*") if p.is_dir()):
        for t, msg in read_lines(d):
            items = msg if isinstance(msg, list) else [msg]
            t_recv = t
            for it in items:
                if not isinstance(it, dict):
                    continue
                kind = it.get("event_type")
                if it.get("timestamp"):
                    try:
                        lat.append(t_recv - float(it["timestamp"]))
                        if clock == "server":
                            t = float(it["timestamp"])
                    except (TypeError, ValueError):
                        pass
                if kind == "book":
                    tok = str(it.get("asset_id"))
                    if tok not in meta:
                        continue
                    books[tok] = {s: {float(l["price"]): float(l["size"]) for l in it.get(s) or [] if float(l["size"]) > 0}
                                  for s in ("bids", "asks")}
                    emit(t, tok)
                elif kind == "price_change":
                    touched = set()
                    for ch in it.get("price_changes") or it.get("changes") or []:
                        tok = str(ch.get("asset_id") or it.get("asset_id"))
                        b = books.get(tok)
                        side = {"BUY": "bids", "SELL": "asks"}.get(str(ch.get("side")).upper())
                        if b is None or side is None:
                            continue
                        px, sz = float(ch["price"]), float(ch["size"])
                        if sz <= 0:
                            b[side].pop(px, None)
                        else:
                            b[side][px] = sz
                        touched.add(tok)
                    for tok in touched:
                        emit(t, tok)
                elif kind == "last_trade_price":
                    tok = str(it.get("asset_id"))
                    if tok in meta:
                        trades.append((t, tok, float(it["price"]), float(it["size"]), str(it.get("side")).upper(),
                                       float(it.get("fee_rate_bps") or 0), float(it.get("timestamp") or "nan")))
    best = pd.DataFrame(best_rows, columns=["t", "token", "bid", "bid_sz", "ask", "ask_sz"])
    # The same tape can hold a book twice (two sockets around a rollover): order by time, keep last per instant.
    best = best.sort_values("t", kind="stable")
    tr = pd.DataFrame(trades, columns=["t", "token", "price", "size", "side", "fee_rate_bps", "server_t"]).sort_values("t")
    tr = tr.drop_duplicates(["token", "price", "size", "side", "t"])
    return best, tr, lat


def find_jumps(df: pd.DataFrame, j_bps: float, w_ms: float = W_MS, refractory_ms: float = REFRACTORY_MS) -> list:
    """(t0, sign, move_bps) for each Binance move >= j_bps within w_ms, one per refractory period."""
    if df.empty:
        return []
    t, mid = df.t.to_numpy(), df.mid.to_numpy()
    j = np.clip(np.searchsorted(t, t - w_ms, side="right") - 1, 0, None)     # price as of t - W
    ret = (mid / mid[j] - 1) * 1e4
    out, last = [], -np.inf
    for i in np.flatnonzero(np.abs(ret) >= j_bps):
        if t[i] - last >= refractory_ms:
            out.append((float(t[i]), int(np.sign(ret[i])), float(ret[i])))
            last = t[i]
    return out


class Series:
    """As-of lookup of the best-quote rows of one token."""

    def __init__(self, g: pd.DataFrame):
        self.t = g.t.to_numpy()
        self.ask, self.ask_sz = g.ask.to_numpy(dtype=float), g.ask_sz.to_numpy(dtype=float)
        self.bid = g.bid.to_numpy(dtype=float)

    def at(self, t: float):
        i = np.searchsorted(self.t, t, side="right") - 1
        return None if i < 0 else i

    def mid(self, t: float):
        i = self.at(t)
        if i is None or np.isnan(self.ask[i]) or np.isnan(self.bid[i]):
            return None
        return (self.ask[i] + self.bid[i]) / 2


def evaluate(jumps_by_asset: dict, best: pd.DataFrame, trades: pd.DataFrame, meta: dict, d_pm: float,
             l_grid=L_GRID_MS, w_ms: float = W_MS) -> pd.DataFrame:
    series = {tok: Series(g) for tok, g in best.groupby("token", sort=False)}
    tr_by_tok = {tok: g for tok, g in trades.groupby("token", sort=False)}
    by_asset = defaultdict(list)
    for tok, m in meta.items():
        by_asset[m["asset"]].append(tok)
    sibling = {}
    for tok, m in meta.items():
        for other, mo in meta.items():
            if other != tok and mo["slug"] == m["slug"]:
                sibling[tok] = other
    rows = []
    for asset, jumps in jumps_by_asset.items():
        for t0, sign, move in jumps:
            for tok in by_asset.get(asset, []):
                m = meta[tok]
                if (m["outcome"].lower() == "up") != (sign > 0):
                    continue
                if not (m["start_ts"] * 1000 + 5000 <= t0 <= m["end_ts"] * 1000 - 5000):
                    continue
                s = series.get(tok)
                if s is None or s.at(t0) is None:
                    continue
                i0 = s.at(t0)
                a0, q0 = s.ask[i0], s.ask_sz[i0]
                if np.isnan(a0) or not (0.03 <= a0 <= 0.97):
                    continue
                ip = s.at(t0 - w_ms)
                pm_led = ip is not None and not np.isnan(s.ask[ip]) and a0 > s.ask[ip] + 1e-9
                after = np.flatnonzero((s.t > t0) & ((s.ask > a0 + 1e-9) | np.isnan(s.ask)))
                stale_end = min(s.t[after[0]], t0 + HORIZON_MS) if len(after) else t0 + HORIZON_MS
                g = tr_by_tok.get(tok)
                buys = (g[(g.t > t0 - w_ms) & (g.t <= stale_end) & (g.side == "BUY") & (g.price <= a0 + 1e-9)]
                        if g is not None else trades.iloc[0:0])
                go = tr_by_tok.get(sibling.get(tok))
                sells = (go[(go.t > t0 - w_ms) & (go.t <= stale_end) & (go.side == "SELL")]
                         if go is not None else trades.iloc[0:0])
                snipe_ts = sorted(list(buys.t) + list(sells.t))
                first_snipe = snipe_ts[0] - t0 if snipe_ts else np.nan
                mids = {h: s.mid(t0 + h) for h in MARKOUT_MS}
                fee = FEE_COEF * a0 * (1 - a0)
                row = {"asset": asset, "slug": m["slug"], "token": tok, "outcome": m["outcome"],
                       "duration": m["duration"], "t0": t0, "move_bps": move, "tte_sec": m["end_ts"] - t0 / 1000,
                       "a0": a0, "q0": q0, "pm_led": pm_led, "stale_ms": stale_end - t0,
                       "n_snipes": len(snipe_ts), "first_snipe_ms": first_snipe, "fee": fee}
                for h, v in mids.items():
                    row[f"net_{h // 1000}s"] = (v - a0 - fee) if v is not None else np.nan
                for L in l_grid:
                    arrive = t0 + L                                      # engine time of our order
                    level_alive = arrive < stale_end - d_pm or stale_end - t0 >= HORIZON_MS
                    ahead = float(buys[(buys.t - d_pm) < arrive]["size"].sum())
                    row[f"cap_{L}"] = bool(level_alive and q0 - ahead >= SIZE)
                rows.append(row)
    return pd.DataFrame(rows)


def fetch_outcomes(slugs) -> dict:
    out = {}
    for slug in slugs:
        try:
            req = urllib.request.Request("https://gamma-api.polymarket.com/markets?slug=" + slug,
                                         headers={"User-Agent": "analyze-snipe/1.0"})
            m = json.loads(urllib.request.urlopen(req, timeout=10).read())[0]
            prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
            names = json.loads(m.get("outcomes") or "[]")
            if m.get("closed") and prices and max(prices) == 1.0:
                out[slug] = names[prices.index(1.0)]
        except Exception:
            continue
        time.sleep(0.05)
    return out


def boot_lb(x: np.ndarray, clusters: np.ndarray, seed: int = 20261017, n: int = 5000) -> float:
    """One-sided 95% lower bound of the mean, resampling clusters (market slugs)."""
    if len(x) < 2:
        return float("nan")
    keys = pd.unique(clusters)
    sums = pd.Series(x).groupby(clusters).sum().reindex(keys).to_numpy()
    cnts = pd.Series(x).groupby(clusters).size().reindex(keys).to_numpy()
    idx = np.random.default_rng(seed).integers(0, len(keys), size=(n, len(keys)))
    return float(np.quantile(sums[idx].sum(1) / cnts[idx].sum(1), 0.05))


def summarize(ev: pd.DataFrame, hours: float, our_l: float) -> dict:
    out = {}
    if ev.empty:
        return out
    for j in sorted(ev.j_bps.unique()):
        e = ev[ev.j_bps == j]
        r = {"events": len(e), "events_per_day": round(len(e) / hours * 24, 1),
             "pm_led_pct": round(100 * e.pm_led.mean(), 1),
             "stale_ms_p50": round(e.stale_ms.median()), "stale_ms_p90": round(e.stale_ms.quantile(.9)),
             "sniped_pct": round(100 * (e.n_snipes > 0).mean(), 1),
             "first_snipe_ms_p10_p50_p90": [round(e.first_snipe_ms.quantile(q), 1) for q in (.1, .5, .9)],
             "a0_p50": round(e.a0.median(), 3), "q0_p50": round(e.q0.median(), 1), "by_latency": {}}
        for L in L_GRID_MS:
            c = e[e[f"cap_{L}"]]
            rr = {"captured": len(c), "per_day": round(len(c) / hours * 24, 1)}
            for col in [x for x in e.columns if x.startswith("net_")]:
                v = c[col].dropna()
                rr[col] = round(v.mean(), 4) if len(v) else None
                if col == "net_60s" and len(v):
                    rr["net_60s_lb95"] = round(boot_lb(v.to_numpy(), c.loc[v.index, "slug"].to_numpy()), 4)
            if "net_settle" in c and c.net_settle.notna().any():
                rr["usdc_per_day_settle"] = round(c.net_settle.sum() * SIZE / hours * 24, 2)
            r["by_latency"][L] = rr
        out[f"J={j:g}bps"] = r
    # Pre-stated reading (README): primary J, our latency rounded up to the grid.
    e = ev[ev.j_bps == PRIMARY_J_BPS]
    L = min([x for x in L_GRID_MS if x >= our_l] or [L_GRID_MS[-1]])
    c = e[e[f"cap_{L}"]] if len(e) else e
    med_snipe = float(e.first_snipe_ms.median()) if len(e) and e.first_snipe_ms.notna().any() else float("nan")
    v = c["net_60s"].dropna() if len(c) else pd.Series(dtype=float)
    lb = boot_lb(v.to_numpy(), c.loc[v.index, "slug"].to_numpy()) if len(v) >= 2 else float("nan")
    if len(v) and (v.mean() <= 0 or (not np.isnan(med_snipe) and med_snipe < L)):
        verdict = "CLOSE"
    elif len(v) / hours * 24 >= 30 and lb > 0:
        verdict = "WRITE_PREREG"
    else:
        verdict = "INCONCLUSIVE"
    out["reading"] = {"primary_J_bps": PRIMARY_J_BPS, "our_latency_ms_grid": L, "median_first_snipe_ms": med_snipe,
                      "captured_per_day": round(len(v) / hours * 24, 1), "net_60s_mean": round(v.mean(), 4) if len(v) else None,
                      "net_60s_lb95": round(lb, 4) if not np.isnan(lb) else None, "verdict": verdict}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("tape")
    ap.add_argument("--rtt-ms", type=float, required=True, help="POST /order p50 from clob_rtt_probe.py on this VM")
    ap.add_argument("--d-pm-ms", type=float, default=None, help="override PM one-way delay (default: measured)")
    ap.add_argument("--fetch-outcomes", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    tape = Path(a.tape)
    meta = load_markets(tape)
    bn, bn_lat = load_binance(tape)
    best, trades, pm_lat = replay_pm(tape, meta)
    d_meas = float(np.median(pm_lat)) if pm_lat else float("nan")
    clock_ok = 0.0 <= d_meas <= a.rtt_ms                               # NTP-synced clocks give 0 < one-way < RTT
    d_pm = a.d_pm_ms if a.d_pm_ms is not None else (d_meas if clock_ok else a.rtt_ms / 2)
    our_l = a.rtt_ms / 2 + 2.0                                          # one-way to engine + decision
    t_lo = max(df.t.min() for df in bn.values())
    t_hi = min(df.t.max() for df in bn.values())
    hours = (t_hi - t_lo) / 3.6e6
    evs = []
    for j in J_GRID_BPS:
        e = evaluate({asset: find_jumps(df, j) for asset, df in bn.items()}, best, trades, meta, d_pm)
        if len(e):
            evs.append(e.assign(j_bps=j))
    ev = pd.concat(evs, ignore_index=True) if evs else pd.DataFrame()
    if a.fetch_outcomes and len(ev):
        oc = fetch_outcomes(sorted(ev.slug.unique()))
        won = ev.apply(lambda r: (oc.get(r.slug) or "").lower() == r.outcome.lower() if r.slug in oc else np.nan, axis=1)
        ev["net_settle"] = won.astype(float) - ev.a0 - ev.fee
    info = {"tape_hours": round(hours, 2), "rtt_ms": a.rtt_ms, "our_one_way_ms": our_l,
            "d_pm_ms_measured": round(d_meas, 1), "clock_ok": clock_ok, "d_pm_ms_used": round(d_pm, 1), "binance_recv_minus_T_ms_p50": round(float(np.median(bn_lat)), 1) if bn_lat else None,
            "pm_trades": len(trades), "pm_fee_rate_bps_seen": trades.fee_rate_bps.value_counts().head(5).to_dict()}
    bn_tr = load_binance_trades(tape)
    info["server_time_reaction"] = {f"J={j:g}bps": server_reaction(bn_tr, trades, meta, j) for j in (PRIMARY_J_BPS, 5.0)}
    # Location-free capture: book on PM server clock, jumps on Binance exchange clock, our order reaches the
    # engine D ms after the Binance print (D = Binance->us + us->engine; >= ~100 ms between Tokyo and Europe).
    best_s, trades_s, _ = replay_pm(tape, meta, clock="server")
    sv = []
    for j in J_GRID_BPS:
        e = evaluate({asset: find_jumps(df, j) for asset, df in bn_tr.items()}, best_s, trades_s, meta, 0.0,
                     l_grid=SERVER_D_GRID_MS)
        if len(e):
            sv.append(e.assign(j_bps=j))
    sev = pd.concat(sv, ignore_index=True) if sv else pd.DataFrame()
    info["server_clock_capture"] = {
        f"J={j:g}bps": {"events": int((sev.j_bps == j).sum()), "pm_led_pct": round(100 * sev[sev.j_bps == j].pm_led.mean(), 1),
                        "stale_ms_p50": round(sev[sev.j_bps == j].stale_ms.median()),
                        "by_D_ms": {D: {"captured": int(sev[(sev.j_bps == j) & sev[f"cap_{D}"]].shape[0]),
                                        "net_5s": round(sev[(sev.j_bps == j) & sev[f"cap_{D}"]].net_5s.mean(), 4),
                                        "net_60s": round(sev[(sev.j_bps == j) & sev[f"cap_{D}"]].net_60s.mean(), 4)}
                                    for D in SERVER_D_GRID_MS}}
        for j in sorted(sev.j_bps.unique())} if len(sev) else {}
    res = {"info": info, "summary": summarize(ev, hours, our_l)}
    print(json.dumps(res, indent=1, default=str))
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1, default=str))
        ev.to_csv(Path(a.out).with_suffix(".events.csv"), index=False)


if __name__ == "__main__":
    main()
