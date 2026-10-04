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
    """Create one compact snapshot; stale probability/quotes cannot form residuals."""
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
        source_age = _number(context.get(f"market_{side}_source_age_sec"))
        receive_age = _number(context.get(f"market_{side}_receive_age_sec"))
        source_ts = _number(context.get(f"market_{side}_source_ts"))
        received_ts = _number(context.get(f"market_{side}_received_ts"))
        fresh = (bid is not None and ask is not None and 0 <= bid <= ask <= 1
                 and _fresh(source_age, market_max_age) and _fresh(receive_age, market_max_age))
        if not fresh:
            return None, None, None, source_ts, received_ts, source_age, receive_age, False
        return bid, ask, (bid + ask) / 2.0, source_ts, received_ts, source_age, receive_age, True

    bid_up, ask_up, mid_up, up_ts, up_received_ts, up_input_age, up_input_receive_age, up_fresh = quote("up")
    bid_down, ask_down, mid_down, down_ts, down_received_ts, down_input_age, down_input_receive_age, down_fresh = quote("down")
    # Freshness ages are recomputed against the actual common snapshot time.
    up_source_age, up_receive_age = _age(now, up_ts), _age(now, up_received_ts)
    down_source_age, down_receive_age = _age(now, down_ts), _age(now, down_received_ts)
    up_fresh = up_fresh and _fresh(up_source_age, market_max_age) and _fresh(up_receive_age, market_max_age)
    down_fresh = down_fresh and _fresh(down_source_age, market_max_age) and _fresh(down_receive_age, market_max_age)
    if not up_fresh:
        bid_up = ask_up = mid_up = None
    if not down_fresh:
        bid_down = ask_down = mid_down = None

    residual_up = p_up - mid_up if p_up is not None and mid_up is not None else None
    residual_down = p_down - mid_down if p_down is not None and mid_down is not None else None
    btc_ts = _number(context.get("btc_source_ts"))
    btc_age = _age(now, btc_ts)
    btc_fresh = _fresh(btc_age, btc_max_age)
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
    twap_age = _age(now, context.get("twap_source_ts"))
    twap_fresh = _fresh(twap_age, pex_max_age)
    twap_value = _number(context.get("official_twap")) if twap_fresh else None
    probability_reason = (None if p_up is not None else
                          "p_ex_unavailable" if pex_raw is None else
                          "sigma_stale_or_unavailable" if not pex_sigma_fresh else
                          "p_ex_source_stale")
    identity = dict(context.get("identity") or {})
    result = {
        "event_type": "PREDICTION_RESEARCH_SNAPSHOT",
        "prediction_schema_version": PREDICTION_SCHEMA_VERSION,
        "snapshot_ts": now,
        "snapshot_trigger": str(context.get("trigger") or "periodic"),
        "snapshot_interval_sec": _number(context.get("snapshot_interval_sec")),
        **identity,
        "p_ex_source_ts": _number(context.get("p_ex_source_ts")),
        "p_ex_age_sec": pex_age,
        "p_ex_fresh": p_up is not None,
        "p_ex_unavailable_reason": probability_reason,
        "p_up_ex_market": p_up,
        "p_down_ex_market": p_down,
        "probability_model_mode": context.get("probability_model_mode"),
        "probability_model_version": context.get("probability_model_version"),
        "sigma_ex_market": _number(context.get("sigma_ex_market")),
        "sigma_ex_market_fresh": pex_sigma_fresh,
        "sigma_ex_market_age_sec": _number(context.get("sigma_ex_market_age_sec")),
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
        "btc_fresh": btc_spot is not None,
        **returns, **disagreement,
        **{f"btc_return_{horizon}s_prior_age_sec": _number(context.get(f"btc_return_{horizon}s_prior_age_sec"))
           for horizon in (1, 5, 10, 30, 60)},
        "twap_source_ts": _number(context.get("twap_source_ts")),
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
        self.metrics_interval_sec = max(10.0, float(metrics_interval_sec))
        self._last_periodic: dict[str, float] = {}
        self._last_snapshot_ts: dict[str, float] = {}
        self._last_payload_by_slug: dict[str, dict[str, Any]] = {}
        self._last_side_by_slug: dict[str, str] = {}
        self._last_side_change_ts: dict[str, float] = {}
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
        if not slug:
            return None
        if trigger == "periodic" and not force:
            last = self._last_periodic.get(slug, 0.0)
            if now - last < self.interval_sec:
                return None
            self._last_periodic[slug] = now
        self._counters["eligible"] += 1
        try:
            end_ts = _number(getattr(strategy, "current_market_end_timestamp", None))
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
                return None
            strike = getattr(strategy, "market_strike_cache_by_slug", {}).get(slug)
            twap = getattr(strategy, "_polymarket_chainlink_twap_price", None)
            twap_ts = _number(getattr(strategy, "_polymarket_chainlink_twap_observation_ts", None))
            diagnostics = {}
            if twap is not None and callable(getattr(strategy, "_settlement_probability_shadow_inputs", None)):
                diagnostics = strategy._settlement_probability_shadow_inputs(
                    slug=slug, official_twap=twap, strike=strike, time_left_sec=left,
                    now_ts=now, source_observed_ts=twap_ts,
                ) or {}
            spot_source = str(diagnostics.get("path_spot_source") or "")
            if spot_source == "polymarket_chainlink_spot":
                pex_source_ts = _number(getattr(strategy, "_polymarket_chainlink_price_observation_ts", None))
            elif spot_source == "binance_ws":
                pex_source_ts = _number(getattr(strategy, "_binance_ws_price_source_ts", None)) or _number(getattr(strategy, "_binance_ws_price_ts", None))
            else:
                pex_source_ts = None
            pex_source_age = _age(now, pex_source_ts)
            sigma_age = _number(diagnostics.get("sigma_ex_market_age_sec"))
            pex_age = max((x for x in (pex_source_age, sigma_age, _age(now, twap_ts)) if x is not None), default=None)
            pair = strategy._research_market_quote_instruments(slug=slug, runtime_slug=str(getattr(strategy, "current_market_slug", "") or ""))
            up_inst, down_inst = pair or ("", "")
            quote_ts_map = getattr(strategy, "last_quote_source_ts_by_inst", {}) or {}
            receive_ts_map = getattr(strategy, "last_quote_received_ts_by_inst", {}) or {}
            q_source = getattr(strategy, "latest_quote_by_inst", {}) or {}
            max_quote_age = max(0.1, float(getattr(strategy, "quote_max_delivery_delay_sec", 2.0)))

            def quote_context(side: str, inst: str) -> dict[str, Any]:
                src_ts = _number(quote_ts_map.get(inst)) if inst else None
                recv_ts = _number(receive_ts_map.get(inst)) if inst else None
                book = q_source.get(inst) if inst else None
                bid, ask = (book[0], book[1]) if book and len(book) >= 2 else (None, None)
                return {f"best_bid_{side}": bid, f"best_ask_{side}": ask,
                        f"market_{side}_source_ts": src_ts, f"market_{side}_received_ts": recv_ts,
                        f"market_{side}_source_age_sec": _age(now, src_ts),
                        f"market_{side}_receive_age_sec": _age(now, recv_ts)}

            raw_history = getattr(strategy, "_prediction_btc_research_history", ())
            btc_points = list(raw_history)
            btc = btc_points[-1] if btc_points else None
            btc_source_ts = btc[0] if btc else _number(getattr(strategy, "_binance_ws_price_source_ts", None))
            btc_spot = btc[1] if btc else getattr(strategy, "_binance_ws_price", None)
            trend = getattr(strategy, "side_decision_inputs", {}) or {}
            side_obj = getattr(strategy, "active_side", "NONE")
            side = str(getattr(side_obj, "value", side_obj) or "NONE").upper()
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
                             "down_instrument_id": down_inst or None},
                "p_up_ex_market": diagnostics.get("p_up_ex_market"),
                "p_ex_age_sec": pex_age, "p_ex_source_ts": pex_source_ts,
                "sigma_ex_market": diagnostics.get("sigma_ex_market"),
                "sigma_ex_market_fresh": diagnostics.get("sigma_ex_market_fresh"),
                "sigma_ex_market_age_sec": diagnostics.get("sigma_ex_market_age_sec"),
                "probability_model_mode": diagnostics.get("probability_model_mode"),
                "probability_model_version": diagnostics.get("probability_model_version"),
                "official_twap": twap, "twap_source_ts": twap_ts,
                "settlement_state_side": diagnostics.get("settlement_state_side"),
                "strike": strike, "required_move_mode": diagnostics.get("required_move_mode"),
                "required_future_avg_to_flip": diagnostics.get("required_future_avg_to_flip"),
                "required_move_usd": diagnostics.get("required_move_usd"),
                "required_move_bps": diagnostics.get("required_move_bps"),
                "required_move_sigma": diagnostics.get("required_move_sigma"),
                "remaining_final_window_sec": diagnostics.get("remaining_final_window_sec"),
                "btc_spot": btc_spot, "btc_source_ts": btc_source_ts,
                **self._btc_returns(btc_points, now),
                **quote_context("up", up_inst), **quote_context("down", down_inst),
                "active_side": side,
                "side_score": getattr(strategy, "side_decision_score", None),
                "side_reason": getattr(strategy, "side_decision_reason", None),
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
                                   "down_instrument_id": down_inst or None}
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
            self._recent_quality.append((now, bool(snapshot["joint_fresh"]), accepted, interval))
            self._latencies_ms.append((time.perf_counter() - started) * 1000.0)
            if accepted:
                self._last_snapshot_ts[slug] = now
                self._last_payload_by_slug[slug] = snapshot
                self._intervals.append(interval if interval is not None else self.interval_sec)
                for key, value in (("pex", snapshot["p_ex_fresh"]), ("market", snapshot["market_mid_fresh"]), ("joint", snapshot["joint_fresh"])):
                    self._fresh[key] += int(bool(value))
            self._emit_metrics(now)
            return snapshot if accepted else None
        except Exception as exc:
            self._counters["errors"] += 1
            if self._counters["errors"] == 1:
                logger.warning(f"Prediction research snapshot disabled/skipped: {type(exc).__name__}: {exc}")
            return None

    def recent_health(self, now_ts: float, *, window_sec: float = 300.0) -> dict:
        """Bounded recent capture quality; no DB reads and no freshness authority."""
        recent = [row for row in tuple(self._recent_quality) if now_ts - window_sec <= row[0] <= now_ts]
        accepted_times = [row[0] for row in recent if row[2]]
        accepted_times.extend(ts for ts in tuple(self._last_snapshot_ts.values()) if ts <= now_ts)
        last = max(accepted_times, default=None)
        gaps = [row[3] for row in recent if row[3] is not None and row[2]]
        # Detect a collector that went silent, even when no new rows arrive.
        if last is not None:
            gaps.append(max(0, now_ts - last))
        return {"interval_sec": self.interval_sec, "sample_count": len(recent),
                "recent_largest_gap_sec": max(gaps) if gaps else None,
                "joint_fresh_pct": 100 * sum(row[1] for row in recent) / len(recent) if recent else None,
                "drops": sum(not row[2] for row in recent), "errors": self._counters["errors"],
                "window_sec": window_sec}

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
        logger.info("prediction_snapshot: "
                    f"written={self._counters['written']} dropped={self._counters['dropped']} "
                    f"eligible={self._counters['eligible']} pex_fresh_pct={100*self._fresh['pex']/n:.1f} "
                    f"market_fresh_pct={100*self._fresh['market']/n:.1f} joint_fresh_pct={100*self._fresh['joint']/n:.1f} "
                    f"avg_interval={mean(self._intervals) if self._intervals else 0:.2f}s p95_interval={p95 if p95 is not None else 0:.2f}s "
                    f"compute_enqueue_p95_ms={latency_p95:.2f} queue_depth={health.get('queue_depth', 'unknown')} "
                    f"queue_drops={health.get('queue_drops', 'unknown')}")
