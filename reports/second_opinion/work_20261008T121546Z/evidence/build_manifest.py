#!/usr/bin/env python3
"""Second-opinion audit: build data_validity_manifest.csv and per-market cutoff table.

READ-ONLY: opens SQLite with mode=ro. Writes only under reports/second_opinion/.
Re-run:  python3 reports/second_opinion/work_20261008T121546Z/evidence/build_manifest.py
"""
import csv, json, sqlite3, subprocess, sys, ast
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
W = Path(__file__).resolve().parents[1]
EV = W / "evidence"
TJ = ROOT / "logs/trade_journal.db"
TW = ROOT / "data/research/twap_forward_shadow.db"
RECOMP = ROOT / "reports/research_analysis/prediction_freshness/historical_freshness_recomputation_markets_20261007_000418_+0800.csv"
RECOMP_ROWS = ROOT / "reports/research_analysis/prediction_freshness/historical_freshness_recomputation_rows_20261007_000418_+0800.csv"
WIN_START = datetime(2026, 10, 3, tzinfo=timezone.utc).timestamp()
WIN_END = datetime(2026, 10, 8, tzinfo=timezone.utc).timestamp()
SHA_A = "3b50637"   # retire outcome fast-follow execution authority
SHA_B = "33896ce"   # retire outcome hyperliquid subsystem
SHA_A_TS = datetime.fromisoformat("2026-10-07T10:02:00+08:00").timestamp()  # author time of 3b50637 (approx, minute)
GAP_MAX = 10.0      # max allowed snapshot gap (s) inside the market window
FIRST_MAX = 30.0    # structural ~19 s opening blind spot exists in ALL markets (see notes); allow 30 s
EDGE_MAX = 10.0     # last snapshot within 10 s of close
CUTOFFS = (300, 180, 120, 60, 30)


def ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def is_ancestor(a, b):
    return subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", a, b],
                          capture_output=True).returncode == 0


def main():
    # 1. run manifests -> phase per run
    runs = {}
    with ro(TJ) as c:
        for run_id, st, en, mode, notes in c.execute(
                "SELECT run_id, started_at, ended_at, mode, notes_json FROM strategy_runs WHERE started_at>='2026-09-30'"):
            try:
                man = (json.loads(notes or "{}") or {}).get("run_manifest") or {}
            except Exception:
                man = {}
            commit = man.get("git_commit") or ""
            if commit and is_ancestor(SHA_B, commit):
                phase = "POST_B"
            elif commit and is_ancestor(SHA_A, commit):
                phase = "POST_A"
            elif commit:
                phase = "PRE_A"
            else:
                phase = "PRE_A_BY_TIME" if st and datetime.fromisoformat(st).timestamp() < SHA_A_TS else "UNKNOWN"
            ts = lambda s: datetime.fromisoformat(s).timestamp() if s else None
            runs[run_id] = dict(start=ts(st), end=ts(en), mode=mode, commit=commit[:7],
                                dirty=man.get("git_dirty"), phase=phase)
    # 2. snapshots + summaries
    snaps = defaultdict(list)
    summary = {}
    q = ("SELECT slug, run_id, payload_json FROM lead_lag_decisions WHERE decision_epoch_ns >= ? AND decision_epoch_ns < ? "
         "AND json_extract(payload_json,'$.event_type') IN ('PREDICTION_RESEARCH_SNAPSHOT','MARKET_TWAP_SUMMARY')")
    with ro(TW) as c:
        for slug, run_id, pj in c.execute(q, (int((WIN_START - 900) * 1e9), int((WIN_END + 900) * 1e9))):
            d = json.loads(pj)
            if d["event_type"] == "MARKET_TWAP_SUMMARY":
                summary[slug] = d
                continue
            snaps[slug].append((float(d["snapshot_ts"]), d.get("freshness_clock_semantics_version"), run_id,
                                d.get("twap_fresh"), d.get("official_twap"), d.get("strike"),
                                d.get("settlement_state_side"), d.get("required_move_sigma"), d.get("required_move_bps"),
                                d.get("time_left_sec"), d.get("sigma_ex_market"), d.get("market_mid_up"),
                                d.get("joint_fresh")))
    # 3. prior recomputation labels (input artifact, used only for provenance of pre-v2 markets)
    recomp = defaultdict(lambda: {"rows": 0, "not_recomputable": 0, "status": set()})
    if RECOMP.is_file():
        with RECOMP.open() as f:
            for r in csv.DictReader(f):
                m = recomp[r["market_slug"]]
                m["rows"] += int(r["rows"] or 0)
                m["not_recomputable"] += int(r["joint_not_recomputable"] or 0)
                m["status"].add(r["new_status"])
    twap_rows = {}
    twap_bad = defaultdict(int)
    if RECOMP_ROWS.is_file():
        with RECOMP_ROWS.open() as f:
            for r in csv.DictReader(f):
                twap_rows[(r["market_slug"], round(float(r["snapshot_ts"]), 3))] = r["TWAP_classification"]
                if not r["TWAP_classification"].startswith("FRESH"):
                    twap_bad[r["market_slug"]] += 1
    # 4. market universe = every 15-min slot in window
    rows, cut_rows = [], []
    start = int(WIN_START)
    while start < WIN_END:
        slug = f"btc-updown-15m-{start}"
        s = sorted(snaps.get(slug, []))
        end = start + 900
        inwin = [x for x in s if start <= x[0] <= end]
        ts = [x[0] for x in inwin]
        versions = {x[1] for x in inwin}
        run_ids = sorted({x[2] for x in inwin})
        phases = sorted({runs.get(r, {}).get("phase", "UNKNOWN") for r in run_ids})
        first_off = (ts[0] - start) if ts else None
        end_gap = (end - ts[-1]) if ts else None
        max_gap = max([b - a for a, b in zip(ts, ts[1:])], default=None) if ts else None
        span = (ts[-1] - ts[0]) if ts else 0.0
        started_runs = [r for r in run_ids if runs.get(r, {}).get("start") and start < runs[r]["start"] < end]
        startup_partial = bool(ts) and first_off is not None and first_off > FIRST_MAX and bool(started_runs)
        complete = bool(ts) and first_off <= FIRST_MAX and end_gap <= EDGE_MAX and (max_gap or 0) <= GAP_MAX
        interrupted = bool(ts) and (max_gap or 0) > GAP_MAX
        span_ok = span >= 900 - FIRST_MAX - EDGE_MAX
        sm = summary.get(slug) or {}
        settle = sm.get("canonical_settlement_side")
        settlement_valid = settle in ("UP", "DOWN") and sm.get("settlement_reference_is_canonical") is True
        if not ts:
            prov = "NO_DATA"
        elif versions == {2}:
            prov = "NATIVE_V2"
        elif 2 in versions:
            prov = "MIXED_V1_V2"
        else:
            # Purpose = market dynamics: required evidence is official TWAP vs strike + settlement.
            # RESOLVED iff every recomputed row of this market has a FRESH_* TWAP classification.
            rc = recomp.get(slug)
            if rc is None or rc["rows"] == 0:
                prov = "RAW_PRE_V2"
            elif twap_bad.get(slug, 0) == 0:
                prov = "RECOMPUTED_RESOLVED"
            else:
                prov = "RECOMPUTED_UNRESOLVED"
        joint_prov = ("NATIVE_V2" if prov == "NATIVE_V2" else "NO_DATA" if not ts else
                      "JOINT_RESOLVED" if recomp.get(slug) and recomp[slug]["not_recomputable"] == 0 and recomp[slug]["rows"] else
                      "JOINT_UNRESOLVED" if recomp.get(slug) else "RAW_PRE_V2")
        reasons = []
        if not ts: reasons.append("no_snapshots")
        if startup_partial: reasons.append("startup_partial")
        if ts and not complete and not startup_partial: reasons.append("incomplete_edges_or_gap")
        if interrupted: reasons.append(f"gap>{GAP_MAX:g}s")
        if ts and not span_ok: reasons.append("insufficient_span")
        if not settlement_valid: reasons.append("settlement_missing")
        if prov in ("RAW_PRE_V2", "RECOMPUTED_UNRESOLVED", "MIXED_V1_V2"): reasons.append(prov.lower())
        if len(phases) > 1: reasons.append("mixed_runtime_phase")
        clean = complete and span_ok and settlement_valid and not interrupted and len(phases) == 1
        tier = 1 if clean and prov == "NATIVE_V2" else 2 if clean and prov == "RECOMPUTED_RESOLVED" else 3
        rows.append(dict(market_id=slug, date_utc=datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%d"),
                         runtime_phase="|".join(phases) if phases else "NONE", freshness_provenance=prov,
                         complete_observation=int(complete), startup_partial=int(startup_partial),
                         rollover_gap_sec=round(max_gap, 3) if max_gap is not None else "",
                         settlement_valid=int(settlement_valid), span_ok=int(span_ok), interrupted=int(interrupted),
                         tier=tier, exclusion_reason=";".join(reasons), usable_primary=int(tier == 1),
                         usable_sensitivity=int(tier == 2),
                         n_snapshots=len(ts), first_offset_sec=round(first_off, 2) if first_off is not None else "",
                         end_gap_sec=round(end_gap, 2) if end_gap is not None else "",
                         settlement_side=settle or "", twap_cross_count=sm.get("twap_cross_count", ""),
                         joint_provenance=joint_prov, twap_unresolved_rows=twap_bad.get(slug, 0),
                         run_ids="|".join(run_ids)))
        # cutoff leader table (market level)
        for k in CUTOFFS:
            target = end - k
            cand = [x for x in inwin if target - 5.0 <= x[0] <= target and x[3] is True
                    and x[4] is not None and x[5] is not None]
            if prov != "NATIVE_V2":
                cand = [x for x in cand if str(twap_rows.get((slug, round(x[0], 3)), "")).startswith("FRESH")]
            if cand:
                x = cand[-1]
                tw, kk = float(x[4]), float(x[5])
                leader = "UP" if tw > kk else "DOWN" if tw < kk else "TIE"
                cut_rows.append(dict(market_id=slug, tier=tier, provenance=prov, cutoff_sec=k, snap_ts=round(x[0], 3), leader=leader,
                                     settlement_side=settle or "", flip=int(bool(settle) and leader in ("UP", "DOWN") and leader != settle),
                                     dist_bps=round((tw - kk) / kk * 1e4, 3),
                                     required_move_sigma=x[7], required_move_bps=x[8], time_left_sec=x[9],
                                     sigma_ex_market=x[10]))
        start += 900
    fields = ["market_id", "date_utc", "runtime_phase", "freshness_provenance", "complete_observation", "startup_partial",
              "rollover_gap_sec", "settlement_valid", "span_ok", "interrupted", "tier", "exclusion_reason",
              "usable_primary", "usable_sensitivity", "n_snapshots", "first_offset_sec", "end_gap_sec",
              "settlement_side", "twap_cross_count", "joint_provenance", "twap_unresolved_rows", "run_ids"]
    for out in (W.parent / "data_validity_manifest.csv", EV / "data_validity_manifest.csv"):
        with out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
    with (EV / "cutoff_leaders.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(cut_rows[0].keys())); w.writeheader(); w.writerows(cut_rows)
    with (EV / "runs_phase.json").open("w") as f:
        json.dump(runs, f, indent=1, default=str)
    print(f"markets={len(rows)} cutoff_rows={len(cut_rows)} runs={len(runs)}")


if __name__ == "__main__":
    sys.exit(main())
