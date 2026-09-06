"""Low-frequency, non-executing large-order L2 research collector."""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from bot.depth_risk import simulate_large_order_ladder


MARKOUT_HORIZONS_SEC = (5, 10, 30, 60)


class DepthRiskShadowMixin:
    """Persist hypothetical 10--200 share fills without touching order routing."""

    def _depth_risk_shadow_enabled_for_run(self) -> bool:
        return bool(
            getattr(self, "depth_risk_shadow_enabled", True)
            and getattr(self, "trade_db", None) is not None
        )

    def _record_depth_risk_shadow(
        self,
        *,
        instrument_id: Any,
        tick_size: Decimal,
        asks: Any,
        bids: Any,
        now_ts: float,
    ) -> None:
        if not self._depth_risk_shadow_enabled_for_run():
            return
        key = str(instrument_id)
        last_by_inst = getattr(self, "_depth_risk_shadow_last_ts_by_inst", {})
        interval = max(1.0, float(getattr(self, "depth_risk_shadow_interval_sec", 15.0)))
        if now_ts - float(last_by_inst.get(key, 0.0)) < interval:
            return
        plans = simulate_large_order_ladder(
            asks=asks, bids=bids, tick_size=tick_size,
            boundary_ticks=int(getattr(self, "depth_risk_price_boundary_ticks", 2)),
        )
        if not plans:
            return
        last_by_inst[key] = now_ts
        self._depth_risk_shadow_last_ts_by_inst = last_by_inst
        states = getattr(self, "_depth_risk_shadow_states", None)
        if not isinstance(states, dict):
            states = {}
            self._depth_risk_shadow_states = states
        slug = str(getattr(self, "current_market_slug", "") or "")
        side = self._side_for_instrument_id(instrument_id).value
        for plan in plans:
            entry = plan["entry"]
            exit_estimate = plan["exit"]
            requested = Decimal(str(plan["requested_quantity"]))
            simulation_id = f"depth-risk:{slug}:{key}:{requested}:{int(now_ts * 1000)}"
            payload = {
                "simulation_id": simulation_id,
                "slug": slug,
                "instrument_id": key,
                "side": side,
                "created_ts": now_ts,
                "requested_quantity": float(requested),
                "entry": entry.as_payload(),
                "exit": exit_estimate.as_payload() if exit_estimate else None,
                "immediate_round_trip_markout_usdc": (
                    float(plan["immediate_round_trip_markout_usdc"])
                    if plan["immediate_round_trip_markout_usdc"] is not None else None
                ),
                "markouts_observed_sec": [],
            }
            states[simulation_id] = payload
            self._db_order_event(
                event_type="DEPTH_RISK_SHADOW_CANDIDATE",
                client_order_id=simulation_id,
                side=side,
                price=float(entry.vwap) if entry.vwap is not None else None,
                qty=float(requested), status="OBSERVED",
                reason="counterfactual_l2_large_order",
                payload=payload,
            )

    def _depth_risk_shadow_on_quote(
        self, instrument_id: Any, bid: Decimal, ask: Decimal, now_ts: float
    ) -> None:
        if not self._depth_risk_shadow_enabled_for_run():
            return
        key = str(instrument_id)
        for simulation_id, state in list(getattr(self, "_depth_risk_shadow_states", {}).items()):
            if state.get("instrument_id") != key:
                continue
            entry = state.get("entry") or {}
            entry_vwap = entry.get("vwap")
            filled = entry.get("filled_quantity")
            if entry_vwap is None or not filled:
                continue
            elapsed = now_ts - float(state.get("created_ts", now_ts))
            observed = set(state.get("markouts_observed_sec") or [])
            for horizon in MARKOUT_HORIZONS_SEC:
                if elapsed < horizon or horizon in observed:
                    continue
                # Mark to a conservative immediately executable sell price, not mid.
                markout = (Decimal(str(bid)) - Decimal(str(entry_vwap))) * Decimal(str(filled))
                payload = {
                    "simulation_id": simulation_id,
                    "slug": state.get("slug"),
                    "instrument_id": key,
                    "requested_quantity": state.get("requested_quantity"),
                    "entry_vwap": entry_vwap,
                    "entry_filled_quantity": filled,
                    "markout_horizon_sec": horizon,
                    "actual_elapsed_sec": elapsed,
                    "exit_best_bid": float(bid),
                    "exit_best_ask": float(ask),
                    "markout_usdc": float(markout),
                    "markout_per_filled_share": float(markout / Decimal(str(filled))),
                }
                self._db_order_event(
                    event_type="DEPTH_RISK_SHADOW_MARKOUT",
                    client_order_id=simulation_id,
                    side=state.get("side"), price=float(bid), qty=float(filled),
                    status="OBSERVED", reason="counterfactual_bbo_markout", payload=payload,
                )
                observed.add(horizon)
            state["markouts_observed_sec"] = sorted(observed)
            if len(observed) == len(MARKOUT_HORIZONS_SEC):
                self._depth_risk_shadow_states.pop(simulation_id, None)
