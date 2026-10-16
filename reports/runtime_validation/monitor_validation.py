#!/usr/bin/env python3
"""Read-only checkpoint monitor for a dry-run validation (SQLite mode=ro, never immutable).

Usage: python3 reports/runtime_validation/monitor_validation.py <run_dir> <label>
Writes <run_dir>/checkpoint_<label>.json and prints a compact summary.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict

D, LABEL = sys.argv[1], sys.argv[2]
base = json.load(open(f"{D}/baseline.json"))
pid = int(open(f"{D}/bot.pid").read().strip())
tj = sqlite3.connect("file:logs/trade_journal.db?mode=ro", uri=True, timeout=10)
rs = sqlite3.connect("file:data/research/twap_forward_shadow.db?mode=ro", uri=True, timeout=10)
size = lambda p: os.path.getsize(p) if os.path.exists(p) else 0
now = time.time()
out = {"label": LABEL, "ts": now, "elapsed_min": (now - base["start_ts"]) / 60 if "start_ts" in base else None}
out["pid_alive"] = subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0

run = tj.execute("SELECT run_id, started_at, mode, test_mode, notes_json FROM strategy_runs WHERE rowid > 0 "
                 "ORDER BY started_at DESC LIMIT 1").fetchone()
manifest = (json.loads(run[4] or "{}") or {}).get("run_manifest") or {}
run_id = run[0]
out["run"] = {"run_id": run_id, "started_at": run[1], "mode": run[2], "test_mode": run[3],
              "git_commit": manifest.get("git_commit"), "git_dirty": manifest.get("git_dirty"),
              "execution_mode": manifest.get("execution_mode")}

# --- order safety --------------------------------------------------------------------------
orders = Counter(r[0] for r in tj.execute(
    "SELECT event_type FROM order_events WHERE id > ? AND run_id = ?", (base["order_events_max_id"], run_id)))
real_types = {"ORDER_SUBMIT", "ORDER_MAKER_INTENT", "ORDER_TAKER_EXIT_SUBMIT", "ORDER_FILLED",
              "ORDER_ORPHAN_CANCEL_ON_START", "ORDER_CANCELED"}
out["orders"] = {"real_lifecycle_events": {k: v for k, v in orders.items() if k in real_types},
                 "dry_run": {k: v for k, v in orders.items() if "DRY_RUN" in k or k.startswith("SHADOW_SIM")},
                 "skip_unreal_quote": orders.get("ORDER_SKIP_UNREAL_QUOTE", 0)}

# --- strategy events: phases, strike provenance, traces --------------------------------------
sev = defaultdict(list)
for et, ts, pj in tj.execute("SELECT event_type, ts, payload_json FROM strategy_events WHERE id > ? AND run_id = ? "
                             "AND event_type IN ('MARKET_PHASE_CHANGE','MARKET_STRIKE_PROVENANCE','MARKET_STRIKE_LOCKED',"
                             "'ENTRY_RESEARCH_TELEMETRY_SUMMARY','DIAGNOSTIC_SAMPLING_SUMMARY','SIDE_INVALIDATION_CONFIRMED')",
                             (base["strategy_events_max_id"], run_id)):
    sev[et].append((ts, json.loads(pj or "{}")))
trace_rows = [r[0] for r in tj.execute("SELECT ts FROM strategy_events WHERE id > ? AND run_id = ? AND "
                                        "event_type = 'ENTRY_DECISION_TRACE' ORDER BY id",
                                        (base["strategy_events_max_id"], run_id))]
iso = lambda s: __import__("datetime").datetime.fromisoformat(s).timestamp()
last_summary = sev["ENTRY_RESEARCH_TELEMETRY_SUMMARY"][-1] if sev["ENTRY_RESEARCH_TELEMETRY_SUMMARY"] else None
out["entry_trace"] = {
    "rows": len(trace_rows),
    "lifecycle_written_at_last_summary": last_summary[1].get("research_snapshots_written") if last_summary else None,
    "rows_up_to_last_summary": sum(1 for t in trace_rows if last_summary and t <= last_summary[0]),
    "lifecycle_write_failures": last_summary[1].get("research_snapshot_write_failures") if last_summary else None,
}
out["diagnostic_sampling_summaries"] = [p for _, p in sev["DIAGNOSTIC_SAMPLING_SUMMARY"]]
out["invalidation_confirmed"] = len(sev["SIDE_INVALIDATION_CONFIRMED"])

# --- per-market opening -------------------------------------------------------------------
start_after = iso(run[1])
snaps = defaultdict(list)
versions = Counter()
policies = Counter()
for slug, pj in rs.execute("SELECT slug, payload_json FROM lead_lag_decisions WHERE id > ? AND run_id = ? AND "
                           "json_extract(payload_json,'$.event_type') = 'PREDICTION_RESEARCH_SNAPSHOT'",
                           (base["research_max_id"], run_id)):
    d = json.loads(pj)
    versions[d.get("freshness_clock_semantics_version")] += 1
    policies[d.get("snapshot_interval_policy")] += 1
    snaps[slug].append(d)
markets = []
for slug, rows in sorted(snaps.items()):
    open_ts = float(slug.rsplit("-", 1)[-1])
    if open_ts < start_after:
        continue  # startup-partial market
    rows.sort(key=lambda d: d["snapshot_ts"])
    first = lambda pred: next((round(d["snapshot_ts"] - open_ts, 2) for d in rows if pred(d)), None)
    prov = [(round(iso(ts) - open_ts, 2), p.get("status")) for ts, p in sev["MARKET_STRIKE_PROVENANCE"] if p.get("slug") == slug]
    locked = [round(iso(ts) - open_ts, 2) for ts, p in sev["MARKET_STRIKE_LOCKED"] if p.get("slug") == slug]
    active = [round(iso(ts) - open_ts, 2) for ts, p in sev["MARKET_PHASE_CHANGE"]
              if p.get("slug") == slug and p.get("to") == "ACTIVE"]
    strike_avail = first(lambda d: d.get("strike_available") or d.get("strike") is not None)
    markets.append({
        "slug": slug,
        "first_snapshot_sec": first(lambda d: True),
        "first_strike_available_sec": strike_avail,
        "first_joint_fresh_sec": first(lambda d: d.get("joint_fresh")),
        "pre_handoff_rows": sum(1 for d in rows if d.get("research_before_trading_handoff")),
        "strike_provenance_attempts": prov,
        "strike_locked_sec": locked[0] if locked else None,
        "strike_fill_lag_after_lock_sec": (round(strike_avail - locked[0], 2)
                                           if locked and strike_avail is not None else None),
        "trading_active_sec": active[0] if active else None,
        "max_gap_sec": round(max((b["snapshot_ts"] - a["snapshot_ts"] for a, b in zip(rows, rows[1:])), default=0), 2),
    })
out["prediction"] = {"rows": sum(versions.values()), "versions": {str(k): v for k, v in versions.items()},
                     "interval_policies": dict(policies)}
out["markets"] = markets

# --- research store composition & growth ----------------------------------------------------
comp = {}
for et, n, b in rs.execute("SELECT json_extract(payload_json,'$.event_type'), COUNT(*), SUM(length(payload_json)) "
                           "FROM lead_lag_decisions WHERE id > ? AND run_id = ? GROUP BY 1 ORDER BY 3 DESC",
                           (base["research_max_id"], run_id)):
    comp[et] = {"rows": n, "payload_mb": round(b / 2**20, 3)}
out["research_events"] = comp
policy_marks = Counter(r[0] for r in rs.execute(
    "SELECT json_extract(payload_json,'$.capture_policy') FROM lead_lag_decisions WHERE id > ? AND run_id = ? AND "
    "json_extract(payload_json,'$.event_type') IN ('SHADOW_POSITION_MARK','SHADOW_BBO_SNAPSHOT')",
    (base["research_max_id"], run_id)))
out["shadow_capture_policies"] = dict(policy_marks)
journal_mb = sum((r[0] or 0) for r in tj.execute(
    "SELECT SUM(length(payload_json)) FROM strategy_events WHERE id > ? AND run_id = ? UNION ALL "
    "SELECT SUM(length(payload_json)) FROM order_events WHERE id > ? AND run_id = ?",
    (base["strategy_events_max_id"], run_id, base["order_events_max_id"], run_id))) / 2**20
paths = ["logs/trade_journal.db", "logs/trade_journal.db-wal", "backups/trade_journal.db",
         "data/research/twap_forward_shadow.db", "data/research/twap_forward_shadow.db-wal"]
out["storage"] = {"journal_payload_mb": round(journal_mb, 3),
                  "research_payload_mb": round(sum(v["payload_mb"] for v in comp.values()), 3),
                  "file_delta_mb": {p: round((size(p) - base["sizes"][p]) / 2**20, 3) for p in paths},
                  "free_gib": round(shutil.disk_usage(".").free / 2**30, 3)}

# --- runtime log --------------------------------------------------------------------------
log = open(f"{D}/runtime.log", errors="replace").read()
out["log"] = {
    "error_lines": len(re.findall(r"\| ERROR ", log)),
    "traceback": log.count("Traceback"),
    "retry_at_keyerror": log.count("KeyError: 'retry_at'"),
    "sqlite_errors": len(re.findall(r"sqlite3\.\w*Error", log)),
    "storage_guard": re.findall(r"STORAGE \S+ -> \S+", log)[-3:],
    "last_prediction_metrics": (re.findall(r"prediction_snapshot: [^\n]+", log) or [None])[-1],
    "last_btc1s": (re.findall(r"BTC1S history: [^\n]+", log) or [None])[-1],
    "last_dataengine": (re.findall(r"DataEngine queue: [^\n]+", log) or [None])[-1],
    "last_errors": re.findall(r"[^\n]*\| ERROR [^\n]*", log)[-5:],
}
json.dump(out, open(f"{D}/checkpoint_{LABEL}.json", "w"), indent=1, default=str)
print(json.dumps(out, indent=1, default=str))
