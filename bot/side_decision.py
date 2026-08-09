"""
bot/side_decision.py – SideDecisionMixin

Extracted from run_bot.py (L1421-L1949).
All 12 side-decision + regime-guard methods live here as a Mixin.
IntegratedBTCStrategy inherits this mixin so all self.* references remain valid.

NOTE: MarketPhase and ActiveSide were extracted to bot/enums.py together with this
refactoring so that both run_bot.py and this module can import them safely.
"""
from __future__ import annotations

import json
import sqlite3
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Protocol

from loguru import logger

from bot.enums import ActiveSide, MarketPhase
from bot.edge_state import build_edge_state
from bot.market_data import extract_market_start_ts_from_slug
from bot.signal_engine import SignalEngine
from execution.maker_engine import MakerEngine


class SideDecisionHost(Protocol):
    """
    Minimum contract expected by SideDecisionMixin.

    This is intentionally incomplete; it documents the highest-risk runtime
    dependencies so refactors do not silently drop required state.
    """

    active_side: ActiveSide
    active_side_locked: bool
    active_side_locked_since_ts: float
    bi_side_enabled: bool
    bi_side_default_mode: str
    side_decision_score: Decimal
    side_decision_reason: str
    side_decision_due_ts: float
    side_decision_done_for_market: bool
    side_flip_count: int
    current_market_slug: str
    current_market_open_spot: Any
    market_strike_source_by_slug: Dict[str, str]
    external_spot_history: List[Any]
    active_maker_orders: Dict[str, Any]
    live_inventory_cost: Dict[str, Any]
    market_buy_count_by_slug: Dict[str, int]
    _signal_engine: SignalEngine

    def _normalize_active_side(self, value: Any) -> ActiveSide: ...
    def _sync_active_instrument(self) -> None: ...
    def _cancel_maker_order_side(self, order_key: str, reason: str = "") -> None: ...
    def _instrument_for_side(self, side: ActiveSide) -> Any: ...
    def _normalize_instrument_id(self, instrument_id: Any) -> Any: ...
    def _primary_instrument_for_market(self) -> Any: ...
    def _bump_thesis_epoch(self, slug: str) -> int: ...
    def _current_thesis_epoch(self, slug: str) -> int: ...
    def _db_strategy_event(self, event_type: str, payload: Dict[str, Any]) -> None: ...


class SideDecisionMixin:
    """Mixin providing side-decision and regime-guard logic for IntegratedBTCStrategy."""

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _reset_side_decision_state(self) -> None:
        self.active_side = ActiveSide.UP if not self.bi_side_enabled else self._normalize_active_side(self.bi_side_default_mode)
        self.active_side_locked = False
        self.active_side_locked_since_ts = 0.0
        self.active_side_lock_score_abs = Decimal("0")
        self.side_decision_ts = 0.0
        self.side_decision_score = Decimal("0")
        self.side_decision_reason = "market_reset"
        self.side_decision_due_ts = 0.0
        self.side_decision_done_for_market = not self.bi_side_enabled
        self.side_decision_inputs = {}
        self.side_flip_count = 0
        self.side_pending_flip_side = ActiveSide.NONE
        self.side_pending_flip_count = 0
        self.side_pending_flip_since_ts = 0.0
        self._last_side_observation_signature = None
        self._last_side_decision_log_ts = 0.0
        self._last_side_decision_log_signature = None
        # Reset the per-market open anchor and let the first post-rollover external
        # spot observation lock it exactly once for open-drift calculations.
        self.current_market_open_spot = None
        # Reset SignalEngine for new market cycle
        _sig_eng = getattr(self, '_signal_engine', None)
        if _sig_eng is not None:
            _sig_eng.reset()
        self._sync_active_instrument()

    def _effective_recent_cycle_window(self) -> List[float]:
        window = list(self.recent_market_combined_pnls)[-self.bi_side_regime_n_markets :]
        return [float(v) for v in window]

    def _cancel_stale_buy_orders_after_side_change(
        self,
        *,
        old_side: ActiveSide,
        new_side: ActiveSide,
    ) -> None:
        if new_side == ActiveSide.NONE or not getattr(self, "active_side_locked", False):
            return
        active_maker_orders = getattr(self, "active_maker_orders", None)
        cancel_fn = getattr(self, "_cancel_maker_order_side", None)
        if not isinstance(active_maker_orders, dict) or not callable(cancel_fn):
            return
        target_inst = self._normalize_instrument_id(self._instrument_for_side(new_side))
        if target_inst is None:
            return
        canceled_order_keys: List[str] = []
        for order_key, state in list(active_maker_orders.items()):
            if str(state.get("side", "") or "") != "buy":
                continue
            inst = self._normalize_instrument_id(state.get("instrument_id"))
            if inst is None or inst == target_inst:
                continue
            cancel_fn(order_key, reason="side_change_stale_buy")
            canceled_order_keys.append(str(order_key))
        if canceled_order_keys:
            logger.warning(
                "Canceled stale BUY orders after side change: "
                f"{old_side.value}->{new_side.value} "
                f"target_inst={target_inst} count={len(canceled_order_keys)}"
            )
            self._db_strategy_event(
                "SIDE_CHANGE_CANCELED_STALE_BUYS",
                {
                    "slug": str(getattr(self, "current_market_slug", "") or ""),
                    "old_side": old_side.value,
                    "new_side": new_side.value,
                    "target_instrument_id": str(target_inst),
                    "canceled_order_keys": canceled_order_keys,
                },
            )

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def _log_side_decision_skip_throttled(
        self,
        reason: str,
        now_ts: float,
        inputs: Dict[str, Any],
        phase: Any,  # MarketPhase – defined in run_bot.py
    ) -> None:
        key = f"{self.current_market_slug or '-'}:{reason}"
        last_ts = float(self._side_decision_skip_log_ts_by_reason.get(key, 0.0))
        if now_ts - last_ts < float(self.side_decision_skip_log_interval_sec):
            return
        self._side_decision_skip_log_ts_by_reason[key] = now_ts

        slug = str(self.current_market_slug or "")
        strike_source = self.market_strike_source_by_slug.get(slug, "pending")
        logger.info(
            "Side decision skipped: "
            f"slug={slug or '-'} reason={reason} phase={phase.value} "
            f"due_in={max(0.0, self.side_decision_due_ts - now_ts):.1f}s "
            f"spot={inputs.get('spot')} market_open_spot={inputs.get('market_open_spot')} "
            f"strike={inputs.get('strike')} strike_source={strike_source} "
            f"primary_inst={self._primary_instrument_for_market() or '-'} "
            f"history_len={len(self.external_spot_history)}"
        )

    def _log_side_decision_result_throttled(
        self,
        side: ActiveSide,
        score: Decimal,
        reason: str,
        locked: bool,
        now_ts: float,
    ) -> None:
        locked_txt = "yes" if locked else "no"
        side_signature = (side.value, locked_txt)
        if (
            side_signature == self._last_side_decision_log_signature
            and (now_ts - self._last_side_decision_log_ts) < float(self.bi_side_decision_log_interval_sec)
        ):
            return
        self._last_side_decision_log_signature = side_signature
        self._last_side_decision_log_ts = now_ts
        logger.info(
            "Side decision: "
            f"slug={self.current_market_slug or '-'} active_side={side.value} "
            f"score={float(score):+.2f} reason={reason} "
            f"locked={locked_txt}"
        )

    def _record_side_observation(
        self,
        side: ActiveSide,
        score: Decimal,
        reason: str,
        inputs: Dict[str, Any],
        now_ts: float,
    ) -> None:
        signature = (str(self.current_market_slug or ""), side.value, reason)
        if signature == self._last_side_observation_signature:
            return
        self._last_side_observation_signature = signature
        payload = dict(inputs)
        payload.update(
            {
                "proposed_side": side.value,
                "score": float(score),
                "reason": reason,
                "decision_ts": now_ts,
                "observation_only": True,
                "observation_remaining_sec": max(0.0, self.side_decision_due_ts - now_ts),
            }
        )
        self._db_strategy_event("SIDE_DECISION_OBSERVATION", payload)

    # ------------------------------------------------------------------
    # Flip helper
    # ------------------------------------------------------------------

    def _side_flip_requirements_met(
        self,
        side: ActiveSide,
        score: Decimal,
        inputs: Dict[str, Any],
    ) -> bool:
        min_score_up = Decimal(
            str(
                getattr(
                    self,
                    "bi_side_flip_min_score_up_new",
                    getattr(self, "directional_entry_min_score_abs_new", Decimal("0.05")),
                )
            )
        )
        max_score_down = Decimal(
            str(
                getattr(
                    self,
                    "bi_side_flip_max_score_down_new",
                    -min_score_up,
                )
            )
        )
        if side == ActiveSide.UP:
            fair_value = inputs.get("fair_up")
            if score < min_score_up:
                return False
        elif side == ActiveSide.DOWN:
            fair_value = inputs.get("fair_down")
            if score > max_score_down:
                return False
        else:
            return False
        if fair_value is None:
            return False
        try:
            fair_dec = Decimal(str(fair_value))
        except Exception:
            return False
        min_fair = Decimal(str(getattr(self, "bi_side_flip_min_fair", "0.60")))
        if fair_dec < min_fair:
            return False
        opposing_fair = inputs.get("fair_down") if side == ActiveSide.UP else inputs.get("fair_up")
        if opposing_fair is None:
            return True
        try:
            opposing_fair_dec = Decimal(str(opposing_fair))
        except Exception:
            return True
        fair_inversion_min_ps = Decimal(
            str(getattr(self, "bi_side_flip_fair_inversion_min_ps", Decimal("0.03")))
        )
        return fair_dec >= (opposing_fair_dec + fair_inversion_min_ps)

    def _held_inventory_allows_extra_flip(self, *, proposed_side: ActiveSide) -> bool:
        if not self.active_side_locked or proposed_side in (ActiveSide.NONE, self.active_side):
            return False
        if not self.bi_side_allow_intramarket_flip:
            return False
        held_inst = self._instrument_for_side(self.active_side)
        if held_inst is None:
            return False
        state = self.live_inventory_cost.get(self._instrument_key(held_inst)) or {}
        try:
            qty = Decimal(str(state.get("qty", "0")))
        except Exception:
            qty = Decimal("0")
        if qty <= Decimal("1"):
            return False
        return True

    def _held_inventory_flip_requirements_met(
        self,
        *,
        side: ActiveSide,
        score: Decimal,
        now_ts: float,
    ) -> bool:
        if not self._held_inventory_allows_extra_flip(proposed_side=side):
            return True
        min_score_up = Decimal(
            str(
                getattr(
                    self,
                    "bi_side_flip_min_score_up_held_new",
                    getattr(self, "bi_side_flip_min_score_up_new", Decimal("0.18")),
                )
            )
        )
        max_score_down = Decimal(
            str(
                getattr(
                    self,
                    "bi_side_flip_max_score_down_held_new",
                    -min_score_up,
                )
            )
        )
        if side == ActiveSide.UP and score < min_score_up:
            return False
        if side == ActiveSide.DOWN and score > max_score_down:
            return False
        min_persist = float(getattr(self, "bi_side_flip_min_persist_sec_held_new", 0.0))
        if min_persist <= 0:
            return True
        pending_since = float(getattr(self, "side_pending_flip_since_ts", 0.0) or 0.0)
        if pending_since <= 0:
            return False
        return (now_ts - pending_since) >= min_persist

    def _pre_entry_flip_allowed(self, *, proposed_side: ActiveSide) -> bool:
        """Permit a confirmed reversal only before the strategy owns inventory."""
        if not bool(getattr(self, "bi_side_allow_pre_entry_flip", True)):
            return False
        if not self.active_side_locked or proposed_side in (ActiveSide.NONE, self.active_side):
            return False
        try:
            if Decimal(str(getattr(self, "inventory_delta_shares", Decimal("0")))) > 0:
                return False
        except (ArithmeticError, TypeError, ValueError):
            return False
        for state in getattr(self, "live_inventory_cost", {}).values():
            try:
                if Decimal(str(state.get("qty", "0"))) > 0:
                    return False
            except (AttributeError, ArithmeticError, TypeError, ValueError):
                return False
        return True

    def _populate_spot_source_inputs(
        self,
        inputs: Dict[str, Any],
        *,
        reference_spot: Optional[Decimal],
        now_ts: float,
    ) -> None:
        selected_spot = None
        if hasattr(self, "_capture_market_open_spot_detail"):
            try:
                selected_spot = self._capture_market_open_spot_detail(now_ts=now_ts)
            except Exception:
                selected_spot = None
        selected_price = selected_spot[0] if selected_spot is not None else reference_spot
        selected_source = selected_spot[1] if selected_spot is not None else str(getattr(self, "latest_external_spot_source", "") or "")
        selected_age = selected_spot[2] if selected_spot is not None else None
        inputs["reference_spot_source"] = selected_source
        inputs["reference_spot_price"] = float(selected_price) if selected_price is not None else None
        inputs["reference_spot_age_sec"] = float(selected_age) if selected_age is not None else None
        inputs["selected_spot_source"] = selected_source
        inputs["selected_spot_age_sec"] = float(selected_age) if selected_age is not None else None

        binance_px = getattr(self, "_binance_ws_price", None)
        binance_ts = float(getattr(self, "_binance_ws_price_ts", 0.0) or 0.0)
        inputs["binance_spot_price"] = float(binance_px) if binance_px is not None else None
        inputs["binance_spot_age_sec"] = max(0.0, now_ts - binance_ts) if binance_ts > 0 else None

        poly_px = getattr(self, "_polymarket_chainlink_price", None)
        poly_ts = float(getattr(self, "_polymarket_chainlink_price_ts", 0.0) or 0.0)
        inputs["polymarket_chainlink_price"] = float(poly_px) if poly_px is not None else None
        inputs["polymarket_chainlink_age_sec"] = max(0.0, now_ts - poly_ts) if poly_ts > 0 else None

        twap_px = getattr(self, "_polymarket_chainlink_twap_price", None)
        twap_ts = float(getattr(self, "_polymarket_chainlink_twap_price_ts", 0.0) or 0.0)
        inputs["polymarket_chainlink_twap_price"] = float(twap_px) if twap_px is not None else None
        inputs["polymarket_chainlink_twap_age_sec"] = max(0.0, now_ts - twap_ts) if twap_ts > 0 else None
        inputs["polymarket_chainlink_twap_window_sec"] = getattr(
            self,
            "_polymarket_chainlink_twap_window_sec",
            getattr(self, "polymarket_chainlink_twap_window_sec", None),
        )

    # ------------------------------------------------------------------
    # Core decision logic (dispatcher)
    # ------------------------------------------------------------------

    def _compute_side_decision(self, now_ts: float) -> tuple[ActiveSide, Decimal, str, Dict[str, Any]]:
        """Compute side decision using the new probabilistic SignalEngine."""
        return self._compute_side_decision_new(now_ts)

    # ------------------------------------------------------------------
    # Helper: get UP token mid-price from cache
    # ------------------------------------------------------------------

    def _get_up_token_mid_for_side_decision(self) -> Optional[Decimal]:
        """Fetch the UP token mid-price from the instrument cache.

        Returns None if unavailable.
        """
        up_inst = getattr(self, 'current_up_instrument_id', None)
        if up_inst is None:
            up_inst = self._primary_instrument_for_market()
        if up_inst is None:
            return None
        try:
            quote = self.cache.quote_tick(up_inst)
            if quote and quote.bid_price and quote.ask_price:
                mid = (quote.bid_price + quote.ask_price) / 2
                if Decimal("0.01") < mid < Decimal("0.99"):
                    return mid
        except Exception:
            pass
        # Fallback: last recorded real price
        inst_key = str(up_inst)
        history = self.real_price_history_by_inst.get(inst_key)
        if history and len(history) > 0:
            return history[-1]
        if self.real_price_history:
            return self.real_price_history[-1]
        return None

    # ------------------------------------------------------------------
    # NEW: SignalEngine-based side decision
    # ------------------------------------------------------------------

    def _compute_side_decision_new(self, now_ts: float) -> tuple[ActiveSide, Decimal, str, Dict[str, Any]]:
        """Continuous probabilistic side decision using SignalEngine."""
        inputs: Dict[str, Any] = {
            "slug": self.current_market_slug or "",
            "engine": "new_signal",
            "fair_up": None,
            "fair_down": None,
        }
        if not self.bi_side_enabled:
            return ActiveSide.UP, Decimal("999"), "bi_side_disabled", inputs

        # --- Gather spot & strike ---
        spot = self._capture_market_open_spot()
        inputs["spot"] = float(spot) if spot is not None else None
        self._populate_spot_source_inputs(inputs, reference_spot=spot, now_ts=now_ts)
        if spot is None or spot <= 0:
            return self._normalize_active_side(self.bi_side_default_mode), Decimal("0"), "spot_unavailable", inputs

        strike_dec = self.market_strike_cache_by_slug.get(str(self.current_market_slug or ""))
        if strike_dec is None or strike_dec <= 0:
            return self._normalize_active_side(self.bi_side_default_mode), Decimal("0"), "strike_unavailable", inputs
        inputs["strike"] = float(strike_dec)

        # Lock per-market open spot for open-drift style reference and logging.
        self._ensure_market_open_spot_locked(strike_dec)
        inputs["market_open_spot"] = float(self.current_market_open_spot) if self.current_market_open_spot is not None else None

        # --- Time left ---
        end_ts = getattr(self, "current_market_end_timestamp", None)
        time_left_sec = max(0.0, float(end_ts - now_ts)) if end_ts is not None else 0.0
        inputs["time_left_sec"] = time_left_sec

        # --- Sigma ---
        sigma = self.maker_digital_sigma_default
        est_sigma = self._estimate_external_spot_sigma_annualized()
        if est_sigma and est_sigma > 0:
            sigma = est_sigma
        sigma = sigma * self.maker_digital_vol_scale
        sigma = max(self.maker_digital_sigma_floor, min(self.maker_digital_sigma_ceiling, sigma))

        # --- Get market mid-price ---
        market_mid = self._get_up_token_mid_for_side_decision()
        inputs["market_mid"] = float(market_mid) if market_mid is not None else None

        # Feed mid into signal engine
        _sig_eng: Optional[SignalEngine] = getattr(self, '_signal_engine', None)
        if _sig_eng is None:
            logger.warning("SignalEngine not initialised; holding NONE until engine is ready")
            return ActiveSide.NONE, Decimal("0"), "signal_engine_unavailable", inputs

        if market_mid is not None:
            _sig_eng.update_market_mid(market_mid, now_ts)

        # --- Compute signals ---
        signals = _sig_eng.compute(
            spot=spot,
            strike=strike_dec,
            sigma=sigma,
            time_left_sec=time_left_sec,
            market_mid=market_mid,
        )
        inputs.update(signals.to_dict())

        # --- Fair values from digital pricer (for flip requirements / logging) ---
        fair_up = None
        fair_down = None
        if self.maker_fair_pricer_mode == "digital":
            fair_up = MakerEngine.digital_up_probability(
                spot=float(spot),
                strike=float(strike_dec),
                sigma_annual=float(sigma),
                time_left_sec=time_left_sec,
            )
            fair_down = Decimal("1.0") - fair_up
        inputs["fair_up"] = float(fair_up) if fair_up is not None else None
        inputs["fair_down"] = float(fair_down) if fair_down is not None else None

        # P0 edge state is initially observational. It keeps the market mid
        # as an implied-probability baseline and records model-vs-price edge
        # without turning this path into an aggressive taker strategy.
        edge_state = build_edge_state(
            model_probability_up=fair_up,
            market_mid=market_mid,
            up_bid=None,
            up_ask=None,
            down_bid=None,
            down_ask=None,
            total_cost_buffer=Decimal("0"),
        )
        inputs.update({"edge_mode": "shadow_only", **edge_state.to_dict()})

        # --- Decision ---
        score = Decimal(str(round(signals.composite_score, 6)))
        min_confidence = float(getattr(self, 'side_signal_min_confidence', 0.15))
        up_threshold = float(getattr(self, 'side_signal_threshold_up', 0.05))
        down_threshold = float(getattr(self, 'side_signal_threshold_down', 0.05))

        reason_parts = [
            f"cs={signals.composite_score:+.4f}",
            f"conf={signals.confidence:.3f}",
            f"mkt={signals.market_consensus:+.3f}",
            f"btc={signals.btc_trend:+.3f}",
            f"zs={signals.strike_proximity:+.3f}",
            f"w={signals.w_market:.2f}/{signals.w_btc:.2f}/{signals.w_strike:.2f}",
        ]
        reason = " ".join(reason_parts)

        if signals.confidence < min_confidence:
            return ActiveSide.NONE, score, f"{reason} low_confidence", inputs

        proposed_side = ActiveSide.NONE
        if signals.composite_score > up_threshold:
            proposed_side = ActiveSide.UP
        elif signals.composite_score < -down_threshold and self.current_down_instrument_id is not None:
            proposed_side = ActiveSide.DOWN

        # --- Side penalty check (shared with legacy) ---
        if proposed_side != ActiveSide.NONE and self.current_market_slug:
            penalty_key = f"{self.current_market_slug}:{proposed_side.value}"
            penalty_until = float(self.side_stop_loss_penalty_until_by_market_side.get(penalty_key, 0.0))
            if now_ts < penalty_until:
                inputs["side_penalty_side"] = proposed_side.value
                inputs["side_penalty_remaining_sec"] = max(0.0, penalty_until - now_ts)
                return ActiveSide.NONE, score, f"{reason} side_penalty={proposed_side.value.lower()}", inputs

        if proposed_side != ActiveSide.NONE:
            return proposed_side, score, reason, inputs
        return ActiveSide.NONE, score, reason, inputs

    # ------------------------------------------------------------------
    # Helper: lock market open spot (shared logic)
    # ------------------------------------------------------------------

    def _ensure_market_open_spot_locked(self, strike_dec: Decimal) -> None:
        """Lock the per-market open spot if not yet set (shared by legacy and new)."""
        if self.current_market_open_spot is not None:
            return
        if not self.current_market_slug:
            return
        slug = str(self.current_market_slug)
        start_ts = self.market_start_ts_by_slug.get(slug)
        if start_ts is None:
            parsed_start = extract_market_start_ts_from_slug(slug)
            if parsed_start is not None:
                start_ts = parsed_start
                self.market_start_ts_by_slug[slug] = parsed_start
        if start_ts is not None:
            anchor = self._resolve_opening_strike_from_polymarket_history(int(start_ts))
            if anchor is None:
                anchor = self._resolve_opening_strike_from_history(int(start_ts))
            if anchor is not None:
                _anchor_ts, anchor_px = anchor
                self.current_market_open_spot = anchor_px
                logger.info(
                    f"✓ Locked current_market_open_spot for {slug} from spot history: "
                    f"${float(anchor_px):,.2f}"
                )
                return
        if self.current_market_open_spot is None and strike_dec > 0:
            self.current_market_open_spot = strike_dec
            logger.info(
                f"✓ Locked current_market_open_spot for {slug} from strike fallback: "
                f"${float(strike_dec):,.2f}"
            )

    async def _maybe_finalize_side_decision(self, now_ts: float, phase: Any) -> None:  # phase: MarketPhase
        if not self.bi_side_enabled:
            self.active_side = ActiveSide.UP
            self.side_decision_done_for_market = True
            self._sync_active_instrument()
            return
        flip_max_per_market = int(getattr(self, "bi_side_flip_max_per_market", 1))
        allow_intramarket_flip = bool(getattr(self, "bi_side_allow_intramarket_flip", False))
        if phase in (MarketPhase.WAITING, MarketPhase.SETTLING):
            return
        time_left_sec = None
        if self.current_market_end_timestamp is not None:
            time_left_sec = float(self.current_market_end_timestamp - now_ts)
            if time_left_sec < float(self.bi_side_min_time_left_sec):
                return
        if (
            self.active_side == ActiveSide.NONE
            and self.side_decision_ts > 0
            and (now_ts - self.side_decision_ts) < float(self.bi_side_reeval_interval_sec)
        ):
            return

        slug = str(self.current_market_slug or "")
        primary_inst = self._primary_instrument_for_market()
        if slug and slug not in self.market_strike_cache_by_slug and primary_inst is not None:
            warmed_strike = await self._get_market_strike_for_instrument(primary_inst)
            strike_source = self.market_strike_source_by_slug.get(slug, "pending")
            if warmed_strike is not None:
                logger.info(
                    "Side decision warmup: "
                    f"slug={slug} strike={float(warmed_strike):.2f} "
                    f"strike_source={strike_source} primary_inst={primary_inst}"
                )
            else:
                self._log_side_decision_skip_throttled(
                    reason="strike_warmup_pending",
                    now_ts=now_ts,
                    inputs={
                        "spot": float(self.latest_external_spot) if self.latest_external_spot is not None else None,
                        "market_open_spot": float(self.current_market_open_spot) if self.current_market_open_spot is not None else None,
                        "strike": None,
                    },
                    phase=phase,
                )

        side, score, reason, inputs = self._compute_side_decision(now_ts)
        pre_entry_flip_allowed = self._pre_entry_flip_allowed(proposed_side=side)
        if reason in {"spot_unavailable", "strike_unavailable"}:
            self.side_decision_score = score
            self.side_decision_reason = reason
            self.side_decision_ts = now_ts
            self.side_decision_inputs = dict(inputs)
            payload = dict(inputs)
            payload.update({"reason": reason, "decision_ts": now_ts})
            self._db_strategy_event("SIDE_DECISION_SKIPPED", payload)
            self._log_side_decision_skip_throttled(reason=reason, now_ts=now_ts, inputs=inputs, phase=phase)
            return
        if self.side_decision_due_ts > 0 and now_ts < self.side_decision_due_ts:
            self.side_decision_score = score
            self.side_decision_reason = reason
            self.side_decision_ts = now_ts
            self.side_decision_inputs = dict(inputs)
            self._record_side_observation(side=side, score=score, reason=reason, inputs=inputs, now_ts=now_ts)
            return

        if self.active_side_locked:
            self.side_decision_score = score
            self.side_decision_reason = reason
            self.side_decision_ts = now_ts
            self.side_decision_inputs = dict(inputs)
            can_attempt_flip = bool(
                self.active_side_locked
                and (
                    (
                        allow_intramarket_flip
                        and self.side_flip_count < flip_max_per_market
                    )
                    or pre_entry_flip_allowed
                )
            )
            extra_flip_for_held_inventory = (
                not can_attempt_flip
                and self._held_inventory_allows_extra_flip(proposed_side=side)
            )
            flip_ready = self._side_flip_requirements_met(side=side, score=score, inputs=inputs)
            if side == ActiveSide.NONE or side == self.active_side or not flip_ready:
                self.side_pending_flip_side = ActiveSide.NONE
                self.side_pending_flip_count = 0
                self.side_pending_flip_since_ts = 0.0
                return
            if not can_attempt_flip and not extra_flip_for_held_inventory:
                return
            if side != self.side_pending_flip_side:
                self.side_pending_flip_side = side
                self.side_pending_flip_count = 1
                self.side_pending_flip_since_ts = now_ts
                return
            self.side_pending_flip_count += 1
            required_confirmations = self.bi_side_flip_confirmations
            if self._held_inventory_allows_extra_flip(proposed_side=side):
                required_confirmations = max(
                    required_confirmations,
                    int(getattr(self, "bi_side_flip_confirmations_held_new", required_confirmations)),
                )
            if self.side_pending_flip_count < required_confirmations:
                return
            if not self._held_inventory_flip_requirements_met(
                side=side,
                score=score,
                now_ts=now_ts,
            ):
                return
            old_side = self.active_side
            self.active_side = side
            self.side_decision_score = score
            self.side_decision_reason = f"{reason} flip={self.side_flip_count + 1}"
            self.side_decision_ts = now_ts
            self.side_decision_done_for_market = True
            self.side_decision_inputs = dict(inputs)
            self.side_flip_count += 1
            if self.active_side_locked and side != ActiveSide.NONE and old_side != side:
                self.active_side_locked_since_ts = now_ts
                self.active_side_lock_score_abs = abs(score)
            self.side_pending_flip_side = ActiveSide.NONE
            self.side_pending_flip_count = 0
            self.side_pending_flip_since_ts = 0.0
            self._sync_active_instrument()
            if old_side != side:
                thesis_epoch = (
                    self._bump_thesis_epoch(str(self.current_market_slug or ""))
                    if hasattr(self, "_bump_thesis_epoch")
                    else int(getattr(self, "thesis_epoch", 0))
                )
                self._cancel_stale_buy_orders_after_side_change(
                    old_side=old_side,
                    new_side=side,
                )
            else:
                thesis_epoch = self._current_thesis_epoch(str(self.current_market_slug or ""))
            if self.active_side_locked and side != ActiveSide.NONE:
                self._force_quote_refresh_once = True
                self._force_quote_refresh_reason = f"locked_flip:{old_side.value}->{side.value}"
            payload = dict(inputs)
            payload.update(
                {
                    "active_side": side.value,
                    "previous_side": old_side.value if old_side is not None else None,
                    "score": float(score),
                    "reason": self.side_decision_reason,
                    "decision_ts": now_ts,
                    "flip_count": self.side_flip_count,
                    "extra_flip_for_held_inventory": bool(extra_flip_for_held_inventory),
                    "pre_entry_flip_allowed": bool(pre_entry_flip_allowed),
                    "thesis_epoch": int(thesis_epoch),
                }
            )
            self._db_strategy_event("SIDE_MODE_FLIPPED", payload)
            self._log_side_decision_result_throttled(
                side=side,
                score=score,
                reason=self.side_decision_reason,
                locked=self.active_side_locked,
                now_ts=now_ts,
            )
            return

        old_side = self.active_side
        self.active_side = side
        self.side_decision_score = score
        self.side_decision_reason = reason
        self.side_decision_ts = now_ts
        self.side_decision_done_for_market = True
        self.side_decision_inputs = inputs
        if self.bi_side_lock_until_reduce_only and phase == MarketPhase.ACTIVE and side != ActiveSide.NONE:
            self.active_side_locked = True
            if old_side != side or self.active_side_locked_since_ts <= 0:
                self.active_side_locked_since_ts = now_ts
                self.active_side_lock_score_abs = abs(score)
            self.side_pending_flip_side = ActiveSide.NONE
            self.side_pending_flip_count = 0
            self.side_pending_flip_since_ts = 0.0
            if old_side != side:
                self._force_quote_refresh_once = True
                self._force_quote_refresh_reason = f"locked_entry:{old_side.value}->{side.value}"
        elif side == ActiveSide.NONE:
            self.active_side_locked_since_ts = 0.0
            self.active_side_lock_score_abs = Decimal("0")
            self.side_decision_due_ts = now_ts + float(self.bi_side_reeval_interval_sec)
        self._sync_active_instrument()
        if old_side != side:
            thesis_epoch = self._bump_thesis_epoch(str(self.current_market_slug or ""))
            self._cancel_stale_buy_orders_after_side_change(
                old_side=old_side,
                new_side=side,
            )
        else:
            thesis_epoch = self._current_thesis_epoch(str(self.current_market_slug or ""))
        event_name = "SIDE_MODE_CHANGED" if old_side != side else "SIDE_DECISION"
        payload = dict(inputs)
        payload.update(
            {
                "active_side": side.value,
                "previous_side": old_side.value if old_side is not None else None,
                "score": float(score),
                "reason": reason,
                "decision_ts": now_ts,
                "thesis_epoch": int(thesis_epoch),
            }
        )
        self._db_strategy_event(event_name, payload)
        self._log_side_decision_result_throttled(
            side=side,
            score=score,
            reason=reason,
            locked=self.active_side_locked,
            now_ts=now_ts,
        )

    # ------------------------------------------------------------------
    # Regime guard
    # ------------------------------------------------------------------

    def _regime_guard_min_negative_markets(self) -> int:
        return self.regime_guard_policy.min_negative_markets()

    def _regime_guard_should_trigger(self, window: List[float]) -> tuple[bool, float, int]:
        return self.regime_guard_policy.should_trigger(window)

    def _append_cycle_and_maybe_trigger_regime_guard(
        self,
        cycle_combined_pnl: float,
        slug: str,
        source: str,
    ) -> None:
        if not self.regime_guard_enabled:
            return
        self.recent_market_combined_pnls.append(float(cycle_combined_pnl))
        if len(self.recent_market_combined_pnls) < self.regime_guard_n_markets:
            return
        window = list(self.recent_market_combined_pnls)[-self.regime_guard_n_markets :]
        should_trigger, window_sum, neg_count = self._regime_guard_should_trigger(window)
        if not should_trigger:
            return
        until_ts = time.time() + float(self.regime_guard_cooldown_sec)
        self.regime_guard_conservative_until_ts = max(self.regime_guard_conservative_until_ts, until_ts)
        logger.warning(
            "REGIME GUARD triggered: "
            f"window={window} neg={neg_count}/{self.regime_guard_n_markets} "
            f"sum={window_sum:.4f} <= trigger={float(self.regime_guard_trigger_sum_pnl_usdc):.4f}; "
            f"raise BUY edge gate to {float(self.maker_min_directional_edge_ps_conservative):.4f} "
            f"for {self.regime_guard_cooldown_sec}s"
        )
        self._db_strategy_event(
            "REGIME_GUARD_TRIGGERED",
            {
                "source": source,
                "slug": slug,
                "window_combined_pnls": window,
                "window_sum_pnl_usdc": window_sum,
                "negative_markets": neg_count,
                "min_negative_markets": self._regime_guard_min_negative_markets(),
                "trigger_sum_pnl_usdc": float(self.regime_guard_trigger_sum_pnl_usdc),
                "n_markets": self.regime_guard_n_markets,
                "cooldown_sec": self.regime_guard_cooldown_sec,
            },
        )

    def _bootstrap_regime_guard_window_from_db(self) -> None:
        if not self.regime_guard_enabled or not self.trade_db:
            return
        db_path = getattr(self.trade_db, "db_path", "")
        if not db_path:
            return
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT payload_json
                FROM strategy_events
                WHERE event_type='MARKET_CYCLE_PNL'
                ORDER BY id DESC
                LIMIT ?
                """,
                (self.regime_guard_bootstrap_lookback_markets,),
            ).fetchall()
            conn.close()
            if not rows:
                return
            recovered: List[float] = []
            for row in reversed(rows):
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except Exception:
                    payload = {}
                val = payload.get("cycle_combined_pnl_usdc")
                if val is None:
                    continue
                recovered.append(float(val))
            if not recovered:
                return
            self.recent_market_combined_pnls.clear()
            for v in recovered[-self.regime_guard_n_markets :]:
                self.recent_market_combined_pnls.append(float(v))
            logger.info(
                "Regime guard bootstrap: recovered recent combined window "
                f"{list(self.recent_market_combined_pnls)}"
            )
            if len(self.recent_market_combined_pnls) >= self.regime_guard_n_markets:
                window = list(self.recent_market_combined_pnls)[-self.regime_guard_n_markets :]
                should_trigger, window_sum, neg_count = self._regime_guard_should_trigger(window)
                if should_trigger:
                    until_ts = time.time() + float(self.regime_guard_cooldown_sec)
                    self.regime_guard_conservative_until_ts = max(
                        self.regime_guard_conservative_until_ts,
                        until_ts,
                    )
                    logger.warning(
                        "Regime guard armed from bootstrap window: "
                        f"neg={neg_count}/{self.regime_guard_n_markets} sum={window_sum:.4f}"
                    )
                    self._db_strategy_event(
                        "REGIME_GUARD_BOOTSTRAP_TRIGGERED",
                        {
                            "window_combined_pnls": window,
                            "window_sum_pnl_usdc": window_sum,
                            "negative_markets": neg_count,
                            "min_negative_markets": self._regime_guard_min_negative_markets(),
                            "trigger_sum_pnl_usdc": float(self.regime_guard_trigger_sum_pnl_usdc),
                            "n_markets": self.regime_guard_n_markets,
                            "cooldown_sec": self.regime_guard_cooldown_sec,
                        },
                    )
        except Exception as e:
            logger.warning(f"Regime guard bootstrap failed: {e}")
