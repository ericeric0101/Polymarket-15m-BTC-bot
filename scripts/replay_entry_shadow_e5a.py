"""Deterministic replay: runtime E5B C/B/D evaluators vs the offline E5A semantics.

Feeds every E5A post-placement path row (reports/entry_research_e5_maker_selection_20261011/
post_order_paths.csv) through bot.entry_shadow.evaluate_ec1 (h=15) and evaluate_chb (h=10)
and compares the trigger counts with E5A's early_cancel / chase counterfactual tables.
Outcome columns are never read by the evaluators.  Read-only; prints a JSON summary.

Usage: python -I scripts/replay_entry_shadow_e5a.py --e5a-dir <reports/entry_research_e5_maker_selection_20261011>
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.entry_shadow import EdgeCalibration, EntryShadowSnapshot, evaluate_chb, evaluate_ec1, evaluate_edge  # noqa: E402


def _num(v):
    try:
        return float(v) if v not in ("", None, "nan") else None
    except ValueError:
        return None


def to_snapshot(r: dict) -> EntryShadowSnapshot:
    censored = str(r["censored"]).lower() == "true"
    ask, bid, d_ask = _num(r["ask"]), _num(r["bid"]), _num(r["d_ask"])
    placement_ask = (ask - d_ask) if ask is not None and d_ask is not None else None
    return EntryShadowSnapshot(
        schema_version="replay", timestamp=0.0, attempt_id=r["slug"], market=r["slug"], instrument="replay", side="UP",
        horizon_sec=float(r["h"]), tte_sec=None, side_score=None, side_score_signed=_num(r["score_signed"]),
        entry_side_score_signed=None, strike=None, strike_verified=None, quote_receipt_ts=None,
        quote_age_at_horizon_sec=None, observation_state="CENSORED" if censored else "OBSERVED",
        best_bid=bid, best_ask=ask, spread=_num(r["spread"]), top_ask_size=None, top_bid_size=None, l2_age_sec=None,
        binance_spot=None, binance_move_bps_signed=_num(r["btc_bps"]), chainlink_spot=None, chainlink_age_sec=None,
        chainlink_move_bps_signed=_num(r["cl_bps"]), twap_value=None, twap_state=None, signed_distance_usd=None,
        placement_ask=placement_ask, placement_bid=None, planned_maker_price=None, planned_qty=None, maker_age_sec=None,
        maker_filled_qty=None, maker_status="REPLAY", canonical_filled_by_horizon=str(r["filled_by_h"]).lower() == "true",
        market_filled_by_horizon=False, production_action="REPLAY", market_day_type="REPLAY")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--e5a-dir", required=True)
    args = ap.parse_args()
    d = Path(args.e5a_dir)
    rows = list(csv.DictReader(open(d / "post_order_paths.csv")))
    c_counts, b_counts, c_states, b_states = Counter(), Counter(), Counter(), Counter()
    for r in rows:
        key = (r["tier"], r["split"])
        if float(r["h"]) == 15:
            res = evaluate_ec1(to_snapshot(r))["decision"]
            c_states[res] += 1
            c_counts[key] += res == "WOULD_CANCEL"
        if float(r["h"]) == 10:
            res = evaluate_chb(to_snapshot(r))["decision"]
            b_states[res] += 1
            b_counts[key] += res == "WOULD_CHASE"
    e5a_c = {(x["tier"], x["split"]): int(x["triggered"]) for x in csv.DictReader(open(d / "early_cancel_counterfactuals.csv"))
             if x["context"] == "EC1_ask_toward_maker" and x["h"] == "15" and x["latency_s"] == "2.4"}
    e5a_b = {(x["tier"], x["split"]): int(x["n_eligible"]) for x in csv.DictReader(open(d / "chase_counterfactuals.csv"))
             if x["variant"] == "CH_B_up5c_signal_ok" and x["h"] == "10" and x["latency_s"] == "2.4"}
    keys = sorted(set(e5a_c) | set(c_counts))
    c_cmp = {f"{t}/{s}": {"runtime": c_counts.get((t, s), 0), "e5a": e5a_c.get((t, s), 0)} for t, s in keys}
    keys = sorted(set(e5a_b) | set(b_counts))
    b_cmp = {f"{t}/{s}": {"runtime": b_counts.get((t, s), 0), "e5a": e5a_b.get((t, s), 0)} for t, s in keys}
    cal = EdgeCalibration.load()
    d_examples = {}
    for label, ask, tte in (("low_ask", 0.35, 600), ("mid_ask", 0.62, 300), ("high_ask", 0.90, 200),
                            ("above_range", 0.98, 200), ("missing_tte", 0.70, None)):
        s = to_snapshot({"slug": label, "censored": "False", "ask": str(ask), "bid": str(ask - 0.01), "d_ask": "0",
                         "h": "0", "score_signed": "0.4", "spread": "0.01", "btc_bps": "", "cl_bps": "", "filled_by_h": "False"})
        s = EntryShadowSnapshot(**{**s.as_payload(), "tte_sec": tte})
        out = evaluate_edge(s, cal)
        d_examples[label] = {k: out[k] for k in ("edge_state", "calibration_cell", "calibrated_probability", "raw_edge",
                                                 "net_edge_before_selection")}
    summary = {
        "rows": len(rows), "ec1_at_15s": c_cmp, "chb_at_10s": b_cmp,
        "ec1_match": all(v["runtime"] == v["e5a"] for v in c_cmp.values()),
        "chb_match": all(v["runtime"] == v["e5a"] for v in b_cmp.values()),
        "ec1_state_counts": dict(c_states), "chb_state_counts": dict(b_states), "d_examples": d_examples,
    }
    print(json.dumps(summary, indent=1))
    return 0 if summary["ec1_match"] and summary["chb_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
