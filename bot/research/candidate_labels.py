"""Post-hoc label table for Entry V3 candidates (outcome linker; research only, offline).

Joins canonical candidate snapshots (bot.candidate_telemetry, never modified) with
official outcomes obtained AFTER collection by read-only GET (UNKNOWN when not
available).  Lives apart from the snapshot store: monitoring tools must never import
this module (tests/test_prereg_evaluator.py import-graph test).

One row per canonical candidate:
  candidate_id, market_slug, candidate_side, official_outcome, official_final_strike,
  hold_y (1 if the candidate side won, 0 if lost, None if UNKNOWN), and hypothetical
  hold payoffs per share at the decision-time executable ask, at the first maker price,
  for the CURRENT fill, the FQO lower/upper simulated fills and the B simulated chase.
"""
from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping, Optional

LABEL_SCHEMA_VERSION = "candidate_label_v1"
UNKNOWN = "UNKNOWN"


def hold_y(side: Optional[str], outcome: Optional[str]) -> Optional[int]:
    side_u, out_u = str(side or "").upper(), str(outcome or "").upper()
    if side_u not in ("UP", "DOWN") or out_u not in ("UP", "DOWN"):
        return None
    return 1 if side_u == out_u else 0


def _payoff(y: Optional[int], price: Any) -> Optional[float]:
    try:
        return None if y is None or price is None else float(y) - float(price)
    except (TypeError, ValueError):
        return None


def build_label_rows(candidates: Iterable[Mapping[str, Any]], outcomes: Mapping[str, Mapping[str, Any]], *,
                     current_fills: Optional[Mapping[str, Mapping[str, Any]]] = None,
                     fqo: Optional[Mapping[str, Mapping[str, Any]]] = None,
                     b_chase: Optional[Mapping[str, Mapping[str, Any]]] = None) -> list[dict[str, Any]]:
    """Pure: inputs are deep-copied, never mutated.  Keys of the optional maps are candidate_id."""
    cands = copy.deepcopy(list(candidates))
    outcomes = copy.deepcopy(dict(outcomes))
    current_fills, fqo, b_chase = (copy.deepcopy(dict(m or {})) for m in (current_fills, fqo, b_chase))
    rows = []
    for c in cands:
        cid = c.get("candidate_id")
        o = outcomes.get(str(c.get("market_slug") or "")) or {}
        outcome = o.get("outcome") if o.get("outcome") in ("UP", "DOWN") else UNKNOWN
        y = hold_y(c.get("candidate_side"), outcome)
        cur = current_fills.get(cid) or {}
        fq = fqo.get(cid) or {}
        b = b_chase.get(cid) or {}
        rows.append({
            "record_type": "LABEL", "label_schema_version": LABEL_SCHEMA_VERSION, "candidate_id": cid,
            "market_slug": c.get("market_slug"), "candidate_side": c.get("candidate_side"),
            "run_fingerprint": c.get("run_fingerprint"),
            "official_outcome": outcome, "official_final_strike": o.get("final_strike"),
            "outcome_source": o.get("source"), "hold_y": y,
            "payoff_ps_executable_ask": _payoff(y, c.get("executable_ask")),
            "payoff_ps_first_maker": _payoff(y, c.get("proposed_maker_price")),
            "current_filled": bool(cur.get("filled")), "current_fill_price": cur.get("fill_price"),
            "current_filled_qty": cur.get("filled_qty"),
            "payoff_ps_current": _payoff(y, cur.get("fill_price")) if cur.get("filled") else None,
            "fqo_lower": fq.get("lower"), "fqo_upper": fq.get("upper"), "fqo_fill_price": fq.get("fill_price"),
            "payoff_ps_fqo": _payoff(y, fq.get("fill_price")) if fq.get("fill_price") is not None else None,
            "b_decision": b.get("b_decision"), "b_chase_price_2p4s": b.get("b_chase_price_2p4s"),
            "b_chase_price_3p6s": b.get("b_chase_price_3p6s"),
            "payoff_ps_b_2p4s": _payoff(y, b.get("b_chase_price_2p4s")),
            "payoff_ps_b_3p6s": _payoff(y, b.get("b_chase_price_3p6s")),
        })
    return rows
