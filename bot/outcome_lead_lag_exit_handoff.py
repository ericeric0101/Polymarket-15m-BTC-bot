"""Strategy-owned live Outcome-to-Chainlink fast-follow entry handoff."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from zoneinfo import ZoneInfo

from loguru import logger
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.objects import Price, Quantity


TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class FastFollowLiveConfig:
    signal_ttl_ms: int = 6_000
    max_entry_price: Decimal = Decimal("0.90")
    max_slippage_ticks: int = 1
    max_entries_per_night: int = 6
    max_loss_usdc_per_night: Decimal = Decimal("5")
    # Outcome lead/lag has no approved authority to liquidate an existing
    # position by default.  It must be explicitly opted in after OOS review.
    reversal_exit_enabled: bool = False


def _night_key(now_ts: float) -> str | None:
    local = datetime.fromtimestamp(now_ts, tz=TAIPEI)
    if local.hour >= 19:
        return local.date().isoformat() if local.weekday() < 5 else None
    if local.hour < 7:
        prior = local - timedelta(days=1)
        return prior.date().isoformat() if prior.weekday() < 5 else None
    return None


def _instrument_tick(instrument) -> Decimal:
    raw = getattr(instrument, "price_increment", None)
    if raw is not None:
        try:
            return Decimal(str(raw.as_decimal() if hasattr(raw, "as_decimal") else raw))
        except Exception:
            pass
    return Decimal(str(10 ** (-int(getattr(instrument, "price_precision", 2)))))


class OutcomeFastFollowLive:
    """Queue a confirmed signal off-thread; submit only on a quote callback."""

    def __init__(self, strategy, config: FastFollowLiveConfig) -> None:
        self.strategy = strategy
        self.config = config
        self._lock = threading.Lock()
        self._pending = None
        self._attempted_slugs: set[str] = set()
        self._pending_order_ids: dict[str, dict] = {}
        self._position_instruments: set[str] = set()
        self._reversal_price_floor_by_inst: dict[str, Decimal] = {}
        self._night_entries: dict[str, int] = {}
        self._night_realized_pnl: dict[str, Decimal] = {}
        self._loaded_nights: set[str] = set()

    def _ensure_night_loaded(self, night: str) -> None:
        if night in self._loaded_nights:
            return
        loader = getattr(getattr(self.strategy, "trade_db", None), "load_fast_follow_night_risk", None)
        recovered = loader(night) if loader is not None else {}
        self._night_entries[night] = max(
            self._night_entries.get(night, 0),
            int(recovered.get("attempted_entries") or 0),
        )
        self._night_realized_pnl[night] = min(
            self._night_realized_pnl.get(night, Decimal("0")),
            Decimal(str(recovered.get("realized_pnl_usdc") or "0")),
        )
        self._loaded_nights.add(night)

    def _persist_night(self, night: str) -> None:
        self.strategy._db_strategy_event("FAST_FOLLOW_RISK_STATE", {
            "night_key": night,
            "attempted_entries": self._night_entries.get(night, 0),
            "realized_pnl_usdc": float(self._night_realized_pnl.get(night, Decimal("0"))),
        })

    def record_candidate(self, candidate) -> None:
        if candidate.decision.state != "follower_confirmed":
            return
        with self._lock:
            self._pending = candidate
        self.strategy._db_strategy_event("FAST_FOLLOW_CONFIRMED", {
            "slug": candidate.slug,
            "market_id": candidate.market_id,
            "direction": candidate.decision.direction,
            "signal_created_epoch_ns": candidate.created_epoch_ns,
            "decision": candidate.decision.__dict__,
        })

    def order_metadata(self, client_order_id: str) -> dict | None:
        return self._pending_order_ids.get(client_order_id)

    def on_order_terminal(self, client_order_id: str) -> None:
        self._pending_order_ids.pop(client_order_id, None)

    def blocks_normal_buy(self, slug: str) -> bool:
        with self._lock:
            pending_slug = getattr(self._pending, "slug", None)
        return slug in self._attempted_slugs or pending_slug == slug

    def on_fill(self, *, client_order_id: str, side: str, instrument_id: str, realized_net_usdc=None) -> None:
        metadata = self._pending_order_ids.pop(client_order_id, None)
        if metadata is not None and side == "buy":
            self._position_instruments.add(instrument_id)
        if side == "sell":
            self._reversal_price_floor_by_inst.pop(self.strategy._instrument_key(instrument_id), None)
        if side == "sell" and instrument_id in self._position_instruments and realized_net_usdc is not None:
            key = _night_key(time.time())
            if key is not None:
                self._ensure_night_loaded(key)
                self._night_realized_pnl[key] = self._night_realized_pnl.get(key, Decimal("0")) + Decimal(str(realized_net_usdc))
                self._persist_night(key)
            state = getattr(self.strategy, "live_inventory_cost", {}).get(instrument_id, {})
            exchange_min = Decimal(str(getattr(self.strategy, "maker_exchange_min_shares", "5")))
            if Decimal(str(state.get("qty", "0"))) < exchange_min:
                self._position_instruments.discard(instrument_id)

    def _try_reversal_exit(
        self, *, candidate, instrument_id, inst_key: str, side: str,
        wanted_side: str, best_bid: Decimal, best_ask: Decimal,
    ) -> bool:
        if not self.config.reversal_exit_enabled:
            return False
        if side == wanted_side:
            return False
        state = getattr(self.strategy, "live_inventory_cost", {}).get(inst_key, {})
        held = Decimal(str(state.get("qty", "0")))
        exchange_min = Decimal(str(getattr(self.strategy, "maker_exchange_min_shares", "5")))
        if held < exchange_min or best_bid <= 0:
            return False
        # A fast-follow signal must never bypass the established hold/TP owner.
        # Standard exit logic remains responsible for independently configured
        # hard invalidation and stop-loss policy.
        if bool(getattr(self.strategy, "hold_to_redeem_enabled", False)) or bool(
            getattr(self.strategy, "tail_protect_tp_enabled", False)
        ):
            self.strategy._db_strategy_event("FAST_FOLLOW_REVERSAL_EXIT_BLOCKED", {
                "slug": candidate.slug, "reason": "existing_hold_or_tp_policy",
                "best_bid": float(best_bid),
            })
            return False
        mid = (best_bid + best_ask) / Decimal("2") if best_bid + best_ask > 0 else Decimal("0")
        spread_pct = (best_ask - best_bid) / mid if mid > 0 else Decimal("1")
        max_spread = Decimal(str(getattr(self.strategy, "taker_exit_stop_loss_max_spread_pct", "0.03")))
        if spread_pct > max_spread:
            return False
        for active in getattr(self.strategy, "active_maker_orders", {}).values():
            if str(active.get("side", "")).lower() != "sell":
                continue
            if self.strategy._instrument_key(active.get("instrument_id")) != inst_key:
                continue
            self.strategy._cancel_maker_order_side(
                "sell", reason="outcome_fast_follow_reversal", instrument_id=instrument_id,
            )
            return False
        sellable = Decimal(str(self.strategy._get_effective_sellable_qty(instrument_id=instrument_id)))
        quantity = min(held, sellable)
        if quantity < exchange_min:
            return False
        avg_entry = Decimal(str(state.get("avg_entry_price", "0")))
        fee_rate = Decimal(str(self.strategy._infer_market_fee_rate_default() or "0"))
        if avg_entry <= 0:
            # Unknown cost is not a conservatively profitable position.  This
            # guards missed fill acknowledgements and prevents a ghost balance
            # from being liquidated as fictional profit.
            self.strategy._db_strategy_event("FAST_FOLLOW_REVERSAL_EXIT_BLOCKED", {
                "slug": candidate.slug, "reason": "unknown_cost_basis",
                "best_bid": float(best_bid), "quantity": float(quantity),
            })
            return False
        conservative_fee = max(Decimal("0"), best_bid * quantity * fee_rate)
        est_net = (best_bid - avg_entry) * quantity - conservative_fee
        if est_net <= 0:
            self.strategy._db_strategy_event("FAST_FOLLOW_REVERSAL_EXIT_BLOCKED", {
                "slug": candidate.slug, "reason": "non_profitable_after_fee",
                "best_bid": float(best_bid), "avg_entry": float(avg_entry),
                "estimated_net_usdc": float(est_net),
            })
            return False
        prior_floor = self._reversal_price_floor_by_inst.get(inst_key)
        if prior_floor is not None and best_bid < prior_floor:
            self.strategy._db_strategy_event("FAST_FOLLOW_REVERSAL_EXIT_BLOCKED", {
                "slug": candidate.slug, "reason": "reversal_retry_below_prior_bid",
                "best_bid": float(best_bid), "prior_bid_floor": float(prior_floor),
            })
            return False
        submitted = self.strategy._submit_taker_exit_order(
            instrument_id=instrument_id,
            quantity=quantity,
            reason="outcome_fast_follow_reversal",
            est_net_if_exit=est_net,
            best_bid=best_bid,
            fee_rate=fee_rate,
            decision_payload={
                "slug": candidate.slug,
                "prior_side": side,
                "confirmed_side": wanted_side,
                "signal_created_epoch_ns": candidate.created_epoch_ns,
                "execution_penalty_bypassed": True,
            },
            execution_mode="limit_fok",
        )
        if submitted:
            self._reversal_price_floor_by_inst[inst_key] = best_bid
            with self._lock:
                if self._pending is candidate:
                    self._pending = None
            self.strategy._db_strategy_event("FAST_FOLLOW_REVERSAL_EXIT_SUBMITTED", {
                "slug": candidate.slug, "instrument_id": inst_key,
                "prior_side": side, "confirmed_side": wanted_side,
                "quantity": float(quantity), "best_bid": float(best_bid),
            })
        return submitted

    def on_quote(self, *, instrument_id, best_bid: Decimal, best_ask: Decimal, ask_size: Decimal | None, now_ts: float) -> bool:
        with self._lock:
            candidate = self._pending
        if candidate is None or candidate.slug != str(getattr(self.strategy, "current_market_slug", "") or ""):
            return False
        age_ms = (time.time_ns() - candidate.created_epoch_ns) / 1_000_000
        if age_ms < 0 or age_ms > self.config.signal_ttl_ms:
            with self._lock:
                if self._pending is candidate:
                    self._pending = None
            self.strategy._db_strategy_event("FAST_FOLLOW_EXPIRED", {
                "slug": candidate.slug, "direction": candidate.decision.direction, "signal_age_ms": age_ms,
            })
            return False
        slug = candidate.slug
        side = getattr(self.strategy._side_for_instrument_id(instrument_id), "value", "NONE")
        wanted_side = "UP" if candidate.decision.direction > 0 else "DOWN"
        inst_key = self.strategy._instrument_key(instrument_id)
        if self._try_reversal_exit(
            candidate=candidate, instrument_id=instrument_id, inst_key=inst_key,
            side=side, wanted_side=wanted_side, best_bid=best_bid, best_ask=best_ask,
        ):
            return True
        # Entry budgets never suppress a protective reversal exit. They apply
        # only after the current instrument has been checked for held exposure.
        if slug in self._attempted_slugs or int(getattr(self.strategy, "market_buy_count_total_by_slug", {}).get(slug, 0)) > 0:
            return False
        night = _night_key(now_ts)
        if night is None:
            return False
        self._ensure_night_loaded(night)
        if self._night_entries.get(night, 0) >= self.config.max_entries_per_night:
            return False
        if self._night_realized_pnl.get(night, Decimal("0")) <= -self.config.max_loss_usdc_per_night:
            return False
        if side != wanted_side or best_ask <= 0 or best_ask > self.config.max_entry_price:
            return False
        if ask_size is not None and ask_size <= 0:
            return False
        if bool(getattr(self.strategy, "_twap_reference_degraded", True)):
            return False
        strike_eligible = getattr(self.strategy, "_market_strike_is_entry_eligible", None)
        if strike_eligible is None or not strike_eligible(slug):
            return False
        end_ts = getattr(self.strategy, "current_market_end_timestamp", None)
        min_time_left = max(
            float(getattr(self.strategy, "maker_min_minutes_to_close", 0.0) or 0.0) * 60.0,
            float(getattr(self.strategy, "bi_side_min_time_left_sec", 0.0) or 0.0),
        )
        time_left = end_ts - now_ts if end_ts is not None else None
        if time_left is None or time_left < min_time_left:
            return False
        max_time_left = float(getattr(self.strategy, "first_entry_max_time_left_sec", 0.0) or 0.0)
        if max_time_left > 0 and time_left > max_time_left:
            return False
        active = getattr(getattr(self.strategy, "active_side", None), "value", "NONE")
        if bool(getattr(self.strategy, "active_side_locked", False)) and active not in {"NONE", wanted_side}:
            return False
        held = Decimal(str(getattr(self.strategy, "live_inventory_cost", {}).get(inst_key, {}).get("qty", "0")))
        if held > 0:
            return False

        instrument = self.strategy.cache.instrument(instrument_id)
        if instrument is None:
            return False
        for state in getattr(self.strategy, "active_maker_orders", {}).values():
            if str(state.get("side", "")).lower() != "buy":
                continue
            if self.strategy._instrument_key(state.get("instrument_id")) != inst_key:
                continue
            self.strategy._cancel_maker_order_side(
                "buy", reason="fast_follow_entry", instrument_id=instrument_id,
            )
            return False
        base_shares = Decimal(str(getattr(self.strategy, "maker_fixed_shares", "10")))
        high_threshold = Decimal(str(getattr(self.strategy, "maker_high_entry_price_size_adjust_threshold", "0.70")))
        multiplier = Decimal(str(getattr(self.strategy, "maker_high_entry_price_size_adjust_multiplier", "0.55")))
        quantity = base_shares * multiplier if best_ask > high_threshold else base_shares
        exchange_min = Decimal(str(getattr(self.strategy, "maker_exchange_min_shares", "5")))
        if quantity < exchange_min:
            return False
        precision = int(getattr(instrument, "size_precision", 6))
        quantum = Decimal(str(10 ** (-precision)))
        quantity = quantity.quantize(quantum, rounding=ROUND_FLOOR)
        tick = max(Decimal("0.001"), _instrument_tick(instrument))
        limit_price = self.strategy._align_price_to_tick(
            min(self.config.max_entry_price, best_ask + tick * self.config.max_slippage_ticks),
            "buy", instrument,
        )
        balance = getattr(self.strategy, "_cached_usdc_balance", None)
        if balance is None or Decimal(str(balance)) < limit_price * quantity:
            return False

        coid = ClientOrderId(f"BTC-15M-FAST-FOLLOW-BUY-{int(now_ts * 1000)}")
        order = self.strategy.order_factory.limit(
            instrument_id=instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity(float(quantity), precision=precision),
            price=Price(float(limit_price), precision=int(getattr(instrument, "price_precision", 2))),
            client_order_id=coid,
            quote_quantity=False,
            time_in_force=TimeInForce.FOK,
        )
        metadata = {
            "slug": slug, "direction": candidate.decision.direction,
            "wanted_side": wanted_side, "signal_age_ms": age_ms,
            "best_bid": float(best_bid), "best_ask": float(best_ask),
            "limit_price": float(limit_price), "quantity": float(quantity),
            "requested_tif": "FOK", "execution_penalty_bypassed": True,
        }
        self._attempted_slugs.add(slug)
        self._night_entries[night] = self._night_entries.get(night, 0) + 1
        self._persist_night(night)
        self._pending_order_ids[str(coid)] = metadata
        # Record the submission before handing it to the venue, exactly as the
        # maker path does.  If the venue fills but its fill callback is lost,
        # ghost reconciliation can restore a real cost basis instead of zero.
        inst_key = self.strategy._instrument_key(instrument_id)
        recent_submits = getattr(self.strategy, "recent_buy_submit_by_inst", None)
        if not isinstance(recent_submits, dict):
            recent_submits = {}
            self.strategy.recent_buy_submit_by_inst = recent_submits
        recent_submits[inst_key] = {
            "price": limit_price, "quantity": quantity,
            "created_ts": time.time(), "client_order_id": str(coid),
        }
        with self._lock:
            if self._pending is candidate:
                self._pending = None
        if self.strategy._is_dry_run_mode():
            event_type = "ORDER_DRY_RUN_SUBMITTED"
        else:
            self.strategy.submit_order(order)
            event_type = "ORDER_FAST_FOLLOW_SUBMIT"
        self.strategy._db_order_event(
            event_type=event_type, client_order_id=str(coid), side="BUY",
            price=float(limit_price), qty=float(quantity), status="SUBMITTED",
            reason="outcome_then_twap_confirmed", payload=metadata,
        )
        logger.warning(
            f"FAST FOLLOW BUY submit: slug={slug} side={wanted_side} qty={quantity} "
            f"limit={limit_price} signal_age_ms={age_ms:.1f}"
        )
        return True


def handoff_confirmed_candidate(*args, **kwargs) -> bool:
    owner = kwargs.pop("owner", None)
    return bool(owner and owner.on_quote(*args, **kwargs))
