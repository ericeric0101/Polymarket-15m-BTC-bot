"""Synchronized, research-only prediction snapshots.

This module observes values already maintained by the live strategy. It has
no imports from execution or strategy policy and never returns a trading
decision. Persistence uses the existing bounded asynchronous LeadLagDB queue.
"""
from __future__ import annotations

from collections import defaultdict, deque
from statistics import mean
from typing import Any

from bot.research.provenance import PREDICTION_SCHEMA_VERSION
from bot.research.clocks import observed_source_reference
import math
import time

from loguru import logger


def _number(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _age(now_ts: float, source_ts: Any) -> float | None:
    ts = _number(source_ts)
    return now_ts - ts if ts is not None and ts > 0 else None


def _fresh(age: float | None, max_age: float) -> bool:
    return age is not None and 0.0 <= age <= max_age


def build_prediction_snapshot(context: dict[str, Any]) -> dict[str, Any]:
    """One freshness authority: local transport and optional same-stream value age.

    Source timestamps are metadata, never subtracted from the local snapshot.
    Legacy age field names remain, with semantics version 2 identifying the
    corrected authority: btc_age_sec/twap_age_sec are local transport ages;
    market source ages are relative to an already received CLOB observation;
    p_ex_age_sec is the maximum of individually validated component ages.
    Without a CLOB reference source age is explicitly unavailable metadata,
    never an invented remote wall clock. Required local receipts/BTC source
    references fail closed. This does not rewrite legacy snapshots.
    """
    now = float(context["snapshot_ts"])
    pex_max_age = float(context.get("pex_max_age_sec", 10.0))
    market_max_age = float(context.get("market_max_age_sec", 2.0))
    btc_max_age = float(context.get("btc_max_age_sec", 10.0))
    pex_age = _number(context.get("p_ex_age_sec"))
    pex_sigma_fresh = context.get("sigma_ex_market_fresh") is True
    pex_raw = _number(context.get("p_up_ex_market"))
    pex_fresh = pex_raw is not None and pex_sigma_fresh and _fresh(pex_age, pex_max_age)
    p_up = pex_raw if pex_fresh else None
    p_down = 1.0 - p_up if p_up is not None else None

    def quote(side: str) -> tuple[float | None, float | None, float | None, float | None, float | None, float | None, float | None, bool]:
        bid = _number(context.get(f"best_bid_{side}"))
        ask = _number(context.get(f"best_ask_{side}"))
        reference_ts = _number(context.get("market_source_reference_ts"))
        source_age = _age(reference_ts, context.get(f"market_{side}_source_ts")) if reference_ts is not None else None
        receive_age = _age(now, context.get(f"market_{side}_received_ts"))
        source_ts = _number(context.get(f"market_{side}_source_ts"))
        received_ts = _number(context.get(f"market_{side}_received_ts"))
        fresh = (bid is not None and ask is not None and 0 <= bid <= ask <= 1
                 and source_ts is not None and source_ts > 0
                 and (context.get("market_source_reference_ts") is None or _fresh(source_age, market_max_age))
                 and _fresh(receive_age, market_max_age))
        if not fresh:
            return None, None, None, source_ts, received_ts, source_age, receive_age, False
        return bid, ask, (bid + ask) / 2.0, source_ts, received_ts, source_age, receive_age, True

    bid_up, ask_up, mid_up, up_ts, up_received_ts, up_input_age, up_input_receive_age, up_fresh = quote("up")
    bid_down, ask_down, mid_down, down_ts, down_received_ts, down_input_age, down_input_receive_age, down_fresh = quote("down")
    up_source_age, up_receive_age = up_input_age, up_input_receive_age
    down_source_age, down_receive_age = down_input_age, down_input_receive_age

    residual_up = p_up - mid_up if p_up is not None and mid_up is not None else None
    residual_down = p_down - mid_down if p_down is not None and mid_down is not None else None
    btc_ts = _number(context.get("btc_source_ts"))
    btc_received_ts = _number(context.get("btc_received_ts"))
    btc_reference_ts = _number(context.get("btc_source_reference_ts"))
    btc_age = _age(now, btc_received_ts)
    btc_value_age = _age(btc_reference_ts, btc_ts) if btc_reference_ts is not None else None
    btc_fresh = _fresh(btc_age, btc_max_age) and _fresh(btc_value_age, btc_max_age)
    btc_spot = _number(context.get("btc_spot")) if btc_fresh else None
    returns = {}
    for horizon in (1, 5, 10, 30, 60):
        value = _number(context.get(f"btc_return_{horizon}s_bps")) if btc_fresh else None
        prior_age = _number(context.get(f"btc_return_{horizon}s_prior_age_sec"))
        returns[f"btc_return_{horizon}s_bps"] = value if _fresh(prior_age, max(2.0, horizon * 0.25)) else None

    side = str(context.get("active_side") or "NONE").upper()
    disagreement = {}
    for horizon in (5, 10, 30):
        ret = returns[f"btc_return_{horizon}s_bps"]
        disagreement[f"btc_disagree_{horizon}s"] = (
            (ret < 0 if side == "UP" else ret > 0 if side == "DOWN" else None)
            if ret is not None else None
        )
    twap_age = _age(now, context.get("twap_received_ts"))
    twap_fresh = _fresh(twap_age, pex_max_age) and (_number(context.get("twap_source_ts")) or 0) > 0
    twap_value = _number(context.get("official_twap")) if twap_fresh else None
    probability_reason = (None if p_up is not None else
                          "p_ex_unavailable" if pex_raw is None else
                          "sigma_stale_or_unavailable" if not pex_sigma_fresh else
                          "p_ex_source_stale")
    identity = dict(context.get("identity") or {})
    result = {
        "event_type": "PREDICTION_RESEARCH_SNAPSHOT",
        "prediction_schema_version": PREDICTION_SCHEMA_VERSION,
        "freshness_clock_semantics_version": 2,
        "snapshot_ts": now,
        "snapshot_trigger": str(context.get("trigger") or "periodic"),
        "snapshot_interval_sec": _number(context.get("snapshot_interval_sec")),
        **identity,
        "p_ex_source_ts": _number(context.get("p_ex_source_ts")),
        "p_ex_age_sec": pex_age,
        "p_ex_received_ts": _number(context.get("p_ex_received_ts")),
        "p_ex_fresh": p_up is not None,
        "p_ex_unavailable_reason": probability_reason,
        "p_up_ex_market": p_up,
        "p_down_ex_market": p_down,
        "probability_model_mode": context.get("probability_model_mode"),
        "probability_model_version": context.get("probability_model_version"),
        "sigma_ex_market": _number(context.get("sigma_ex_market")),
        "sigma_ex_market_fresh": pex_sigma_fresh,
        "sigma_ex_market_age_sec": _number(context.get("sigma_ex_market_age_sec")),
        "sigma_ex_market_transport_age_sec": _number(context.get("sigma_ex_market_transport_age_sec")),
        "sigma_ex_market_value_age_sec": _number(context.get("sigma_ex_market_value_age_sec")),
        "market_source_reference_ts": _number(context.get("market_source_reference_ts")),
        "market_quote_source_ts": max((x for x in (up_ts, down_ts) if x is not None), default=None),
        "market_quote_received_ts": max((x for x in (up_received_ts, down_received_ts) if x is not None), default=None),
        "market_quote_up_source_ts": up_ts, "market_quote_up_received_ts": up_received_ts,
        "market_quote_down_source_ts": down_ts, "market_quote_down_received_ts": down_received_ts,
        "market_source_age_sec": max((x for x in (up_source_age, down_source_age) if x is not None), default=None),
        "market_receive_age_sec": max((x for x in (up_receive_age, down_receive_age) if x is not None), default=None),
        "market_quote_fresh": bool(up_fresh and down_fresh),
        "market_quote_up_fresh": bool(up_fresh), "market_quote_down_fresh": bool(down_fresh),
        "market_mid_fresh": bool(up_fresh or down_fresh),
        "market_mid_up_fresh": bool(up_fresh), "market_mid_down_fresh": bool(down_fresh),
        "market_quote_up_source_age_sec": up_source_age if up_source_age is not None else up_input_age,
        "market_quote_up_receive_age_sec": up_receive_age if up_receive_age is not None else up_input_receive_age,
        "market_quote_down_source_age_sec": down_source_age if down_source_age is not None else down_input_age,
        "market_quote_down_receive_age_sec": down_receive_age if down_receive_age is not None else down_input_receive_age,
        "best_bid_up": bid_up, "best_ask_up": ask_up, "market_mid_up": mid_up,
        "spread_up": ask_up - bid_up if ask_up is not None and bid_up is not None else None,
        "best_bid_down": bid_down, "best_ask_down": ask_down, "market_mid_down": mid_down,
        "spread_down": ask_down - bid_down if ask_down is not None and bid_down is not None else None,
        "residual_up": residual_up, "residual_down": residual_down,
        "edge_vs_ask_up": p_up - ask_up if p_up is not None and ask_up is not None else None,
        "edge_vs_ask_down": p_down - ask_down if p_down is not None and ask_down is not None else None,
        "edge_vs_bid_up": p_up - bid_up if p_up is not None and bid_up is not None else None,
        "edge_vs_bid_down": p_down - bid_down if p_down is not None and bid_down is not None else None,
        "btc_spot": btc_spot, "btc_source_ts": btc_ts, "btc_age_sec": btc_age,
        "btc_received_ts": btc_received_ts, "btc_source_reference_ts": btc_reference_ts,
        "btc_transport_age_sec": btc_age, "btc_value_age_sec": btc_value_age,
        "btc_fresh": btc_spot is not None,
        **returns, **disagreement,
        **{f"btc_return_{horizon}s_prior_age_sec": _number(context.get(f"btc_return_{horizon}s_prior_age_sec"))
           for horizon in (1, 5, 10, 30, 60)},
        "twap_source_ts": _number(context.get("twap_source_ts")),
        "twap_received_ts": _number(context.get("twap_received_ts")),
        "twap_age_sec": twap_age, "twap_fresh": twap_fresh,
        "official_twap": twap_value,
        "settlement_state_side": context.get("settlement_state_side") if twap_fresh else "UNKNOWN",
        "strike": _number(context.get("strike")),
        "required_move_mode": context.get("required_move_mode") if twap_fresh else "UNAVAILABLE",
        "required_future_avg_to_flip": _number(context.get("required_future_avg_to_flip")) if twap_fresh else None,
        "required_move_usd": _number(context.get("required_move_usd")) if twap_fresh else None,
        "required_move_bps": _number(context.get("required_move_bps")) if twap_fresh else None,
        "required_move_sigma": _number(context.get("required_move_sigma")) if twap_fresh else None,
        "remaining_final_window_sec": _number(context.get("remaining_final_window_sec")) if twap_fresh else None,
        "active_side": side,
        "side_score": _number(context.get("side_score")),
        "side_reason": context.get("side_reason"),
        "signal_confidence": _number(context.get("signal_confidence")),
        "market_component": _number(context.get("market_component")),
        "btc_component": _number(context.get("btc_component")),
        "structural_component": _number(context.get("structural_component")),
        "previous_active_side": context.get("previous_active_side"),
        "last_side_change_ts": _number(context.get("last_side_change_ts")),
        "entry_side": context.get("entry_side"),
        "entry_price": _number(context.get("entry_price")),
        "entry_candidate_id": context.get("entry_candidate_id"),
    }
    result["joint_fresh"] = bool(result["p_ex_fresh"] and result["market_mid_fresh"] and result["btc_fresh"] and result["twap_fresh"])
    return result


class PredictionResearchSnapshotter:
    """Best-effort ~1 Hz capture and enqueue via an existing research writer."""

    def __init__(self, *, db: Any, run_id: str, interval_sec: float = 1.0,
                 metrics_interval_sec: float = 60.0) -> None:
        self.db = db
        self.run_id = str(run_id)
        self.interval_sec = max(1.0, float(interval_sec))
        self.metrics_interval_sec = min(60.0, max(30.0, float(metrics_interval_sec)))
        self._last_periodic: dict[str, float] = {}
        self._last_snapshot_ts: dict[str, float] = {}
        self._last_payload_by_slug: dict[str, dict[str, Any]] = {}
        self._last_side_by_slug: dict[str, str] = {}
        self._last_side_change_ts: dict[str, float] = {}
        self._first_marker_accepted = False
        self._first_snapshot_identity = None
        self._collection_identity = {}
        self._last_capture_ts = None
        self._last_enqueue_ts = None
        self._last_metrics_ts = 0.0
        self._intervals: deque[float] = deque(maxlen=600)
        self._latencies_ms: deque[float] = deque(maxlen=600)
        self._counters: dict[str, int] = defaultdict(int)
        self._fresh: dict[str, int] = defaultdict(int)
        self._recent_quality: deque[tuple] = deque(maxlen=600)

    @staticmethod
    def _btc_returns(history: Any, now_ts: float) -> dict[str, float | None]:
        try:
            points = list(history or [])
        except Exception:
            points = []
        points = sorted((float(ts), float(px)) for ts, px, *_ in points
                        if _number(ts) is not None and _number(px) is not None and float(px) > 0 and float(ts) <= now_ts)
        result: dict[str, float | None] = {}
        current = points[-1] if points else None
        for horizon in (1, 5, 10, 30, 60):
            prior = next((item for item in reversed(points[:-1]) if item[0] <= now_ts - horizon), None) if current else None
            result[f"btc_return_{horizon}s_prior_age_sec"] = now_ts - horizon - prior[0] if prior else None
            result[f"btc_return_{horizon}s_bps"] = ((current[1] / prior[1] - 1.0) * 10000.0
                                                       if current and prior else None)
        return result

    def capture(self, strategy: Any, *, now_ts: float | None = None, trigger: str = "periodic",
                force: bool = False, entry_context: dict[str, Any] | None = None) -> dict[str, Any] | None:
        started = time.perf_counter()
        now = float(now_ts if now_ts is not None else time.time())
        slug = str(getattr(strategy, "current_market_slug", "") or "")
        runtime_slug = slug
        research_ahead_of_handoff = False
        # Trading intentionally holds its prior market through settlement grace.
        # Research may observe the wall-open market only through its already
        # admitted pair. This never promotes/subscribes an instrument or changes
        # trading lifecycle state.
        if trigger == "periodic" and slug:
            try:
                runtime_start = float(slug.rsplit("-", 1)[-1])
                if now >= runtime_start + 900:
                    wall_slug = f"{slug.rsplit('-', 1)[0]}-{int(now // 900) * 900}"
                    known = getattr(strategy, "research_market_instruments_by_slug", {}) or {}
                    if wall_slug in known:
                        slug = wall_slug
                        research_ahead_of_handoff = slug != runtime_slug
            except (TypeError, ValueError):
                pass
        if not slug:
            self._collection_identity = {**getattr(strategy, "collection_identity", {}), "run_id": self.run_id}
            self._emit_metrics(now)
            return None
        if trigger == "periodic" and not force:
            last = self._last_periodic.get(slug, 0.0)
            if now - last < self.interval_sec:
                self._emit_metrics(now)
                return None
            self._last_periodic[slug] = now
        self._counters["eligible"] += 1
        self._last_capture_ts = now
        self._collection_identity = {**getattr(strategy, "collection_identity", {}), "run_id": self.run_id}
        try:
            end_ts = None if research_ahead_of_handoff else _number(getattr(strategy, "current_market_end_timestamp", None))
            start_ts = _number(getattr(strategy, "market_start_ts_by_slug", {}).get(slug))
            if start_ts is None:
                try:
                    start_ts = float(slug.rsplit("-", 1)[-1])
                except (TypeError, ValueError):
                    pass
            end_ts = end_ts or (start_ts + 900 if start_ts is not None else None)
            left = end_ts - now if end_ts is not None else None
            if trigger == "periodic" and ((start_ts is not None and now < start_ts)
                                           or (end_ts is not None and now >= end_ts)):
                self._emit_metrics(now)
                return None
            strike = getattr(strategy, "market_strike_cache_by_slug", {}).get(slug)
            if research_ahead_of_handoff and (_number(strike) is None or float(strike) <= 0):
                return None  # wait for this market's strike, never borrow the prior one
            twap = getattr(strategy, "_polymarket_chainlink_twap_price", None)
            twap_ts = _number(getattr(strategy, "_polymarket_chainlink_twap_observation_ts", None))
            twap_received_ts = _number(getattr(strategy, "_polymarket_chainlink_twap_price_ts", None))
            diagnostics = {}
            if twap is not None and callable(getattr(strategy, "_settlement_probability_shadow_inputs", None)):
                diagnostics = strategy._settlement_probability_shadow_inputs(
                    slug=slug, official_twap=twap, strike=strike, time_left_sec=left,
                    now_ts=now, source_observed_ts=twap_ts,
                ) or {}
            spot_source = str(diagnostics.get("path_spot_source") or "")
            if spot_source == "polymarket_chainlink_spot":
                pex_source_ts = _number(getattr(strategy, "_polymarket_chainlink_price_observation_ts", None))
                pex_received_ts = _number(getattr(strategy, "_polymarket_chainlink_price_ts", None))
            elif spot_source == "binance_ws":
                pex_source_ts = _number(getattr(strategy, "_binance_ws_price_source_ts", None))
                pex_received_ts = _number(getattr(strategy, "_binance_ws_price_ts", None))
            else:
                pex_source_ts = None
                pex_received_ts = None
            pex_transport_age = _age(now, pex_received_ts)
            sigma_age = _number(diagnostics.get("sigma_ex_market_age_sec"))
            pair = strategy._research_market_quote_instruments(slug=slug, runtime_slug=runtime_slug)
            up_inst, down_inst = pair or ("", "")
            if research_ahead_of_handoff and (not up_inst or not down_inst):
                return None
            quote_ts_map = dict(getattr(strategy, "last_quote_source_ts_by_inst", {}) or {})
            receive_ts_map = dict(getattr(strategy, "last_quote_received_ts_by_inst", {}) or {})
            q_source = dict(getattr(strategy, "latest_quote_by_inst", {}) or {})
            if research_ahead_of_handoff:
                # Read the existing bounded quote-only prewarm authority; preserve
                # its real receipt/source clocks. Freshness stays in build_snapshot.
                for inst in (up_inst, down_inst):
                    prewarm = (getattr(strategy, "quote_prewarm_latest_by_inst", {}) or {}).get(inst)
                    if prewarm and prewarm.get("received_ts", 0) > receive_ts_map.get(inst, 0):
                        quote_ts_map[inst] = prewarm.get("source_ts")
                        receive_ts_map[inst] = prewarm.get("received_ts")
                        q_source[inst] = (prewarm.get("bid"), prewarm.get("ask"))
            max_quote_age = max(0.1, float(getattr(strategy, "quote_max_delivery_delay_sec", 2.0)))

            def quote_context(side: str, inst: str) -> dict[str, Any]:
                src_ts = _number(quote_ts_map.get(inst)) if inst else None
                recv_ts = _number(receive_ts_map.get(inst)) if inst else None
                book = q_source.get(inst) if inst else None
                bid, ask = (book[0], book[1]) if book and len(book) >= 2 else (None, None)
                return {f"best_bid_{side}": bid, f"best_ask_{side}": ask,
                        f"market_{side}_source_ts": src_ts, f"market_{side}_received_ts": recv_ts,
                        f"market_{side}_receive_age_sec": _age(now, recv_ts)}

            raw_history = getattr(strategy, "_prediction_btc_research_history", ())
            # Only ticks already locally received may supply a source reference.
            btc_points = [point for point in list(raw_history)
                          if len(point) >= 3 and _fresh(_age(now, point[2]), float("inf"))]
            btc = btc_points[-1] if btc_points else None
            btc_source_ts = btc[0] if btc else None
            btc_received_ts = btc[2] if btc else None
            btc_reference_ts = max((point[0] for point in btc_points), default=None)
            btc_spot = btc[1] if btc else None
            market_reference_ts = observed_source_reference(quote_ts_map, receive_ts_map, (up_inst, down_inst), now)
            pex_ages = [pex_transport_age, sigma_age, _age(now, twap_received_ts)]
            if spot_source == "binance_ws":
                pex_ages.append(_age(btc_reference_ts, pex_source_ts) if btc_reference_ts is not None else None)
            pex_max_age = float(getattr(strategy, "_RAW_SPOT_FRESHNESS_SEC", 10.0))
            pex_age = max(pex_ages) if all(_fresh(age, pex_max_age) for age in pex_ages) else None
            trend = {} if research_ahead_of_handoff else (getattr(strategy, "side_decision_inputs", {}) or {})
            side_obj = getattr(strategy, "active_side", "NONE")
            side = "NONE" if research_ahead_of_handoff else str(getattr(side_obj, "value", side_obj) or "NONE").upper()
            previous_side = self._last_side_by_slug.get(slug)
            if previous_side is not None and side != previous_side:
                self._last_side_change_ts[slug] = now
            if side in {"UP", "DOWN"}:
                self._last_side_by_slug[slug] = side
            entry = dict(entry_context or {})
            context = {
                "snapshot_ts": now, "trigger": trigger,
                "snapshot_interval_sec": self.interval_sec,
                "pex_max_age_sec": float(getattr(strategy, "_RAW_SPOT_FRESHNESS_SEC", 10.0)),
                "market_max_age_sec": max_quote_age,
                "btc_max_age_sec": float(getattr(strategy, "side_signal_btc_trend_primary_stale_sec", 10.0)),
                "identity": {"run_id": self.run_id, "market_slug": slug,
                             "market_start_ts": start_ts, "market_end_ts": end_ts,
                             "time_left_sec": left, "up_instrument_id": up_inst or None,
                             "down_instrument_id": down_inst or None,
                             "research_before_trading_handoff": research_ahead_of_handoff},
                "p_up_ex_market": diagnostics.get("p_up_ex_market"),
                "p_ex_age_sec": pex_age, "p_ex_source_ts": pex_source_ts,
                "p_ex_received_ts": pex_received_ts, "market_source_reference_ts": market_reference_ts,
                "sigma_ex_market": diagnostics.get("sigma_ex_market"),
                "sigma_ex_market_fresh": diagnostics.get("sigma_ex_market_fresh"),
                "sigma_ex_market_age_sec": diagnostics.get("sigma_ex_market_age_sec"),
                "sigma_ex_market_transport_age_sec": diagnostics.get("sigma_ex_market_transport_age_sec"),
                "sigma_ex_market_value_age_sec": diagnostics.get("sigma_ex_market_value_age_sec"),
                "probability_model_mode": diagnostics.get("probability_model_mode"),
                "probability_model_version": diagnostics.get("probability_model_version"),
                "official_twap": twap, "twap_source_ts": twap_ts, "twap_received_ts": twap_received_ts,
                "settlement_state_side": diagnostics.get("settlement_state_side"),
                "strike": strike, "required_move_mode": diagnostics.get("required_move_mode"),
                "required_future_avg_to_flip": diagnostics.get("required_future_avg_to_flip"),
                "required_move_usd": diagnostics.get("required_move_usd"),
                "required_move_bps": diagnostics.get("required_move_bps"),
                "required_move_sigma": diagnostics.get("required_move_sigma"),
                "remaining_final_window_sec": diagnostics.get("remaining_final_window_sec"),
                "btc_spot": btc_spot, "btc_source_ts": btc_source_ts,
                "btc_received_ts": btc_received_ts, "btc_source_reference_ts": btc_reference_ts,
                **(self._btc_returns(btc_points, btc_source_ts) if btc_source_ts is not None else {}),
                **quote_context("up", up_inst), **quote_context("down", down_inst),
                "active_side": side,
                "side_score": None if research_ahead_of_handoff else getattr(strategy, "side_decision_score", None),
                "side_reason": "research_before_trading_handoff" if research_ahead_of_handoff else getattr(strategy, "side_decision_reason", None),
                "signal_confidence": trend.get("confidence") if isinstance(trend, dict) else None,
                "market_component": trend.get("market_consensus") if isinstance(trend, dict) else None,
                "btc_component": trend.get("btc_trend") if isinstance(trend, dict) else None,
                "structural_component": trend.get("structural_score") if isinstance(trend, dict) else None,
                "previous_active_side": previous_side,
                "last_side_change_ts": self._last_side_change_ts.get(slug),
                "entry_side": entry.get("entry_side"), "entry_price": entry.get("entry_price"),
                "entry_candidate_id": entry.get("entry_candidate_id"),
            }
            context.update({k: v for k, v in diagnostics.items()
                            if k in {"probability_model_mode", "probability_model_version", "sigma_ex_market",
                                     "sigma_ex_market_fresh", "sigma_ex_market_age_sec", "required_move_mode",
                                     "required_future_avg_to_flip", "required_move_usd", "required_move_bps",
                                     "required_move_sigma", "remaining_final_window_sec", "settlement_state_side"}})
            context["snapshot_ts"] = now
            context["trigger"] = trigger
            context["identity"] = {"run_id": self.run_id, "market_slug": slug,
                                   "market_start_ts": start_ts, "market_end_ts": end_ts,
                                   "time_left_sec": left, "up_instrument_id": up_inst or None,
                                   "down_instrument_id": down_inst or None,
                                   "research_before_trading_handoff": research_ahead_of_handoff}
            context["pex_max_age_sec"] = float(getattr(strategy, "_RAW_SPOT_FRESHNESS_SEC", 10.0))
            context["market_max_age_sec"] = max_quote_age
            context["btc_max_age_sec"] = float(getattr(strategy, "side_signal_btc_trend_primary_stale_sec", 10.0))
            snapshot = build_prediction_snapshot(context)
            interval = now - self._last_snapshot_ts[slug] if self._last_snapshot_ts.get(slug) else None
            snapshot["snapshot_interval_sec"] = interval
            if trigger == "periodic" and interval is not None and interval < self.interval_sec:
                return None
            accepted = bool(self.db is not None and self.db.enqueue_decision(
                run_id=self.run_id, slug=slug, market_id=None,
                decision_epoch_ns=int(now * 1_000_000_000), payload=snapshot,
            ))
            self._counters["written" if accepted else "dropped"] += 1
            self._recent_quality.append((now, bool(snapshot["joint_fresh"]), accepted, interval, slug))
            self._latencies_ms.append((time.perf_counter() - started) * 1000.0)
            if accepted:
                self._last_enqueue_ts = now
                if self._first_snapshot_identity is None:
                    self._first_snapshot_identity = {"snapshot_ts": now, "market_slug": slug}
                if not self._first_marker_accepted:
                    from bot.ops import collection_lifecycle
                    self._first_marker_accepted = collection_lifecycle(
                        strategy, "first_prediction_snapshot", **self._first_snapshot_identity)
                self._last_snapshot_ts[slug] = now
                self._last_payload_by_slug[slug] = snapshot
                self._intervals.append(interval if interval is not None else self.interval_sec)
                for key, value in (("pex", snapshot["p_ex_fresh"]), ("market", snapshot["market_mid_fresh"]), ("joint", snapshot["joint_fresh"])):
                    self._fresh[key] += int(bool(value))
            self._emit_metrics(now)
            return snapshot if accepted else None
        except Exception as exc:
            self._counters["errors"] += 1
            self._emit_metrics(now)
            if self._counters["errors"] == 1:
                logger.warning(f"Prediction research snapshot disabled/skipped: {type(exc).__name__}: {exc}")
            return None

    def recent_health(self, now_ts: float, *, window_sec: float = 300.0, slug: str | None = None) -> dict:
        """Bounded capture/accepted quality; accepted does not prove durable storage.

        joint_fresh_pct is the compatibility alias for capture_joint_fresh_pct.
        Current-slug requests never use another market's last accepted capture.
        """
        recent = [row for row in tuple(self._recent_quality)
                  if now_ts - window_sec <= row[0] <= now_ts
                  and (slug is None or (len(row) > 4 and row[4] == slug))]
        accepted = [row for row in recent if row[2]]
        accepted_times = [row[0] for row in accepted]
        if slug is not None:
            last_slug = self._last_snapshot_ts.get(slug)
            if last_slug is not None and last_slug <= now_ts:
                accepted_times.append(last_slug)
        else:
            accepted_times.extend(ts for ts in tuple(self._last_snapshot_ts.values()) if ts <= now_ts)
        last = max(accepted_times, default=None)
        gaps = [row[3] for row in accepted if row[3] is not None]
        if last is not None:
            gaps.append(max(0, now_ts - last))
        capture_pct = 100 * sum(row[1] for row in recent) / len(recent) if recent else None
        persisted_pct = 100 * sum(row[1] for row in accepted) / len(accepted) if accepted else None
        drops = len(recent) - len(accepted)
        return {"interval_sec": self.interval_sec, "sample_count": len(recent), "accepted_count": len(accepted),
                "slug": slug, "recent_largest_gap_sec": max(gaps) if gaps else None,
                "capture_joint_fresh_pct": capture_pct, "persisted_joint_fresh_pct": persisted_pct,
                "joint_fresh_pct": capture_pct, "drop_count": drops,
                "drop_pct": 100 * drops / len(recent) if recent else None,
                "drops": drops, "errors": self._counters["errors"], "window_sec": window_sec}

    def _emit_metrics(self, now: float) -> None:
        if now - self._last_metrics_ts < self.metrics_interval_sec:
            return
        self._last_metrics_ts = now
        n = max(1, self._counters["written"])
        intervals = sorted(self._intervals)
        p95 = intervals[min(len(intervals) - 1, int(0.95 * (len(intervals) - 1)))] if intervals else None
        latencies = sorted(self._latencies_ms)
        latency_p95 = latencies[min(len(latencies) - 1, int(0.95 * (len(latencies) - 1)))] if latencies else 0.0
        health = {}
        try:
            health = self.db.research_health() if self.db is not None else {}
        except Exception:
            pass
        try:
            if self.db is not None:
                self.db.enqueue_decision(run_id=self.run_id, slug="", market_id=None,
                    decision_epoch_ns=int(now * 1_000_000_000), payload={
                        "event_type": "PREDICTION_RESEARCH_HEALTH", **self._collection_identity,
                        "timestamp": now, "capture_attempts_total": self._counters["eligible"],
                        "accepted_total": self._counters["written"],
                        "dropped_total": self._counters["dropped"], "errors_total": self._counters["errors"],
                        "queue_depth": None, "queue_capacity": None,
                        **health, "oldest_queue_age_sec": None, "last_persist_ts": None,
                        "unavailable_metrics": ["oldest_queue_age_sec", "last_persist_ts"],
                        "last_capture_ts": self._last_capture_ts, "last_enqueue_ts": self._last_enqueue_ts})
        except Exception:
            pass
        logger.info("prediction_snapshot: "
                    f"written={self._counters['written']} dropped={self._counters['dropped']} "
                    f"eligible={self._counters['eligible']} pex_fresh_pct={100*self._fresh['pex']/n:.1f} "
                    f"market_fresh_pct={100*self._fresh['market']/n:.1f} joint_fresh_pct={100*self._fresh['joint']/n:.1f} "
                    f"avg_interval={mean(self._intervals) if self._intervals else 0:.2f}s p95_interval={p95 if p95 is not None else 0:.2f}s "
                    f"compute_enqueue_p95_ms={latency_p95:.2f} queue_depth={health.get('queue_depth', 'unknown')} "
                    f"queue_drops={health.get('queue_drops', 'unknown')}")
