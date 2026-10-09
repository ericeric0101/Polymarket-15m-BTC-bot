#!/usr/bin/env python3
"""Stage 5 offline: market calibration, stop-vs-hold replay, STOP_ALPHA vs STOP_RISK_CONTROL.

Replays DRY-RUN shadow positions through the same StopCandidateShadow used at runtime, feeding tier P
path snapshots (TWAP, strike, held-token bid/ask). Official outcomes from the provenance CSV.
STOP_ALPHA = does stopping improve mean PnL? STOP_RISK_CONTROL = does it reduce worst loss / drawdown?
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import random
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.research.stop_shadow import StopCandidateShadow  # noqa: E402

HORIZONS = (600, 300, 180, 120, 60)
BINS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01))


def wilson(k, n, z=1.959964):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 4), round(c + h, 4)]


def day_boot(rows, stat, B=2000, seed=11):
    days = sorted({r[0] for r in rows})
    if len(days) < 3:
        return None
    by = defaultdict(list)
    for r in rows:
        by[r[0]].append(r)
    rng, vals = random.Random(seed), []
    for _ in range(B):
        vals.append(stat([r for d in (rng.choice(days) for _ in days) for r in by[d]]))
    vals.sort()
    return [round(vals[int(.025 * B)], 4), round(vals[int(.975 * B)], 4)]


def load_paths(export_root):
    paths = {}
    for path in glob.glob(str(export_root / "P_paths" / "*" / "paths_*.parquet")):
        prov = "NATIVE_V2" if "native" in Path(path).name else "HISTORICAL_PRE_V2"
        t = pq.read_table(path)
        if t.num_rows == 0 or "market_slug" not in t.column_names:
            continue
        cols = [c for c in ("market_slug", "snapshot_ts", "official_twap", "strike", "twap_fresh", "best_bid_up", "best_ask_up",
                            "best_bid_down", "best_ask_down", "market_mid_up", "market_mid_fresh", "required_move_sigma",
                            "required_move_z_diffusion", "p_up_ex_market", "side_score", "twap_recomputation_class") if c in t.column_names]
        for r in t.select(cols).to_pylist():
            if prov == "HISTORICAL_PRE_V2" and not str(r.get("twap_recomputation_class") or "").startswith("FRESH"):
                continue
            paths.setdefault((prov, r["market_slug"]), []).append(r)
    for v in paths.values():
        v.sort(key=lambda r: float(r["snapshot_ts"]))
    return paths


def calibration(paths, official):
    out = []
    for prov in ("NATIVE_V2", "HISTORICAL_PRE_V2"):
        for h in HORIZONS:
            cells = defaultdict(list)
            for (p, slug), series in paths.items():
                if p != prov or slug not in official:
                    continue
                start = int(slug.rsplit("-", 1)[-1])
                target = start + 900 - h
                cand = [r for r in series if target - 5 <= float(r["snapshot_ts"]) <= target and r.get("market_mid_up") is not None
                        and str(r.get("market_mid_fresh")).lower() not in ("false", "0")]
                if not cand:
                    continue
                mid = float(cand[-1]["market_mid_up"])  # one token (UP) per market: no complementary double count
                for lo, hi in BINS:
                    if lo <= mid < hi:
                        cells[(lo, hi)].append((datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%d"), mid,
                                                official[slug] == "UP"))
            for (lo, hi), sel in sorted(cells.items()):
                n, k = len(sel), sum(1 for s in sel if s[2])
                cell = {"provenance": prov, "tte_sec": h, "price_bin": f"[{lo},{min(hi, 1.0)})", "n_markets": n,
                        "n_days": len({s[0] for s in sel}), "mean_implied": round(st.mean(s[1] for s in sel), 4),
                        "observed_win": round(k / n, 4), "win_minus_implied": round(k / n - st.mean(s[1] for s in sel), 4)}
                if cell["n_days"] >= 3:
                    cell["wilson95_exploratory"] = wilson(k, n)
                    cell["day_boot95_win_minus_implied"] = day_boot(sel, lambda rs: sum(r[2] for r in rs) / len(rs) - st.mean(r[1] for r in rs))
                out.append(cell)
    return out


def replay(paths, official, sims_csv, journal, hard_loss_usdc, hard_loss_min_hold):
    conn = sqlite3.connect(f"file:{Path(journal).resolve()}?mode=ro", uri=True, timeout=30)
    fills = {}
    for (pj,) in conn.execute("SELECT payload_json FROM order_events WHERE event_type='SHADOW_SIM_ENTRY_FILLED'"):
        d = json.loads(pj or "{}")
        fills[d.get("slug")] = d
    conn.close()
    positions = []
    for r in csv.DictReader(open(sims_csv)):
        f = fills.get(r["slug"])
        series = paths.get(("NATIVE_V2", r["slug"])) or paths.get(("HISTORICAL_PRE_V2", r["slug"]))
        if not f or not series or r["slug"] not in official:
            continue
        fill_ts = float(f.get("filled_ts") or f.get("created_ts"))
        pos = {"simulation_id": f.get("simulation_id"), "slug": r["slug"], "side": r["side"], "entry_price": float(r["price"]),
               "qty": float(f["qty"]), "filled_ts": fill_ts}
        shadow = StopCandidateShadow(db=None, run_id="replay", hard_loss_usdc=hard_loss_usdc, hard_loss_min_hold_sec=hard_loss_min_hold)
        end = int(r["slug"].rsplit("-", 1)[-1]) + 900
        side_l = r["side"].lower()
        for snap in series:
            ts = float(snap["snapshot_ts"])
            if ts < fill_ts or snap.get(f"best_bid_{side_l}") is None or str(snap.get("twap_fresh")).lower() in ("false", "0"):
                continue
            shadow.observe(position=pos, bid=snap[f"best_bid_{side_l}"], ask=snap.get(f"best_ask_{side_l}"), now_ts=ts,
                           twap=snap.get("official_twap"), strike=snap.get("strike"), time_left_sec=end - ts,
                           score=snap.get("side_score"), legacy_sigma=snap.get("required_move_sigma"),
                           z_diffusion=snap.get("required_move_z_diffusion"))
        resolved = shadow.resolve(slug=r["slug"], outcome=official[r["slug"]], settlement_ts=end)
        hold = pos["qty"] * ((1.0 if official[r["slug"]] == r["side"] else 0.0) - pos["entry_price"])
        positions.append({"slug": r["slug"], "day": r["day"], "ts": end, "hold_pnl": hold,
                          "first_by_reason": {x["stop_reason"]: x for x in sorted(resolved, key=lambda x: x["candidate_ts"])}})
    return positions


def policy_eval(positions, reason):
    """Apply 'stop at the first candidate of this reason' to every replayed position."""
    rows = []
    for p in positions:
        c = p["first_by_reason"].get(reason)
        rows.append((p["day"], c["pnl_if_stop_now_usdc"] if c else p["hold_pnl"], p["hold_pnl"], c is not None, p["ts"]))
    def dd(values):
        peak = cum = worst = 0.0
        for v in values:
            cum += v
            peak = max(peak, cum)
            worst = max(worst, peak - cum)
        return round(worst, 3)
    ordered = sorted(rows, key=lambda r: r[4])
    triggered = [r for r in rows if r[3]]
    return {"stop_reason": reason, "n_positions": len(rows), "n_days": len({r[0] for r in rows}),
            "n_triggered": len(triggered), "triggered_where_hold_won": sum(1 for r in triggered if r[2] > 0),
            "mean_pnl_policy": round(st.mean(r[1] for r in rows), 4), "mean_pnl_hold": round(st.mean(r[2] for r in rows), 4),
            "mean_diff_policy_minus_hold": round(st.mean(r[1] - r[2] for r in rows), 4),
            "day_boot95_mean_diff": day_boot(rows, lambda rs: st.mean(r[1] - r[2] for r in rs)),
            "worst_position_policy": round(min(r[1] for r in rows), 3), "worst_position_hold": round(min(r[2] for r in rows), 3),
            "max_drawdown_policy": dd([r[1] for r in ordered]), "max_drawdown_hold": dd([r[2] for r in ordered]),
            "stdev_policy": round(st.pstdev(r[1] for r in rows), 4), "stdev_hold": round(st.pstdev(r[2] for r in rows), 4)}


def effective_breakers():
    from bot.runtime_env import load_runtime_env
    from bot.app_config import AppConfig
    load_runtime_env()
    exit_cfg = AppConfig.from_env(enable_terminal_dashboard=False).exit
    keys = ("stop_loss_enabled", "absolute_max_loss_enabled", "absolute_max_loss_usdc", "absolute_max_loss_min_hold_sec",
            "catastrophic_stop_loss_enabled", "catastrophic_stop_loss_usdc", "catastrophic_stop_loss_confirmations",
            "maker_urgent_exit_enabled", "taker_exit_enabled", "hold_to_redeem_enabled")
    return {k: str(getattr(exit_cfg, k)) for k in keys if hasattr(exit_cfg, k)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--export", default="data/research_export")
    ap.add_argument("--provenance", required=True)
    ap.add_argument("--sims", required=True)
    ap.add_argument("--journal", default="logs/trade_journal.db")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    official = {r["market_slug"]: r["outcome_used"] for r in csv.DictReader(open(a.provenance)) if r["outcome_used"] in ("UP", "DOWN")}
    breakers = effective_breakers()
    paths = load_paths(Path(a.export))
    res = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "effective_exit_config": breakers,
           "calibration": calibration(paths, official)}
    positions = replay(paths, official, a.sims, a.journal, float(breakers.get("absolute_max_loss_usdc", 2.0)),
                       float(breakers.get("absolute_max_loss_min_hold_sec", 60)))
    res["replay_positions"] = len(positions)
    res["policies"] = [policy_eval(positions, reason) for reason in
                       ("ADVERSE_CROSS", "ADVERSE_CROSS_PERSIST_15S", "HARD_LOSS_EQUIVALENT")]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "stop_analysis.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({"replay_positions": len(positions), "calibration_cells": len(res["calibration"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
