import math
import time
import logging
from enum import Enum
from typing import Dict, Any, Tuple, Optional, List
from decimal import Decimal

from execution.rebate_model import (
    QuoteEconomics,
    estimate_quote_economics,
    estimate_taker_fee_usdc,
)

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
        maker_min_shares: Decimal,
        maker_fixed_shares: Decimal,
        maker_max_order_usdc: Decimal,
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
        maker_buy_taker_leakage_prob: Decimal,
        maker_execution_empirical_adverse_markout_per_share: Optional[Decimal] = None,
    ):
        self.maker_half_spread = maker_half_spread
        self.maker_quote_size_usdc = maker_quote_size_usdc
        self.maker_min_shares = maker_min_shares
        self.maker_fixed_shares = maker_fixed_shares
        self.maker_max_order_usdc = maker_max_order_usdc
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
        self.maker_buy_taker_leakage_prob = maker_buy_taker_leakage_prob
        self.maker_execution_empirical_adverse_markout_per_share = (
            max(Decimal("0"), maker_execution_empirical_adverse_markout_per_share)
            if maker_execution_empirical_adverse_markout_per_share is not None
            else None
        )


class MakerEngine:
    """
    Stateless calculation engine for quoting logics.
    Takes market data, inventory state, and configuration to produce a set of desired quotes.
    Phase 1: Pure mathematical port of IntegratedBTCStrategy quoting logic.
    """

    def __init__(self, config: MakerEngineConfig):
        self.config = config

    def _compute_effective_quote_shares(self, quote_price: Decimal) -> Decimal:
        """
        Match the runtime maker order sizing logic closely enough that quote
        economics are evaluated on the same exposure that would actually be
        submitted.
        """
        if quote_price <= 0:
            return Decimal("0")

        min_qty = max(Decimal("0"), self.config.maker_min_shares)

        if self.config.maker_fixed_shares > 0:
            qty = max(self.config.maker_fixed_shares, min_qty)
            if self.config.maker_max_order_usdc > 0:
                max_shares_by_notional = self.config.maker_max_order_usdc / quote_price
                if qty > max_shares_by_notional:
                    qty = max(max_shares_by_notional, min_qty)
            return max(Decimal("0"), qty)

        quote_notional_usdc = self.config.maker_quote_size_usdc
        if self.config.maker_max_order_usdc > 0:
            quote_notional_usdc = min(quote_notional_usdc, self.config.maker_max_order_usdc)

        token_qty = quote_notional_usdc / quote_price if quote_price > 0 else Decimal("0")
        return max(Decimal("0"), max(token_qty, min_qty))

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
    def twap_settlement_up_probability(
        spot: float,
        strike: float,
        sigma_annual: float,
        time_left_sec: float,
        twap_window_sec: int = 60,
        observed_window_avg: Optional[float] = None,
        observed_window_sec: float = 0.0,
    ) -> Decimal:
        """Approximate probability that the final rolling TWAP clears strike.

        For a Brownian price path, the average of a future window has lower
        conditional variance than a terminal snapshot.  Before the final
        window, the equivalent variance horizon is ``T - 2W/3``.  During the
        final window the exact observed partial integral is not yet available
        from RTDS, so retain the conservative terminal horizon instead of
        fabricating it.  Raw Chainlink ticks are now retained for the next
        stage, where that integral can be computed directly.
        """
        window = max(1.0, float(twap_window_sec))
        horizon = float(time_left_sec)
        observed_sec = max(0.0, min(window, float(observed_window_sec)))
        if observed_window_avg is not None and observed_sec > 0:
            remaining_sec = max(0.0, window - observed_sec)
            if remaining_sec <= 0:
                return Decimal("1") if float(observed_window_avg) >= strike else Decimal("0")
            # The remaining segment must average at least this level for the
            # complete 60-second settlement average to clear the strike.
            required_remaining_avg = (
                window * float(strike) - observed_sec * float(observed_window_avg)
            ) / remaining_sec
            return MakerEngine.digital_up_probability(
                spot=spot,
                strike=required_remaining_avg,
                sigma_annual=sigma_annual,
                # Average a future residual path rather than a terminal tick.
                time_left_sec=max(1.0, remaining_sec / 3.0),
            )
        if horizon > window:
            horizon = max(1.0, horizon - (2.0 * window / 3.0))
        return MakerEngine.digital_up_probability(
            spot=spot,
            strike=strike,
            sigma_annual=sigma_annual,
            time_left_sec=horizon,
        )

    @staticmethod
    def implied_sigma_from_market_mid(
        market_mid: float,
        spot: float,
        strike: float,
        time_left_sec: float,
        outcome: str = "up",
        max_iter: int = 20,
        tol: float = 1e-4,
    ) -> Optional[Decimal]:
        """
        Solve for the implied sigma that makes digital_up_probability match market_mid.
        Uses bisection method (robust for bounded problems).
        Returns None if inputs are degenerate or solution doesn't converge.
        """
        if spot <= 0 or strike <= 0 or time_left_sec <= 1.0:
            return None
        target = market_mid
        if outcome == "down":
            target = 1.0 - market_mid
        if target <= 0.01 or target >= 0.99:
            return None

        lo, hi = 0.05, 5.0
        for _ in range(max_iter):
            mid_sigma = (lo + hi) / 2.0
            p = float(MakerEngine.digital_up_probability(spot, strike, mid_sigma, time_left_sec))
            if abs(p - target) < tol:
                return Decimal(str(round(mid_sigma, 4)))
            # Higher sigma → probability closer to 0.5
            # If current p > target, we need higher sigma to push p toward 0.5
            # If current p < target, we need lower sigma
            if spot >= strike:
                # p > 0.5 region: higher sigma → lower p
                if p > target:
                    lo = mid_sigma
                else:
                    hi = mid_sigma
            else:
                # p < 0.5 region: higher sigma → higher p
                if p < target:
                    lo = mid_sigma
                else:
                    hi = mid_sigma
        # Return best estimate even if not fully converged
        final = (lo + hi) / 2.0
        return Decimal(str(round(final, 4)))

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

        Drift Fallback Design
        ---------------------
        When strike is unavailable we estimate the "fairness signal" from the
        rate-of-change of the external spot.  The challenge is converting an
        equity-world percentage move into a probability shift for a binary event.

        Old approach (removed): drift * 8.0  — arbitrary, routinely saturated
        the ±5% clamp on any meaningful BTC tick, yielding no information.

        New approach:
          shift = drift × (0.5 / sigma_effective)
        Rationale: think of the drift as a z-score expressed in σ-per-period units.
        Dividing by 2σ maps a 1-σ spot move to roughly a 0.5 / 2 = 0.25 probability
        shift, which is an upper bound consistent with binary option deltas near ATM.
        Clamp is tightened to ±0.03 so a single noisy tick cannot pin fair at an
        extreme.  This keeps the fallback informative without being
        overconfident.
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
            # Drift / Fallback (strike unavailable)
            # ------------------------------------
            # Principled multiplier: scale drift by 0.5/sigma so that a
            # 1-sigma spot move maps to at most a ~0.25 probability shift.
            # Clamp to ±0.03 (was ±0.05) to reduce single-tick saturation.
            if last_external_spot > 0:
                drift = (external_spot - last_external_spot) / last_external_spot
                sigma_effective = max(0.20, float(sigma))  # floor matches MAKER_DIGITAL_SIGMA_FLOOR
                # Principled multiplier replaces magic '8.0'
                drift_multiplier = 0.5 / sigma_effective  # ≈ 2.5 at σ=0.20, ≈ 0.83 at σ=0.60
                raw_shift = float(drift) * drift_multiplier
                clamped_shift = max(-0.03, min(0.03, raw_shift))  # tighter clamp (was ±0.05)
                shift = Decimal(str(clamped_shift))
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

    def _execution_penalty_components(
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
    ) -> dict[str, Decimal]:
        """
        Minimal execution-risk proxy (depth-aware when available):
        - Slippage proxy from current top-of-book spread and touch depth.
        - Non-atomic risk proxy from recent volatility.
        - Floor penalty to avoid overfitting to optimistic micro snapshots.
        """
        if not self.config.maker_execution_penalty_enable:
            return {
                "slippage_usdc": Decimal("0"),
                "vwap_usdc": Decimal("0"),
                "non_atomic_usdc": Decimal("0"),
                "floor_usdc": Decimal("0"),
                "total_usdc": Decimal("0"),
                "recent_vol": max(Decimal("0"), recent_vol or Decimal("0")),
            }

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
        book_vwap_penalty = Decimal("0")
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
                    book_vwap_penalty += notional * impact_pct * self.config.maker_execution_vwap_mult
            # If requested size exceeds observed levels, penalize exhaustion explicitly.
            if remaining_qty > 0 and quote_shares > 0:
                exhaustion_ratio = remaining_qty / quote_shares
                book_vwap_penalty += notional * exhaustion_ratio * spread * self.config.maker_execution_vwap_mult

        # Preserve the legacy proxy as telemetry.  The convergence program
        # compares it with a single observed-markout model before changing any
        # economics gate; neither comparison value changes current behavior.
        vol = max(Decimal("0"), recent_vol or Decimal("0"))
        legacy_non_atomic_penalty = (
            notional * vol * self.config.maker_execution_non_atomic_vol_mult
        )
        legacy_raw_total = slippage_penalty + book_vwap_penalty + legacy_non_atomic_penalty
        legacy_floor_penalty = max(
            Decimal("0"),
            self.config.maker_execution_penalty_floor_usdc - legacy_raw_total,
        )
        legacy_total = max(self.config.maker_execution_penalty_floor_usdc, legacy_raw_total)

        # Order-book VWAP is a stress estimate for an immediate full liquidation.
        # A real maker-fill adverse markout is a direct measurement of the
        # expected short-horizon loss after a passive entry. It replaces both
        # that forced-exit stress and the overlapping non-atomic proxy.
        vwap_penalty = book_vwap_penalty
        empirical_markout_penalty: Optional[Decimal] = None
        use_empirical_markout = False
        if self.config.maker_execution_empirical_adverse_markout_per_share is not None:
            empirical_markout_penalty = (
                quote_shares * self.config.maker_execution_empirical_adverse_markout_per_share
            )
            vwap_penalty = Decimal("0")
            use_empirical_markout = True

        non_atomic_penalty = (
            Decimal("0")
            if use_empirical_markout
            else notional * vol * self.config.maker_execution_non_atomic_vol_mult
        )
        raw_total = (
            slippage_penalty
            + vwap_penalty
            + non_atomic_penalty
            + (empirical_markout_penalty or Decimal("0"))
        )
        floor_penalty = max(Decimal("0"), self.config.maker_execution_penalty_floor_usdc - raw_total)
        return {
            "slippage_usdc": slippage_penalty,
            "vwap_usdc": vwap_penalty,
            "book_vwap_usdc": book_vwap_penalty,
            "empirical_markout_usdc": empirical_markout_penalty or Decimal("0"),
            "empirical_markout_applied": (
                Decimal("1") if empirical_markout_penalty is not None else Decimal("0")
            ),
            "empirical_markout_replaces_non_atomic": (
                Decimal("1") if use_empirical_markout else Decimal("0")
            ),
            # Observation-only comparison for execution-cost convergence.
            # ``single_empirical_penalty_usdc`` deliberately excludes spread,
            # depth, VWAP, volatility and fixed-floor proxies: those effects
            # are represented by the observed post-fill adverse markout.
            "legacy_proxy_slippage_usdc": slippage_penalty,
            "legacy_proxy_vwap_usdc": book_vwap_penalty,
            "legacy_proxy_non_atomic_usdc": legacy_non_atomic_penalty,
            "legacy_proxy_floor_usdc": legacy_floor_penalty,
            "legacy_proxy_penalty_usdc": legacy_total,
            "single_empirical_penalty_usdc": empirical_markout_penalty or Decimal("0"),
            "single_empirical_available": Decimal("1") if use_empirical_markout else Decimal("0"),
            "non_atomic_usdc": non_atomic_penalty,
            "floor_usdc": floor_penalty,
            "total_usdc": max(self.config.maker_execution_penalty_floor_usdc, raw_total),
            "recent_vol": vol,
        }

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
    ) -> Dict[str, Tuple[Decimal, QuoteEconomics, bool, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]]:
        """
        Produce target limit prices and economic estimations for buy and sell sides.
        Returns mapped dictionary:
        {"buy": (price, econ, should_quote, robust_net, execution_penalty), ...}
        """
        regime, spread_mult, size_mult, regime_reduce_only = self.determine_regime(recent_vol)
        
        effective_half_spread = self.config.maker_half_spread * spread_mult
        base_quote_size = self.config.maker_quote_size_usdc * size_mult
        
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
            logger.debug(
                "generate_quote_plan: empty plan (bid=%.4f >= ask=%.4f, inst_bid=%.4f, inst_ask=%.4f, fair=%.4f)",
                float(quote_bid), float(quote_ask), float(inst_bid), float(inst_ask), float(fair_price),
            )
            return {}

        bid_quote_shares = self._compute_effective_quote_shares(quote_bid)
        ask_quote_shares = self._compute_effective_quote_shares(quote_ask)
        bid_quote_size = bid_quote_shares * quote_bid
        ask_quote_size = ask_quote_shares * quote_ask

        bid_econ = estimate_quote_economics(
            quote_size_usdc=bid_quote_size,
            probability=quote_bid,
            half_spread=(skewed_fair - quote_bid),
            adverse_selection_buffer=self.config.maker_adverse_selection_buffer,
            fee_rate_override=fee_rate,
        )
        ask_econ = estimate_quote_economics(
            quote_size_usdc=ask_quote_size,
            probability=quote_ask,
            half_spread=(quote_ask - skewed_fair),
            adverse_selection_buffer=self.config.maker_adverse_selection_buffer,
            fee_rate_override=fee_rate,
        )

        side_plan = {}
        bid_exec_components = self._execution_penalty_components(
            side="buy",
            quote_price=quote_bid,
            quote_shares=bid_econ.shares,
            effective_quote_size=bid_quote_size,
            inst_bid=inst_bid,
            inst_ask=inst_ask,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            bid_levels=bid_levels,
            ask_levels=ask_levels,
            recent_vol=recent_vol,
        )
        ask_exec_components = self._execution_penalty_components(
            side="sell",
            quote_price=quote_ask,
            quote_shares=ask_econ.shares,
            effective_quote_size=ask_quote_size,
            inst_bid=inst_bid,
            inst_ask=inst_ask,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            bid_levels=bid_levels,
            ask_levels=ask_levels,
            recent_vol=recent_vol,
        )
        bid_exec_penalty = bid_exec_components["total_usdc"]
        ask_exec_penalty = ask_exec_components["total_usdc"]
        
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

        bid_fee_ps = (
            bid_econ.fee_equivalent_usdc / bid_econ.shares
            if bid_econ.shares > 0
            else Decimal("0")
        )
        bid_taker_leakage_usdc = Decimal("0")
        if bid_econ.shares > 0 and self.config.maker_buy_taker_leakage_prob > 0:
            bid_taker_leakage_usdc = estimate_taker_fee_usdc(
                shares=bid_econ.shares,
                probability=quote_bid,
            ) * self.config.maker_buy_taker_leakage_prob
        bid_taker_leakage_ps = (
            bid_taker_leakage_usdc / bid_econ.shares
            if bid_econ.shares > 0
            else Decimal("0")
        )
        bid_exec_components["legacy_proxy_robust_net_usdc"] = (
            bid_econ.expected_net_usdc
            - bid_exec_components["legacy_proxy_penalty_usdc"]
            - bid_taker_leakage_usdc
        )
        bid_exec_components["single_empirical_robust_net_usdc"] = (
            bid_econ.expected_net_usdc
            - bid_exec_components["single_empirical_penalty_usdc"]
            - bid_taker_leakage_usdc
        )
        ask_exec_components["legacy_proxy_robust_net_usdc"] = (
            ask_econ.expected_net_usdc - ask_exec_components["legacy_proxy_penalty_usdc"]
        )
        ask_exec_components["single_empirical_robust_net_usdc"] = (
            ask_econ.expected_net_usdc - ask_exec_components["single_empirical_penalty_usdc"]
        )
        ask_fee_ps = (
            ask_econ.fee_equivalent_usdc / ask_econ.shares
            if ask_econ.shares > 0
            else Decimal("0")
        )
        bid_exec_penalty_ps = (
            bid_exec_penalty / bid_econ.shares
            if bid_econ.shares > 0
            else Decimal("0")
        )
        ask_exec_penalty_ps = (
            ask_exec_penalty / ask_econ.shares
            if ask_econ.shares > 0
            else Decimal("0")
        )
        bid_adverse_ps = (
            self.config.maker_adverse_selection_buffer / bid_econ.shares
            if bid_econ.shares > 0
            else Decimal("0")
        )
        ask_adverse_ps = (
            self.config.maker_adverse_selection_buffer / ask_econ.shares
            if ask_econ.shares > 0
            else Decimal("0")
        )

        # Directional edge:
        # BUY  -> value minus paid price and execution/friction costs
        # SELL -> received price minus fair value and execution/friction costs
        bid_directional_edge_ps = fair_price - quote_bid - bid_fee_ps - bid_taker_leakage_ps - bid_exec_penalty_ps - bid_adverse_ps
        ask_directional_edge_ps = quote_ask - fair_price - ask_fee_ps - ask_exec_penalty_ps - ask_adverse_ps
        bid_directional_edge_usdc = bid_directional_edge_ps * bid_econ.shares
        ask_directional_edge_usdc = ask_directional_edge_ps * ask_econ.shares

        if allowed_buy:
            robust_bid_net = bid_econ.expected_net_usdc - bid_exec_penalty - bid_taker_leakage_usdc
            side_plan["buy"] = (
                quote_bid,
                bid_econ,
                robust_bid_net >= self.config.maker_min_expected_net_usdc,
                robust_bid_net,
                bid_exec_penalty,
                bid_directional_edge_ps,
                bid_directional_edge_usdc,
                fair_price,
                bid_fee_ps + bid_taker_leakage_ps,
                bid_exec_penalty_ps + bid_adverse_ps,
                bid_exec_components,
            )
        else:
            side_plan["buy"] = (
                quote_bid,
                bid_econ,
                False,
                bid_econ.expected_net_usdc - bid_exec_penalty - bid_taker_leakage_usdc,
                bid_exec_penalty,
                bid_directional_edge_ps,
                bid_directional_edge_usdc,
                fair_price,
                bid_fee_ps + bid_taker_leakage_ps,
                bid_exec_penalty_ps + bid_adverse_ps,
                bid_exec_components,
            )
            
        if allowed_sell:
            robust_ask_net = ask_econ.expected_net_usdc - ask_exec_penalty
            side_plan["sell"] = (
                quote_ask,
                ask_econ,
                robust_ask_net >= self.config.maker_min_expected_net_usdc,
                robust_ask_net,
                ask_exec_penalty,
                ask_directional_edge_ps,
                ask_directional_edge_usdc,
                fair_price,
                ask_fee_ps,
                ask_exec_penalty_ps + ask_adverse_ps,
                ask_exec_components,
            )
        else:
            side_plan["sell"] = (
                quote_ask,
                ask_econ,
                False,
                ask_econ.expected_net_usdc - ask_exec_penalty,
                ask_exec_penalty,
                ask_directional_edge_ps,
                ask_directional_edge_usdc,
                fair_price,
                ask_fee_ps,
                ask_exec_penalty_ps + ask_adverse_ps,
                ask_exec_components,
            )

        return side_plan
