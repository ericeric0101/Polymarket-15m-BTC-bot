"""
bot/taker_exit.py – TakerExitMixin

Extracted from run_bot.py.
Contains taker exit position management logic as a Mixin.
IntegratedBTCStrategy inherits this mixin so all self.* references remain valid.
"""
from __future__ import annotations

import time
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Dict, Optional, Protocol

from loguru import logger
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.objects import Quantity

from bot.enums import ActiveSide
from bot.models import (
    ExitDecisionType,
    MarketSnapshot,
    PositionState,
    SignalDecision,
)


class TakerExitHost(Protocol):
    taker_exit_enabled: bool
    hold_to_redeem_enabled: bool
    taker_exit_cooldown_sec: int
    taker_exit_eval_interval_sec: float
    taker_exit_reject_cooldown_until_by_inst: dict[str, float]
    taker_exit_tail_attempted_by_inst: dict[str, Any]
    taker_exit_last_eval_ts_by_inst: dict[str, float]
    _stop_loss_execution_priority_by_inst: dict[str, bool]
    live_inventory_cost: dict[str, Any]
    pending_taker_exit_by_inst: dict[str, Any]
    last_taker_exit_ts_by_inst: dict[str, float]
    current_market_end_timestamp: Any
    current_market_slug: Any
    market_strike_cache_by_slug: dict[str, Any]
    maker_reduce_only_no_new_sell_last_sec: int
    taker_exit_disable_stop_loss_last_sec: int
    market_phase: Any
    active_side: Any
    side_decision_score: Decimal
    active_side_locked: bool
    side_decision_reason: str

    def _capture_market_open_spot(self) -> Optional[Decimal]: ...

    def _maker_quote_instruments(self) -> list[Any]: ...
    def _instrument_key(self, instrument_id: Any) -> str: ...
    def _normalize_instrument_id(self, instrument_id: Any) -> Any: ...
    def _get_quote_for_instrument(self, instrument_id: Any) -> Optional[tuple[Decimal, Decimal]]: ...
    async def _get_dynamic_fee_rate(self, token_id: Optional[str] = None) -> Optional[Decimal]: ...
    def _extract_token_id_from_instrument(self, instrument_id: str) -> Optional[str]: ...
    def _infer_market_fee_rate_default(self) -> Decimal: ...
    def _is_emergency_exit_window(self, time_left_sec: Optional[float]) -> bool: ...
    def _get_effective_sellable_qty(self, instrument_id: Optional[Any]) -> Decimal: ...
    def _instrument_for_side(self, side: Any) -> Any: ...
    def _side_for_instrument_id(self, instrument_id: Any) -> Any: ...


class TakerExitMixin:
    """Mixin providing taker exit position management logic."""

    async def _maybe_taker_exit_positions(self: TakerExitHost, now_ts: float, is_simulation: bool) -> None:
        if is_simulation or not self.taker_exit_enabled:
            return
        if getattr(self, "hold_to_redeem_enabled", False):
            return
        if self.taker_exit_cooldown_sec < 0:
            return
        end_ts = getattr(self, "current_market_end_timestamp", None)
        time_left_sec = (end_ts - now_ts) if end_ts is not None else None
        exit_stage = self.exit_policy.stage(time_left_sec)
        in_reduce_only_tail = (
            time_left_sec is not None
            and self.maker_reduce_only_no_new_sell_last_sec > 0
            and time_left_sec <= float(self.maker_reduce_only_no_new_sell_last_sec)
        )
        stop_loss_disabled_in_tail = (
            time_left_sec is not None
            and self.taker_exit_disable_stop_loss_last_sec > 0
            and time_left_sec <= float(self.taker_exit_disable_stop_loss_last_sec)
        )
        target_instruments = list(self._maker_quote_instruments())
        seen_instruments = {self._instrument_key(inst_id) for inst_id in target_instruments}
        # Critical: active_side can flip away from the actual held instrument.
        # When that happens we still need to evaluate exits on the off-side inventory,
        # otherwise a wrong-side position can survive all the way to settlement.
        for inv_inst_key, inv_state in list(self.live_inventory_cost.items()):
            try:
                inv_qty = Decimal(str(inv_state.get("qty", "0")))
            except Exception:
                inv_qty = Decimal("0")
            if inv_qty <= 0:
                continue
            if inv_inst_key in seen_instruments:
                continue
            inv_inst = self._normalize_instrument_id(inv_inst_key)
            if inv_inst is None:
                continue
            target_instruments.append(inv_inst)
            seen_instruments.add(inv_inst_key)
        for inst_id in target_instruments:
            inst_key = self._instrument_key(inst_id)
            if not inst_key:
                continue
            if self.taker_exit_eval_interval_sec > 0:
                last_eval_ts = float(self.taker_exit_last_eval_ts_by_inst.get(inst_key, 0.0))
                if now_ts - last_eval_ts < float(self.taker_exit_eval_interval_sec):
                    continue
                self.taker_exit_last_eval_ts_by_inst[inst_key] = now_ts
            reject_cooldown_until = float(self.taker_exit_reject_cooldown_until_by_inst.get(inst_key, 0.0))
            if now_ts < reject_cooldown_until:
                continue
            if in_reduce_only_tail and inst_key in self.taker_exit_tail_attempted_by_inst:
                continue
            state = self.live_inventory_cost.get(inst_key)
            if not state:
                continue
            qty = Decimal(str(state.get("qty", "0")))
            if qty <= 0:
                continue
            if inst_key in self.pending_taker_exit_by_inst:
                continue
            last_ts = float(self.last_taker_exit_ts_by_inst.get(inst_key, 0.0))
            if now_ts - last_ts < self.taker_exit_cooldown_sec:
                continue
            quote = self._get_quote_for_instrument(inst_id)
            if quote is None:
                continue
            best_bid, best_ask = quote
            if best_bid <= 0:
                continue
            spread = max(Decimal("0"), best_ask - best_bid)
            mid = (best_bid + best_ask) / Decimal("2") if (best_bid + best_ask) > 0 else Decimal("0")
            spread_pct = (spread / mid) if mid > 0 else Decimal("0")
            fair = None
            if mid > 0 and hasattr(self, "_compute_fair_probability"):
                try:
                    fair = await self._compute_fair_probability(mid, instrument_id=inst_id)
                except Exception:
                    fair = None
            fair_edge_ps = (
                max(Decimal("0"), fair - best_bid)
                if fair is not None and fair > 0
                else None
            )
            spot_minus_strike_bps = None
            if hasattr(self, "_spot_minus_strike_bps"):
                try:
                    spot_minus_strike_bps = self._spot_minus_strike_bps()
                except Exception:
                    spot_minus_strike_bps = None

            token_id = self._extract_token_id_from_instrument(inst_key)
            dynamic_fee_rate = await self._get_dynamic_fee_rate(token_id=token_id)
            fee_rate = dynamic_fee_rate if (dynamic_fee_rate is not None and dynamic_fee_rate > 0) else self._infer_market_fee_rate_default()
            if fee_rate is None or fee_rate < 0:
                fee_rate = Decimal("0")

            avg_entry = Decimal(str(state.get("avg_entry_price", "0")))
            high_cost_cooldown_until = float(self.high_cost_exit_cooldown_until_by_inst.get(inst_key, 0.0))
            emergency_window = self._is_emergency_exit_window(time_left_sec)
            # After a high-cost BUY fill, avoid active exits below cost during cooldown.
            if (
                self.maker_high_cost_exit_cooldown_enabled
                and self.maker_high_cost_exit_cooldown_sec > 0
                and now_ts < high_cost_cooldown_until
                and avg_entry > 0
                and best_bid < avg_entry
                and not emergency_window
            ):
                self._log_taker_exit_skip_throttled(
                    inst_key=inst_key,
                    reason_tag="high_cost_cooldown",
                    message=(
                        "Skip taker exit: high-cost cooldown active "
                        f"(best_bid={float(best_bid):.4f} < avg_entry={float(avg_entry):.4f}, "
                        f"cooldown_left={high_cost_cooldown_until - now_ts:.1f}s)"
                    ),
                    now_ts=now_ts,
                )
                continue

            entry_fee_remaining = Decimal(str(state.get("entry_fee_remaining", "0")))
            opened_ts = float(state.get("opened_ts", 0.0))
            hold_sec = max(0.0, now_ts - opened_ts) if opened_ts > 0 else 0.0
            slip = max(Decimal("0"), self.taker_exit_slippage_buffer_pct)
            snapshot = MarketSnapshot(
                instrument_id=inst_key,
                phase=self.market_phase,
                time_left_sec=time_left_sec,
                best_bid=best_bid,
                best_ask=best_ask,
                fee_rate=fee_rate,
                spread=spread,
                spread_pct=spread_pct,
                slippage_buffer_pct=slip,
                exit_stage=exit_stage,
                in_reduce_only_tail=in_reduce_only_tail,
                stop_loss_disabled_in_tail=stop_loss_disabled_in_tail,
                fair=fair,
                fair_edge_ps=fair_edge_ps,
                spot_minus_strike_bps=spot_minus_strike_bps,
            )
            position = PositionState(
                instrument_id=inst_key,
                qty=qty,
                sellable_qty=self._get_effective_sellable_qty(instrument_id=inst_id),
                avg_entry_price=avg_entry,
                entry_fee_remaining=entry_fee_remaining,
                hold_sec=hold_sec,
                stop_loss_confirm_hits=int(self.taker_exit_stop_loss_hits_by_inst.get(inst_key, 0)),
                held_side=self._side_for_instrument_id(inst_id).value,
                peak_bid=getattr(self, "maker_profit_run_peak_bid_by_inst", {}).get(inst_key),
                peak_fair=getattr(self, "maker_profit_run_peak_fair_by_inst", {}).get(inst_key),
            )
            signal_decision = SignalDecision(
                active_side=self.active_side.value,
                score=self.side_decision_score,
                locked=self.active_side_locked,
                reason=self.side_decision_reason,
                matches_position=(self._instrument_for_side(self.active_side) == inst_id),
            )
            stop_loss_pending_active = bool(
                self._stop_loss_execution_priority_by_inst.get(inst_key, False)
                or int(self.taker_exit_stop_loss_hits_by_inst.get(inst_key, 0)) > 0
            )
            confirmed_locked_side_invalidated = bool(
                getattr(self, "_side_invalidation_confirmed_by_slug", {}).get(
                    str(self.current_market_slug or ""),
                    False,
                )
            )
            exit_decision = self.exit_policy_engine.evaluate(
                snapshot,
                position,
                signal_decision,
                stop_loss_pending_active=stop_loss_pending_active,
                locked_side_invalidated=confirmed_locked_side_invalidated,
                confirmed_adverse_exit_active=confirmed_locked_side_invalidated,
            )
            net_if_exit = exit_decision.net_if_exit
            force_offside_near_close = (
                (
                    confirmed_locked_side_invalidated
                    or not signal_decision.matches_position
                )
                and time_left_sec is not None
                and self.taker_exit_max_hold_near_close_sec > 0
                and time_left_sec <= float(self.taker_exit_max_hold_near_close_sec)
            )
            held_side = self._side_for_instrument_id(inst_id).value if hasattr(self, "_side_for_instrument_id") else "NONE"

            if exit_decision.decision_type in (ExitDecisionType.HOLD_TO_REDEEM, ExitDecisionType.HOLD_IN_BAND):
                if hasattr(self, "position_manager"):
                    self.position_manager.reset_stop_loss_regime(inst_key)
                if not stop_loss_pending_active:
                    self.taker_exit_stop_loss_hits_by_inst.pop(inst_key, None)
                    self._stop_loss_execution_priority_by_inst.pop(inst_key, None)
                hold_reason_tag = "hold_band" if exit_decision.decision_type == ExitDecisionType.HOLD_IN_BAND else "hold_to_redeem"
                self._record_exit_policy_decision_throttled(
                    inst_key=inst_key,
                    reason_tag=hold_reason_tag,
                    now_ts=now_ts,
                    payload={
                        "slug": self.current_market_slug or "",
                        "instrument_id": inst_key,
                        "decision_type": exit_decision.decision_type.value,
                        "reason": exit_decision.reason,
                        "band": exit_decision.metadata.get("band", "neutral"),
                        "signal_score": exit_decision.metadata.get("signal_score", str(self.side_decision_score)),
                        "signal_locked": exit_decision.metadata.get("signal_locked", "0"),
                        "signal_matches_position": exit_decision.metadata.get("signal_matches_position", "0"),
                        "avg_entry": float(avg_entry),
                        "qty": float(qty),
                        "sellable_qty": float(position.sellable_qty),
                        "time_left_sec": time_left_sec,
                        "exit_stage": exit_stage.value,
                        "best_bid": float(best_bid),
                        "best_ask": float(best_ask),
                        "gross_if_exit": float(exit_decision.gross_if_exit),
                        "net_if_exit": float(exit_decision.net_if_exit),
                        "exit_fee_est": float(exit_decision.exit_fee_est),
                        "confirm_hits": int(exit_decision.confirm_hits),
                        "required_confirmations": int(exit_decision.metadata.get("required_confirmations", self.taker_exit_stop_loss_confirmations)),
                    },
                )
                if not getattr(self, "_logged_hold_redeem_te", False) or now_ts - getattr(self, "_last_hold_redeem_te_log", 0) > 60:
                    logger.info(
                        "Hold band: skipping taker stop-loss. "
                        f"band={exit_decision.metadata.get('band', 'neutral')} "
                        f"best_bid={float(best_bid):.4f} avg_entry={float(avg_entry):.4f} "
                        f"score={exit_decision.metadata.get('signal_score', str(self.side_decision_score))} "
                        f"stage={exit_stage.value}"
                    )
                    self._logged_hold_redeem_te = True
                    self._last_hold_redeem_te_log = now_ts
                continue

            if exit_decision.decision_type == ExitDecisionType.DE_RISK:
                self._record_exit_policy_decision_throttled(
                    inst_key=inst_key,
                    reason_tag="de_risk",
                    now_ts=now_ts,
                    payload={
                        "slug": self.current_market_slug or "",
                        "instrument_id": inst_key,
                        "decision_type": exit_decision.decision_type.value,
                        "reason": exit_decision.reason,
                        "band": exit_decision.metadata.get("band", "neutral"),
                        "signal_score": exit_decision.metadata.get("signal_score", str(self.side_decision_score)),
                        "avg_entry": float(avg_entry),
                        "qty": float(qty),
                        "sellable_qty": float(position.sellable_qty),
                        "time_left_sec": time_left_sec,
                        "exit_stage": exit_stage.value,
                        "best_bid": float(best_bid),
                        "best_ask": float(best_ask),
                        "fair": float(fair) if fair is not None else None,
                        "fair_edge_ps": float(fair_edge_ps) if fair_edge_ps is not None else None,
                        "spot_minus_strike_bps": float(spot_minus_strike_bps) if spot_minus_strike_bps is not None else None,
                        "gross_if_exit": float(exit_decision.gross_if_exit),
                        "net_if_exit": float(exit_decision.net_if_exit),
                        "metadata": exit_decision.metadata,
                    },
                )
                continue

            if exit_decision.decision_type in (
                ExitDecisionType.STOP_LOSS_PENDING_CONFIRMATION,
                ExitDecisionType.TAKER_STOP_LOSS,
            ) and hasattr(self, "position_manager"):
                # The unconditional circuit breaker must bypass the position_manager
                # gate entirely. By design, it fires when the signal is healthy
                # (locked + matching + thesis good) but the loss exceeds the
                # absolute max. The position_manager would return "hold" in this
                # exact scenario, silently blocking the breaker.
                _is_absolute_breaker = (
                    exit_decision.reason == "absolute_max_loss_breaker"
                )
                if not _is_absolute_breaker:
                    _te_current_price = self._capture_market_open_spot() if hasattr(self, "_capture_market_open_spot") else None
                    _te_price_to_beat = self.market_strike_cache_by_slug.get(str(self.current_market_slug or "")) if hasattr(self, "market_strike_cache_by_slug") else None
                    regime = self.position_manager.assess_stop_loss_regime(
                        inst_key=inst_key,
                        now_ts=now_ts,
                        qty=qty,
                        opened_ts=opened_ts,
                        held_side=held_side,
                        signal_active_side=signal_decision.active_side,
                        signal_score=signal_decision.score,
                        signal_matches_position=signal_decision.matches_position,
                        force_exit=force_offside_near_close,
                        current_price=_te_current_price,
                        price_to_beat=_te_price_to_beat,
                        best_bid=best_bid,
                        best_ask=best_ask,
                        fair=fair,
                        time_left_sec=time_left_sec,
                        avg_entry=avg_entry,
                        peak_bid=position.peak_bid,
                        peak_fair=position.peak_fair,
                    )
                    if regime.status != "armed":
                        self.taker_exit_stop_loss_hits_by_inst.pop(inst_key, None)
                        self._stop_loss_execution_priority_by_inst.pop(inst_key, None)
                        self._log_taker_exit_skip_throttled(
                            inst_key=inst_key,
                            reason_tag=f"position_manager_{regime.status}",
                            message=(
                                "Skip taker exit: position manager gate "
                                f"({regime.reason}) best_bid={float(best_bid):.4f} "
                                f"avg_entry={float(avg_entry):.4f} est_net={float(net_if_exit):+.4f}"
                            ),
                            now_ts=now_ts,
                        )
                        continue

            if exit_decision.decision_type == ExitDecisionType.STOP_LOSS_PENDING_CONFIRMATION:
                self.taker_exit_stop_loss_hits_by_inst[inst_key] = exit_decision.confirm_hits
                self._stop_loss_execution_priority_by_inst[inst_key] = True
                self._record_exit_policy_decision_throttled(
                    inst_key=inst_key,
                    reason_tag="stop_loss_confirming",
                    now_ts=now_ts,
                    payload={
                        "slug": self.current_market_slug or "",
                        "instrument_id": inst_key,
                        "decision_type": exit_decision.decision_type.value,
                        "reason": exit_decision.reason,
                        "band": exit_decision.metadata.get("band", "neutral"),
                        "signal_score": exit_decision.metadata.get("signal_score", str(self.side_decision_score)),
                        "signal_locked": exit_decision.metadata.get("signal_locked", "0"),
                        "signal_matches_position": exit_decision.metadata.get("signal_matches_position", "0"),
                        "avg_entry": float(avg_entry),
                        "qty": float(qty),
                        "sellable_qty": float(position.sellable_qty),
                        "time_left_sec": time_left_sec,
                        "exit_stage": exit_stage.value,
                        "best_bid": float(best_bid),
                        "best_ask": float(best_ask),
                        "gross_if_exit": float(exit_decision.gross_if_exit),
                        "net_if_exit": float(exit_decision.net_if_exit),
                        "exit_fee_est": float(exit_decision.exit_fee_est),
                        "confirm_hits": int(exit_decision.confirm_hits),
                        "required_confirmations": int(exit_decision.metadata.get("required_confirmations", self.taker_exit_stop_loss_confirmations)),
                        "stop_loss_threshold": exit_decision.metadata.get("stop_loss_threshold", str(self.taker_exit_stop_loss_usdc)),
                    },
                )
                self._log_taker_exit_skip_throttled(
                    inst_key=inst_key,
                    reason_tag="stop_loss_confirming",
                    message=(
                        "Skip taker exit: stop-loss pending confirmation "
                        f"({exit_decision.confirm_hits}/{exit_decision.metadata.get('required_confirmations', self.taker_exit_stop_loss_confirmations)}) "
                        f"band={exit_decision.metadata.get('band', 'neutral')} "
                        f"best_bid={float(best_bid):.4f} avg_entry={float(avg_entry):.4f} "
                        f"est_net={float(net_if_exit):+.4f}"
                    ),
                    now_ts=now_ts,
                )
                continue

            if not force_offside_near_close and exit_decision.decision_type != ExitDecisionType.TAKER_STOP_LOSS:
                if hasattr(self, "position_manager"):
                    self.position_manager.reset_stop_loss_regime(inst_key)
                self.taker_exit_stop_loss_hits_by_inst.pop(inst_key, None)
                self._stop_loss_execution_priority_by_inst.pop(inst_key, None)
                continue
            self.taker_exit_stop_loss_hits_by_inst[inst_key] = exit_decision.confirm_hits
            self._stop_loss_execution_priority_by_inst[inst_key] = True
            trigger = "offside_near_close" if force_offside_near_close else "stop_loss"

            # Avoid paying taker costs into a wide spread unless it's an emergency path.
            if (
                trigger == "stop_loss"
                and spread_pct > self.taker_exit_stop_loss_max_spread_pct
                and not in_reduce_only_tail
            ):
                self._log_taker_exit_skip_throttled(
                    inst_key=inst_key,
                    reason_tag="spread_guard_stop_loss",
                    message=(
                        "Skip taker exit (stop_loss spread guard): "
                        f"spread_pct={float(spread_pct):.4f} > max={float(self.taker_exit_stop_loss_max_spread_pct):.4f}"
                    ),
                    now_ts=now_ts,
                )
                continue
            # If a fresh SELL maker quote is already working, let it try first before forced taker exit.
            sell_key = self._order_key_for("sell", inst_id)
            active_sell = self.active_maker_orders.get(sell_key)
            if active_sell:
                created_ts = float(active_sell.get("created_ts", 0.0))
                pending_cancel = bool(active_sell.get("pending_cancel"))
                if (
                    trigger == "stop_loss"
                    and not pending_cancel
                    and created_ts > 0
                    and (now_ts - created_ts) < float(self.taker_exit_wait_for_sell_quote_sec)
                ):
                    self._log_taker_exit_skip_throttled(
                        inst_key=inst_key,
                        reason_tag="wait_for_sell_quote",
                        message=(
                            "Skip taker exit: waiting for active SELL maker quote "
                            f"(age={now_ts - created_ts:.1f}s < {float(self.taker_exit_wait_for_sell_quote_sec):.1f}s)"
                        ),
                        now_ts=now_ts,
                    )
                    continue

            sellable_qty = position.sellable_qty
            qty_to_exit = min(qty, sellable_qty)
            if qty_to_exit + Decimal("0.000001") < self.maker_exchange_min_shares:
                continue
            ok = self._submit_taker_exit_order(
                instrument_id=inst_id,
                quantity=qty_to_exit,
                reason=trigger,
                est_net_if_exit=net_if_exit,
                best_bid=best_bid,
                fee_rate=fee_rate,
                decision_payload={
                    "slug": self.current_market_slug or "",
                    "decision_type": exit_decision.decision_type.value,
                    "decision_reason": exit_decision.reason,
                    "forced_offside_near_close": "1" if force_offside_near_close else "0",
                    "band": exit_decision.metadata.get("band", "neutral"),
                    "signal_score": exit_decision.metadata.get("signal_score", str(self.side_decision_score)),
                    "signal_locked": exit_decision.metadata.get("signal_locked", "0"),
                    "signal_matches_position": exit_decision.metadata.get("signal_matches_position", "0"),
                    "avg_entry": float(avg_entry),
                    "time_left_sec": time_left_sec,
                    "exit_stage": exit_stage.value,
                    "gross_if_exit": float(exit_decision.gross_if_exit),
                    "exit_fee_est": float(exit_decision.exit_fee_est),
                    "exit_px_effective": float(exit_decision.exit_px_effective),
                    "confirm_hits": int(exit_decision.confirm_hits),
                    "required_confirmations": int(exit_decision.metadata.get("required_confirmations", self.taker_exit_stop_loss_confirmations)),
                    "stop_loss_threshold": exit_decision.metadata.get("stop_loss_threshold", str(self.taker_exit_stop_loss_usdc)),
                    "sellable_qty": float(sellable_qty),
                },
            )
            if ok:
                self.taker_exit_stop_loss_hits_by_inst.pop(inst_key, None)
                self.last_taker_exit_ts_by_inst[inst_key] = now_ts
                if in_reduce_only_tail:
                    self.taker_exit_tail_attempted_by_inst[inst_key] = now_ts

    def _submit_taker_exit_order(
        self,
        instrument_id: Any,
        quantity: Decimal,
        reason: str,
        est_net_if_exit: Decimal,
        best_bid: Decimal,
        fee_rate: Decimal,
        decision_payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        inst = self._normalize_instrument_id(instrument_id)
        if inst is None:
            return False
        instrument = self.cache.instrument(inst)
        if instrument is None:
            return False
        precision = int(getattr(instrument, "size_precision", 6))
        min_lot = Decimal(str(10 ** (-precision)))
        qty_dec = max(min_lot, quantity).quantize(min_lot, rounding=ROUND_FLOOR)
        if qty_dec + Decimal("0.000001") < self.maker_exchange_min_shares:
            return False

        self._cancel_maker_order_side("buy", reason="taker_exit", instrument_id=inst)
        self._cancel_maker_order_side("sell", reason="taker_exit", instrument_id=inst)

        qty = Quantity(float(qty_dec), precision=precision)
        coid = ClientOrderId(f"BTC-15M-TAKER-EXIT-{int(time.time() * 1000)}")
        order = self.order_factory.market(
            instrument_id=inst,
            order_side=OrderSide.SELL,
            quantity=qty,
            client_order_id=coid,
            quote_quantity=False,
            time_in_force=TimeInForce.IOC,
        )
        self.submit_order(order)
        inst_key = self._instrument_key(inst)
        self.pending_taker_exit_by_inst[inst_key] = str(coid)
        self.taker_exit_reason_by_client_order_id[str(coid)] = reason
        if getattr(self, "terminal_dashboard", None):
            token_side = getattr(self._side_for_instrument_id(inst), "value", "NONE")
            self.terminal_dashboard.record_order_submitted(
                side="sell",
                token_side=token_side,
                qty=float(qty_dec),
                price=float(best_bid),
                client_order_id=str(coid),
                is_taker=True,
            )
        logger.warning(
            "TAKER EXIT submit: "
            f"reason={reason} inst={inst_key} qty={float(qty_dec):.6f} "
            f"best_bid={float(best_bid):.4f} est_net={float(est_net_if_exit):+.4f} "
            f"fee_rate={float(fee_rate):.4f}"
        )
        self._db_order_event(
            event_type="ORDER_TAKER_EXIT_SUBMIT",
            client_order_id=str(coid),
            side="SELL",
            price=float(best_bid),
            qty=float(qty_dec),
            status="SUBMITTED",
            reason=reason,
            expected_net_usdc=float(est_net_if_exit),
            payload={
                "reason": reason,
                "est_net_if_exit": float(est_net_if_exit),
                "instrument_id": inst_key,
                "fee_rate": float(fee_rate),
                "fee_rate_decimal": float(fee_rate),
                "best_bid": float(best_bid),
                **(decision_payload or {}),
            },
        )
        return True

    def _clear_pending_taker_exit_for_order(self, client_order_id: str) -> None:
        target = str(client_order_id or "")
        if not target:
            return
        self.taker_exit_reason_by_client_order_id.pop(target, None)
        for inst_key, coid in list(self.pending_taker_exit_by_inst.items()):
            if coid == target:
                self.pending_taker_exit_by_inst.pop(inst_key, None)
                break

    def _log_taker_exit_skip_throttled(self, inst_key: str, reason_tag: str, message: str, now_ts: float) -> None:
        key = f"{inst_key}:{reason_tag}"
        last_ts = float(self._taker_exit_skip_log_ts_by_key.get(key, 0.0))
        if now_ts - last_ts < float(self.taker_exit_skip_log_interval_sec):
            return
        self._taker_exit_skip_log_ts_by_key[key] = now_ts
        logger.info(message)

    def _record_exit_policy_decision_throttled(
        self,
        inst_key: str,
        reason_tag: str,
        now_ts: float,
        payload: Dict[str, Any],
    ) -> None:
        key = f"{inst_key}:{reason_tag}:db"
        last_ts = float(self._taker_exit_skip_log_ts_by_key.get(key, 0.0))
        if now_ts - last_ts < float(self.taker_exit_skip_log_interval_sec):
            return
        self._taker_exit_skip_log_ts_by_key[key] = now_ts
        self._db_strategy_event("EXIT_POLICY_DECISION", payload)

    # ------------------------------------------------------------------
    # Maker-Style Urgent Exit
    # ------------------------------------------------------------------
    # When thesis weakens (signal flips or score drops), place a SELL limit
    # order at best_bid + 1 tick (ask side, true maker) to exit wrong-side
    # inventory. Price selection (above best_bid) ensures maker execution.
    #
    # NOT a taker order — sits on the ask side of the book waiting for
    # a buyer to cross. Zero taker fee by design.
    # Crossing guard: if price would match bids, order is skipped.
    #
    # TTL: urgent exits use MAKER_URGENT_EXIT_TTL_SEC (default 15s),
    # managed by the existing active_maker_orders TTL cleanup loop.
    # ------------------------------------------------------------------

    async def _maybe_maker_urgent_exit(self, now_ts: float) -> None:
        """Evaluate and execute maker-style urgent exit for wrong-side positions."""
        if not getattr(self, "maker_urgent_exit_enabled", False):
            return
        # NOTE: intentionally NOT gated on taker_exit_enabled —
        # this is a maker mechanism, independent of taker exit config.

        # Cooldown check
        last_urgent_ts = getattr(self, "_maker_urgent_exit_last_ts", 0.0)
        cooldown = getattr(self, "maker_urgent_exit_cooldown_sec", 5)
        if now_ts - last_urgent_ts < cooldown:
            return

        end_ts = getattr(self, "current_market_end_timestamp", None)
        time_left_sec = (end_ts - now_ts) if end_ts is not None else None

        # Don't do urgent exit in the last 45s (let taker exit handle emergency)
        if time_left_sec is not None and time_left_sec <= 45:
            return

        target_instruments = list(self._maker_quote_instruments())
        # Also check off-side inventory instruments
        seen = {self._instrument_key(inst_id) for inst_id in target_instruments}
        for inv_inst_key, inv_state in list(self.live_inventory_cost.items()):
            try:
                inv_qty = Decimal(str(inv_state.get("qty", "0")))
            except Exception:
                inv_qty = Decimal("0")
            if inv_qty <= 0:
                continue
            if inv_inst_key in seen:
                continue
            inv_inst = self._normalize_instrument_id(inv_inst_key)
            if inv_inst is None:
                continue
            target_instruments.append(inv_inst)
            seen.add(inv_inst_key)

        for inst_id in target_instruments:
            inst_key = self._instrument_key(inst_id)
            if not inst_key:
                continue

            state = self.live_inventory_cost.get(inst_key)
            if not state:
                continue
            qty = Decimal(str(state.get("qty", "0")))
            if qty <= 0:
                continue

            # Only trigger urgent maker exit after a confirmed side flip.
            # A weak score alone is too noisy and can shake us out of
            # positions that later settle in our favor.
            matches_position = (self._instrument_for_side(self.active_side) == inst_id)
            confirmed_offside = (not matches_position) and bool(self.active_side_locked)

            # Under the continuous SignalEngine, mid-price reversal can be a
            # useful warning signal, but it must not by itself override a still-
            # matching locked thesis. Requiring an actual side mismatch avoids
            # urgent exits firing while score/side still support the position.

            # Require consecutive confirmed-offside cycles to filter out
            # transient flips before escalating to an urgent exit.
            if not hasattr(self, "_urgent_exit_confirm_hits"):
                self._urgent_exit_confirm_hits: dict[str, int] = {}
            if confirmed_offside:
                self._urgent_exit_confirm_hits[inst_key] = self._urgent_exit_confirm_hits.get(inst_key, 0) + 1
            else:
                self._urgent_exit_confirm_hits[inst_key] = 0
                continue

            min_confirms = getattr(self, "maker_urgent_exit_min_confirmations", 3)
            avg_entry = Decimal(str(state.get("avg_entry_price", "0")))
            peak_bid = getattr(self, "maker_profit_run_peak_bid_by_inst", {}).get(inst_key)
            peak_fair = getattr(self, "maker_profit_run_peak_fair_by_inst", {}).get(inst_key)
            peak_profit_ps = Decimal("0")
            for peak_ref in (peak_bid, peak_fair):
                if peak_ref is None:
                    continue
                try:
                    peak_dec = Decimal(str(peak_ref))
                except Exception:
                    continue
                if peak_dec > avg_entry:
                    peak_profit_ps = max(peak_profit_ps, peak_dec - avg_entry)
            if self._urgent_exit_confirm_hits[inst_key] < min_confirms:
                continue

            if hasattr(self, "position_manager"):
                held_side = self._side_for_instrument_id(inst_id).value if hasattr(self, "_side_for_instrument_id") else "NONE"
                opened_ts = float(state.get("opened_ts", 0.0))
                quote = self._get_quote_for_instrument(inst_id)
                if quote is None:
                    continue
                best_bid, best_ask = quote
                _ue_current_price = self._capture_market_open_spot() if hasattr(self, "_capture_market_open_spot") else None
                _ue_price_to_beat = self.market_strike_cache_by_slug.get(str(self.current_market_slug or "")) if hasattr(self, "market_strike_cache_by_slug") else None
                regime = self.position_manager.assess_stop_loss_regime(
                    inst_key=inst_key,
                    now_ts=now_ts,
                    qty=qty,
                    opened_ts=opened_ts,
                    held_side=held_side,
                    signal_active_side=self.active_side.value,
                    signal_score=self.side_decision_score,
                    signal_matches_position=matches_position,
                    force_exit=False,
                    current_price=_ue_current_price,
                    price_to_beat=_ue_price_to_beat,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    fair=None,
                    time_left_sec=time_left_sec,
                    avg_entry=avg_entry,
                    peak_bid=peak_bid,
                    peak_fair=peak_fair,
                )
                if regime.status != "armed":
                    continue

            # Check unrealized loss
            quote = self._get_quote_for_instrument(inst_id)
            if quote is None:
                continue
            best_bid, best_ask = quote
            if best_bid <= 0:
                continue

            unrealized_loss_ps = avg_entry - best_bid
            min_loss = getattr(self, "maker_urgent_exit_min_loss_usdc", Decimal("0.10"))
            unrealized_loss_total = unrealized_loss_ps * qty
            if unrealized_loss_total < min_loss:
                continue

            # Skip if there's already a pending taker exit
            if inst_key in self.pending_taker_exit_by_inst:
                continue

            # Check if an active sell order already covers this exit
            sell_key = self._order_key_for("sell", inst_id)
            active_sell = self.active_maker_orders.get(sell_key)
            if active_sell:
                existing_price = Decimal(str(active_sell.get("price", "0")))
                existing_created_ts = float(active_sell.get("created_ts", 0.0))
                existing_age_sec = max(0.0, now_ts - existing_created_ts) if existing_created_ts > 0 else 0.0
                if active_sell.get("is_urgent_exit"):
                    urgent_ttl = float(active_sell.get("urgent_exit_ttl", getattr(self, "maker_urgent_exit_ttl_sec", 15)))
                    replace_grace_sec = max(5.0, urgent_ttl * 0.5)
                    if existing_age_sec < replace_grace_sec:
                        continue
                if existing_price <= best_ask:
                    continue  # Already quoting at ask or better
                # Stale sell is too far from market — cancel it and wait for
                # exchange ack before placing new order (next cycle).
                logger.info(
                    f"Urgent exit: cancelling stale SELL at {float(existing_price):.4f} "
                    f"(best_ask={float(best_ask):.4f}), will place new order next cycle"
                )
                self._cancel_maker_order_side("sell", reason="urgent_exit_replace", instrument_id=inst_id)
                # Also cancel BUY now so capital is freed by next cycle
                self._cancel_maker_order_side("buy", reason="urgent_exit", instrument_id=inst_id)
                continue  # Wait for cancel reconcile before sending new order

            # Cancel active BUY to free up capital for the urgent SELL
            self._cancel_maker_order_side("buy", reason="urgent_exit", instrument_id=inst_id)

            # Compute exit qty
            sellable_qty = self._get_effective_sellable_qty(instrument_id=inst_id)
            qty_to_exit = min(qty, sellable_qty)
            if qty_to_exit + Decimal("0.000001") < self.maker_exchange_min_shares:
                continue

            instrument = self.cache.instrument(self._normalize_instrument_id(inst_id))
            if instrument is None:
                continue
            precision = int(getattr(instrument, "size_precision", 6))
            min_lot = Decimal(str(10 ** (-precision)))
            qty_dec = max(min_lot, qty_to_exit).quantize(min_lot, rounding=ROUND_FLOOR)
            if qty_dec + Decimal("0.000001") < self.maker_exchange_min_shares:
                continue

            from nautilus_trader.model.objects import Price
            price_precision = int(getattr(instrument, "price_precision", 2))
            tick = Decimal(str(10 ** (-price_precision)))

            # True maker price: best_bid + 1 tick
            # Sits on the ASK side of the book. A buyer must cross to fill this.
            urgent_sell_price = best_bid + tick
            # If best_bid + tick >= best_ask, join existing ask
            if best_ask > 0 and urgent_sell_price >= best_ask:
                urgent_sell_price = best_ask

            # FIX #2: Crossing guard — if our price would still match bids,
            # skip this cycle. We do NOT fallback to taker.
            # (Nautilus does not support post_only; we rely on price selection.)
            if urgent_sell_price <= best_bid:
                logger.debug(
                    f"Urgent exit: skipping, sell_price={float(urgent_sell_price):.4f} "
                    f"would cross best_bid={float(best_bid):.4f}"
                )
                continue

            # FIX #3: Use maker_urgent_exit_ttl_sec (not maker_order_ttl_sec)
            urgent_ttl = getattr(self, "maker_urgent_exit_ttl_sec", 15)

            coid = ClientOrderId(f"BTC-15M-URGENT-EXIT-{int(time.time() * 1000)}")
            order_kwargs = {
                "instrument_id": self._normalize_instrument_id(inst_id),
                "order_side": OrderSide.SELL,
                "quantity": Quantity(float(qty_dec), precision=precision),
                "price": Price(float(urgent_sell_price), precision=price_precision),
                "client_order_id": coid,
                "time_in_force": TimeInForce.GTC,
            }

            try:
                order = self.order_factory.limit(**order_kwargs)
            except Exception as e:
                logger.warning(f"Urgent exit limit order creation failed: {e}")
                continue

            self.submit_order(order)
            self._maker_urgent_exit_last_ts = now_ts

            # Track via active_maker_orders for lifecycle management.
            # `urgent_exit_ttl` is stored so the TTL cleanup can use a
            # shorter expiry for urgent exits vs normal maker orders.
            self.active_maker_orders[sell_key] = {
                "order": order,
                "econ": None,
                "directional_snapshot": {},
                "price": urgent_sell_price,
                "side": "sell",
                "instrument_id": inst_id,
                "token_id": None,
                "quantity": qty_dec,
                "created_ts": now_ts,
                "target_version": 0,
                "is_urgent_exit": True,
                "urgent_exit_ttl": urgent_ttl,
            }

            logger.warning(
                "MAKER URGENT EXIT submit: "
                f"inst={inst_key} qty={float(qty_dec):.6f} "
                f"price={float(urgent_sell_price):.4f} (bid={float(best_bid):.4f}+tick) "
                f"avg_entry={float(avg_entry):.4f} "
                f"unrealized_loss={float(unrealized_loss_total):+.4f} "
                f"score={float(self.side_decision_score):.2f} "
                f"matches_position={matches_position} "
                f"active_side_locked={self.active_side_locked} "
                f"confirms={self._urgent_exit_confirm_hits[inst_key]} "
                f"ttl={urgent_ttl}s"
            )
            self._db_order_event(
                event_type="ORDER_MAKER_URGENT_EXIT_SUBMIT",
                client_order_id=str(coid),
                side="SELL",
                price=float(urgent_sell_price),
                qty=float(qty_dec),
                status="SUBMITTED",
                reason="urgent_maker_exit",
                expected_net_usdc=float(-unrealized_loss_total),
                payload={
                    "reason": "urgent_maker_exit",
                    "instrument_id": inst_key,
                    "avg_entry": float(avg_entry),
                    "best_bid": float(best_bid),
                    "best_ask": float(best_ask),
                    "urgent_sell_price": float(urgent_sell_price),
                    "unrealized_loss_ps": float(unrealized_loss_ps),
                    "unrealized_loss_total": float(unrealized_loss_total),
                    "signal_score": float(self.side_decision_score),
                    "matches_position": "1" if matches_position else "0",
                    "active_side_locked": "1" if self.active_side_locked else "0",
                    "time_left_sec": time_left_sec,
                    "confirmations": self._urgent_exit_confirm_hits[inst_key],
                    "required_confirmations": min_confirms,
                    "peak_profit_ps": float(peak_profit_ps),
                    "ttl_sec": urgent_ttl,
                },
            )
            break  # Only one urgent exit per cycle
