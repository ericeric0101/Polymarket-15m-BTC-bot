"""Prospective, observation-only early-entry and exit experiment.

This module deliberately has no order/venue APIs. Its only persistence surface
is the bounded asynchronous research journal.
"""
from __future__ import annotations

from datetime import datetime
from math import floor
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo


CONFIGS = (("120_0", 120, 0), ("120_2", 120, 2), ("120_5", 120, 5))
EXIT_POLICIES = ("HOLD", "TP20", "TRAIL5", "TRAIL10", "COMBINED180", "COMBINED300")
NOTIONAL_USDC = 5.0
# Optional trajectory persistence only; all quote-driven calculations still run.
OPTIONAL_SNAPSHOT_INTERVAL_SEC = 5.0
# Under research storage pressure the optional trajectory slows down; exits,
# reversals, loss warnings and settlements are never thinned.
PRESSURE_SNAPSHOT_INTERVAL_SEC = {"db_size_cap": 30.0, "free_disk_low": 60.0}
PRESSURE_MATERIAL_MIN_GAP_SEC = {"db_size_cap": 5.0, "free_disk_low": 15.0}
CAPTURE_POLICY_VERSION = 2
PROFIT_LOCK_LADDER = ((.05, 0.0), (.10, .03), (.15, .05), (.20, .10), (.30, .15))


def _num(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


def _levels(rows: Iterable[Any] | None) -> list[tuple[float, float]]:
    result = []
    for row in rows or ():
        try:
            price = _num(getattr(row, "price", row[0] if isinstance(row, (tuple, list)) else None))
            size_value = getattr(row, "size", None)
            size = _num(size_value() if callable(size_value) else size_value if size_value is not None else row[1])
            if price is not None and size is not None and price > 0 and size > 0:
                result.append((price, size))
        except Exception:
            continue
    return result


def _depth(levels: list[tuple[float, float]], best: float, side: str, cents: int) -> float | None:
    if not levels:
        return None
    band = cents / 100.0
    return sum(size for price, size in levels if (price <= best + band if side == "ask" else price >= best - band))


def _weighted_price(levels: list[tuple[float, float]], shares: float, side: str) -> tuple[float | None, float]:
    remaining, cost, filled = shares, 0.0, 0.0
    ordered = sorted(levels, key=lambda item: item[0], reverse=(side == "bid"))
    for price, size in ordered:
        take = min(remaining, size)
        cost += take * price
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            break
    return (cost / filled if filled > 0 else None), filled


def _time_labels(ts: float) -> tuple[str, int]:
    et = datetime.fromtimestamp(ts, ZoneInfo("America/New_York"))
    return ("weekend" if et.weekday() >= 5 else "weekday", et.hour)


def _production_signal_side(inputs: dict[str, Any]) -> str | None:
    score = _num(inputs.get("composite_score"))
    confidence = _num(inputs.get("confidence"))
    min_confidence = _num(inputs.get("min_confidence"))
    up_threshold = _num(inputs.get("up_threshold"))
    down_threshold = _num(inputs.get("down_threshold"))
    if score is None or confidence is None or confidence < (min_confidence if min_confidence is not None else 0.15):
        return None
    if score > (up_threshold if up_threshold is not None else 0.05):
        return "UP"
    if score < -(down_threshold if down_threshold is not None else 0.05):
        return "DOWN"
    return None


class ForwardShadowExperiment:
    """Record one stable 120s opportunity per market/config and replay exits."""

    def __init__(
        self,
        *,
        db: Any,
        run_id: str,
        weekday_only: bool = True,
        canonical_wait_sec: float = 15.0,
        storage_pressure: Callable[[], str] | None = None,
    ) -> None:
        self.db, self.run_id = db, str(run_id)
        self._storage_pressure = storage_pressure
        self.weekday_only = bool(weekday_only)
        self.canonical_wait_sec = max(0.0, float(canonical_wait_sec))
        self._attempted: set[tuple[str, str]] = set()
        self._candidates: dict[str, dict[str, Any]] = {}
        self._tracked_tokens: set[tuple[str, str]] = set()
        self._last_bbo_ts: dict[tuple[str, str], float] = {}
        self._last_snapshot: dict[str, float] = {}
        self._last_health_hour: int | None = None
        self._last_health_counters = {"events_enqueued": 0, "events_suppressed": 0, "queue_drops": 0, "write_errors": 0}
        self.counters = {"events_enqueued": 0, "events_suppressed": 0, "queue_drops": 0, "write_errors": 0}

    def _emit(self, slug: str, ts: float, payload: dict[str, Any]) -> None:
        try:
            accepted = self.db.enqueue_decision(run_id=self.run_id, slug=slug, market_id=None,
                                                decision_epoch_ns=int(ts * 1_000_000_000), payload=payload)
            if accepted is False:
                self.counters["queue_drops"] += 1
            else:
                self.counters["events_enqueued"] += 1
        except Exception:
            self.counters["write_errors"] += 1

    def on_quote(self, *, slug: str, start_ts: float, end_ts: float | None, now_ts: float,
                 instrument_id: Any, side: str, bid: Any, ask: Any, bid_size: Any,
                 ask_size: Any, bid_levels: Iterable[Any] | None, ask_levels: Iterable[Any] | None,
                 reference_spot: Any, reference_ts: Any, reference_source: str,
                 strike: Any, strike_ts: Any, strike_source: str,
                 signal_inputs: dict[str, Any] | None = None, signal_ts: Any = None,
                 fair_probability: Any = None, economics: dict[str, Any] | None = None,
                 live_snapshot: dict[str, Any] | None = None,
                 quote_source_ts: Any = None, strike_crossings: dict[str, Any] | None = None,
                 fair_probability_age_sec: Any = None) -> int:
        slug, side = str(slug or ""), str(side or "").upper()
        now = _num(now_ts)
        start, bid, ask = _num(start_ts), _num(bid), _num(ask)
        spot, strike = _num(reference_spot), _num(strike)
        ref_ts, strike_ts = _num(reference_ts), _num(strike_ts)
        quote_ts = _num(quote_source_ts)
        if not slug or now is None or start is None or bid is None or ask is None or ask < bid:
            return 0
        bl, al = _levels(bid_levels), _levels(ask_levels)
        bid_sz, ask_sz = _num(bid_size), _num(ask_size)
        if not bl and bid_sz is not None:
            bl = [(bid, bid_sz)]
        if not al and ask_sz is not None:
            al = [(ask, ask_sz)]
        market_type, et_hour = _time_labels(now)
        age = max(0.0, now - start)
        signed_usd = spot - strike if spot is not None and strike is not None else None
        trend_bps = signed_usd / strike * 10000 if signed_usd is not None and strike and strike > 0 else None
        leader = "UP" if signed_usd is not None and signed_usd > 0 else "DOWN" if signed_usd is not None and signed_usd < 0 else "TIE" if signed_usd == 0 else None
        is_weekend_shadow = market_type == "weekend"
        signal_inputs = signal_inputs if isinstance(signal_inputs, dict) else {}
        strike_crossings = strike_crossings if isinstance(strike_crossings, dict) else {}
        emitted = 0

        # Candidates are based on the approved canonical Chainlink/TWAP return
        # from market strike (the historical study's closest executable proxy),
        # while the production SignalEngine output is captured alongside it.
        for config, window, threshold in CONFIGS:
            key = (slug, config)
            if key in self._attempted or age < window:
                continue
            # The opening strike and canonical Chainlink/TWAP reference can
            # become eligible a few seconds after the scheduled observation.
            # Keep the schedule pending during a bounded grace period instead
            # of permanently consuming it on the first incomplete quote.
            canonical_wait_timed_out = trend_bps is None and age > window + self.canonical_wait_sec
            if trend_bps is None and not canonical_wait_timed_out:
                continue
            self._attempted.add(key)
            candidate_side = "UP" if trend_bps is not None and trend_bps > threshold else "DOWN" if trend_bps is not None and trend_bps < -threshold else None
            zero = trend_bps == 0
            outcome_token = str(instrument_id)
            desired_token = live_snapshot.get("instrument_for_side", {}).get(candidate_side) if live_snapshot and candidate_side else None
            quote_matches_candidate = candidate_side is not None and side == candidate_side and (desired_token is None or str(desired_token) == outcome_token)
            status = ("candidate" if quote_matches_candidate else "none" if trend_bps is not None
                      else "observation_unavailable" if candidate_side is None else "awaiting_candidate_side_bbo")
            # If this token's BBO does not match the signal side, leave the
            # schedule pending; a later quote for the correct token may capture it.
            if candidate_side is not None and not quote_matches_candidate:
                self._attempted.discard(key)
                continue
            candidate_id = f"{slug}|{config}"
            quote_valid = 0 < bid <= ask < 1
            if status == "candidate" and not quote_valid:
                status = "candidate_quote_invalid"
            shares = NOTIONAL_USDC / ask if status == "candidate" and ask > 0 else None
            top_fillable = bool(ask_sz is not None and shares is not None and ask_sz + 1e-9 >= shares)
            weighted_ask, weighted_ask_fill = _weighted_price(al, shares or 0.0, "ask") if shares else (None, 0.0)
            executable = status == "candidate" and candidate_side == side and quote_valid
            executable_variants = []
            if executable and top_fillable:
                executable_variants.append("ENTRY_TOP_ASK")
            if executable and weighted_ask_fill + 1e-9 >= (shares or 0) and shares:
                executable_variants.append("ENTRY_DEPTH_WEIGHTED_5USD")
            entry_execution_status = ("fillable" if executable_variants else "ENTRY_INSUFFICIENT_DEPTH") if executable else "not_applicable"
            gross_edge = _num((economics or {}).get("gross_probability_edge_ps"))
            if gross_edge is None and executable and _num(fair_probability) is not None:
                gross_edge = _num(fair_probability) - ask
            payload = {
                "event_type": "SHADOW_ENTRY_CANDIDATE", "candidate_id": candidate_id,
                "slug": slug, "run_id": self.run_id, "entry_config": config,
                "window_sec": window, "threshold_bps": threshold, "market_age_sec": age,
                "schedule_lateness_sec": max(0.0, age - window),
                "canonical_wait_sec": self.canonical_wait_sec,
                "canonical_wait_timed_out": canonical_wait_timed_out,
                "candidate_ts": now, "candidate_side": candidate_side, "signal_status": status,
                "observation_unavailable_reason": None if trend_bps is not None else "canonical_reference_or_strike_unavailable",
                "signal_source": "canonical_chainlink_twap_vs_official_market_strike",
                "signal_fast": _num(signal_inputs.get("btc_ema_fast")),
                "signal_slow": _num(signal_inputs.get("btc_ema_slow")),
                "production_btc_trend": _num(signal_inputs.get("btc_trend")),
                "trend_bps": trend_bps, "confidence": _num(signal_inputs.get("confidence")),
                "production_composite_score": _num(signal_inputs.get("composite_score")),
                "production_signal_side": _production_signal_side(signal_inputs),
                "production_side": str((live_snapshot or {}).get("production_side") or "") or None,
                "production_signal_age_sec": max(0.0, now - _num(signal_ts)) if _num(signal_ts) else None,
                "near_zero_signal": bool(trend_bps is not None and abs(trend_bps) < 0.5),
                "exact_zero_tie": bool(zero), "is_weekend": is_weekend_shadow,
                "weekday_only_primary": self.weekday_only,
                "experiment_class": ("WEEKEND_SHADOW_ONLY" if self.weekday_only else "WEEKEND_PRIMARY") if is_weekend_shadow else "WEEKDAY_PRIMARY",
                "et_hour": et_hour, "taiwan_hour": datetime.fromtimestamp(now, ZoneInfo("Asia/Taipei")).hour,
                "instrument_id": outcome_token, "token_side": side,
                "official_strike": strike, "strike_source": strike_source or None,
                "strike_ts": strike_ts, "strike_age_sec": max(0.0, now - strike_ts) if strike_ts else None,
                "reference_spot": spot, "reference_source": reference_source or None,
                "reference_ts": ref_ts, "reference_age_sec": max(0.0, now - ref_ts) if ref_ts else None,
                "signed_distance_usd": signed_usd, "abs_distance_usd": abs(signed_usd) if signed_usd is not None else None,
                "signed_distance_bps": trend_bps, "abs_distance_bps": abs(trend_bps) if trend_bps is not None else None,
                "leader_side": leader, "entry_top_ask": ask if executable else None,
                "crossings_last_60s": strike_crossings.get("crossings_last_60s"),
                "crossings_last_120s": strike_crossings.get("crossings_last_120s"),
                "entry_bid": bid if executable else None, "entry_bid_size": bid_sz,
                "entry_ask_size": ask_sz, "entry_spread": ask - bid,
                "quote_source_ts": quote_ts,
                "quote_age_sec": max(0.0, now - quote_ts) if quote_ts else None,
                "entry_mid": (ask + bid) / 2,
                "entry_top_ask_fillable_notional": ask * ask_sz if ask_sz is not None else None,
                "research_notional_usdc": NOTIONAL_USDC,
                "research_shares": shares if executable else None,
                "top_level_fillable": top_fillable if executable else None,
                "depth_weighted_entry_price": weighted_ask if executable and weighted_ask_fill + 1e-9 >= (shares or 0) else None,
                "depth_weighted_entry_status": "filled" if weighted_ask_fill + 1e-9 >= (shares or 0) and shares else "insufficient_depth" if executable else "not_applicable",
                "entry_executable_variants": executable_variants,
                "entry_execution_status": entry_execution_status,
                "bid_depth_1c": _depth(bl, bid, "bid", 1), "bid_depth_2c": _depth(bl, bid, "bid", 2),
                "bid_depth_5c": _depth(bl, bid, "bid", 5), "ask_depth_1c": _depth(al, ask, "ask", 1),
                "ask_depth_2c": _depth(al, ask, "ask", 2), "ask_depth_5c": _depth(al, ask, "ask", 5),
                "fair_probability": _num(fair_probability),
                "fair_probability_age_sec": _num(fair_probability_age_sec),
                "gross_probability_edge_ps": gross_edge,
                "net_directional_edge_ps": _num((economics or {}).get("net_directional_edge_ps")),
                "robust_net_usdc": _num((economics or {}).get("robust_net_usdc")),
                "fee_estimate": _num((economics or {}).get("fee_estimate")),
                "execution_penalty": _num((economics or {}).get("execution_penalty")),
                "live_comparator": live_snapshot or {},
                "authority": "research_only_no_order_or_ownership",
            }
            self._emit(slug, now, payload)
            emitted += 1
            if executable:
                variants = [("ENTRY_TOP_ASK", ask)] if top_fillable else []
                if payload["depth_weighted_entry_status"] == "filled":
                    variants.append(("ENTRY_DEPTH_WEIGHTED_5USD", weighted_ask))
                for variant, entry_price in variants:
                    position_id = f"{candidate_id}|{variant}"
                    state = {"candidate_id": candidate_id, "position_id": position_id,
                             "entry_variant": variant, "slug": slug, "config": config,
                             "side": side, "token": outcome_token, "entry_ts": now,
                             "entry_ask": entry_price, "bid": bid, "shares": NOTIONAL_USDC / entry_price,
                             "policy": {}, "mfe": 0.0, "mae": 0.0, "mfe_ts": now, "mae_ts": now,
                             "seen_drawdowns": set(), "seen_recoveries": set(), "first_cross_against_ts": None,
                             "last_leader": leader, "crossings_since_entry": 0,
                             "signal_reversed": False, "fair_entry": _num(fair_probability),
                             "loss_warning_emitted": False,
                             "last_mark_ts": 0.0, "entry_payload": payload}
                    for policy in EXIT_POLICIES:
                        state["policy"][policy] = {"status": "open", "exit_ts": None, "exit_price": None,
                                                    "exit_depth_price": None, "reason": None, "peak_return": 0.0}
                    self._candidates[position_id] = state
                    self._tracked_tokens.add((slug, outcome_token))
        # Continue mark/path capture for every active candidate matching this quote.
        for state in tuple(self._candidates.values()):
            if state["slug"] == slug and state["token"] == str(instrument_id):
                self._mark(state, now, bid, ask, bid_sz, ask_sz, bl, al, side,
                           trend_bps, leader, signal_inputs, fair_probability, end_ts, reference_ts, quote_ts,
                           _num(fair_probability_age_sec))
        if (slug, str(instrument_id)) in self._tracked_tokens:
            self._record_bbo(slug=slug, now=now, instrument_id=str(instrument_id), side=side,
                bid=bid, ask=ask, bid_size=bid_sz, ask_size=ask_sz, bids=bl, asks=al,
                time_left=max(0.0, _num(end_ts) - now) if _num(end_ts) else None,
                quote_ts=quote_ts, reference_ts=ref_ts)
        self._maybe_emit_health(slug, now)
        return emitted

    def _capture_policy(self) -> tuple[float, float, str]:
        """Return (periodic interval, material-change min gap, policy label)."""
        try:
            reason = str(self._storage_pressure() or "") if self._storage_pressure is not None else ""
        except Exception:
            reason = ""
        if reason in PRESSURE_SNAPSHOT_INTERVAL_SEC:
            return PRESSURE_SNAPSHOT_INTERVAL_SEC[reason], PRESSURE_MATERIAL_MIN_GAP_SEC[reason], f"storage_{reason}"
        return OPTIONAL_SNAPSHOT_INTERVAL_SEC, 1.0, "normal"

    def _record_bbo(self, *, slug: str, now: float, instrument_id: str, side: str,
                    bid: float, ask: float, bid_size: float | None, ask_size: float | None,
                    bids: list[tuple[float, float]], asks: list[tuple[float, float]],
                    time_left: float | None, quote_ts: float | None, reference_ts: float | None) -> None:
        token_key = (slug, instrument_id)
        interval, _, policy = self._capture_policy()
        if now - self._last_bbo_ts.get(token_key, 0.0) < interval:
            self.counters["events_suppressed"] += 1
            return
        self._last_bbo_ts[token_key] = now
        self._emit(slug, now, {"event_type": "SHADOW_BBO_SNAPSHOT", "slug": slug,
            "capture_policy_version": CAPTURE_POLICY_VERSION, "snapshot_interval_sec": interval,
            "capture_policy": policy,
            "candidate_ids": [s["candidate_id"] for s in self._candidates.values()
                              if s["slug"] == slug and s["token"] == instrument_id],
            "instrument_id": instrument_id, "side": side, "event_ts": now,
            "best_bid": bid, "best_bid_size": bid_size, "best_ask": ask, "best_ask_size": ask_size,
            "spread": ask - bid, "mid": (ask + bid) / 2,
            "bid_depth_1c": _depth(bids, bid, "bid", 1), "bid_depth_2c": _depth(bids, bid, "bid", 2),
            "bid_depth_5c": _depth(bids, bid, "bid", 5), "ask_depth_1c": _depth(asks, ask, "ask", 1),
            "ask_depth_2c": _depth(asks, ask, "ask", 2), "ask_depth_5c": _depth(asks, ask, "ask", 5),
            "time_left_sec": time_left, "quote_source_ts": quote_ts,
            "quote_age_sec": max(0.0, now - quote_ts) if quote_ts else None,
            "reference_age_sec": max(0.0, now - reference_ts) if reference_ts else None,
            "authority": "research_only_no_order_or_ownership"})

    def _maybe_emit_health(self, slug: str, now: float) -> None:
        hour = int(now // 3600)
        if self._last_health_hour == hour:
            return
        self._last_health_hour = hour
        db_health = getattr(self.db, "research_health", lambda: {})()
        deltas = {f"{key}_this_hour": max(0, int(self.counters.get(key, 0)) - int(self._last_health_counters.get(key, 0)))
                  for key in self._last_health_counters}
        db_drops = int(db_health.get("queue_drops", 0) or 0)
        db_errors = int(db_health.get("write_errors", 0) or 0)
        db_rows_written = int(db_health.get("decision_rows_written", 0) or 0)
        deltas["db_queue_drops_this_hour"] = max(0, db_drops - self._last_health_counters.get("db_queue_drops", 0))
        deltas["db_write_errors_this_hour"] = max(0, db_errors - self._last_health_counters.get("db_write_errors", 0))
        deltas["db_decision_rows_written_this_hour"] = max(0, db_rows_written - self._last_health_counters.get("db_decision_rows_written", 0))
        self._last_health_counters = {**self.counters, "db_queue_drops": db_drops,
            "db_write_errors": db_errors, "db_decision_rows_written": db_rows_written}
        self._emit(slug, now, {"event_type": "FORWARD_SHADOW_CAPTURE_HEALTH", "run_id": self.run_id,
            "hour_epoch": hour * 3600, **self.counters, **deltas, **db_health,
            "capture_policy_version": CAPTURE_POLICY_VERSION, "snapshot_interval_sec": OPTIONAL_SNAPSHOT_INTERVAL_SEC,
            "authority": "research_only_no_order_or_ownership"})

    def _mark(self, state: dict[str, Any], now: float, bid: float, ask: float,
              bid_size: float | None, ask_size: float | None,
              bids: list[tuple[float, float]], asks: list[tuple[float, float]], side: str,
              trend_bps: float | None, leader: str | None, signal_inputs: dict[str, Any],
              fair: Any, end_ts: Any, reference_ts: float | None, quote_ts: float | None,
              fair_age: float | None) -> None:
        if not 0 < bid <= ask < 1 or bid <= 0:
            return
        entry = state["entry_ask"]
        ret = (bid - entry) / entry
        if ret > state["mfe"]:
            state["mfe"] = ret
            state["mfe_ts"] = now
        if ret < state["mae"]:
            state["mae"] = ret
            state["mae_ts"] = now
        for boundary in (0.05, 0.10, 0.15, 0.20):
            if state["mae"] <= -boundary:
                state["seen_drawdowns"].add(boundary)
            if ret >= boundary:
                state["seen_recoveries"].add(boundary)
        position_side = state["side"]
        prior_leader = state.get("last_leader")
        if leader in {"UP", "DOWN"} and prior_leader in {"UP", "DOWN"} and leader != prior_leader:
            state["crossings_since_entry"] += 1
        if leader in {"UP", "DOWN"}:
            state["last_leader"] = leader
        if leader in {"UP", "DOWN"} and leader != position_side and state["first_cross_against_ts"] is None:
            state["first_cross_against_ts"] = now
        score = _num(signal_inputs.get("composite_score"))
        prod_side = _production_signal_side(signal_inputs)
        reversed_now = prod_side is not None and prod_side != position_side
        probability = _num(fair)
        fair_deterioration = (
            state["fair_entry"] - probability
            if probability is not None and state["fair_entry"] is not None else None
        )
        thesis_components = {
            "signal_reversed": reversed_now,
            "strike_crossed_against": leader in {"UP", "DOWN"} and leader != position_side,
            # fair is already the held token's probability, for both UP and DOWN.
            "fair_deteriorated_5pct": fair_deterioration is not None and fair_deterioration >= .05 - 1e-12,
        }
        thesis_count = sum(bool(value) for value in thesis_components.values())
        if reversed_now and not state["signal_reversed"]:
            state["signal_reversed"] = True
            self._emit(state["slug"], now, {"event_type": "SHADOW_SIGNAL_REVERSAL", "candidate_id": state["candidate_id"],
                "position_id": state["position_id"], "entry_variant": state["entry_variant"],
                "signal_side": prod_side, "signal_score": score, "trend_bps": trend_bps,
                "event_ts": now, "authority": "research_only_no_order_or_ownership"})
        # Periodic trajectory every five seconds; peak/risk transitions retain the
        # existing one-second bound. Exit/reversal/loss events remain independent.
        signature = (round(bid, 2), round(ask, 2), round(state["mfe"], 2), round(state["mae"], 2), reversed_now, leader)
        key = state["position_id"]
        previous = getattr(self, "_last_signature", {}).get(key)
        elapsed_since_snapshot = now - state["last_mark_ts"]
        interval, material_gap, policy = self._capture_policy()
        due = elapsed_since_snapshot >= interval
        prior_mfe, prior_mae = previous[2:4] if previous is not None else (None, None)
        material = previous is not None and (
            abs(round(state["mfe"], 2) - prior_mfe) >= 0.01
            or abs(round(state["mae"], 2) - prior_mae) >= 0.01
            or signature[4] != previous[4]
            or signature[5] != previous[5]
        )
        if due or (material and elapsed_since_snapshot >= material_gap):
            age_ref = max(0.0, now - reference_ts) if reference_ts else None
            self._emit(state["slug"], now, {"event_type": "SHADOW_POSITION_MARK", "candidate_id": state["candidate_id"],
                "capture_policy_version": CAPTURE_POLICY_VERSION, "snapshot_interval_sec": interval,
                "capture_policy": policy,
                "position_id": key, "entry_variant": state["entry_variant"],
                "slug": state["slug"], "entry_config": state["config"], "side": position_side,
                "instrument_id": state["token"], "event_ts": now, "best_bid": bid,
                "time_left_sec": max(0.0, _num(end_ts) - now) if _num(end_ts) else None,
                "quote_source_ts": quote_ts,
                "quote_age_sec": max(0.0, now - quote_ts) if quote_ts else None,
                "mark_return_pct": ret, "mark_pnl_usdc": ret * NOTIONAL_USDC,
                "mfe_bid_pct": state["mfe"], "mae_bid_pct": state["mae"],
                "time_to_mfe_sec": state["mfe_ts"] - state["entry_ts"], "time_to_mae_sec": state["mae_ts"] - state["entry_ts"],
                "signal_side": prod_side, "signal_score": score,
                "trend_bps": trend_bps, "confidence": _num(signal_inputs.get("confidence")),
                "signal_reversed": reversed_now, "leader_side": leader,
                "crossings_since_entry": state["crossings_since_entry"],
                "strike_crossed_against": leader in {"UP", "DOWN"} and leader != position_side,
                "first_strike_cross_against_ts": state["first_cross_against_ts"],
                "fair_probability": probability,
                "fair_probability_age_sec": fair_age,
                "fair_minus_bid": probability - bid if probability is not None else None,
                "fair_minus_ask": probability - ask if probability is not None else None,
                "fair_change_from_entry": probability - state["fair_entry"] if probability is not None and state["fair_entry"] is not None else None,
                "shadow_thesis_invalidated_components": thesis_components,
                "thesis_weakening_count": thesis_count,
                "loss_warning_active": ret <= -0.08,
                "reference_age_sec": age_ref, "authority": "research_only_no_order_or_ownership"})
            state["last_mark_ts"] = now
            if not hasattr(self, "_last_signature"):
                self._last_signature = {}
            self._last_signature[key] = signature
        else:
            self.counters["events_suppressed"] += 1
        if ret <= -0.08 and not state["loss_warning_emitted"]:
            state["loss_warning_emitted"] = True
            self._emit(state["slug"], now, {"event_type": "SHADOW_LOSS_WARNING",
                "candidate_id": state["candidate_id"], "position_id": key,
                "entry_variant": state["entry_variant"], "mark_return_pct": ret,
                "mark_pnl_usdc": ret * NOTIONAL_USDC,
                "thesis_weakening_count": thesis_count,
                "thesis_components": thesis_components, "event_ts": now,
                "authority": "research_only_no_order_or_ownership"})
        for policy, rule in state["policy"].items():
            if rule["status"] != "open":
                continue
            trigger = None
            if policy == "TP20" and ret + 1e-9 >= 0.20:
                trigger = "tp20"
            elif policy in {"TRAIL5", "TRAIL10"}:
                rule["peak_return"] = max(rule["peak_return"], ret)
                trail = 0.05 if policy == "TRAIL5" else 0.10
                if rule["peak_return"] + 1e-9 >= 0.05 and ret <= rule["peak_return"] - trail + 1e-9:
                    trigger = f"{policy.lower()}_drawdown"
            elif policy in {"COMBINED180", "COMBINED300"}:
                rule["peak_return"] = max(rule["peak_return"], ret)
                active_floors = [floor for activation, floor in PROFIT_LOCK_LADDER
                                 if rule["peak_return"] + 1e-12 >= activation]
                profit_floor = max(active_floors) if active_floors else None
                trailing_floor = rule["peak_return"] - .05 if rule["peak_return"] >= .05 else None
                protected_floor = max(value for value in (profit_floor, trailing_floor) if value is not None) \
                    if profit_floor is not None or trailing_floor is not None else None
                age = now - state["entry_ts"]
                no_progress_age = 180 if policy == "COMBINED180" else 300
                if ret * NOTIONAL_USDC <= -2.0 + 1e-12:
                    trigger = "hard_max_loss_2usdc"
                elif ret <= -0.10 + 1e-12 and thesis_count >= 2:
                    trigger = "thesis_weakening_loss"
                elif (age >= no_progress_age and rule["peak_return"] < .05
                      and ret <= 0 and thesis_count >= 2):
                    trigger = f"no_progress_{no_progress_age}s_with_thesis_weakening"
                elif protected_floor is not None and ret <= protected_floor + 1e-12:
                    trigger = "profit_floor_or_peak_trail"
            if trigger:
                weighted, filled = _weighted_price(bids, state["shares"], "bid")
                rule.update({"status": "exited", "exit_ts": now, "exit_price": bid,
                             "exit_depth_price": weighted if filled + 1e-9 >= state["shares"] else None,
                             "top_exit_fillable": bid_size is not None and bid_size + 1e-9 >= state["shares"],
                             "reason": trigger})
                top_pnl = state["shares"] * (bid - state["entry_ask"])
                depth_pnl = state["shares"] * (weighted - state["entry_ask"]) if rule["exit_depth_price"] is not None else None
                self._emit(state["slug"], now, {"event_type": "SHADOW_EXIT", "candidate_id": state["candidate_id"],
                    "position_id": key, "entry_variant": state["entry_variant"],
                    "entry_config": state["config"], "exit_policy": policy, "exit_reason": trigger,
                    "exit_ts": now, "exit_best_bid": bid, "exit_depth_weighted_bid": rule["exit_depth_price"],
                    "depth_exit_status": "filled" if rule["exit_depth_price"] is not None else "insufficient_depth",
                    "top_exit_fillable": rule["top_exit_fillable"],
                    "top_quote_pnl_usdc": top_pnl,
                    "pnl_usdc": top_pnl if rule["top_exit_fillable"] else None,
                    "depth_weighted_pnl_usdc": depth_pnl,
                    "exit_return_pct": ret,
                    "thesis_weakening_count": thesis_count,
                    "thesis_components": thesis_components,
                    "authority": "research_only_no_order_or_ownership"})

    def on_settlement(self, *, slug: str, outcome: str, settlement_ts: float,
                      settlement_source: str = "unknown") -> int:
        slug, outcome = str(slug), str(outcome or "").upper()
        ended = 0
        for position_id, state in tuple(self._candidates.items()):
            if state["slug"] != slug:
                continue
            for policy, rule in state["policy"].items():
                if rule["status"] == "open" and outcome in {"UP", "DOWN"}:
                    rule.update({"status": "settled", "exit_ts": float(settlement_ts),
                                 "exit_price": 1.0 if outcome == state["side"] else 0.0,
                                 "reason": "settlement"})
                settlement_price = (1.0 if outcome == state["side"] else 0.0) if rule["reason"] == "settlement" else rule["exit_price"]
                gross_pnl = state["shares"] * (settlement_price - state["entry_ask"]) if settlement_price is not None else None
                pnl = gross_pnl if rule["reason"] == "settlement" or rule.get("top_exit_fillable") else None
                depth_pnl = (gross_pnl if rule["reason"] == "settlement" else
                             state["shares"] * (rule["exit_depth_price"] - state["entry_ask"])
                             if rule["exit_depth_price"] is not None else None)
                self._emit(slug, float(settlement_ts), {"event_type": "SHADOW_SETTLEMENT", "candidate_id": state["candidate_id"],
                    "position_id": position_id, "entry_variant": state["entry_variant"],
                    "entry_config": state["config"], "side": state["side"], "exit_policy": policy,
                    "outcome": outcome if outcome in {"UP", "DOWN"} else None,
                    "event_ts": float(settlement_ts),
                    "settlement_status": "observed" if outcome in {"UP", "DOWN"} else "canonical_settlement_unavailable",
                    "settlement_source": settlement_source,
                    "is_weekend": bool(state["entry_payload"].get("is_weekend")),
                    "entry_price": state["entry_ask"],
                    "entry_top_ask": state["entry_payload"].get("entry_top_ask"),
                    "entry_depth_weighted_price": state["entry_payload"].get("depth_weighted_entry_price"),
                    "pnl_usdc": pnl, "depth_weighted_pnl_usdc": depth_pnl,
                    "top_quote_pnl_usdc": gross_pnl,
                    "top_exit_fillable": rule.get("top_exit_fillable"),
                    "exit_reason": rule["reason"], "exit_ts": rule["exit_ts"], "exit_price": settlement_price,
                    "mfe_bid_pct": state["mfe"], "mae_bid_pct": state["mae"],
                    "ever_mae_le_minus_5": 0.05 in state["seen_drawdowns"],
                    "ever_mae_le_minus_10": 0.10 in state["seen_drawdowns"],
                    "ever_mae_le_minus_15": 0.15 in state["seen_drawdowns"],
                    "ever_mae_le_minus_20": 0.20 in state["seen_drawdowns"],
                    "recovered_plus_5": 0.05 in state["seen_recoveries"],
                    "recovered_plus_10": 0.10 in state["seen_recoveries"],
                    "recovered_plus_20": 0.20 in state["seen_recoveries"],
                    "profitable_settlement": outcome == state["side"] if outcome in {"UP", "DOWN"} else None,
                    "authority": "research_only_no_order_or_ownership"})
            ended += 1
            self._candidates.pop(position_id, None)
            getattr(self, "_last_signature", {}).pop(position_id, None)
        if not any(state["slug"] == slug for state in self._candidates.values()):
            self._tracked_tokens = {key for key in self._tracked_tokens if key[0] != slug}
            self._last_bbo_ts = {key: value for key, value in self._last_bbo_ts.items() if key[0] != slug}
        self._maybe_emit_health(slug, float(settlement_ts))
        return ended


def record_strategy_quote(strategy: Any, *, instrument_id: Any, now_ts: float,
                          bid: Any, ask: Any, bid_size: Any, ask_size: Any,
                          quote_source_ts: float) -> int:
    """Best-effort bridge; all exceptions are contained in research code."""
    experiment = getattr(strategy, "forward_shadow_experiment", None)
    if experiment is None:
        return 0
    try:
        slug = str(getattr(strategy, "current_market_slug", "") or "")
        start = int(slug.rsplit("-", 1)[1])
        side_value = strategy._side_for_instrument_id(instrument_id)
        side = str(getattr(side_value, "value", side_value) or "").upper()
        strike_map = getattr(strategy, "market_strike_cache_by_slug", {}) or {}
        strike = strike_map.get(slug)
        strike_eligible = getattr(strategy, "_market_strike_is_entry_eligible", lambda _slug: False)(slug)
        strike_ts_map = getattr(strategy, "market_strike_observed_ts_by_slug", {}) or {}
        strike_source_map = getattr(strategy, "market_strike_source_by_slug", {}) or {}
        source = str(getattr(strategy, "latest_external_spot_source", "") or "")
        spot = getattr(strategy, "latest_external_spot", None)
        spot_ts = getattr(strategy, "latest_external_spot_source_ts", None)
        # Approved production strike-relative reference only; never Binance proxy.
        canonical = (source.startswith("polymarket_chainlink_twap_") and strike_eligible
                     and _num(spot_ts) is not None and 0 <= float(now_ts) - float(spot_ts) <= 10.0)
        signal = getattr(strategy, "side_decision_inputs", {}) or {}
        signal_engine = getattr(strategy, "_signal_engine", None)
        signal = {**signal,
                  "btc_ema_fast": getattr(getattr(signal_engine, "_btc_ema_fast", None), "value", None),
                  "btc_ema_slow": getattr(getattr(signal_engine, "_btc_ema_slow", None), "value", None),
                  "min_confidence": getattr(strategy, "side_signal_min_confidence", 0.15),
                  "up_threshold": getattr(strategy, "side_signal_threshold_up", 0.05),
                  "down_threshold": getattr(strategy, "side_signal_threshold_down", 0.05)}
        crossing_tracker = getattr(strategy, "_live_strike_crossing_tracker", None)
        crossing = crossing_tracker.snapshot(slug, float(now_ts)) if crossing_tracker is not None else {}
        forecast = getattr(strategy, "last_forecast_state", None)
        forecast_ts = _num(getattr(forecast, "created_ts", None)) if forecast is not None else None
        fair_age = max(0.0, float(now_ts) - forecast_ts) if forecast_ts is not None else None
        fair = getattr(forecast, "selected_up_probability", None) if fair_age is not None and fair_age <= 10.0 else None
        fair_side = fair if side == "UP" else (1 - fair if fair is not None else None)
        economics = getattr(strategy, "last_live_economics", None)
        economics = economics if isinstance(economics, dict) else {}
        inst_for_side = {"UP": str(getattr(strategy, "current_up_instrument_id", "") or ""),
                         "DOWN": str(getattr(strategy, "current_down_instrument_id", "") or "")}
        comparator = {"production_side": str(getattr(strategy, "active_side", "") or ""),
                      "instrument_for_side": inst_for_side,
                      "live_candidate_existed": bool(getattr(strategy, "active_maker_orders", {})),
                      "live_entry_price": None, "live_fill_price": None, "live_qty": None,
                      "live_exit_reason": None, "live_exit_price": None, "live_pnl": None}
        return experiment.on_quote(
            slug=slug, start_ts=start, end_ts=getattr(strategy, "current_market_end_timestamp", None),
            now_ts=now_ts, instrument_id=instrument_id, side=side, bid=bid, ask=ask,
            bid_size=bid_size, ask_size=ask_size,
            bid_levels=(strategy.cache.order_book(instrument_id).bids() if getattr(strategy, "cache", None) and strategy.cache.order_book(instrument_id) else None),
            ask_levels=(strategy.cache.order_book(instrument_id).asks() if getattr(strategy, "cache", None) and strategy.cache.order_book(instrument_id) else None),
            reference_spot=spot if canonical else None,
            reference_ts=spot_ts if canonical else None,
            reference_source=source if canonical else "unavailable_noncanonical_source",
            strike=strike if canonical else None,
            strike_ts=strike_ts_map.get(slug) if canonical else None,
            strike_source=strike_source_map.get(slug) if canonical else None,
            signal_inputs=signal, signal_ts=getattr(strategy, "side_decision_ts", None),
            fair_probability=fair_side, economics=economics, live_snapshot=comparator,
            quote_source_ts=quote_source_ts, strike_crossings=crossing,
            fair_probability_age_sec=fair_age)
    except Exception:
        experiment.counters["write_errors"] += 1
        return 0
