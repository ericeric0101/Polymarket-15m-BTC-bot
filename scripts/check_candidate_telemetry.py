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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.dry_run_plumbing import PopulationMergeRefused, check_population  # noqa: E402

CORE = ("decision_ts", "best_bid", "best_ask", "tte_sec", "side_score", "candidate_side", "proposed_maker_price",
        "market_slug")
SCHEMA = "candidate_decision_v2"
# Optional feature groups: coverage reported separately; never fails the gate on its own
# (a missing value must carry a reason code instead).
OPTIONAL = ("raw_p_ex", "required_move_sigma", "top_bid_size", "top_ask_size", "top_imbalance", "btc_ret_5s",
            "btc_ret_60s", "btc_chainlink_price", "btc_binance_price", "btc_twap_price", "twap_state_projected_side",
            "other_side_bid", "runtime_strike", "signed_strike_distance_usd")
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
    optional_cov = {f: (sum(1 for r in cands if r.get(f) is not None) / len(cands)) if cands else None
                    for f in OPTIONAL}
    reasons = Counter(v for r in cands for v in (r.get("missing_reasons") or {}).values())
    pex_missing_without_reason = sum(1 for r in cands if r.get("raw_p_ex") is None and not r.get("pex_reason"))
    ok = bool(cands) and all(v is not None and v >= COVERAGE_MIN for v in cov.values())
    # DRY-RUN plumbing: simulated fills, exactly-one terminal state, execution population.
    life = [r for r in recs if r.get("record_type") == "LIFECYCLE"]
    terminal_by_market = Counter(r.get("market_slug") for r in life if r.get("terminal") is True)
    closed = {r.get("market_slug") for r in recs if r.get("record_type") == "PATH_STATS"}
    first_accepted = {r["market_slug"] for r in cands if r.get("candidate_role") == "FIRST_ACCEPTED"}
    dry_accepted = {r["market_slug"] for r in cands if r.get("candidate_role") == "FIRST_ACCEPTED"
                    and r.get("eligibility_event_type") == "ORDER_DRY_RUN_SUBMITTED"}
    try:
        population, population_ok = check_population(recs, allow_non_evaluable=True), True
    except PopulationMergeRefused as exc:
        population, population_ok = {"refused": str(exc)}, False
    return {"records": len(recs), "candidates": len(cands), "bad_schema": bad_schema, "duplicate_ids": dup,
            "stable_role_duplicates_across_restarts": restart_dups, "non_monotonic_decisions": nonmono,
            "core_coverage": cov, "core_coverage_ok": ok and bad_schema == 0 and dup == 0,
            "optional_coverage": optional_cov,
            "missing_reason_counts": dict(reasons), "pex_missing_without_reason": pex_missing_without_reason,
            "record_types": dict(Counter(r.get("record_type") for r in recs)),
            "simulated_fills": sum(1 for r in life if r.get("lifecycle_event") == "SIMULATED_FILL"),
            "simulated_fill_unmapped": sum(1 for r in life if r.get("lifecycle_event") == "SIMULATED_FILL_UNMAPPED"),
            "fill_anchored_markouts": sum(1 for r in recs if r.get("record_type") == "MARKOUT"
                                          and r.get("anchor") in ("FILL", "SIMULATED_FILL")),
            "dry_run_terminal_states": sum(terminal_by_market.values()),
            "terminal_kinds": dict(Counter(r.get("lifecycle_event") for r in life if r.get("terminal") is True)),
            "accepted_closed_without_terminal": len([m for m in dry_accepted if m in closed
                                                     and not terminal_by_market.get(m)]),
            "accepted_with_multiple_terminals": len([m for m in first_accepted if terminal_by_market.get(m, 0) > 1]),
            "production_p_fair_coverage_accepted": (
                sum(1 for r in cands if r.get("eligibility") == "ACCEPTED" and r.get("production_p_fair") is not None)
                / max(1, sum(1 for r in cands if r.get("eligibility") == "ACCEPTED"))) if cands else None,
            "production_p_fair_missing_without_reason": sum(
                1 for r in cands if r.get("production_p_fair") is None
                and "production_p_fair" not in (r.get("missing_reasons") or {})),
            "population": population, "population_ok": population_ok}


if __name__ == "__main__":
    rep = check([Path(p) for p in sys.argv[1:]])
    print(json.dumps(rep, indent=1))
    sys.exit(0 if rep["core_coverage_ok"] and rep["pex_missing_without_reason"] == 0 and rep["population_ok"] else 1)
