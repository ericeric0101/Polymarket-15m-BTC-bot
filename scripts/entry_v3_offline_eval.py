"""Offline Entry V3 execution-selection evaluation (read-only; outcomes linked post hoc).

Usage: python -I scripts/entry_v3_offline_eval.py --jsonl <file>... --outcomes <slug->UP/DOWN json>
       [--v1-labels <coid -> {c_decision, c_decision_ts, b_decision, b_chase_price} json>] [--out <dir>]
Produces the funnel, comparator and selection tables.  On synthetic or replayed
data these tables only demonstrate the tool; they are not research results.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot.execution_selection_eval as ev  # noqa: E402
from bot.dry_run_plumbing import PopulationMergeRefused, check_population  # noqa: E402


def _load(paths):
    out = []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            out += [json.loads(line) for line in fh if line.strip()]
    return out


def evaluate(jsonl_paths, outcomes: dict, v1_labels: dict) -> dict:
    recs = _load(jsonl_paths)
    # Data wiring only: refuse to pool execution populations (DRY_RUN vs LIVE, plumbing revision,
    # fingerprint, non-evaluable run labels), including E5B v1 labels from another mode.
    population = check_population(recs)
    label_modes = {str(v.get("execution_mode")) for v in v1_labels.values() if isinstance(v, dict) and v.get("execution_mode")}
    if label_modes and (len(label_modes) > 1 or (population["execution_mode"] and label_modes != {population["execution_mode"]})):
        raise PopulationMergeRefused(f"v1 label execution_mode {sorted(label_modes)} != telemetry {population['execution_mode']}")
    canon = ev.canonical_candidates(recs)
    life = [r for r in recs if r.get("record_type") == "LIFECYCLE"]
    paths = {r["market_slug"]: r for r in recs if r.get("record_type") == "PATH_STATS"}
    fills = {}
    for r in life:
        if r.get("event_type") in ("ORDER_FILLED", "ORDER_DRY_RUN_FILLED") and r.get("side") == "BUY":
            fills.setdefault(r["market_slug"], r)
    cands = []
    for c in canon:
        slug = c["market_slug"]
        o = outcomes.get(slug)
        win = None if o not in ("UP", "DOWN") or c.get("candidate_side") is None else int(o == c["candidate_side"])
        f = fills.get(slug) if c["eligibility"] == "ACCEPTED" else None
        p = paths.get(slug)
        lab = v1_labels.get(c.get("client_order_id") or "", {})
        c_ts = lab.get("c_decision_ts")
        cands.append({
            "market_slug": slug, "win": win, "qty": c.get("proposed_qty") or 0,
            "current_filled": f is not None, "current_fill_price": f.get("price") if f else None,
            "fqo": ev.classify_fqo(p, current_canonical_fill_ts=(p or {}).get("canonical_fill_ts")) if c["eligibility"] == "ACCEPTED" else
            {"lower": ev.NOT_FILL, "upper": ev.NOT_FILL, "fill_price": None, "evidence": "NOT_ACCEPTED"},
            "c_decision": lab.get("c_decision"),
            "c_removes_fill": bool(f is not None and c_ts is not None and f.get("wall_ts", 0) > c_ts),
            "b_decision": lab.get("b_decision"), "b_chase_price": lab.get("b_chase_price"),
            "world1_ev": ev.world1_ev(c, win), "eligibility": c["eligibility"]})
    rows = ev.paired_rows(cands)
    w1 = {}
    for elig in ("ACCEPTED", "REJECTED"):
        v = [r["world1_ev"] for r in rows if r.get("eligibility") == elig and r.get("world1_ev") is not None]
        w1[elig] = {"n": len(v), "mean_world1_ev_per_share": (sum(v) / len(v)) if v else None}
    return {"funnel": ev.funnel(recs, life, rows), "comparators": ev.comparator_table(rows),
            "selection": ev.selection_table(rows), "world1_by_eligibility": w1, "rows": rows}


def _write(out: Path, tables: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name in ("funnel", "comparators", "selection"):
        rs = tables[name]
        with open(out / f"{name}.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rs[0]))
            w.writeheader()
            w.writerows([{k: (json.dumps(v) if isinstance(v, (list, tuple, dict)) else v) for k, v in r.items()} for r in rs])
    (out / "world1_by_eligibility.json").write_text(json.dumps(tables["world1_by_eligibility"], indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", nargs="+", required=True)
    ap.add_argument("--outcomes", required=True)
    ap.add_argument("--v1-labels")
    ap.add_argument("--out")
    a = ap.parse_args()
    t = evaluate(a.jsonl, json.loads(Path(a.outcomes).read_text()),
                 json.loads(Path(a.v1_labels).read_text()) if a.v1_labels else {})
    if a.out:
        _write(Path(a.out), t)
    print(json.dumps({k: v for k, v in t.items() if k != "rows"}, indent=1, default=str))
