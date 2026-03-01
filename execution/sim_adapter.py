import time
import random
import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

logger = logging.getLogger(__name__)

@dataclass
class PaperTrade:
    """Track paper/simulation trades"""
    timestamp: datetime
    direction: str
    size_usd: float
    price: float
    signal_score: float
    signal_confidence: float
    outcome: str = "PENDING"
    
    def to_dict(self):
        return {
            'timestamp': self.timestamp.isoformat(),
            'direction': self.direction,
            'size_usd': self.size_usd,
            'price': self.price,
            'signal_score': self.signal_score,
            'signal_confidence': self.signal_confidence,
            'outcome': self.outcome,
        }

class SimAdapterConfig:
    def __init__(
        self,
        sim_fill_base_prob: float,
        sim_fill_edge_boost: float,
        sim_fill_queue_penalty: float,
        sim_fill_age_bonus_max: float,
        sim_fill_age_to_max_sec: float,
        sim_partial_fill_min_ratio: float,
        sim_partial_fill_max_ratio: float,
        maker_sim_eval_sec: int,
        maker_fee_rate_bps_default: int,
    ):
        self.sim_fill_base_prob = sim_fill_base_prob
        self.sim_fill_edge_boost = sim_fill_edge_boost
        self.sim_fill_queue_penalty = sim_fill_queue_penalty
        self.sim_fill_age_bonus_max = sim_fill_age_bonus_max
        self.sim_fill_age_to_max_sec = sim_fill_age_to_max_sec
        self.sim_partial_fill_min_ratio = sim_partial_fill_min_ratio
        self.sim_partial_fill_max_ratio = sim_partial_fill_max_ratio
        self.maker_sim_eval_sec = maker_sim_eval_sec
        self.maker_fee_rate_bps_default = maker_fee_rate_bps_default

class SimAdapter:
    """
    Handles paper trading and shadow fills logic for Simulation mode.
    Decoupled from the orchestration layer to keep run_bot.py clean.
    """
    def __init__(self, config: SimAdapterConfig):
        self.config = config
        self.paper_trades: List[PaperTrade] = []
        self.sim_maker_positions: List[Dict[str, Any]] = []
        self.sim_maker_closed_wins = 0
        self.sim_maker_closed_total = 0

    def get_win_rate(self) -> float:
        if self.sim_maker_closed_total > 0:
            return self.sim_maker_closed_wins / self.sim_maker_closed_total
        return 0.0

    def simulate_shadow_maker_fills_and_closes(
        self, 
        active_maker_orders: Dict[str, Any],
        inventory_delta_shares: Decimal,
        bid_price: Decimal, 
        ask_price: Decimal, 
        now_ts: float,
        get_quote_for_instrument_fn,
        normalize_instrument_id_fn,
        instrument_cache_fn,
        db_event_fn,
        record_cancel_fn,
        record_trade_fn
    ) -> Decimal:
        """
        In simulation mode, emulate maker order lifecycle.
        Updates active_maker_orders directly. 
        Returns the new inventory_delta_shares.
        """
        mid_price = (bid_price + ask_price) / 2
        new_inventory = inventory_delta_shares

        # Advance simulated order states.
        for order_key, state in list(active_maker_orders.items()):
            side = str(state.get("side", "") or "")
            if not state.get("simulated"):
                continue
            coid = str(state.get("client_order_id", f"SIM-{side}-{int(now_ts*1000)}"))

            # Ack transition.
            if state.get("status", "PENDING_ACK") == "PENDING_ACK":
                ack_at = float(state.get("ack_at", 0.0))
                if now_ts >= ack_at:
                    state["status"] = "RESTING"
                    db_event_fn(
                        event_type="ORDER_SIM_ACCEPTED",
                        client_order_id=coid,
                        side=side.upper(),
                        price=float(state.get("price", 0.0)),
                        qty=float(state.get("quantity", 0.0)),
                        status="ACCEPTED",
                    )

            # Cancel transition.
            if state.get("pending_cancel"):
                cancel_effective_at = float(state.get("cancel_effective_at", 0.0))
                if now_ts >= cancel_effective_at:
                    active_maker_orders.pop(order_key, None)
                    record_cancel_fn(str(state.get("cancel_reason", "cancel")))
                    db_event_fn(
                        event_type="ORDER_SIM_CANCELED",
                        client_order_id=coid,
                        side=side.upper(),
                        price=float(state.get("price", 0.0)),
                        qty=float(state.get("quantity", 0.0)),
                        status="CANCELED",
                        reason=str(state.get("cancel_reason", "cancel")),
                    )
                    continue

            if state.get("status") != "RESTING":
                continue

            limit_price = Decimal(str(state.get("price", "0")))
            qty = Decimal(str(state.get("quantity", "0")))
            if qty <= 0:
                continue
            filled_qty = Decimal(str(state.get("filled_qty", "0")))
            remaining_qty = qty - filled_qty
            if remaining_qty <= 0:
                continue

            instrument_for_state = normalize_instrument_id_fn(state.get("instrument_id"))
            if not instrument_for_state:
                # Default logic fallback handled upstream if instrument missing, but let's be safe
                pass
            
            instrument = instrument_cache_fn(instrument_for_state) if instrument_for_state else None
            tick = Decimal("0.01")
            if instrument is not None:
                try:
                    raw_tick = getattr(instrument, "price_increment", None)
                    if raw_tick is not None:
                        tick = Decimal(str(raw_tick))
                    elif hasattr(instrument, "info") and instrument.info:
                        maybe_tick = instrument.info.get("minimum_tick_size")
                        if maybe_tick is not None:
                            tick = Decimal(str(maybe_tick))
                except Exception:
                    pass
            if tick <= 0:
                tick = Decimal("0.01")

            state_inst = normalize_instrument_id_fn(state.get("instrument_id"))
            state_quote = get_quote_for_instrument_fn(state_inst) if state_inst else None
            eval_bid = state_quote[0] if state_quote is not None else bid_price
            eval_ask = state_quote[1] if state_quote is not None else ask_price

            # Fill trigger:
            # 1) hard cross (always eligible),
            # 2) resting at/near top-of-book (maker can be hit without spread crossing).
            crossed = False
            near_top = False
            if side == "buy":
                crossed = eval_ask <= limit_price
                near_top = limit_price >= (eval_bid - tick)
            else:
                crossed = eval_bid >= limit_price
                near_top = limit_price <= (eval_ask + tick)

            if not crossed and not near_top:
                continue

            created_ts = float(state.get("created_ts", now_ts))
            age_sec = max(0.0, now_ts - created_ts)
            queue_rank = float(state.get("queue_rank", 0.5))  # 0=front, 1=back
            edge = float((limit_price - ask_price) if side == "buy" else (bid_price - limit_price))
            edge = max(0.0, edge)

            # If near top but not crossed, reward top-of-book proximity.
            if side == "buy":
                dist_to_best = max(Decimal("0"), eval_bid - limit_price)
            else:
                dist_to_best = max(Decimal("0"), limit_price - eval_ask)
            proximity_ratio = 0.0
            if tick > 0:
                proximity_ratio = max(0.0, 1.0 - float(dist_to_best / tick))
                proximity_ratio = min(1.0, proximity_ratio)

            age_bonus = min(
                self.config.sim_fill_age_bonus_max,
                (age_sec / float(self.config.sim_fill_age_to_max_sec)) * self.config.sim_fill_age_bonus_max,
            )
            crossed_bonus = 0.45 if crossed else 0.0
            top_book_bonus = 0.20 * proximity_ratio if near_top else 0.0
            fill_prob = (
                self.config.sim_fill_base_prob
                + min(self.config.sim_fill_edge_boost, edge * 30.0)
                + crossed_bonus
                + top_book_bonus
                + age_bonus
                - (self.config.sim_fill_queue_penalty * queue_rank)
            )
            fill_prob = max(0.0, min(0.98, fill_prob))
            if random.random() >= fill_prob:
                continue

            fill_ratio_low = max(0.01, min(self.config.sim_partial_fill_min_ratio, self.config.sim_partial_fill_max_ratio))
            fill_ratio_high = max(fill_ratio_low, self.config.sim_partial_fill_max_ratio)
            fill_ratio = random.uniform(fill_ratio_low, fill_ratio_high)
            this_fill_qty = remaining_qty * Decimal(str(fill_ratio))
            min_lot = Decimal("0.000001")
            if this_fill_qty < min_lot:
                this_fill_qty = min_lot
            if this_fill_qty > remaining_qty:
                this_fill_qty = remaining_qty

            fee_rate_bps = int(state.get("fee_rate_bps", self.config.maker_fee_rate_bps_default))
            fee_rate_dec = Decimal(str(max(0, fee_rate_bps))) / Decimal("10000")
            entry_commission = (this_fill_qty * limit_price) * fee_rate_dec

            state["filled_qty"] = filled_qty + this_fill_qty
            state["entry_commission_usdc"] = Decimal(str(state.get("entry_commission_usdc", "0"))) + entry_commission
            state["last_fill_ts"] = now_ts

            status = "PARTIALLY_FILLED" if state["filled_qty"] < qty else "FILLED"
            db_event_fn(
                event_type="ORDER_SIM_FILLED",
                client_order_id=coid,
                side=side.upper(),
                price=float(limit_price),
                qty=float(this_fill_qty),
                status=status,
                commission_usdc=float(entry_commission),
                payload={
                    "fill_prob": fill_prob,
                    "queue_rank": queue_rank,
                    "age_sec": age_sec,
                    "edge": edge,
                    "crossed": crossed,
                    "near_top": near_top,
                    "proximity_ratio": proximity_ratio,
                    "filled_qty_total": float(state["filled_qty"]),
                    "qty_total": float(qty),
                    "fee_rate_bps": fee_rate_bps,
                },
            )

            if state["filled_qty"] < qty:
                continue

            active_maker_orders.pop(order_key, None)
            final_qty = state["filled_qty"]
            if side == "buy":
                new_inventory += final_qty
            else:
                new_inventory -= final_qty

            self.sim_maker_positions.append(
                {
                    "client_order_id": coid,
                    "side": side,
                    "qty": final_qty,
                    "entry_price": limit_price,
                    "entry_commission_usdc": Decimal(str(state.get("entry_commission_usdc", "0"))),
                    "fee_rate_bps": fee_rate_bps,
                    "opened_ts": now_ts,
                }
            )

        # Close simulated fills after hold horizon and compute win-rate.
        remaining_positions: List[Dict[str, Any]] = []
        for pos in self.sim_maker_positions:
            opened_ts = float(pos.get("opened_ts", 0.0))
            if now_ts - opened_ts < self.config.maker_sim_eval_sec:
                remaining_positions.append(pos)
                continue

            side = str(pos.get("side"))
            qty = Decimal(str(pos.get("qty", "0")))
            entry = Decimal(str(pos.get("entry_price", "0")))
            entry_commission = Decimal(str(pos.get("entry_commission_usdc", "0")))
            fee_rate_bps = int(pos.get("fee_rate_bps", self.config.maker_fee_rate_bps_default))
            fee_rate_dec = Decimal(str(max(0, fee_rate_bps))) / Decimal("10000")
            if qty <= 0 or entry <= 0:
                continue

            if side == "buy":
                gross_pnl = (mid_price - entry) * qty
                new_inventory -= qty
                direction = "long"
            else:
                gross_pnl = (entry - mid_price) * qty
                new_inventory += qty
                direction = "short"
            exit_commission = (mid_price * qty) * fee_rate_dec
            pnl = gross_pnl - entry_commission - exit_commission

            self.sim_maker_closed_total += 1
            if pnl > 0:
                self.sim_maker_closed_wins += 1

            # Persist into existing performance tracker as synthetic maker trade.
            try:
                record_trade_fn(
                    trade_id=f"sim_maker_{pos.get('client_order_id')}",
                    direction=direction,
                    entry_price=entry,
                    exit_price=mid_price,
                    size=entry * qty,
                    entry_time=datetime.fromtimestamp(opened_ts),
                    exit_time=datetime.fromtimestamp(now_ts),
                    signal_score=0.0,
                    signal_confidence=0.0,
                    metadata={"sim_maker": True, "market_slug": "sim", "pnl_usdc": float(pnl)},
                )
            except Exception as e:
                logger.debug(f"Failed to record simulated maker trade: {e}")

        self.sim_maker_positions = remaining_positions
        return new_inventory
