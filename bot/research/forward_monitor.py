"""Outcome-blind collection monitor for Entry V3 forward telemetry (research only, offline).

Allowed during collection (prereg section 9): candidate counts, field completeness,
storage, processing health, lifecycle join, telemetry latency and quote coverage.
This module must never see outcomes or PnL: it imports nothing from the label /
evaluator modules (tests/test_prereg_evaluator.py import-graph test) and refuses any
record that looks like a label row.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

CORE_FIELDS = ("decision_ts", "best_bid", "best_ask", "tte_sec", "side_score", "candidate_side",
               "proposed_maker_price", "market_slug")
CORE_MIN = 0.99
_LABEL_TOKENS = ("official_outcome", "hold_y", "payoff", "pnl")


class OutcomeDataRefused(ValueError):
    """A label/outcome record reached the monitor (outcome locking, prereg section 9)."""


def _records(paths: Iterable[Path]):
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    rec = json.loads(line)
                    if rec.get("record_type") == "LABEL" or any(t in k for k in rec for t in _LABEL_TOKENS):
                        raise OutcomeDataRefused(f"outcome/label data offered to the monitor: {p}")
                    yield rec


def monitor(paths: Iterable[Path]) -> dict[str, Any]:
    paths = [Path(p) for p in paths]
    recs = list(_records(paths))
    cands = [r for r in recs if r.get("record_type") == "CANDIDATE"]
    life = [r for r in recs if r.get("record_type") == "LIFECYCLE"]
    accepted = [r for r in cands if r.get("eligibility") == "ACCEPTED" and r.get("client_order_id")]
    life_coids = {r.get("client_order_id") for r in life}
    cov = {}
    for f in CORE_FIELDS:
        need = [r for r in cands if not (f == "proposed_maker_price" and r.get("eligibility") == "REJECTED")]
        cov[f] = (sum(1 for r in need if r.get(f) is not None) / len(need)) if need else None
    lat = sorted(float(r["decision_wall_ts"]) - float(r["decision_ts"]) for r in cands
                 if r.get("decision_wall_ts") is not None and r.get("decision_ts") is not None)
    bbo_age = sorted(float(r["bbo_age_sec"]) for r in cands if r.get("bbo_age_sec") is not None)

    def q(vals, x):
        return vals[min(len(vals) - 1, int(x * len(vals)))] if vals else None

    return {
        "records": len(recs), "record_types": dict(Counter(r.get("record_type") for r in recs)),
        "candidates": len(cands), "roles": dict(Counter(r.get("candidate_role") for r in cands)),
        "markets": len({r.get("market_slug") for r in cands}),
        "run_fingerprints": sorted({str(r.get("run_fingerprint")) for r in cands}),
        "schema_versions": sorted({str(r.get("schema_version")) for r in cands}),
        "core_coverage": cov,
        "core_coverage_ok": bool(cands) and all(v is not None and v >= CORE_MIN for v in cov.values()),
        "lifecycle_join_rate": (sum(1 for r in accepted if r["client_order_id"] in life_coids) / len(accepted))
        if accepted else None,
        "telemetry_latency_sec_p50_p99": (q(lat, 0.5), q(lat, 0.99)),
        "bbo_age_sec_p50_p99": (q(bbo_age, 0.5), q(bbo_age, 0.99)),
        "pex_missing_without_reason": sum(1 for r in cands if r.get("raw_p_ex") is None and not r.get("pex_reason")),
        "missing_reason_counts": dict(Counter(v for r in cands for v in (r.get("missing_reasons") or {}).values())),
        "storage_bytes": sum(p.stat().st_size for p in paths if p.exists()),
    }
