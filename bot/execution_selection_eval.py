"""Offline, pure evaluation of Entry V3 execution-selection comparators (research only).

No I/O, no strategy access, no order authority.  Inputs are candidate telemetry
records (bot.candidate_telemetry), E5B v1 decision labels joined by
client_order_id, and post-hoc official outcomes.  Comparators:

* CURRENT           OBSERVED   real first quote / requotes / fill
* EARLY_CANCEL_C    OBSERVED   v1 EC1@15s only REMOVES CURRENT fills
* FIRST_QUOTE_ONLY  BOUNDED    lower (ask traded through q - tick) / upper (ask touched q)
* CHASE_B           SIMULATED  v1 CH_B@10s adds a fill at the chase execution price
"""
from __future__ import annotations

import math
import random
from typing import Any, Iterable, Optional

FILL = "WOULD_FILL"
NOT_FILL = "WOULD_NOT_FILL"
UNKNOWN = "UNKNOWN"
OBSERVED_SAME = "OBSERVED_SAME"


def classify_fqo(path: Optional[dict], *, current_canonical_fill_ts: Optional[float] = None,
                 deadline_ts: Optional[float] = None) -> dict[str, Any]:
    """FIRST_QUOTE_ONLY fill classification with lower/upper bounds (fill price = first quote).

    * Before the first quote is withdrawn (requote / cancel) the live order IS the first
      quote: a canonical fill before withdrawal is OBSERVED_SAME (both bounds fill).
    * Never withdrawn and never filled: the real order rested to the end -> NOT_FILL (observed).
    * After withdrawal: LOWER fills iff best ask traded through q - 1 tick at/after the
      withdrawal (queue-proof); UPPER fills iff best ask touched q.  Midpoint never counts.
    * No path samples -> UNKNOWN (never assume a fill).
    """
    if not path or path.get("first_quote_price") is None:
        return {"lower": UNKNOWN, "upper": UNKNOWN, "t_fill_lower": None, "t_fill_upper": None,
                "evidence": "NO_PATH_STATS", "fill_price": None}
    q = float(path["first_quote_price"])
    withdrawn = path.get("first_quote_withdrawn_ts")
    fill_ts = current_canonical_fill_ts if current_canonical_fill_ts is not None else path.get("canonical_fill_ts")
    end = deadline_ts if deadline_ts is not None else path.get("path_end_ts")
    if fill_ts is not None and (withdrawn is None or fill_ts <= withdrawn):
        return {"lower": FILL, "upper": FILL, "t_fill_lower": fill_ts, "t_fill_upper": fill_ts,
                "evidence": OBSERVED_SAME, "fill_price": q}
    if withdrawn is None:
        return {"lower": NOT_FILL, "upper": NOT_FILL, "t_fill_lower": None, "t_fill_upper": None,
                "evidence": OBSERVED_SAME, "fill_price": q}
    if not path.get("path_samples"):
        return {"lower": UNKNOWN, "upper": UNKNOWN, "t_fill_lower": None, "t_fill_upper": None,
                "evidence": "NO_PATH_SAMPLES_AFTER_WITHDRAWAL", "fill_price": None}

    def within(t):
        return t is not None and t >= withdrawn and (end is None or t <= end)
    tl, tu = path.get("t_through_after_withdraw"), path.get("t_touch_after_withdraw")
    return {"lower": FILL if within(tl) else NOT_FILL, "upper": FILL if within(tu) else NOT_FILL,
            "t_fill_lower": tl if within(tl) else None, "t_fill_upper": tu if within(tu) else None,
            "evidence": "BOUNDED_BEST_ASK_ONLY", "fill_price": q}


def hold_payoff_per_share(win: Optional[int], price: Optional[float]) -> Optional[float]:
    if win is None or price is None:
        return None
    return float(win) - float(price)


def paired_rows(candidates: Iterable[dict]) -> list[dict]:
    """Per canonical candidate: HOLD PnL for each comparator and paired deltas vs CURRENT.

    Each input dict: market_slug, day, win (0/1/None), qty, current_filled (bool),
    current_fill_price, fqo (classify_fqo output), c_decision (WOULD_CANCEL/...),
    c_removes_fill (bool: CURRENT filled after the C decision point), b_decision,
    b_chase_price (execution price at decision + latency or None).
    """
    out = []
    for c in candidates:
        win, qty = c.get("win"), float(c.get("qty") or 0)
        if win is None:
            out.append({**c, "excluded": "OUTCOME_UNKNOWN"})
            continue
        cur = qty * hold_payoff_per_share(win, c["current_fill_price"]) if c.get("current_filled") else 0.0
        fq = c.get("fqo") or {}
        def fq_pnl(bound):
            st = fq.get(bound)
            if st == FILL:
                return qty * hold_payoff_per_share(win, fq.get("fill_price"))
            if st == NOT_FILL:
                return 0.0
            return None
        lower, upper = fq_pnl("lower"), fq_pnl("upper")
        c_pnl = 0.0 if (c.get("c_decision") == "WOULD_CANCEL" and c.get("c_removes_fill")) else cur
        b_pnl = cur
        if c.get("b_decision") == "WOULD_CHASE" and not c.get("current_filled") and c.get("b_chase_price") is not None:
            b_pnl = qty * hold_payoff_per_share(win, c["b_chase_price"])
        out.append({**c, "excluded": None, "pnl_current": cur, "pnl_fqo_lower": lower, "pnl_fqo_upper": upper,
                    "pnl_c": c_pnl, "pnl_b": b_pnl,
                    "d_fqo_lower": None if lower is None else lower - cur,
                    "d_fqo_upper": None if upper is None else upper - cur,
                    "d_c": c_pnl - cur, "d_b": b_pnl - cur})
    return out


def bootstrap_sum(values: list[float], *, b: int = 2000, seed: int = 20261012, block: int = 1) -> tuple:
    vals = [v for v in values if v is not None]
    if not vals:
        return (None, None)
    rng = random.Random(seed)
    n = len(vals)
    blocks = [vals[i:i + block] for i in range(0, n - block + 1)] if block > 1 else [[v] for v in vals]
    sums = []
    for _ in range(b):
        s, k = 0.0, 0
        while k < n:
            blk = blocks[rng.randrange(len(blocks))]
            take = blk[: n - k]
            s += sum(take)
            k += len(take)
        sums.append(s)
    sums.sort()
    return sums[int(0.05 * b)], sums[int(0.95 * b) - 1]


def comparator_table(rows: list[dict]) -> list[dict]:
    used = [r for r in rows if not r.get("excluded")]
    spec = (("CURRENT", "OBSERVED", "pnl_current", None, lambda r: bool(r.get("current_filled")),
             lambda r: r.get("current_fill_price")),
            ("FQO_LOWER", "BOUNDED", "pnl_fqo_lower", "d_fqo_lower", lambda r: (r.get("fqo") or {}).get("lower") == FILL,
             lambda r: (r.get("fqo") or {}).get("fill_price")),
            ("FQO_UPPER", "BOUNDED", "pnl_fqo_upper", "d_fqo_upper", lambda r: (r.get("fqo") or {}).get("upper") == FILL,
             lambda r: (r.get("fqo") or {}).get("fill_price")),
            ("EARLY_CANCEL_C", "OBSERVED", "pnl_c", "d_c",
             lambda r: bool(r.get("current_filled")) and not (r.get("c_decision") == "WOULD_CANCEL" and r.get("c_removes_fill")),
             lambda r: r.get("current_fill_price")),
            ("CHASE_B", "SIMULATED", "pnl_b", "d_b",
             lambda r: bool(r.get("current_filled")) or (r.get("b_decision") == "WOULD_CHASE" and r.get("b_chase_price") is not None),
             lambda r: r.get("current_fill_price") if r.get("current_filled") else r.get("b_chase_price")))
    out = []
    for name, tier, pk, dk, filled, price in spec:
        fr = [r for r in used if filled(r) and r.get(pk) is not None]
        n_known = [r for r in used if r.get(pk) is not None]
        shares = sum(float(r.get("qty") or 0) for r in fr)
        tot = sum(r[pk] for r in n_known)
        deltas = [r[dk] for r in used if dk and r.get(dk) is not None]
        out.append({"policy": name, "evidence_tier": tier, "candidate_n": len(used), "known_n": len(n_known),
                    "fill_n": len(fr), "fill_rate": (len(fr) / len(used)) if used else None,
                    "mean_fill": (sum(price(r) for r in fr) / len(fr)) if fr else None,
                    "win_rate": (sum(r["win"] for r in fr) / len(fr)) if fr else None,
                    "hold_ev_per_share": (sum(r[pk] for r in fr) / shares) if shares else None,
                    "total_hold_pnl": tot, "paired_delta_vs_current": sum(deltas) if dk else 0.0,
                    "paired_delta_boot90": bootstrap_sum(deltas) if dk else (None, None),
                    "paired_delta_block4_boot90": bootstrap_sum(deltas, block=4) if dk else (None, None)})
    return out


def selection_table(rows: list[dict]) -> list[dict]:
    used = [r for r in rows if not r.get("excluded")]
    out = []
    for name, pk, dk in (("FQO_LOWER", "pnl_fqo_lower", "d_fqo_lower"), ("FQO_UPPER", "pnl_fqo_upper", "d_fqo_upper"),
                         ("EARLY_CANCEL_C", "pnl_c", "d_c"), ("CHASE_B", "pnl_b", "d_b")):
        avoided = missed = added_w = added_l = 0
        for r in used:
            if r.get(pk) is None:
                continue
            cur_in = bool(r.get("current_filled"))
            pol_in = _policy_filled(name, r)
            if cur_in and not pol_in:
                avoided += int(r["win"] == 0)
                missed += int(r["win"] == 1)
            elif pol_in and not cur_in:
                added_w += int(r["win"] == 1)
                added_l += int(r["win"] == 0)
        deltas = [r[dk] for r in used if r.get(dk) is not None]
        out.append({"comparator": name, "avoided_losers": avoided, "missed_winners": missed,
                    "added_winners": added_w, "added_losers": added_l,
                    "net_selection_balance": (avoided - missed) + (added_w - added_l),
                    "ev_delta_vs_current": sum(deltas)})
    return out


def _policy_filled(name: str, r: dict) -> bool:
    if name.startswith("FQO"):
        return (r.get("fqo") or {}).get("lower" if name == "FQO_LOWER" else "upper") == FILL
    if name == "EARLY_CANCEL_C":
        return bool(r.get("current_filled")) and not (r.get("c_decision") == "WOULD_CANCEL" and r.get("c_removes_fill"))
    if name == "CHASE_B":
        return bool(r.get("current_filled")) or (r.get("b_decision") == "WOULD_CHASE" and r.get("b_chase_price") is not None)
    return bool(r.get("current_filled"))


def funnel(candidate_records: list[dict], lifecycle: list[dict], rows: list[dict]) -> list[dict]:
    canon = canonical_candidates(candidate_records)
    fills = {r.get("market_slug") for r in lifecycle if r.get("event_type") in ("ORDER_FILLED", "ORDER_DRY_RUN_FILLED")
             and r.get("side") == "BUY"}
    acc = [c for c in canon if c["eligibility"] == "ACCEPTED"]
    used = [r for r in rows if not r.get("excluded")]
    return [
        {"stage": "All candidates (canonical markets)", "n": len(canon)},
        {"stage": "Eligible", "n": len(acc)},
        {"stage": "Rejected", "n": len(canon) - len(acc)},
        {"stage": "Submitted", "n": len(acc)},
        {"stage": "Filled CURRENT", "n": sum(1 for c in acc if c["market_slug"] in fills)},
        {"stage": "Unfilled CURRENT", "n": sum(1 for c in acc if c["market_slug"] not in fills)},
        {"stage": "FQO would fill (lower)", "n": sum(1 for r in used if (r.get("fqo") or {}).get("lower") == FILL)},
        {"stage": "FQO would fill (upper)", "n": sum(1 for r in used if (r.get("fqo") or {}).get("upper") == FILL)},
        {"stage": "C would cancel", "n": sum(1 for r in used if r.get("c_decision") == "WOULD_CANCEL")},
        {"stage": "B would chase", "n": sum(1 for r in used if r.get("b_decision") == "WOULD_CHASE")},
    ]


def canonical_candidates(candidate_records: list[dict]) -> list[dict]:
    """Offline canonical rule: FIRST_ACCEPTED if present, else FIRST_EVALUATION; dedupe by id (earliest)."""
    by_market: dict[str, dict[str, dict]] = {}
    for r in candidate_records:
        if r.get("record_type") != "CANDIDATE" or r.get("candidate_role") not in ("FIRST_ACCEPTED", "FIRST_EVALUATION"):
            continue
        m = by_market.setdefault(r["market_slug"], {})
        prev = m.get(r["candidate_role"])
        if prev is None or r["decision_ts"] < prev["decision_ts"]:
            m[r["candidate_role"]] = r
    return [m.get("FIRST_ACCEPTED") or m["FIRST_EVALUATION"] for _, m in sorted(by_market.items())]


def world1_ev(candidate: dict, win: Optional[int], taker_fee_per_share: float = 0.0) -> Optional[float]:
    """Opportunity EV at decision: official outcome minus executable ask (and verified taker fee)."""
    ask = candidate.get("executable_ask")
    if win is None or ask is None:
        return None
    return float(win) - float(ask) - float(taker_fee_per_share)


def mde_candidates(sd_per_candidate: float, delta: float, z_alpha: float = 1.2816, z_power: float = 0.8416) -> int:
    return int(math.ceil(((z_alpha + z_power) * sd_per_candidate / delta) ** 2))
