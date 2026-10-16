"""Research-only stop-loss shadow on simulated positions: what if we had stopped here?

For each open DRY-RUN shadow position, a candidate is recorded at most once per transition:
  ADVERSE_CROSS            official TWAP first moves to the side against the position
  ADVERSE_CROSS_PERSIST_15S the adverse state has lasted >= 15 s continuously (descriptive horizon)
  HARD_LOSS_EQUIVALENT     mark loss >= the configured absolute-max-loss breaker after its min hold
Each record carries the state needed for analysis (exit bid, score, TTE, distance, legacy sigma,
diffusion z, cross state/duration/count, market-implied and model probability) and
PnL_if_stop_now = qty * (exit_bid - entry). At settlement every candidate is resolved with
PnL_if_hold = qty * (payout - entry) from the canonical outcome. Nothing here can submit, cancel
or modify an order; adaptive stops stay OFF.

Volume: <= 3 candidates + 1 resolution per simulated position (~100 positions/day) ~ 400 rows/day,
< 0.5 MB/day. All fields REQUIRED (small, canonical research evidence).
"""
from __future__ import annotations

from typing import Any

EVENT_CANDIDATE = "STOP_SHADOW_CANDIDATE"
EVENT_RESOLUTION = "STOP_SHADOW_RESOLUTION"
PERSIST_SEC = 15.0


def _num(value: Any) -> float | None:
    try:
        v = float(value)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


class StopCandidateShadow:
    def __init__(self, *, db: Any, run_id: str, hard_loss_usdc: Any = None, hard_loss_min_hold_sec: Any = None) -> None:
        self.db, self.run_id = db, str(run_id)
        self.hard_loss_usdc = _num(hard_loss_usdc)
        self.hard_loss_min_hold_sec = _num(hard_loss_min_hold_sec) or 0.0
        self._positions: dict[str, dict[str, Any]] = {}

    def _emit(self, slug: str, ts: float, payload: dict[str, Any]) -> None:
        if self.db is None:
            return
        try:
            self.db.enqueue_decision(run_id=self.run_id, slug=slug, market_id=None,
                                     decision_epoch_ns=int(ts * 1e9), payload=payload)
        except Exception:
            pass

    def observe(self, *, position: dict[str, Any], bid: Any, ask: Any, now_ts: float, twap: Any, strike: Any,
                time_left_sec: Any, score: Any = None, legacy_sigma: Any = None, z_diffusion: Any = None,
                model_probability: Any = None) -> list[dict[str, Any]]:
        slug, side = str(position["slug"]), str(position["side"]).upper()
        key = str(position.get("simulation_id") or slug)
        state = self._positions.setdefault(key, {"slug": slug, "side": side, "entry_price": _num(position["entry_price"]),
                                                 "qty": _num(position["qty"]), "filled_ts": _num(position.get("filled_ts")),
                                                 "adverse": False, "adverse_since": None, "cross_count": 0,
                                                 "emitted": set(), "candidates": []})
        b, a, tw, k = _num(bid), _num(ask), _num(twap), _num(strike)
        if b is None or tw is None or k is None or not k:
            return []
        adverse = (tw < k) if side == "UP" else (tw > k)
        if adverse and not state["adverse"]:
            state["cross_count"] += 1
            state["adverse_since"] = now_ts
        elif not adverse:
            state["adverse_since"] = None
        state["adverse"] = adverse
        duration = (now_ts - state["adverse_since"]) if state["adverse_since"] is not None else 0.0
        mark_pnl = state["qty"] * (b - state["entry_price"])
        held = now_ts - (state["filled_ts"] or now_ts)
        reasons = []
        if adverse and "ADVERSE_CROSS" not in state["emitted"]:
            reasons.append("ADVERSE_CROSS")
        if adverse and duration >= PERSIST_SEC and "ADVERSE_CROSS_PERSIST_15S" not in state["emitted"]:
            reasons.append("ADVERSE_CROSS_PERSIST_15S")
        if (self.hard_loss_usdc and mark_pnl <= -abs(self.hard_loss_usdc) and held >= self.hard_loss_min_hold_sec
                and "HARD_LOSS_EQUIVALENT" not in state["emitted"]):
            reasons.append("HARD_LOSS_EQUIVALENT")
        out = []
        for reason in reasons:
            state["emitted"].add(reason)
            record = {
                "event_type": EVENT_CANDIDATE, "stop_reason": reason, "slug": slug, "side": side,
                "simulation_id": key, "observed_ts": now_ts, "entry_price": state["entry_price"], "qty": state["qty"],
                "current_exit_bid": b, "current_ask": a,
                "market_implied_probability": (b + a) / 2 if a is not None else b,
                "model_probability": _num(model_probability), "score": _num(score),
                "time_left_sec": _num(time_left_sec), "distance_bps": abs(tw - k) / k * 1e4 if k else None,
                "legacy_sigma": _num(legacy_sigma), "z_diffusion": _num(z_diffusion),
                "cross_state": "ADVERSE" if adverse else "FAVORABLE", "cross_duration_sec": duration,
                "cross_count": state["cross_count"], "held_sec": held,
                "pnl_if_stop_now_usdc": mark_pnl, "pnl_if_stop_now_note": "taker exit at bid; fees excluded",
                "authority": "research_only_no_order_or_ownership",
            }
            state["candidates"].append(record)
            self._emit(slug, now_ts, record)
            out.append(record)
        return out

    def resolve(self, *, slug: str, outcome: str, settlement_ts: float) -> list[dict[str, Any]]:
        outcome = str(outcome or "").upper()
        if outcome not in ("UP", "DOWN"):
            return []  # unknown canonical outcome: keep candidates unresolved, never guess
        out = []
        for key in [k for k, v in self._positions.items() if v["slug"] == str(slug)]:
            state = self._positions.pop(key)
            payout = 1.0 if state["side"] == outcome else 0.0
            hold = state["qty"] * (payout - state["entry_price"])
            for cand in state["candidates"]:
                record = {"event_type": EVENT_RESOLUTION, "simulation_id": key, "slug": state["slug"],
                          "side": state["side"], "stop_reason": cand["stop_reason"], "candidate_ts": cand["observed_ts"],
                          "settlement_outcome": outcome, "pnl_if_stop_now_usdc": cand["pnl_if_stop_now_usdc"],
                          "pnl_if_hold_usdc": hold, "stop_minus_hold_usdc": cand["pnl_if_stop_now_usdc"] - hold,
                          "authority": "research_only_no_order_or_ownership"}
                self._emit(state["slug"], settlement_ts, record)
                out.append(record)
        return out
