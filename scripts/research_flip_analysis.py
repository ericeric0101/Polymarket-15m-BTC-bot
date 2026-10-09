#!/usr/bin/env python3
"""Stage 4: flip / reversal / crossing statistics and diffusion-z evaluation (offline).

Inputs: tier P paths (native-v2 and historical pre-v2 kept separate; historical rows require a FRESH_*
TWAP recomputation class), tier A summaries (MARKET_TWAP_SUMMARY counts), the outcome provenance CSV
(official labels), and DRY-RUN shadow entries (stage-2 table) for post-entry movement.

Definitions (kept distinct):
  temporary_cross        : sign(official TWAP - strike) changes at least once after the cutoff
  projected_side_change  : MARKET_TWAP_SUMMARY.projected_cross_count (whole market; model-projected side)
  final_flip             : leader at cutoff != official settlement
  cross_then_revert      : temporary_cross after cutoff AND no final_flip
  adverse_post_entry     : after a DRY-RUN fill, TWAP crosses to the side against the position
  final_adverse_outcome  : the DRY-RUN position lost at official settlement
Missing path data is NOT "no crossing": rows whose post-cutoff path has a gap > MAX_GAP are reported
as incomplete and excluded from crossing statistics (final_flip needs only the cutoff + official label).
"""
from __future__ import annotations

import argparse
import csv
import glob
import itertools
import json
import math
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

CUTOFFS = (300, 180, 120, 60)
MAX_GAP = 10.0
PHASE_A = datetime(2026, 10, 7, 2, 2, tzinfo=timezone.utc).timestamp()
PHASE_B = datetime(2026, 10, 7, 14, 43, tzinfo=timezone.utc).timestamp()
SIGMA_BINS = (0, .5, 1, 2, 4)
Z_BINS = (0, .5, 1, 1.5, 2, 3)


def wilson(k, n, z=1.959964):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 4), round(c + h, 4)]


def bucket(v, edges):
    if v is None:
        return "NA"
    for lo, hi in zip(edges, edges[1:]):
        if lo <= v < hi:
            return f"[{lo},{hi})"
    return f">={edges[-1]}"


def phase(ts):
    return "PRE_A" if ts < PHASE_A else "POST_A" if ts < PHASE_B else "POST_B"


def rate_cell(rows, key):
    days = {r["day"] for r in rows}
    k = sum(1 for r in rows if r[key])
    cell = {"n_markets": len(rows), "n_days": len(days), "rate": round(k / len(rows), 4) if rows else None}
    if len(days) >= 3:
        cell["wilson95_exploratory"] = wilson(k, len(rows))
    else:
        cell["note"] = "descriptive only (<3 days)"
    return cell


def load_paths(export_root: Path):
    paths = {}
    for path in glob.glob(str(export_root / "P_paths" / "*" / "paths_*.parquet")):
        prov = "NATIVE_V2" if "native" in Path(path).name else "HISTORICAL_PRE_V2"
        t = pq.read_table(path)
        if t.num_rows == 0 or "market_slug" not in t.column_names:
            continue
        cols = [c for c in ("market_slug", "snapshot_ts", "official_twap", "strike", "twap_fresh", "required_move_sigma",
                            "required_move_z_diffusion", "p_terminal_flip_diffusion", "twap_recomputation_class")
                if c in t.column_names]
        for r in t.select(cols).to_pylist():
            if r.get("official_twap") is None or r.get("strike") is None:
                continue
            if str(r.get("twap_fresh")).lower() in ("false", "0"):
                continue
            if prov == "HISTORICAL_PRE_V2" and not str(r.get("twap_recomputation_class") or "").startswith("FRESH"):
                continue
            paths.setdefault((prov, r["market_slug"]), []).append(r)
    for v in paths.values():
        v.sort(key=lambda r: float(r["snapshot_ts"]))
    return paths


def cutoff_rows(paths, official, summaries):
    rows = []
    for (prov, slug), series in paths.items():
        if slug not in official:
            continue
        start = int(slug.rsplit("-", 1)[-1])
        end = start + 900
        for k in CUTOFFS:
            target = end - k
            before = [r for r in series if target - 5 <= float(r["snapshot_ts"]) <= target]
            if not before:
                continue
            snap = before[-1]
            diff = float(snap["official_twap"]) - float(snap["strike"])
            if diff == 0:
                continue
            leader = "UP" if diff > 0 else "DOWN"
            after = [r for r in series if float(r["snapshot_ts"]) > float(snap["snapshot_ts"])]
            times = [float(snap["snapshot_ts"])] + [float(r["snapshot_ts"]) for r in after]
            gaps = [b - a for a, b in zip(times, times[1:])] + [end - times[-1]]
            complete = max(gaps) <= MAX_GAP
            signs = [1 if float(r["official_twap"]) > float(r["strike"]) else -1 if float(r["official_twap"]) < float(r["strike"]) else 0
                     for r in after]
            lead_sign = 1 if leader == "UP" else -1
            crosses = sum(1 for a, b in zip([lead_sign] + signs, signs) if a and b and a != b)
            temp_cross = any(s == -lead_sign for s in signs)
            flip = official[slug] != leader
            rows.append({"provenance": prov, "slug": slug, "day": datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%d"),
                         "weekend": datetime.fromtimestamp(start, timezone.utc).weekday() >= 5, "phase": phase(start),
                         "cutoff": k, "leader": leader, "final_flip": flip, "path_complete": complete,
                         "temporary_cross": temp_cross if complete else None, "cross_count_after": crosses if complete else None,
                         "cross_then_revert": (temp_cross and not flip) if complete else None,
                         # required_move_sigma: legacy HEURISTIC (TTE-dependent decay/floor), not a diffusion z-score
                         "legacy_sigma": snap.get("required_move_sigma"), "z_diffusion": snap.get("required_move_z_diffusion"),
                         "p_flip_diffusion": snap.get("p_terminal_flip_diffusion"),
                         "distance_bps": abs(diff) / float(snap["strike"]) * 1e4,
                         "summary_twap_cross_count": (summaries.get(slug) or {}).get("twap_cross_count"),
                         "summary_projected_cross_count": (summaries.get(slug) or {}).get("projected_cross_count")})
    return rows


def weekday_weekend(rows, prov, k):
    sel = [r for r in rows if r["provenance"] == prov and r["cutoff"] == k]
    by = defaultdict(lambda: [0, 0])
    wk = {}
    for r in sel:
        by[r["day"]][0] += r["final_flip"]
        by[r["day"]][1] += 1
        wk[r["day"]] = r["weekend"]
    days = sorted(by)
    weekend_days = [d for d in days if wk[d]]
    weekday_days = [d for d in days if not wk[d]]
    out = {"per_day_flip_rate": {d: {"flips": by[d][0], "n_markets": by[d][1], "weekend": wk[d]} for d in days},
           "n_weekend_days": len(weekend_days), "n_weekday_days": len(weekday_days)}
    if weekend_days and weekday_days:
        rates = {d: by[d][0] / by[d][1] for d in days}
        pairs = [(a, b) for a in weekday_days for b in weekend_days]
        out["weekday_day_gt_weekend_day_pairs"] = f"{sum(rates[a] > rates[b] for a, b in pairs)}/{len(pairs)}"
        n_perm = math.comb(len(days), len(weekend_days))
        out["distinct_day_permutations"] = n_perm
        out["min_attainable_p"] = round(1 / n_perm, 4)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--export", default="data/research_export")
    ap.add_argument("--provenance", required=True)
    ap.add_argument("--sims", required=True, help="stage-2 shadow_sim_markets.csv (official labels)")
    ap.add_argument("--journal", default="logs/trade_journal.db")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    official = {r["market_slug"]: r["outcome_used"] for r in csv.DictReader(open(a.provenance)) if r["outcome_used"] in ("UP", "DOWN")}
    summaries = {}
    for path in glob.glob(str(Path(a.export) / "A_market_summary" / "*.csv")):
        for r in csv.DictReader(open(path)):
            summaries[r["market_slug"]] = {"twap_cross_count": float(r["twap_cross_count"]) if r["twap_cross_count"] else None,
                                           "projected_cross_count": float(r["projected_cross_count"]) if r["projected_cross_count"] else None}
    paths = load_paths(Path(a.export))
    rows = cutoff_rows(paths, official, summaries)
    res = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "definitions": __doc__, "cells": {}}
    for prov in ("NATIVE_V2", "HISTORICAL_PRE_V2"):
        for k in CUTOFFS:
            sel = [r for r in rows if r["provenance"] == prov and r["cutoff"] == k]
            comp = [r for r in sel if r["path_complete"]]
            key = f"{prov}|T-{k}"
            res["cells"][key] = {
                "final_flip": rate_cell(sel, "final_flip"),
                "path_complete_share": round(len(comp) / len(sel), 4) if sel else None,
                "temporary_cross": rate_cell(comp, "temporary_cross") if comp else None,
                "cross_then_revert": rate_cell(comp, "cross_then_revert") if comp else None,
                "by_phase": {ph: rate_cell([r for r in sel if r["phase"] == ph], "final_flip")
                             for ph in ("PRE_A", "POST_A", "POST_B") if any(r["phase"] == ph for r in sel)},
                "by_legacy_sigma": {b: rate_cell([r for r in sel if bucket(r["legacy_sigma"], SIGMA_BINS) == b], "final_flip")
                                    for b in sorted({bucket(r["legacy_sigma"], SIGMA_BINS) for r in sel})},
                "by_distance_bps": {b: rate_cell([r for r in sel if bucket(r["distance_bps"], (0, 2, 5, 10, 20)) == b], "final_flip")
                                    for b in sorted({bucket(r["distance_bps"], (0, 2, 5, 10, 20)) for r in sel})},
            }
            # Selection: are incomplete-path rows different on outcome-related observables?
            inc = [r for r in sel if not r["path_complete"]]
            def summ(group):
                cc = [r["summary_twap_cross_count"] for r in group if r["summary_twap_cross_count"] is not None]
                return {"n": len(group), "final_flip_rate": round(sum(r["final_flip"] for r in group) / len(group), 4) if group else None,
                        "mean_summary_twap_crosses": round(st.mean(cc), 3) if cc else None}
            res["cells"][key]["selection_complete_vs_incomplete"] = {"complete": summ(comp), "incomplete": summ(inc)}
    # diffusion z (native only): final flip, cross before expiry, cross then revert, vs Phi(-z)
    zrows = [r for r in rows if r["provenance"] == "NATIVE_V2" and r["z_diffusion"] is not None]
    res["z_diffusion"] = {"n_rows": len(zrows), "n_markets": len({r["slug"] for r in zrows}),
                          "days": sorted({r["day"] for r in zrows}), "by_bin": {}}
    for b in sorted({bucket(r["z_diffusion"], Z_BINS) for r in zrows}):
        sel = [r for r in zrows if bucket(r["z_diffusion"], Z_BINS) == b]
        comp = [r for r in sel if r["path_complete"]]
        res["z_diffusion"]["by_bin"][b] = {
            "final_flip": rate_cell(sel, "final_flip"),
            "mean_p_terminal_flip_diffusion": round(st.mean(r["p_flip_diffusion"] for r in sel if r["p_flip_diffusion"] is not None), 4)
            if any(r["p_flip_diffusion"] is not None for r in sel) else None,
            "cross_before_expiry": rate_cell(comp, "temporary_cross") if comp else None,
            "cross_then_revert": rate_cell(comp, "cross_then_revert") if comp else None}
    res["weekday_weekend"] = {f"{prov}|T-{k}": weekday_weekend(rows, prov, k)
                              for prov in ("NATIVE_V2", "HISTORICAL_PRE_V2") for k in (300, 180, 120)}
    # projected-side changes (summary-level, complete for every market with a summary)
    proj = [(slug, v["projected_cross_count"], v["twap_cross_count"]) for slug, v in summaries.items()
            if v["projected_cross_count"] is not None and slug in official]
    res["projected_side_change"] = {"n_markets": len(proj),
                                    "mean_projected_changes": round(st.mean(p for _, p, _ in proj), 2) if proj else None,
                                    "mean_twap_crosses": round(st.mean(c for _, _, c in proj if c is not None), 2) if proj else None}
    # adverse post-entry movement for DRY-RUN shadow entries
    import sqlite3
    conn = sqlite3.connect(f"file:{Path(a.journal).resolve()}?mode=ro", uri=True, timeout=30)
    fills = {}
    for (pj,) in conn.execute("SELECT payload_json FROM order_events WHERE event_type='SHADOW_SIM_ENTRY_FILLED'"):
        d = json.loads(pj or "{}")
        fills[d.get("slug")] = d
    conn.close()
    entries = []
    for r in csv.DictReader(open(a.sims)):
        f = fills.get(r["slug"], {})
        fill_ts = f.get("filled_ts") or f.get("created_ts")
        series = paths.get(("NATIVE_V2", r["slug"])) or paths.get(("HISTORICAL_PRE_V2", r["slug"]))
        if not fill_ts or not series:
            continue
        after = [x for x in series if float(x["snapshot_ts"]) >= float(fill_ts)]
        if not after:
            continue
        end = int(r["slug"].rsplit("-", 1)[-1]) + 900
        times = [float(fill_ts)] + [float(x["snapshot_ts"]) for x in after]
        complete = max([b - a for a, b in zip(times, times[1:])] + [end - times[-1]]) <= MAX_GAP
        side_sign = 1 if r["side"] == "UP" else -1
        adverse = any((float(x["official_twap"]) - float(x["strike"])) * side_sign < 0 for x in after)
        entries.append({"day": r["day"], "adverse_post_entry": adverse, "final_adverse_outcome": r["won"] != "True",
                        "complete": complete})
    comp = [e for e in entries if e["complete"]]
    res["post_entry"] = {
        "n_entries_with_path": len(entries), "n_complete": len(comp),
        "adverse_post_entry": rate_cell(comp, "adverse_post_entry") if comp else None,
        "final_adverse_given_adverse_move": rate_cell([e for e in comp if e["adverse_post_entry"]], "final_adverse_outcome")
        if any(e["adverse_post_entry"] for e in comp) else None,
        "final_adverse_given_no_adverse_move": rate_cell([e for e in comp if not e["adverse_post_entry"]], "final_adverse_outcome")
        if any(not e["adverse_post_entry"] for e in comp) else None}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "flip_analysis.json").write_text(json.dumps(res, indent=1, default=str))
    with (out / "flip_cutoff_rows.csv").open("w", newline="") as h:
        w = csv.DictWriter(h, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(json.dumps({"cutoff_rows": len(rows), "z_rows": len(zrows), "entries": len(entries)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
