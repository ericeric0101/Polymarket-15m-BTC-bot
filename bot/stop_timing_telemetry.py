"""Prospective stop-timing telemetry (research-only, no execution authority).

Compares, for every ACTUAL live position, the held side's adverse cross of the
strike (official Chainlink TWAP vs strike) with the conditional absolute-loss
breaker and the catastrophic breaker, from the same protective-evaluation
observations the exit engine already sees.

Isolation contract (tested in ``tests/test_stop_timing_telemetry.py``):

* ``observe_safe`` / ``on_settlement_safe`` never raise, never return a trading
  decision and never touch orders or inventory.  Every exception is swallowed
  and counted.
* Emission uses an injected non-blocking sink (``TradeJournalDB.
  enqueue_strategy_event``: non-blocking lock + ``put_nowait`` onto the existing
  journal worker), so the order path never holds the journal write lock for it.
* A call slower than ``SLOW_CALL_SEC`` is counted; after ``MAX_SLOW_CALLS`` the
  recorder disables itself for the rest of the run.
* Only transitions are written (position opened, cross / re-cross, 5/15/30 s
  persistence checkpoints, first-true of each breaker component, first breaker
  eligibility, hard-decision changes, first degradation, settlement), bounded
  per position epoch and per run.
* Persistence checkpoints are telemetry only.  Nothing here feeds back into
  ``ExitPolicyEngine`` or ``submit_order``.
* Missing or stale inputs are written as ``None`` with an explicit
  ``*_state = "UNKNOWN"`` / ``"DEGRADED"``; nothing is interpolated.

``required_move_sigma`` is the legacy TTE-decayed, floored safety sigma.  It is
NOT a calibrated probability and NOT a standard-normal z-score;
``required_move_z_diffusion`` is the model-consistent diffusion distance when
available.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Callable, Optional

SCHEMA_VERSION = "stop_timing_v1"
EVENT_PREFIX = "STOP_TIMING_"
PERSISTENCE_CHECKPOINTS_SEC = (5, 15, 30)
REFERENCE_MAX_AGE_SEC = 5.0
MAX_EVENTS_PER_EPOCH = 80
MAX_CROSS_EVENTS_PER_EPOCH = 12
MAX_EVENTS_PER_RUN = 5000
MAX_EPOCHS = 32
SLOW_CALL_SEC = 0.05
MAX_SLOW_CALLS = 3

# Conditional absolute-loss breaker components, mirroring
# bot/exit_engine.py::ExitPolicyEngine.evaluate (absolute_max_loss_breaker).
ABS_COMPONENTS = (
    "abs_min_hold", "abs_price_adverse", "abs_adverse_trend_confirmed",
    "abs_loss_threshold", "abs_tte_le_120", "abs_persistence_ge_15s",
    "abs_votes_ge_2_of_2",
)


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _dec(value: Any) -> Optional[Decimal]:
    number = _num(value)
    return Decimal(str(number)) if number is not None else None


def absolute_breaker_components(
    *, enabled: bool, min_hold_sec: float, loss_usdc: Decimal, thesis_min_score_abs: Decimal,
    hold_sec: float, avg_entry: Decimal, best_bid: Decimal, gross_if_exit: Decimal,
    net_if_exit: Decimal, time_left_sec: Optional[float], locked_side_invalidated: bool,
    signal_matches_position: bool, signal_side: str, signal_locked: bool, signal_score: Decimal,
    adverse_persistence_sec: float, weakening_count: int, available_count: int,
) -> dict[str, bool]:
    """Pure mirror of the absolute-breaker predicate, split into components."""
    signal_is_none = str(signal_side).upper() == "NONE"
    strong_opposite = (
        (not signal_matches_position) and not signal_is_none and signal_locked
        and abs(signal_score) >= thesis_min_score_abs
    )
    comps = {
        "abs_min_hold": hold_sec >= max(0.0, float(min_hold_sec)),
        "abs_price_adverse": bool(avg_entry > 0 and best_bid < avg_entry and gross_if_exit < 0),
        "abs_adverse_trend_confirmed": bool(locked_side_invalidated or strong_opposite),
        "abs_loss_threshold": net_if_exit <= -abs(loss_usdc),
        "abs_tte_le_120": time_left_sec is not None and time_left_sec <= 120.0,
        "abs_persistence_ge_15s": adverse_persistence_sec >= 15.0,
        "abs_votes_ge_2_of_2": weakening_count >= 2 and available_count >= 2,
    }
    comps["abs_eligible_recomputed"] = bool(
        enabled and comps["abs_min_hold"] and comps["abs_price_adverse"]
        and comps["abs_adverse_trend_confirmed"] and comps["abs_loss_threshold"]
        and (comps["abs_tte_le_120"] or (comps["abs_persistence_ge_15s"] and comps["abs_votes_ge_2_of_2"]))
    )
    return comps


def signed_strike_distance(held_side: str, reference: Optional[Decimal],
                           strike: Optional[Decimal]) -> tuple[Optional[float], Optional[float], str]:
    """Distance in the held side's favour (USD, bps) and cross state.

    Settlement tie semantics: TWAP == strike settles UP, so a tie is favourable
    for UP and adverse for DOWN.
    """
    side = str(held_side).upper()
    if reference is None or strike is None or strike <= 0 or side not in {"UP", "DOWN"}:
        return None, None, "UNKNOWN"
    raw = reference - strike
    distance = raw if side == "UP" else -raw
    adverse = raw < 0 if side == "UP" else raw >= 0
    return float(distance), float(distance / strike * Decimal("10000")), ("ADVERSE" if adverse else "FAVORABLE")


class StopTimingTelemetry:
    def __init__(self, *, emit: Optional[Callable[[str, dict[str, Any]], Any]], run_id: str = "",
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._emit_sink = emit
        self.run_id = str(run_id)
        self._clock = clock
        self._epochs: dict[str, dict[str, Any]] = {}
        self.counters = {"observations": 0, "emitted": 0, "sink_rejected": 0, "capped": 0,
                         "exceptions": 0, "slow_calls": 0}
        self.disabled = False

    # ------------------------------------------------------------------ safety
    def _guarded(self, fn: Callable[..., None], kwargs: dict[str, Any]) -> None:
        if self.disabled:
            return
        started = self._clock()
        try:
            fn(**kwargs)
        except Exception:
            self.counters["exceptions"] += 1
        try:
            if self._clock() - started > SLOW_CALL_SEC:
                self.counters["slow_calls"] += 1
                if self.counters["slow_calls"] >= MAX_SLOW_CALLS:
                    self.disabled = True
        except Exception:
            self.counters["exceptions"] += 1

    def observe_safe(self, **kwargs: Any) -> None:
        """Never raises; returns nothing (no decision authority)."""
        self._guarded(self._observe, kwargs)

    def on_settlement_safe(self, **kwargs: Any) -> None:
        self._guarded(self._on_settlement, kwargs)

    def _emit(self, epoch: Optional[dict[str, Any]], event: str, payload: dict[str, Any]) -> None:
        if self._emit_sink is None:
            return
        total = self.counters["emitted"] + self.counters["sink_rejected"]
        if total >= MAX_EVENTS_PER_RUN or (epoch is not None and epoch["events"] >= MAX_EVENTS_PER_EPOCH):
            self.counters["capped"] += 1
            return
        if epoch is not None:
            epoch["events"] += 1
        body = {"telemetry_schema": SCHEMA_VERSION, "event": event, **payload}
        try:
            accepted = self._emit_sink(EVENT_PREFIX + event, body)
        except Exception:
            self.counters["exceptions"] += 1
            return
        if accepted is False:
            self.counters["sink_rejected"] += 1
        else:
            self.counters["emitted"] += 1

    # ------------------------------------------------------------- observation
    def _observe(
        self, *, now_ts: float, slug: str, instrument_id: str, held_side: str, state: dict[str, Any],
        qty: Any, sellable_qty: Any, best_bid: Any, best_ask: Any, time_left_sec: Optional[float],
        market_end_ts: Optional[float], strike: Any, reference_spot: Any, reference_source: str,
        reference_ts: float, twap_features: Optional[dict[str, Any]], exit_decision: Any,
        engine_config: Any, signal_decision: Any, locked_side_invalidated: bool,
        adverse_persistence_sec: float, thesis_votes: Optional[dict[str, Any]], hold_sec: float,
        sizing_rule_version: Optional[str] = None,
    ) -> None:
        self.counters["observations"] += 1
        mono = self._clock()
        opened_ts = _num(state.get("opened_ts")) or 0.0
        avg_entry = _dec(state.get("avg_entry_price")) or Decimal("0")
        key = f"{slug}|{instrument_id}|{opened_ts:.6f}"
        epoch = self._epochs.get(key)
        qty_d, bid_d = _dec(qty) or Decimal("0"), _dec(best_bid)
        strike_d = _dec(strike)
        feats = twap_features or {}
        twap = _dec(feats.get("official_current_twap"))
        twap_src_ts = _num(feats.get("source_ts"))
        twap_age = max(0.0, float(now_ts) - twap_src_ts) if twap_src_ts else None
        twap_fresh = twap is not None and twap_age is not None and twap_age <= REFERENCE_MAX_AGE_SEC
        distance_usd, distance_bps, cross_state = signed_strike_distance(
            held_side, twap if twap_fresh else None, strike_d)
        if twap is None:
            ref_state = "UNKNOWN"
        elif not twap_fresh:
            ref_state, cross_state = "DEGRADED", "UNKNOWN"
        else:
            ref_state = "FRESH"
        decision_reason = str(getattr(exit_decision, "reason", "") or "")
        net = _dec(getattr(exit_decision, "net_if_exit", None))
        gross = _dec(getattr(exit_decision, "gross_if_exit", None))
        votes = thesis_votes or {}
        snap = {
            "slug": slug, "instrument_id": instrument_id, "held_side": held_side,
            "position_epoch": key, "obs_wall_ts": float(now_ts), "obs_monotonic": mono,
            "obs_interval_sec": (float(now_ts) - epoch["last_obs_ts"]) if epoch else None,
            "qty": float(qty_d), "sellable_qty": _num(sellable_qty), "avg_entry": float(avg_entry),
            "cost_basis_usdc": float(qty_d * avg_entry),
            "best_bid": _num(best_bid), "best_ask": _num(best_ask),
            "exit_px_effective": _num(getattr(exit_decision, "exit_px_effective", None)),
            "net_if_exit": _num(net), "gross_if_exit": _num(gross),
            "time_left_sec": _num(time_left_sec), "hold_sec": _num(hold_sec), "strike": _num(strike),
            "settlement_reference_twap": _num(twap), "settlement_reference_age_sec": twap_age,
            "settlement_reference_state": ref_state,
            "reference_spot": _num(reference_spot), "reference_source": reference_source or None,
            "reference_spot_age_sec": (float(now_ts) - reference_ts) if reference_ts and reference_ts > 0 else None,
            "orderbook_state": "FRESH_BY_PROTECTIVE_GATE" if bid_d is not None and bid_d > 0 else "UNKNOWN",
            "signed_distance_usd": distance_usd, "signed_distance_bps": distance_bps,
            "cross_state": cross_state,
            "required_move_sigma_legacy": _num(feats.get("required_move_sigma")) if twap_fresh else None,
            "required_move_z_diffusion": _num(feats.get("required_move_z_diffusion")) if twap_fresh else None,
            "sigma_ex_market_source": feats.get("sigma_ex_market_source"),
            "required_move_z_sigma_source": feats.get("required_move_z_sigma_source"),
            "required_move_mode": feats.get("required_move_mode"),
            "probability_model_mode": feats.get("probability_model_mode"),
            "decision_reason": decision_reason,
            "decision_type": str(getattr(getattr(exit_decision, "decision_type", None), "value",
                                         getattr(exit_decision, "decision_type", "")) or ""),
            "adverse_persistence_sec_engine": _num(adverse_persistence_sec),
            "thesis_weakening_count": int(votes.get("weakening_count", 0) or 0),
            "thesis_available_count": int(votes.get("available_count", 0) or 0),
            "locked_side_invalidated": bool(locked_side_invalidated),
        }
        if epoch is None:
            if len(self._epochs) >= MAX_EPOCHS:
                self._epochs.pop(next(iter(self._epochs)))
            epoch = {"events": 0, "first": {}, "cross_seq": 0, "cross_state": None, "cross_ts": None,
                     "checkpoints_done": set(), "last_decision": None, "degraded": set(),
                     "slug": slug, "held_side": held_side, "avg_entry": float(avg_entry),
                     "max_qty": float(qty_d), "opened_ts": opened_ts, "last_obs_ts": float(now_ts),
                     "last_mode": None}
            self._epochs[key] = epoch
            tte_entry = (float(market_end_ts) - opened_ts) if market_end_ts and opened_ts > 0 else None
            self._emit(epoch, "POSITION_OPENED", {
                **snap, "entry_fill_ts": opened_ts or None, "entry_price": float(avg_entry),
                "entry_qty": float(qty_d), "entry_cost_usdc": float(qty_d * avg_entry),
                "entry_fee_remaining": _num(state.get("entry_fee_remaining")),
                "fair_at_entry": _num(state.get("fair_at_entry")),
                "tte_at_entry_sec": tte_entry, "sizing_rule_version": sizing_rule_version,
                "first_observation_lag_sec": (float(now_ts) - opened_ts) if opened_ts > 0 else None,
            })
        epoch["max_qty"] = max(epoch["max_qty"], float(qty_d))

        # Degradation (first occurrence per reason only).
        for reason, bad in (("settlement_reference_unknown", ref_state == "UNKNOWN"),
                            ("settlement_reference_stale", ref_state == "DEGRADED"),
                            ("strike_unavailable", strike_d is None)):
            if bad and reason not in epoch["degraded"]:
                epoch["degraded"].add(reason)
                self._emit(epoch, "DEGRADED_FIRST", {**snap, "degraded_reason": reason})

        # Cross / re-cross transitions (UNKNOWN never creates a transition).
        prev = epoch["cross_state"]
        if cross_state in {"ADVERSE", "FAVORABLE"} and cross_state != prev:
            if cross_state == "ADVERSE":
                epoch["cross_seq"] += 1
                epoch["cross_ts"] = float(now_ts)
                epoch["checkpoints_done"] = set()
                if epoch["cross_seq"] <= MAX_CROSS_EVENTS_PER_EPOCH:
                    epoch["first"].setdefault("adverse_cross", float(now_ts))
                    self._emit(epoch, "ADVERSE_CROSS", {
                        **snap, "cross_seq": epoch["cross_seq"],
                        "adverse_at_first_observation": prev is None,
                        "cross_quality": "first_observation" if prev is None else (
                            "observed_transition" if snap["obs_interval_sec"] is not None else "unknown"),
                    })
                else:
                    self.counters["capped"] += 1
            elif prev == "ADVERSE":
                if epoch["cross_seq"] <= MAX_CROSS_EVENTS_PER_EPOCH:
                    epoch["first"].setdefault("favorable_recross", float(now_ts))
                    self._emit(epoch, "FAVORABLE_RECROSS", {
                        **snap, "cross_seq": epoch["cross_seq"],
                        "adverse_duration_sec": float(now_ts) - float(epoch["cross_ts"] or now_ts),
                    })
                epoch["cross_ts"] = None
            epoch["cross_state"] = cross_state
        # Persistence checkpoints after the latest adverse cross: telemetry only.
        if epoch["cross_ts"] is not None and epoch["cross_seq"] <= MAX_CROSS_EVENTS_PER_EPOCH:
            elapsed = float(now_ts) - float(epoch["cross_ts"])
            due = [c for c in PERSISTENCE_CHECKPOINTS_SEC if elapsed >= c and c not in epoch["checkpoints_done"]]
            for checkpoint in due:
                epoch["checkpoints_done"].add(checkpoint)
                self._emit(epoch, "PERSISTENCE_CHECKPOINT", {
                    **snap, "cross_seq": epoch["cross_seq"], "checkpoint_sec": checkpoint,
                    "elapsed_since_cross_sec": elapsed, "capture_lag_sec": elapsed - checkpoint,
                    "still_adverse": cross_state == "ADVERSE" if cross_state != "UNKNOWN" else None,
                })

        # Final-window target switch.
        mode = feats.get("required_move_mode")
        if mode and mode != epoch["last_mode"]:
            if epoch["last_mode"] is not None:
                self._first(epoch, "required_move_mode_switch", snap, {"from_mode": epoch["last_mode"], "to_mode": mode})
            epoch["last_mode"] = mode

        # Breaker components (first-true only).
        cfg = engine_config
        if cfg is not None and net is not None and gross is not None and bid_d is not None:
            comps = absolute_breaker_components(
                enabled=bool(getattr(cfg, "absolute_max_loss_enabled", False)),
                min_hold_sec=float(getattr(cfg, "absolute_max_loss_min_hold_sec", 60)),
                loss_usdc=Decimal(str(getattr(cfg, "absolute_max_loss_usdc", "2.00"))),
                thesis_min_score_abs=Decimal(str(getattr(cfg, "stop_loss_thesis_min_score_abs", "0.05"))),
                hold_sec=float(hold_sec), avg_entry=avg_entry, best_bid=bid_d, gross_if_exit=gross,
                net_if_exit=net, time_left_sec=_num(time_left_sec),
                locked_side_invalidated=bool(locked_side_invalidated),
                signal_matches_position=bool(getattr(signal_decision, "matches_position", True)),
                signal_side=str(getattr(signal_decision, "active_side", "NONE") or "NONE"),
                signal_locked=bool(getattr(signal_decision, "locked", False)),
                signal_score=_dec(getattr(signal_decision, "score", 0)) or Decimal("0"),
                adverse_persistence_sec=float(adverse_persistence_sec or 0.0),
                weakening_count=snap["thesis_weakening_count"], available_count=snap["thesis_available_count"],
            )
            snap_c = {**snap, "abs_components": comps}
            for name in ABS_COMPONENTS:
                if comps[name]:
                    self._first(epoch, name, snap_c)
        else:
            snap_c = {**snap, "abs_components": None, "abs_components_state": "UNKNOWN"}
        if decision_reason == "absolute_max_loss_breaker":
            self._first(epoch, "abs_breaker_eligible", snap_c, event="BREAKER_FIRST_ELIGIBLE",
                        extra={"breaker": "conditional_absolute_loss_breaker"})
        if decision_reason in {"catastrophic_stop_loss_confirming", "catastrophic_stop_loss_confirmed"}:
            self._first(epoch, "catastrophic_candidate", snap_c)
        if decision_reason == "catastrophic_stop_loss_confirmed":
            self._first(epoch, "catastrophic_eligible", snap_c, event="BREAKER_FIRST_ELIGIBLE",
                        extra={"breaker": "catastrophic_stop_loss"})
        # Hard / stop decision changes (deduplicated).
        if decision_reason != epoch["last_decision"]:
            if epoch["last_decision"] is not None and (
                    "stop" in decision_reason or "breaker" in decision_reason
                    or "stop" in str(epoch["last_decision"]) or "breaker" in str(epoch["last_decision"])):
                self._emit(epoch, "DECISION_CHANGE", {**snap_c, "previous_decision_reason": epoch["last_decision"]})
            epoch["last_decision"] = decision_reason
        epoch["last_obs_ts"] = float(now_ts)
        epoch["last_snap"] = {k: snap[k] for k in ("obs_wall_ts", "qty", "net_if_exit", "best_bid",
                                                    "cross_state", "time_left_sec", "signed_distance_bps")}

    def _first(self, epoch: dict[str, Any], name: str, snap: dict[str, Any], extra: Optional[dict[str, Any]] = None,
               event: str = "COMPONENT_FIRST_TRUE") -> None:
        if name in epoch["first"]:
            return
        epoch["first"][name] = float(snap["obs_wall_ts"])
        self._emit(epoch, event, {**snap, "component": name, **(extra or {})})

    # -------------------------------------------------------------- settlement
    def _on_settlement(self, *, slug: str, outcome: str, settlement_ts: float,
                       reference_source: Optional[str] = None, reference_is_canonical: Optional[bool] = None) -> None:
        outcome_u = str(outcome or "UNKNOWN").upper()
        for key in [k for k, e in self._epochs.items() if e["slug"] == str(slug)]:
            epoch = self._epochs.pop(key)
            held = str(epoch["held_side"]).upper()
            hold_pnl = None
            if outcome_u in {"UP", "DOWN"}:
                payout = 1.0 if held == outcome_u else 0.0
                # Counterfactual: hold the max observed quantity to settlement
                # (entry fees excluded; realized PnL comes from journal fills).
                hold_pnl = epoch["max_qty"] * (payout - epoch["avg_entry"])
            self._emit(None, "POSITION_SETTLEMENT", {
                "slug": slug, "position_epoch": key, "held_side": held,
                "settlement_outcome_runtime": outcome_u, "settlement_ts": float(settlement_ts),
                "settlement_reference_source": reference_source,
                "settlement_reference_is_canonical": reference_is_canonical,
                "official_outcome": "PENDING_OFFICIAL_RESOLUTION",
                "counterfactual_hold_gross_pnl": hold_pnl, "max_qty": epoch["max_qty"],
                "avg_entry": epoch["avg_entry"], "first_times": dict(epoch["first"]),
                "cross_count": epoch["cross_seq"], "last_observation": epoch.get("last_snap"),
                "telemetry_counters": dict(self.counters), "telemetry_disabled": self.disabled,
            })
