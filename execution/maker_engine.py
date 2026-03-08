import math
import time
import logging
from enum import Enum
from typing import Dict, Any, Tuple, Optional, List
from decimal import Decimal

from execution.rebate_model import estimate_quote_economics, QuoteEconomics

logger = logging.getLogger(__name__)

class VolatilityRegime(Enum):
    NORMAL = "NORMAL"
    STRESSED = "STRESSED"
    EXTREME = "EXTREME"

class MakerEngineConfig:
    def __init__(
        self,
        maker_half_spread: Decimal,
        maker_quote_size_usdc: Decimal,
        maker_adverse_selection_buffer: Decimal,
        maker_min_expected_net_usdc: Decimal,
        maker_quote_sides: str,
        maker_inventory_skew_max: Decimal,
        maker_max_inventory_shares: Decimal,
        maker_stale_inventory_sec: int,
        maker_stale_inventory_multiplier: Decimal,
        maker_vol_stressed_threshold: Decimal,
        maker_vol_extreme_threshold: Decimal,
        maker_vol_stressed_spread_mult: Decimal,
        maker_vol_stressed_size_mult: Decimal,
        maker_vol_extreme_spread_mult: Decimal,
        maker_pennying_enabled: bool,
        maker_pennying_min_edge: Decimal,
        maker_execution_penalty_enable: bool,
        maker_execution_penalty_floor_usdc: Decimal,
        maker_execution_slippage_spread_mult: Decimal,
        maker_execution_non_atomic_vol_mult: Decimal,
        maker_execution_depth_impact_mult: Decimal,
        maker_execution_vwap_mult: Decimal,
    ):
        self.maker_half_spread = maker_half_spread
        self.maker_quote_size_usdc = maker_quote_size_usdc
        self.maker_adverse_selection_buffer = maker_adverse_selection_buffer
        self.maker_min_expected_net_usdc = maker_min_expected_net_usdc
        self.maker_quote_sides = maker_quote_sides
        self.maker_inventory_skew_max = maker_inventory_skew_max
        self.maker_max_inventory_shares = maker_max_inventory_shares
        self.maker_stale_inventory_sec = maker_stale_inventory_sec
        self.maker_stale_inventory_multiplier = maker_stale_inventory_multiplier
        self.maker_vol_stressed_threshold = maker_vol_stressed_threshold
        self.maker_vol_extreme_threshold = maker_vol_extreme_threshold
        self.maker_vol_stressed_spread_mult = maker_vol_stressed_spread_mult
        self.maker_vol_stressed_size_mult = maker_vol_stressed_size_mult
        self.maker_vol_extreme_spread_mult = maker_vol_extreme_spread_mult
        self.maker_pennying_enabled = maker_pennying_enabled
        self.maker_pennying_min_edge = maker_pennying_min_edge
        self.maker_execution_penalty_enable = maker_execution_penalty_enable
        self.maker_execution_penalty_floor_usdc = maker_execution_penalty_floor_usdc
        self.maker_execution_slippage_spread_mult = maker_execution_slippage_spread_mult
        self.maker_execution_non_atomic_vol_mult = maker_execution_non_atomic_vol_mult
        self.maker_execution_depth_impact_mult = maker_execution_depth_impact_mult
        self.maker_execution_vwap_mult = maker_execution_vwap_mult


class MakerEngine:
    """
    Stateless calculation engine for quoting logics.
    Takes market data, inventory state, and configuration to produce a set of desired quotes.
    Phase 1: Pure mathematical port of IntegratedBTCStrategy quoting logic.
    """

    def __init__(self, config: MakerEngineConfig):
        self.config = config

    @staticmethod
    def normal_cdf(x: float) -> float:
        return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

    @staticmethod
    def digital_up_probability(
        spot: float,
        strike: float,
        sigma_annual: float,
        time_left_sec: float,
    ) -> Decimal:
        if spot <= 0 or strike <= 0:
            return Decimal("0.5")
        if time_left_sec <= 0:
            return Decimal("1.0") if spot >= strike else Decimal("0.0")
        sigma = max(Decimal("0.0001"), Decimal(str(sigma_annual)))
        t_years = max(1e-9, float(time_left_sec) / (365.0 * 24.0 * 3600.0))
        sigma_f = float(sigma)
        denom = sigma_f * math.sqrt(t_years)
        if denom <= 1e-12:
            return Decimal("1.0") if spot >= strike else Decimal("0.0")
        d2 = (math.log(float(spot / strike)) - 0.5 * (sigma_f ** 2) * t_years) / denom
        p = MakerEngine.normal_cdf(d2)
        p = max(0.0, min(1.0, p))
        return Decimal(str(p))

    @staticmethod
    def calculate_fair_price(
        market_mid: Decimal,
        external_spot: float,
        last_external_spot: float,
        strike: Optional[float],
        sigma: float,
        time_left_sec: float,
        outcome: str,
        pricer_mode: str,
    ) -> Decimal:
        """
        Calculates fair price based on external spot price.
        pricer_mode can be 'digital' or 'drift'. 
        If 'digital' but strike is missing, falls back to 'drift'.
        """
        fair = market_mid
        if pricer_mode == "digital" and strike is not None:
            up_prob = MakerEngine.digital_up_probability(
                spot=external_spot,
                strike=strike,
                sigma_annual=sigma,
                time_left_sec=time_left_sec,
            )
            fair_up = max(Decimal("0.01"), min(Decimal("0.99"), up_prob))
            fair_down = Decimal("1.0") - fair_up
            fair_down = max(Decimal("0.01"), min(Decimal("0.99"), fair_down))
            
            if outcome == "up":
                fair = fair_up
            elif outcome == "down":
                fair = fair_down
            else:
                fair = market_mid
        else:
            # Drift / Fallback
            if last_external_spot > 0:
                drift = (external_spot - last_external_spot) / last_external_spot
                shift = Decimal(str(max(-0.05, min(0.05, float(drift) * 8.0))))
                fair = market_mid + shift
                
        return max(Decimal("0.01"), min(Decimal("0.99"), fair))

    def apply_inventory_skew(
        self,
        fair: Decimal,
        inventory_delta_shares: Decimal,
        inventory_last_update_ts: float,
        current_time_ts: float,
    ) -> Decimal:
        """From _apply_inventory_skew in run_bot.py"""
        if self.config.maker_max_inventory_shares <= 0:
            return fair
            
        ratio = inventory_delta_shares / self.config.maker_max_inventory_shares
        ratio = max(Decimal("-1"), min(Decimal("1"), ratio))
        
        multiplier = Decimal("1.0")
        if inventory_delta_shares != 0 and inventory_last_update_ts > 0:
            stale_time = current_time_ts - inventory_last_update_ts
            if stale_time > self.config.maker_stale_inventory_sec:
                multiplier = self.config.maker_stale_inventory_multiplier

        # Long inventory => lower fair to encourage selling and reduce bid aggressiveness.
        skew = ratio * self.config.maker_inventory_skew_max * multiplier
        return max(Decimal("0.01"), min(Decimal("0.99"), fair - skew))

    def determine_regime(self, recent_vol: Optional[Decimal]) -> Tuple[VolatilityRegime, Decimal, Decimal, bool]:
        """Returns (regime, spread_mult, size_mult, reduce_only)"""
        if recent_vol is None:
            return VolatilityRegime.NORMAL, Decimal("1.0"), Decimal("1.0"), False
            
        if recent_vol >= self.config.maker_vol_extreme_threshold:
            return VolatilityRegime.EXTREME, self.config.maker_vol_extreme_spread_mult, Decimal("1.0"), True
        elif recent_vol >= self.config.maker_vol_stressed_threshold:
            return VolatilityRegime.STRESSED, self.config.maker_vol_stressed_spread_mult, self.config.maker_vol_stressed_size_mult, False
            
        return VolatilityRegime.NORMAL, Decimal("1.0"), Decimal("1.0"), False

    def _estimate_side_execution_penalty_usdc(
        self,
        side: str,
        quote_price: Decimal,
        quote_shares: Decimal,
        effective_quote_size: Decimal,
        inst_bid: Decimal,
        inst_ask: Decimal,
        bid_depth: Optional[Decimal],
        ask_depth: Optional[Decimal],
        bid_levels: Optional[List[Tuple[Decimal, Decimal]]],
        ask_levels: Optional[List[Tuple[Decimal, Decimal]]],
        recent_vol: Optional[Decimal],
    ) -> Decimal:
        """
        Minimal execution-risk proxy (depth-aware when available):
        - Slippage proxy from current top-of-book spread and touch depth.
        - Non-atomic risk proxy from recent volatility.
        - Floor penalty to avoid overfitting to optimistic micro snapshots.
        """
        if not self.config.maker_execution_penalty_enable:
            return Decimal("0")

        spread = max(Decimal("0"), inst_ask - inst_bid)
        book_levels = bid_levels if side == "buy" else ask_levels

        def _vwap(levels: List[Tuple[Decimal, Decimal]], qty: Decimal) -> Tuple[Optional[Decimal], Decimal]:
            if not levels or qty <= 0:
                return None, Decimal("0")
            remaining = qty
            consumed_notional = Decimal("0")
            consumed_qty = Decimal("0")
            for px, level_qty in levels:
                if remaining <= 0:
                    break
                if px <= 0 or level_qty <= 0:
                    continue
                take_qty = min(remaining, level_qty)
                consumed_notional += px * take_qty
                consumed_qty += take_qty
                remaining -= take_qty
            if consumed_qty <= 0:
                return None, qty
            return consumed_notional / consumed_qty, remaining
        # Depth-aware slippage proxy:
        # If depth is available, penalize more aggressively when quote size approaches/exceeds top depth.
        # If depth is unavailable, fallback to spread-only proxy.
        touch_depth = bid_depth if side == "buy" else ask_depth
        notional = max(Decimal("0"), quote_shares * quote_price)
        if touch_depth is not None and touch_depth > 0:
            impact_ratio = max(Decimal("0"), quote_shares / touch_depth)
            impact_mult = Decimal("1") + (impact_ratio * self.config.maker_execution_depth_impact_mult)
            slippage_penalty = notional * spread * self.config.maker_execution_slippage_spread_mult * impact_mult
        else:
            slippage_penalty = effective_quote_size * spread * self.config.maker_execution_slippage_spread_mult

        # Multi-level VWAP add-on (if book levels are available).
        vwap_penalty = Decimal("0")
        if book_levels:
            vwap_price, remaining_qty = _vwap(book_levels, quote_shares)
            if vwap_price is not None and quote_shares > 0:
                touch_px = inst_bid if side == "buy" else inst_ask
                if touch_px > 0:
                    if side == "buy":
                        # Buy-side risk = forced exit by selling into bids.
                        impact_pct = max(Decimal("0"), (touch_px - vwap_price) / touch_px)
                    else:
                        # Sell-side risk = forced cover by buying from asks.
                        impact_pct = max(Decimal("0"), (vwap_price - touch_px) / touch_px)
                    vwap_penalty += notional * impact_pct * self.config.maker_execution_vwap_mult
            # If requested size exceeds observed levels, penalize exhaustion explicitly.
            if remaining_qty > 0 and quote_shares > 0:
                exhaustion_ratio = remaining_qty / quote_shares
                vwap_penalty += notional * exhaustion_ratio * spread * self.config.maker_execution_vwap_mult

        vol = max(Decimal("0"), recent_vol or Decimal("0"))
        non_atomic_penalty = notional * vol * self.config.maker_execution_non_atomic_vol_mult
        total_penalty = slippage_penalty + vwap_penalty + non_atomic_penalty
        return max(self.config.maker_execution_penalty_floor_usdc, total_penalty)

    def generate_quote_plan(
        self,
        inst_bid: Decimal,
        inst_ask: Decimal,
        fair_price: Decimal,
        fee_rate: Decimal,
        inventory_delta_shares: Decimal,
        inventory_last_update_ts: float,
        current_time_ts: float,
        tick_size: Decimal,
        recent_vol: Optional[Decimal] = None,
        balance_forced_sell_only: bool = False,
        bid_depth: Optional[Decimal] = None,
        ask_depth: Optional[Decimal] = None,
        bid_levels: Optional[List[Tuple[Decimal, Decimal]]] = None,
        ask_levels: Optional[List[Tuple[Decimal, Decimal]]] = None,
    ) -> Dict[str, Tuple[Decimal, QuoteEconomics, bool, Decimal, Decimal]]:
        """
        Produce target limit prices and economic estimations for buy and sell sides.
        Returns mapped dictionary:
        {"buy": (price, econ, should_quote, robust_net, execution_penalty), ...}
        """
        regime, spread_mult, size_mult, regime_reduce_only = self.determine_regime(recent_vol)
        
        effective_half_spread = self.config.maker_half_spread * spread_mult
        effective_quote_size = self.config.maker_quote_size_usdc * size_mult
        
        skewed_fair = self.apply_inventory_skew(
            fair_price,
            inventory_delta_shares,
            inventory_last_update_ts,
            current_time_ts,
        )
        
        quote_bid = max(Decimal("0.01"), skewed_fair - effective_half_spread)
        quote_ask = min(Decimal("0.99"), skewed_fair + effective_half_spread)

        if self.config.maker_pennying_enabled:
            # Active Top-of-Book Snipe (Pennying)
            penny_bid = inst_bid + tick_size
            penny_ask = inst_ask - tick_size
            
            # Ensure we maintain minimum profit edge
            max_acceptable_bid = fair_price - self.config.maker_pennying_min_edge
            min_acceptable_ask = fair_price + self.config.maker_pennying_min_edge
            
            # Bid Pennying
            if inst_bid < max_acceptable_bid:
                # If beating the best bid is within our max acceptable price, we penny it
                quote_bid = min(penny_bid, max_acceptable_bid)
            else:
                # If the market is too tight, we fall back to our max acceptable (passive)
                quote_bid = max_acceptable_bid
                
            # Ask Pennying
            if inst_ask > min_acceptable_ask:
                quote_ask = max(penny_ask, min_acceptable_ask)
            else:
                quote_ask = min_acceptable_ask
        else:
            # Passive Quoting
            quote_bid = min(quote_bid, inst_bid)
            quote_ask = max(quote_ask, inst_ask)
            
        if quote_bid >= quote_ask:
            return {}

        bid_econ = estimate_quote_economics(
            quote_size_usdc=effective_quote_size,
            probability=quote_bid,
            half_spread=(skewed_fair - quote_bid),
            adverse_selection_buffer=self.config.maker_adverse_selection_buffer,
            fee_rate_override=fee_rate,
        )
        ask_econ = estimate_quote_economics(
            quote_size_usdc=effective_quote_size,
            probability=quote_ask,
            half_spread=(quote_ask - skewed_fair),
            adverse_selection_buffer=self.config.maker_adverse_selection_buffer,
            fee_rate_override=fee_rate,
        )

        side_plan = {}
        bid_exec_penalty = self._estimate_side_execution_penalty_usdc(
            side="buy",
            quote_price=quote_bid,
            quote_shares=bid_econ.shares,
            effective_quote_size=effective_quote_size,
            inst_bid=inst_bid,
            inst_ask=inst_ask,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            bid_levels=bid_levels,
            ask_levels=ask_levels,
            recent_vol=recent_vol,
        )
        ask_exec_penalty = self._estimate_side_execution_penalty_usdc(
            side="sell",
            quote_price=quote_ask,
            quote_shares=ask_econ.shares,
            effective_quote_size=effective_quote_size,
            inst_bid=inst_bid,
            inst_ask=inst_ask,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            bid_levels=bid_levels,
            ask_levels=ask_levels,
            recent_vol=recent_vol,
        )
        
        # Determine allowed sides based on modes and inventory reduction
        allowed_buy = True
        allowed_sell = True
        
        if self.config.maker_quote_sides == "buy":
            allowed_sell = False
        elif self.config.maker_quote_sides == "sell":
            allowed_buy = False
        elif self.config.maker_quote_sides == "both_buy":
            # In both_buy mode, orchestration will iterate over both Up/Down instruments
            # and we only quote BUY side on each instrument.
            allowed_sell = False
            
        if balance_forced_sell_only:
            allowed_buy = False
            allowed_sell = True

        if regime_reduce_only:
            # If reduce only, we only allow quotes on the side that brings inventory closer to 0
            if inventory_delta_shares > 0:
                allowed_buy = False
            elif inventory_delta_shares < 0:
                allowed_sell = False
            # else: inventory=0, nothing to reduce → allow both sides

        if allowed_buy:
            robust_bid_net = bid_econ.expected_net_usdc - bid_exec_penalty
            side_plan["buy"] = (
                quote_bid,
                bid_econ,
                robust_bid_net >= self.config.maker_min_expected_net_usdc,
                robust_bid_net,
                bid_exec_penalty,
            )
        else:
            side_plan["buy"] = (
                quote_bid,
                bid_econ,
                False,
                bid_econ.expected_net_usdc - bid_exec_penalty,
                bid_exec_penalty,
            )
            
        if allowed_sell:
            robust_ask_net = ask_econ.expected_net_usdc - ask_exec_penalty
            side_plan["sell"] = (
                quote_ask,
                ask_econ,
                robust_ask_net >= self.config.maker_min_expected_net_usdc,
                robust_ask_net,
                ask_exec_penalty,
            )
        else:
            side_plan["sell"] = (
                quote_ask,
                ask_econ,
                False,
                ask_econ.expected_net_usdc - ask_exec_penalty,
                ask_exec_penalty,
            )

        return side_plan
