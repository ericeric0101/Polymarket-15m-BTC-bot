"""Integrity gate for candidate telemetry (offline, read-only).

Checks schema version, duplicate ids, monotonic decision clocks per market, core-field
coverage (>= 99% required before any interpretation) and missing-reason distribution.
Usage: python -I scripts/check_candidate_telemetry.py <jsonl> [<jsonl> ...]
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

CORE = ("decision_ts", "best_bid", "best_ask", "tte_sec", "side_score", "candidate_side", "proposed_maker_price",
        "market_slug")
SCHEMA = "candidate_decision_v1"
COVERAGE_MIN = 0.99


def load(paths):
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)


def check(paths) -> dict:
    recs = list(load(paths))
    cands = [r for r in recs if r.get("record_type") == "CANDIDATE"]
    bad_schema = sum(1 for r in recs if r.get("schema_version") != SCHEMA)
    ids = Counter((r["candidate_id"], r.get("run_id")) for r in cands)
    stable_ids = Counter(r["candidate_id"] for r in cands if r.get("candidate_role") in ("FIRST_ACCEPTED", "FIRST_EVALUATION"))
    dup = sum(c - 1 for c in ids.values() if c > 1)
    restart_dups = sum(c - 1 for c in stable_ids.values() if c > 1)
    nonmono = 0
    last = {}
    for r in cands:
        k = (r.get("run_id"), r["market_slug"])
        if k in last and r["decision_wall_ts"] < last[k]:
            nonmono += 1
        last[k] = r["decision_wall_ts"]
    cov = {}
    for f in CORE:
        need = [r for r in cands if not (f == "proposed_maker_price" and r.get("eligibility") == "REJECTED"
                                         and r.get("proposed_maker_price") is None)]
        cov[f] = (sum(1 for r in need if r.get(f) is not None) / len(need)) if need else None
    reasons = Counter(v for r in cands for v in (r.get("missing_reasons") or {}).values())
    pex_missing_without_reason = sum(1 for r in cands if r.get("raw_p_ex") is None and not r.get("pex_reason"))
    ok = bool(cands) and all(v is not None and v >= COVERAGE_MIN for v in cov.values())
    return {"records": len(recs), "candidates": len(cands), "bad_schema": bad_schema, "duplicate_ids": dup,
            "stable_role_duplicates_across_restarts": restart_dups, "non_monotonic_decisions": nonmono,
            "core_coverage": cov, "core_coverage_ok": ok and bad_schema == 0 and dup == 0,
            "missing_reason_counts": dict(reasons), "pex_missing_without_reason": pex_missing_without_reason,
            "record_types": dict(Counter(r.get("record_type") for r in recs))}


if __name__ == "__main__":
    rep = check([Path(p) for p in sys.argv[1:]])
    print(json.dumps(rep, indent=1))
    sys.exit(0 if rep["core_coverage_ok"] and rep["pex_missing_without_reason"] == 0 else 1)
