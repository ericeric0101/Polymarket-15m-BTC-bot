#!/usr/bin/env python3
"""Stage 3 entry analyses under the frozen decision rules (offline, read-only inputs).

Candidates (FROZEN, not searched): |score| >= 0.30, entry price >= 0.75, first-entry TTE <= 480 s.
Primary metric: mean PnL per trade; secondary: edge = win - price. Unit: market; intervals: day-blocked.

  python3 scripts/research_entry_analysis.py --tables <stage2_official>/tables --out <dir>
"""
from __future__ import annotations

import argparse
import bisect
import csv
import glob
import json
import random
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

SCORE_MIN, PRICE_MIN, TTE_MAX = 0.30, 0.75, 480.0
SEED, BOOT = 11, 2000


def num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- datasets
def live_rows(tables: Path):
    out = []
    for r in csv.DictReader((tables / "live_positions.csv").open()):
        out.append({"cohort": "LIVE", "slug": r["slug"], "day": r["day"], "ts": int(r["slug"].rsplit("-", 1)[-1]),
                    "pnl": num(r["pnl"]), "won": r["won"] == "True", "price": num(r["price"]),
                    "score": abs(num(r["side_score"])) if num(r["side_score"]) is not None else None,
                    "tte": num(r["time_left_sec"]), "distance": num(r["signed_spot_distance"]),
                    "sigma": None})
    return out


def _path_index(export_root: Path):
    idx = defaultdict(list)
    for path in glob.glob(str(export_root / "P_paths" / "*" / "paths_*.parquet")):
        t = pq.read_table(path)
        if t.num_rows == 0 or "required_move_sigma" not in t.column_names:
            continue
        for r in t.select(["market_slug", "snapshot_ts", "required_move_sigma"]).to_pylist():
            if r["required_move_sigma"] is not None:
                idx[r["market_slug"]].append((float(r["snapshot_ts"]), float(r["required_move_sigma"])))
    for v in idx.values():
        v.sort()
    return idx


def dry_rows(tables: Path, journal: Path, export_root: Path):
    conn = sqlite3.connect(f"file:{journal.resolve()}?mode=ro", uri=True, timeout=30)
    candidates, filled = defaultdict(list), {}
    for et, pj in conn.execute("SELECT event_type, payload_json FROM order_events WHERE event_type IN "
                               "('SHADOW_SIM_ENTRY_CANDIDATE','SHADOW_SIM_ENTRY_FILLED') ORDER BY id"):
        d = json.loads(pj or "{}")
        if et == "SHADOW_SIM_ENTRY_CANDIDATE":
            candidates[d.get("slug")].append(d)
        else:
            filled[d.get("slug")] = d
    conn.close()
    paths = _path_index(export_root)
    out = []
    for r in csv.DictReader((tables / "shadow_sim_markets.csv").open()):
        slug = r["slug"]
        f = filled.get(slug, {})
        fill_ts = num(f.get("filled_ts")) or num(f.get("created_ts"))
        cands = [c for c in candidates.get(slug, []) if fill_ts is None or (num(c.get("created_ts")) or 0) <= fill_ts]
        cand = cands[-1] if cands else (candidates.get(slug) or [{}])[-1]
        start = int(slug.rsplit("-", 1)[-1])
        sigma = None
        if fill_ts is not None and slug in paths:
            series = paths[slug]
            i = bisect.bisect_right(series, (fill_ts, float("inf"))) - 1
            if i >= 0 and fill_ts - series[i][0] <= 5.0:
                sigma = series[i][1]
        out.append({"cohort": "DRY_RUN", "slug": slug, "day": r["day"], "ts": start, "pnl": num(r["pnl"]),
                    "won": r["won"] == "True", "price": num(r["price"]),
                    "score": abs(num(cand.get("side_score"))) if num(cand.get("side_score")) is not None else None,
                    "tte": (start + 900 - fill_ts) if fill_ts is not None else None,
                    "distance": None, "sigma": sigma})
    return out


# ---------------------------------------------------------------- statistics
def describe(rows):
    if not rows:
        return {"n_markets": 0, "n_days": 0}
    pnl = [r["pnl"] for r in rows]
    w = sum(r["won"] for r in rows)
    return {"n_markets": len(rows), "n_days": len({r["day"] for r in rows}),
            "mean_pnl": round(st.mean(pnl), 4), "median_pnl": round(st.median(pnl), 4),
            "total_pnl": round(sum(pnl), 3), "win_rate": round(w / len(rows), 4),
            "edge_win_minus_price": round(w / len(rows) - st.mean(r["price"] for r in rows), 4),
            "max_drawdown": max_drawdown(rows)}


def max_drawdown(rows):
    peak = cum = dd = 0.0
    for r in sorted(rows, key=lambda r: r["ts"]):
        cum += r["pnl"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return round(dd, 3)


def day_boot_diff(pass_rows, fail_rows):
    days = sorted({r["day"] for r in pass_rows} | {r["day"] for r in fail_rows})
    if len(days) < 3:
        return None
    by_p, by_f = defaultdict(list), defaultdict(list)
    for r in pass_rows:
        by_p[r["day"]].append(r["pnl"])
    for r in fail_rows:
        by_f[r["day"]].append(r["pnl"])
    rng, vals = random.Random(SEED), []
    for _ in range(BOOT):
        sample = [rng.choice(days) for _ in days]
        p = [x for d in sample for x in by_p[d]]
        f = [x for d in sample for x in by_f[d]]
        if p and f:
            vals.append(st.mean(p) - st.mean(f))
    vals.sort()
    return [round(vals[int(.025 * len(vals))], 4), round(vals[int(.975 * len(vals))], 4)] if vals else None


def compare(rows, passes):
    p = [r for r in rows if passes(r)]
    f = [r for r in rows if not passes(r)]
    per_day = {}
    for day in sorted({r["day"] for r in rows}):
        dp = [r for r in p if r["day"] == day]
        df = [r for r in f if r["day"] == day]
        per_day[day] = {"pass": describe(dp), "fail": describe(df),
                        "direction": (None if not dp or not df else
                                      int(np.sign(st.mean(x["pnl"] for x in dp) - st.mean(x["pnl"] for x in df))))}
    dirs = [v["direction"] for v in per_day.values() if v["direction"] is not None]
    raw = (st.mean(x["pnl"] for x in p) - st.mean(x["pnl"] for x in f)) if p and f else None
    return {"pass": describe(p), "fail": describe(f), "raw_mean_pnl_diff": round(raw, 4) if raw is not None else None,
            "day_boot95_diff": day_boot_diff(p, f), "per_day": per_day,
            "days_compared": len(dirs), "days_positive": sum(1 for d in dirs if d > 0),
            "days_negative": sum(1 for d in dirs if d < 0)}


def ols(rows, indicator, controls):
    """PnL ~ 1 + indicator + controls. Returns coefficient of the indicator (+ day-blocked CI if >=3 days)."""
    use = [r for r in rows if all(r.get(c) is not None for c in controls) and r["pnl"] is not None]
    if len(use) < len(controls) + 5:
        return {"n": len(use), "coef": None}

    def fit(sample):
        X = np.array([[1.0, float(indicator(r))] + [float(r[c]) for c in controls] for r in sample])
        y = np.array([r["pnl"] for r in sample])
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        return float(beta[1])

    coef = fit(use)
    days = sorted({r["day"] for r in use})
    ci = None
    if len(days) >= 3:
        by = defaultdict(list)
        for r in use:
            by[r["day"]].append(r)
        rng, vals = random.Random(SEED), []
        for _ in range(BOOT):
            sample = [r for d in (rng.choice(days) for _ in days) for r in by[d]]
            if len({indicator(r) for r in sample}) == 2:
                vals.append(fit(sample))
        vals.sort()
        ci = [round(vals[int(.025 * len(vals))], 4), round(vals[int(.975 * len(vals))], 4)] if vals else None
    return {"n": len(use), "n_days": len(days), "controls": controls, "coef": round(coef, 4), "day_boot95": ci}


def corr(rows, fields):
    use = [r for r in rows if all(r.get(f) is not None for f in fields)]
    if len(use) < 5:
        return {"n": len(use)}
    m = np.corrcoef(np.array([[float(r[f]) for f in fields] for r in use]).T)
    return {"n": len(use), "fields": fields, "matrix": [[round(float(x), 3) for x in row] for row in m]}


def label(result_by_cohort, *, controlled: dict | None = None):
    """Frozen labelling. result_by_cohort: {'LIVE': compare(), 'DRY_RUN': compare()}."""
    live, dry = result_by_cohort.get("LIVE"), result_by_cohort.get("DRY_RUN")
    def direction(c):
        return None if c is None or c["raw_mean_pnl_diff"] is None else np.sign(c["raw_mean_pnl_diff"])
    def every_day(c):
        return c and c["days_compared"] > 0 and c["days_negative"] == 0 and c["days_positive"] == c["days_compared"]
    def majority(c):
        return c and c["days_compared"] > 0 and c["days_positive"] > c["days_compared"] / 2
    if live is None or live["days_compared"] == 0:
        return "UNRESOLVED"
    total_days = live["days_compared"]
    if controlled is not None:
        shrink = controlled.get("LIVE")
        if shrink == "PROXY":
            return "PROXY_FOR_OTHER_FEATURES"
    strong = (direction(live) == 1 and every_day(live) and direction(dry) == 1 and every_day(dry)
              and total_days >= 3 and dry["days_compared"] >= 3
              and (controlled is None or controlled.get("LIVE") == "SURVIVES"))
    if strong:
        return "STRONG"
    if direction(live) == 1 and majority(live):
        return "WEAK"
    return "NOT_SUPPORTED"  # overall sign negative, or no majority of days in the same direction


def control_verdict(raw, coef):
    if raw is None or coef is None or raw == 0:
        return "UNKNOWN"
    if np.sign(coef) != np.sign(raw) or abs(coef) < 0.25 * abs(raw):
        return "PROXY"
    if abs(coef) >= 0.5 * abs(raw):
        return "SURVIVES"
    return "PARTIAL"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tables", required=True)
    ap.add_argument("--journal", default="logs/trade_journal.db")
    ap.add_argument("--export", default="data/research_export")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    data = {"LIVE": live_rows(Path(a.tables)), "DRY_RUN": dry_rows(Path(a.tables), Path(a.journal), Path(a.export))}
    res = {"frozen": {"score_min": SCORE_MIN, "price_min": PRICE_MIN, "tte_max": TTE_MAX},
           "generated_at_utc": datetime.now(timezone.utc).isoformat(),
           "coverage": {c: {"n": len(r), "days": sorted({x["day"] for x in r}),
                            "missing_score": sum(1 for x in r if x["score"] is None),
                            "missing_tte": sum(1 for x in r if x["tte"] is None),
                            "missing_sigma": sum(1 for x in r if x["sigma"] is None)} for c, r in data.items()}}
    control_fields = {"LIVE": "distance", "DRY_RUN": "sigma"}
    res["correlation"] = {c: corr(r, ["price", "score", "tte", control_fields[c]]) for c, r in data.items()}
    tests = {
        "SCORE_030": (lambda r: r["score"] is not None and r["score"] >= SCORE_MIN, None, "score"),
        "ENTRY_PRICE_075": (lambda r: r["price"] >= PRICE_MIN, ["score", "tte"], "price"),
        "TTE_480": (lambda r: r["tte"] is not None and r["tte"] <= TTE_MAX, ["score", "price"], "tte"),
    }
    for name, (passes, controls, feature) in tests.items():
        block = {}
        verdicts = {}
        for cohort, rows in data.items():
            rows = [r for r in rows if r.get(feature) is not None]
            c = compare(rows, passes)
            if controls is not None:
                cs = controls + [control_fields[cohort]]
                c["adjusted"] = ols(rows, passes, cs)
                verdicts[cohort] = control_verdict(c["raw_mean_pnl_diff"], c["adjusted"].get("coef"))
                c["control_verdict"] = verdicts[cohort]
                strata = {}
                for sname, sfn in (("score>=0.30", lambda r: (r["score"] or 0) >= SCORE_MIN),
                                   ("score<0.30", lambda r: (r["score"] or 0) < SCORE_MIN),
                                   ("tte<=480", lambda r: (r["tte"] or 9e9) <= TTE_MAX),
                                   ("tte>480", lambda r: (r["tte"] or 0) > TTE_MAX),
                                   ("price>=0.75", lambda r: r["price"] >= PRICE_MIN),
                                   ("price<0.75", lambda r: r["price"] < PRICE_MIN)):
                    if sname.split(">")[0].split("<")[0] == feature:
                        continue
                    sub = [r for r in rows if sfn(r)]
                    s_c = compare(sub, passes)
                    strata[sname] = {"pass": s_c["pass"], "fail": s_c["fail"], "raw_mean_pnl_diff": s_c["raw_mean_pnl_diff"]}
                c["stratified"] = strata
            block[cohort] = c
        raw_label = label(block, controlled=verdicts if controls is not None else None)
        if name == "SCORE_030":
            status = {"STRONG": "STRONG_CANDIDATE", "WEAK": "WEAK_CANDIDATE"}.get(raw_label, raw_label)
        else:
            status = {"STRONG": "INDEPENDENT_SIGNAL", "WEAK": "UNRESOLVED"}.get(raw_label, raw_label)
            if raw_label == "WEAK" and verdicts.get("LIVE") == "SURVIVES":
                status = "UNRESOLVED"  # majority-day evidence only: not enough for INDEPENDENT_SIGNAL
        block["status"] = status
        res[name] = block
    # 3D combined candidate (in-sample only)
    combined = lambda r: (r["score"] is not None and r["score"] >= SCORE_MIN and r["price"] >= PRICE_MIN
                          and r["tte"] is not None and r["tte"] <= TTE_MAX)
    res["COMBINED_IN_SAMPLE_ONLY"] = {}
    for cohort, rows in data.items():
        c = compare(rows, combined)
        drops = {}
        for dropped, fn in (("drop_score", lambda r: r["price"] >= PRICE_MIN and r["tte"] is not None and r["tte"] <= TTE_MAX),
                            ("drop_price", lambda r: r["score"] is not None and r["score"] >= SCORE_MIN and r["tte"] is not None and r["tte"] <= TTE_MAX),
                            ("drop_tte", lambda r: r["score"] is not None and r["score"] >= SCORE_MIN and r["price"] >= PRICE_MIN)):
            drops[dropped] = describe([r for r in rows if fn(r)])
        c["drop_one_filter"] = drops
        c["all_trades"] = describe(rows)
        res["COMBINED_IN_SAMPLE_ONLY"][cohort] = c
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "entry_analysis.json").write_text(json.dumps(res, indent=1, default=str))
    for cohort, rows in data.items():
        with (out / f"entry_rows_{cohort}.csv").open("w", newline="") as h:
            w = csv.DictWriter(h, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    print(json.dumps({k: res[k]["status"] for k in ("SCORE_030", "ENTRY_PRICE_075", "TTE_480")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
