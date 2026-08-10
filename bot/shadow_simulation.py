"""Dry-run maker-entry simulation with persistent, outcome-labelled samples."""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Dict, Optional

from loguru import logger


MARKOUT_HORIZONS_SEC = (30, 60)


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_safe(value: Any) -> Any:
    """Convert strategy diagnostics to values accepted by the trade journal JSON writer."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


class ShadowSimulationMixin:
    """Records one conservative maker-fill simulation per market in dry-run mode."""

    def _shadow_simulation_enabled_for_run(self) -> bool:
        return bool(
            self._is_dry_run_mode()
            and getattr(self, "shadow_simulation_enabled", True)
            and getattr(self, "trade_db", None) is not None
        )

    def _fair_edge_bucket_shadow_enabled_for_run(self) -> bool:
        """Allow safe counterfactual entry research in either dry-run or live mode."""
        return bool(
            getattr(self, "fair_edge_bucket_shadow_enabled", False)
            and getattr(self, "trade_db", None) is not None
        )

    def _record_fair_edge_bucket_shadow_entry(
        self,
        *,
        instrument_id: Any,
        limit_price: Decimal,
        qty: Decimal,
        econ: Any,
        directional_snapshot: Optional[Dict[str, Any]],
        bucket: str,
    ) -> None:
        """Persist a submit-time-approved, never-live counterfactual BUY."""
        if not self._fair_edge_bucket_shadow_enabled_for_run():
            return
        slug = str(getattr(self, "current_market_slug", "") or "")
        bucket = str(bucket or "")
        if not slug or not bucket:
            return
        side = self._side_for_instrument_id(instrument_id).value
        simulation_id = f"fair-edge-shadow:{slug}:{side.lower()}:{bucket}"
        states = getattr(self, "_fair_edge_bucket_shadow_by_id", None)
        if not isinstance(states, dict):
            states = {}
            self._fair_edge_bucket_shadow_by_id = states
        if simulation_id in states:
            return
        now_ts = time.time()
        snapshot = directional_snapshot or {}
        fair = _as_float(snapshot.get("p_fair"))
        entry_price = float(limit_price)
        state: Dict[str, Any] = {
            "simulation_id": simulation_id,
            "slug": slug,
            "instrument_id": str(instrument_id),
            "side": side,
            "bucket": bucket,
            "status": "PENDING",
            "entry_price": entry_price,
            "qty": float(qty),
            "fair": fair,
            "fair_minus_entry": (fair - entry_price) if fair is not None else None,
            "created_ts": now_ts,
            "expires_ts": now_ts + float(getattr(self, "shadow_simulation_fill_timeout_sec", 90.0)),
            "expected_net_usdc": _as_float(getattr(econ, "expected_net_usdc", None)),
            "expected_rebate_usdc": _as_float(getattr(econ, "expected_rebate_usdc", None)),
            # Capture the submit-time model inputs for the Phase 3 report. These
            # remain counterfactual estimates, never realized trading costs.
            "fee_ps": _as_float(snapshot.get("fee_ps")),
            "other_cost_ps": _as_float(snapshot.get("other_cost_ps")),
            "exec_penalty_usdc": _as_float(snapshot.get("exec_penalty_usdc")),
            "execution_penalty_components": _json_safe(
                snapshot.get("execution_penalty_components")
            ),
            "side_score": _as_float(getattr(self, "side_decision_score", None)),
            "side_reason": str(getattr(self, "side_decision_reason", "") or ""),
            "planned_quote_ts": _as_float(snapshot.get("planned_quote_ts")),
            "planned_best_bid": _as_float(snapshot.get("planned_best_bid")),
            "planned_best_ask": _as_float(snapshot.get("planned_best_ask")),
            "directional_edge_ps": _as_float(snapshot.get("directional_edge_ps")),
            "directional_edge_usdc": _as_float(snapshot.get("directional_edge_usdc")),
            "robust_net_usdc": _as_float(snapshot.get("robust_net_usdc")),
            "entry_mode": str(snapshot.get("entry_mode") or ""),
            "size_multiplier": _as_float(snapshot.get("size_multiplier")),
            "time_left_sec": max(
                0.0,
                float(getattr(self, "current_market_end_timestamp", now_ts) or now_ts) - now_ts,
            ),
            "submit_path": "all_live_gates_except_entry_fair_edge",
        }
        states[simulation_id] = state
        self._db_order_event(
            event_type="FAIR_EDGE_BUCKET_SHADOW_CANDIDATE",
            client_order_id=simulation_id,
            side=side,
            price=entry_price,
            qty=float(qty),
            status="PENDING",
            reason="fair_edge_counterfactual_submit_path_approved",
            expected_net_usdc=state["expected_net_usdc"],
            payload=state,
        )

    def _fair_edge_bucket_shadow_on_quote(
        self,
        instrument_id: Any,
        bid: Decimal,
        ask: Decimal,
        now_ts: float,
    ) -> None:
        if not self._fair_edge_bucket_shadow_enabled_for_run():
            return
        states = getattr(self, "_fair_edge_bucket_shadow_by_id", {})
        for state in list(states.values()):
            if str(state.get("instrument_id")) != str(instrument_id):
                continue
            status = str(state.get("status") or "")
            if status != "PENDING":
                continue
            if now_ts > float(state.get("expires_ts") or 0.0):
                state.update({"status": "EXPIRED", "expired_ts": now_ts})
                self._db_order_event(
                    event_type="FAIR_EDGE_BUCKET_SHADOW_EXPIRED",
                    client_order_id=state["simulation_id"], side=state["side"],
                    price=state["entry_price"], qty=state["qty"], status="EXPIRED",
                    reason="ask_not_reached_before_timeout", payload=state,
                )
                continue
            if ask > Decimal(str(state["entry_price"])):
                continue
            state.update({
                "status": "FILLED", "filled_ts": now_ts,
                "fill_bid": float(bid), "fill_ask": float(ask),
                "fill_mid": float((bid + ask) / Decimal("2")),
            })
            self._db_order_event(
                event_type="FAIR_EDGE_BUCKET_SHADOW_FILLED",
                client_order_id=state["simulation_id"], side=state["side"],
                price=state["entry_price"], qty=state["qty"], status="FILLED",
                reason="later_ask_reached_passive_limit", payload=state,
            )

    def _settle_fair_edge_bucket_shadow_simulations(
        self, *, slug: str, spot: float, strike: float
    ) -> None:
        if not self._fair_edge_bucket_shadow_enabled_for_run() or spot <= 0 or strike <= 0:
            return
        outcome = "UP" if spot >= strike else "DOWN"
        for state in list(getattr(self, "_fair_edge_bucket_shadow_by_id", {}).values()):
            if str(state.get("slug")) != str(slug) or str(state.get("status")) != "FILLED":
                continue
            won = str(state.get("side") or "").upper() == outcome
            price = Decimal(str(state["entry_price"]))
            qty = Decimal(str(state["qty"]))
            gross_pnl = qty * ((Decimal("1") - price) if won else -price)
            state.update({
                "status": "SETTLED", "outcome": outcome, "won": won,
                "settlement_spot": spot, "settlement_strike": strike,
                "simulated_gross_pnl_usdc": float(gross_pnl),
            })
            self._db_order_event(
                event_type="FAIR_EDGE_BUCKET_SHADOW_SETTLED",
                client_order_id=state["simulation_id"], side=state["side"],
                price=state["entry_price"], qty=state["qty"], status="SETTLED",
                reason="twap_reference_settlement", payload=state,
            )

    def _load_shadow_simulation_for_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        cached = getattr(self, "_shadow_simulations_by_slug", {}).get(slug)
        if cached is not None:
            return cached
        record = self.trade_db.load_shadow_simulation(slug) if self.trade_db else None
        if record is not None:
            self._shadow_simulations_by_slug[slug] = record
        return record

    def _restore_shadow_simulation_for_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """Rehydrate an in-progress simulation after a strategy/node restart."""
        self._restore_fair_edge_bucket_shadow_for_slug(slug)
        if not self._shadow_simulation_enabled_for_run():
            return None
        slug = str(slug or "")
        cache = getattr(self, "_shadow_simulations_by_slug", {})
        if not slug or slug in cache:
            return cache.get(slug)
        state = self._load_shadow_simulation_for_slug(slug)
        if state is not None:
            logger.info(
                "Shadow simulation restored after restart: "
                f"slug={slug} status={state.get('status', 'UNKNOWN')}"
            )
        return state

    def _restore_fair_edge_bucket_shadow_for_slug(self, slug: str) -> None:
        """Rehydrate counterfactual states so node rollover cannot discard samples."""
        if not self._fair_edge_bucket_shadow_enabled_for_run() or not self.trade_db:
            return
        slug = str(slug or "")
        if not slug:
            return
        states = getattr(self, "_fair_edge_bucket_shadow_by_id", None)
        if not isinstance(states, dict):
            states = {}
            self._fair_edge_bucket_shadow_by_id = states
        restored = 0
        for state in self.trade_db.load_fair_edge_bucket_shadow_simulations(slug):
            simulation_id = str(state.get("simulation_id") or "")
            if not simulation_id or simulation_id in states:
                continue
            states[simulation_id] = state
            restored += 1
        if restored:
            logger.info(
                "Fair-edge bucket shadow candidates restored after restart: "
                f"slug={slug} count={restored}"
            )

    def _shadow_quote_age_sec(self, instrument_id: Any, now_ts: float) -> Optional[float]:
        quote_received_ts = _as_float(
            getattr(self, "last_quote_received_ts_by_inst", {}).get(str(instrument_id))
        )
        if quote_received_ts is None:
            # Compatibility fallback for a strategy instance created before this field.
            quote_received_ts = _as_float(
                getattr(self, "last_quote_update_ts_by_inst", {}).get(str(instrument_id))
            )
        if quote_received_ts is None or quote_received_ts <= 0:
            return None
        return float(now_ts) - quote_received_ts

    def _shadow_quote_freshness_tier(self, instrument_id: Any, now_ts: float) -> str:
        raw_age = self._shadow_quote_age_sec(instrument_id, now_ts)
        fresh_max_age = float(getattr(self, "shadow_simulation_max_quote_age_sec", 2.0))
        aged_max_age = float(getattr(self, "shadow_simulation_aged_quote_max_age_sec", 30.0))
        skew_tolerance = float(
            getattr(self, "quote_event_clock_skew_tolerance_sec", Decimal("0.25"))
        )
        if raw_age is None or raw_age < -skew_tolerance:
            return "INVALID"
        if raw_age <= fresh_max_age:
            return "FRESH"
        if raw_age <= aged_max_age:
            return "AGED"
        return "STALE"

    def _record_shadow_simulated_entry(
        self,
        *,
        instrument_id: Any,
        limit_price: Decimal,
        qty: Decimal,
        econ: Any,
        directional_snapshot: Optional[Dict[str, Any]],
        target_version: int = 0,
        order_key: str = "",
    ) -> bool:
        """Create or reprice the paper order that mirrors a live maker BUY.

        The quote runtime owns the decision to cancel/requote.  This mixin only
        persists that resulting paper order lifecycle and observes a passive
        fill once a later ask reaches its current limit.
        """
        if not self._shadow_simulation_enabled_for_run():
            return False
        slug = str(getattr(self, "current_market_slug", "") or "")
        if not slug:
            return False

        now_ts = time.time()
        quote = self._get_quote_for_instrument(instrument_id)
        side = self._side_for_instrument_id(instrument_id).value
        snapshot = directional_snapshot or {}
        raw_quote_age_sec = self._shadow_quote_age_sec(instrument_id, now_ts)
        quote_freshness_tier = self._shadow_quote_freshness_tier(instrument_id, now_ts)
        simulation_id = f"shadow-sim:{slug}:{side.lower()}"
        quote_ts = _as_float(
            getattr(self, "last_quote_update_ts_by_inst", {}).get(str(instrument_id))
        )
        quote_received_ts = _as_float(
            getattr(self, "last_quote_received_ts_by_inst", {}).get(str(instrument_id))
        )
        time_left_sec = None
        end_ts = _as_float(getattr(self, "current_market_end_timestamp", None))
        if end_ts is not None:
            time_left_sec = max(0.0, end_ts - now_ts)
        existing = self._load_shadow_simulation_for_slug(slug)
        if existing is not None and str(existing.get("status") or "") in {"FILLED", "SETTLED"}:
            return False

        # A live maker order expires through the same TTL loop before the next
        # quote is considered.  Dry-run must use that TTL, rather than a
        # separate 90-second fixed candidate window.
        live_ttl_sec = float(getattr(self, "maker_order_ttl_sec", 20.0))
        state: Dict[str, Any] = {
            "simulation_id": simulation_id,
            "slug": slug,
            "instrument_id": str(instrument_id),
            "side": side,
            "status": "PENDING",
            "entry_price": float(limit_price),
            "qty": float(qty),
            "created_ts": now_ts,
            "expires_ts": now_ts + live_ttl_sec,
            "order_key": order_key,
            "target_version": int(target_version or 0),
            "live_order_ttl_sec": live_ttl_sec,
            "expected_net_usdc": _as_float(getattr(econ, "expected_net_usdc", None)),
            "expected_rebate_usdc": _as_float(getattr(econ, "expected_rebate_usdc", None)),
            "fair": _as_float(snapshot.get("p_fair")),
            "side_score": _as_float(getattr(self, "side_decision_score", None)),
            "side_reason": str(getattr(self, "side_decision_reason", "") or ""),
            "time_left_sec": time_left_sec,
            "quote_bid": _as_float(quote[0]) if quote is not None else None,
            "quote_ask": _as_float(quote[1]) if quote is not None else None,
            "quote_ts": quote_ts,
            "quote_received_ts": quote_received_ts,
            "quote_age_raw_sec": raw_quote_age_sec,
            "quote_event_lag_sec": (
                quote_received_ts - quote_ts
                if quote_received_ts is not None and quote_ts is not None
                else None
            ),
            "quote_age_sec": max(0.0, raw_quote_age_sec or 0.0),
            "fresh_quote_max_age_sec": float(
                getattr(self, "shadow_simulation_max_quote_age_sec", 2.0)
            ),
            "aged_quote_max_age_sec": float(
                getattr(self, "shadow_simulation_aged_quote_max_age_sec", 30.0)
            ),
            "quote_freshness_tier": quote_freshness_tier,
            "executable_quote_sample": quote_freshness_tier == "FRESH",
            "planned_quote_ts": _as_float(snapshot.get("planned_quote_ts")),
            "planned_best_bid": _as_float(snapshot.get("planned_best_bid")),
            "planned_best_ask": _as_float(snapshot.get("planned_best_ask")),
            "directional_edge_ps": _as_float(snapshot.get("directional_edge_ps")),
            "directional_edge_usdc": _as_float(snapshot.get("directional_edge_usdc")),
            "robust_net_usdc": _as_float(snapshot.get("robust_net_usdc")),
            "fee_ps": _as_float(snapshot.get("fee_ps")),
            "other_cost_ps": _as_float(snapshot.get("other_cost_ps")),
            "exec_penalty_usdc": _as_float(snapshot.get("exec_penalty_usdc")),
            "entry_mode": str(snapshot.get("entry_mode") or ""),
            "size_multiplier": _as_float(snapshot.get("size_multiplier")),
            "entry_quality": _json_safe(snapshot.get("entry_quality")),
            "strike": _as_float(
                getattr(self, "market_strike_cache_by_slug", {}).get(slug)
            ),
            "spot": _as_float(getattr(self, "latest_external_spot", None)),
            "markouts_done": [],
        }
        if existing is not None:
            state["simulation_id"] = str(existing.get("simulation_id") or simulation_id)
            state["first_created_ts"] = float(existing.get("first_created_ts") or existing.get("created_ts") or now_ts)
            state["submission_count"] = int(existing.get("submission_count", 1) or 1) + 1
            state["requote_count"] = int(existing.get("requote_count", 0) or 0) + 1
            state["last_cancel_reason"] = existing.get("last_cancel_reason")
            event_type = "SHADOW_SIM_ENTRY_REQUOTED"
            event_reason = "dry_run_live_lifecycle_requote"
        else:
            state["first_created_ts"] = now_ts
            state["submission_count"] = 1
            state["requote_count"] = 0
            event_type = "SHADOW_SIM_ENTRY_CANDIDATE"
            event_reason = "dry_run_live_lifecycle_submit"
        self._shadow_simulations_by_slug[slug] = state
        self._db_order_event(
            event_type=event_type,
            client_order_id=str(state["simulation_id"]),
            side=side,
            price=float(limit_price),
            qty=float(qty),
            status="PENDING",
            reason=event_reason,
            expected_net_usdc=state["expected_net_usdc"],
            payload=state,
        )
        logger.info(
            "Shadow simulation maker quote: "
            f"slug={slug} side={side} qty={float(qty):.4f} px={float(limit_price):.4f} "
            f"ttl={live_ttl_sec:.0f}s target_version={int(target_version or 0)}"
        )
        return True

    def _shadow_simulation_on_order_cancel(self, *, order_key: str, reason: str) -> None:
        """Mirror a local dry-run cancel before the next live-style requote."""
        if not self._shadow_simulation_enabled_for_run():
            return
        slug = str(getattr(self, "current_market_slug", "") or "")
        state = self._load_shadow_simulation_for_slug(slug) if slug else None
        if state is None or str(state.get("status") or "") != "PENDING":
            return
        if str(state.get("order_key") or "") != str(order_key):
            return
        now_ts = time.time()
        state.update(
            {
                "status": "CANCELED",
                "cancelled_ts": now_ts,
                "last_cancel_reason": str(reason or "risk"),
            }
        )
        self._db_order_event(
            event_type="SHADOW_SIM_ENTRY_CANCELLED",
            client_order_id=str(state["simulation_id"]),
            side=state["side"],
            price=state["entry_price"],
            qty=state["qty"],
            status="CANCELED",
            reason=f"dry_run_live_lifecycle:{reason or 'risk'}",
            payload=state,
        )

    def _shadow_simulation_on_quote(
        self,
        instrument_id: Any,
        bid: Decimal,
        ask: Decimal,
        now_ts: float,
    ) -> None:
        self._fair_edge_bucket_shadow_on_quote(instrument_id, bid, ask, now_ts)
        if not self._shadow_simulation_enabled_for_run():
            return
        slug = str(getattr(self, "current_market_slug", "") or "")
        state = self._load_shadow_simulation_for_slug(slug) if slug else None
        if state is None or str(state.get("instrument_id")) != str(instrument_id):
            return

        status = str(state.get("status") or "")
        if status == "PENDING":
            if now_ts > float(state.get("expires_ts") or 0.0):
                state["status"] = "EXPIRED"
                state["expired_ts"] = now_ts
                self._db_order_event(
                    event_type="SHADOW_SIM_ENTRY_EXPIRED",
                    client_order_id=state["simulation_id"],
                    side=state["side"],
                    price=state["entry_price"],
                    qty=state["qty"],
                    status="EXPIRED",
                    reason="ask_not_reached_before_timeout",
                    payload=state,
                )
                return
            # A post-only BUY can only fill after the market trades back down to it.
            if ask > Decimal(str(state["entry_price"])):
                return
            fill_raw_quote_age_sec = self._shadow_quote_age_sec(instrument_id, now_ts)
            state.update(
                {
                    "status": "FILLED",
                    "filled_ts": now_ts,
                    "fill_bid": float(bid),
                    "fill_ask": float(ask),
                    "fill_mid": float((bid + ask) / Decimal("2")),
                    "fill_quote_age_raw_sec": fill_raw_quote_age_sec,
                    "fill_quote_freshness_tier": self._shadow_quote_freshness_tier(
                        instrument_id, now_ts
                    ),
                }
            )
            for order_key, order_state in list(getattr(self, "active_maker_orders", {}).items()):
                if str(order_state.get("dry_run_simulation_id") or "") == str(state["simulation_id"]):
                    self.active_maker_orders.pop(order_key, None)
            self._db_order_event(
                event_type="SHADOW_SIM_ENTRY_FILLED",
                client_order_id=state["simulation_id"],
                side=state["side"],
                price=state["entry_price"],
                qty=state["qty"],
                status="FILLED",
                reason="later_ask_reached_passive_limit",
                expected_net_usdc=state.get("expected_net_usdc"),
                payload=state,
            )
            logger.info(
                "Shadow simulation filled: "
                f"slug={slug} side={state['side']} px={state['entry_price']:.4f}"
            )
            return

        if status != "FILLED":
            return
        elapsed = max(0.0, now_ts - float(state.get("filled_ts") or now_ts))
        done = set(int(value) for value in state.get("markouts_done", []))
        mid = (bid + ask) / Decimal("2")
        for horizon in MARKOUT_HORIZONS_SEC:
            if horizon in done or elapsed < horizon:
                continue
            signed_markout = mid - Decimal(str(state["entry_price"]))
            state["markouts_done"].append(horizon)
            done.add(horizon)
            self._db_order_event(
                event_type="SHADOW_SIM_MARKOUT",
                client_order_id=state["simulation_id"],
                side=state["side"],
                price=float(mid),
                qty=state["qty"],
                status="OBSERVED",
                reason=f"markout_{horizon}s",
                payload={
                    "simulation_id": state["simulation_id"],
                    "slug": slug,
                    "entry_price": state["entry_price"],
                    "markout_mid": float(mid),
                    "signed_markout_ps": float(signed_markout),
                    "horizon_sec": horizon,
                    "elapsed_sec": elapsed,
                    "quote_freshness_tier": self._shadow_quote_freshness_tier(
                        instrument_id, now_ts
                    ),
                },
            )

    def _settle_shadow_simulation(self, *, slug: str, spot: float, strike: float) -> None:
        self._settle_fair_edge_bucket_shadow_simulations(slug=slug, spot=spot, strike=strike)
        if not self._shadow_simulation_enabled_for_run() or spot <= 0 or strike <= 0:
            return
        state = self._load_shadow_simulation_for_slug(slug)
        if state is None or str(state.get("status") or "") != "FILLED":
            return
        outcome = "UP" if spot >= strike else "DOWN"
        won = str(state.get("side") or "").upper() == outcome
        price = Decimal(str(state["entry_price"]))
        qty = Decimal(str(state["qty"]))
        # Passive maker fills pay no CLOB taker fee.  Preserve the economics
        # rebate estimate separately so results remain comparable to live PnL
        # without pretending that a projected rebate is guaranteed.
        gross_pnl = qty * ((Decimal("1") - price) if won else -price)
        maker_fee_usdc = Decimal("0")
        expected_rebate_usdc = Decimal(str(state.get("expected_rebate_usdc") or "0"))
        pnl = gross_pnl - maker_fee_usdc + expected_rebate_usdc
        state.update(
            {
                "status": "SETTLED",
                "outcome": outcome,
                "won": won,
                "settlement_spot": spot,
                "settlement_strike": strike,
                "simulated_pnl_usdc": float(pnl),
                "simulated_gross_pnl_usdc": float(gross_pnl),
                "simulated_maker_fee_usdc": float(maker_fee_usdc),
                "simulated_expected_rebate_usdc": float(expected_rebate_usdc),
            }
        )
        self._db_order_event(
            event_type="SHADOW_SIM_SETTLED",
            client_order_id=state["simulation_id"],
            side=state["side"],
            price=state["entry_price"],
            qty=state["qty"],
            status="SETTLED",
            reason="twap_reference_settlement",
            payload=state,
        )
        self._db_strategy_event(
            "SHADOW_SIM_CYCLE_RESULT",
            {
                "slug": slug,
                "simulation_id": state["simulation_id"],
                "side": state["side"],
                "outcome": outcome,
                "won": won,
                "entry_price": state["entry_price"],
                "qty": state["qty"],
                "simulated_pnl_usdc": float(pnl),
                "spot": spot,
                "strike": strike,
            },
        )
        logger.info(
            "Shadow simulation settled: "
            f"slug={slug} side={state['side']} outcome={outcome} win={won} pnl={float(pnl):+.4f}"
        )
