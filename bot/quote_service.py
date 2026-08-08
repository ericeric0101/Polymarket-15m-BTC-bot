from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Any, Callable, Optional

from bot.entry_quality import evaluate_entry_quality_adjustment
from bot.probability_calibration import fractional_kelly_stake_fraction
from bot.models import QuoteIntentState, QuoteMode


def should_emit_edge_observation(
    instrument_key: str,
    signature: tuple[Any, ...],
    now_ts: float,
    last_signature_by_inst: dict[str, tuple[Any, ...]],
    last_ts_by_inst: dict[str, float],
    min_interval_sec: float = 1.0,
) -> bool:
    """Throttle duplicate edge snapshots without suppressing quote changes."""
    key = str(instrument_key)
    previous_signature = last_signature_by_inst.get(key)
    previous_ts = float(last_ts_by_inst.get(key, 0.0))
    if previous_signature == signature and float(now_ts) - previous_ts < float(min_interval_sec):
        return False
    last_signature_by_inst[key] = signature
    last_ts_by_inst[key] = float(now_ts)
    return True


@dataclass(frozen=True)
class QuotePlan:
    price: Decimal
    quantity: Decimal
    allowed: bool
    robust_net_usdc: Decimal
    execution_penalty_usdc: Decimal
    directional_edge_per_share: Decimal
    directional_edge_usdc: Decimal
    fair_price: Decimal
    fee_per_share: Decimal
    other_cost_per_share: Decimal


def parse_quote_plan(quote_data: Any) -> QuotePlan | None:
    if not isinstance(quote_data, (tuple, list)) or len(quote_data) < 10:
        return None
    try:
        return QuotePlan(
            price=Decimal(str(quote_data[0])),
            quantity=Decimal(str(quote_data[1].shares if hasattr(quote_data[1], "shares") else quote_data[1])),
            allowed=bool(quote_data[2]),
            robust_net_usdc=Decimal(str(quote_data[3])),
            execution_penalty_usdc=Decimal(str(quote_data[4])),
            directional_edge_per_share=Decimal(str(quote_data[5])),
            directional_edge_usdc=Decimal(str(quote_data[6])),
            fair_price=Decimal(str(quote_data[7])),
            fee_per_share=Decimal(str(quote_data[8])),
            other_cost_per_share=Decimal(str(quote_data[9])),
        )
    except (ArithmeticError, TypeError, ValueError):
        return None


@dataclass
class QuoteInstrumentContext:
    inst_id: Any
    inst_key: str
    quote: tuple[Decimal, Decimal] | None
    fair: Decimal | None
    instrument: Any
    tick: Decimal
    token_id: str | None
    quote_ts: float | None
    dynamic_fee_rate: Decimal | None
    fee_rate_val: Decimal
    bid_levels: Any
    ask_levels: Any
    bid_depth: Any
    ask_depth: Any
    diag_context: dict[str, Any]


@dataclass
class BuyEntryEvaluation:
    skip: bool
    min_expected_net_usdc: Decimal
    entry_mode: str = "value"  # "value" or "trend"
    size_multiplier: Decimal = Decimal("1")
    event_type: str = ""
    reason: str = ""
    payload: dict[str, Any] | None = None


def extract_instrument_tick(instrument: Any, default_tick: str = "0.01") -> Decimal:
    tick = Decimal(default_tick)
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
            tick = Decimal(default_tick)
    if tick <= 0:
        tick = Decimal(default_tick)
    return tick


def build_directional_snapshot(desired: dict[str, Any]) -> dict[str, Any]:
    return {
        "directional_edge_ps": desired.get("directional_edge_ps"),
        "directional_edge_usdc": desired.get("directional_edge_usdc"),
        "p_fair": desired.get("p_fair"),
        "fee_ps": desired.get("fee_ps"),
        "other_cost_ps": desired.get("other_cost_ps"),
        "exec_penalty_usdc": desired.get("exec_penalty"),
        "robust_net_usdc": desired.get("robust_net"),
        "planned_best_bid": desired.get("planned_best_bid"),
        "planned_best_ask": desired.get("planned_best_ask"),
        "planned_quote_ts": desired.get("planned_quote_ts"),
        "entry_mode": desired.get("entry_mode", "value"),
        "size_multiplier": desired.get("size_multiplier", Decimal("1")),
        "weak_pfair_size_adjustment": desired.get("weak_pfair_size_adjustment"),
        "high_entry_price_size_adjustment": desired.get("high_entry_price_size_adjustment"),
        "external_entry_confirmation": desired.get("external_entry_confirmation"),
        "external_entry_confirmation_size_adjustment": desired.get("external_entry_confirmation_size_adjustment"),
        "smart_money_confirmation": desired.get("smart_money_confirmation"),
        "smart_money_size_adjustment": desired.get("smart_money_size_adjustment"),
        "entry_quality": desired.get("entry_quality"),
        "entry_quality_quote_price_cap": desired.get("entry_quality_quote_price_cap"),
        "tail_protect_tp": bool(desired.get("tail_protect_tp", False)),
        "tail_protect_tp_price": desired.get("tail_protect_tp_price"),
        "target_qty_override": desired.get("target_qty_override"),
    }


def apply_weak_pfair_size_adjustment(
    *,
    desired_entry: dict[str, Any],
    side: str,
    enabled: bool,
    lower: Decimal,
    upper: Decimal,
    multiplier: Decimal,
) -> dict[str, Any]:
    if side != "buy" or not enabled or not desired_entry.get("should_quote", False):
        return desired_entry
    if multiplier <= 0 or multiplier >= 1:
        return desired_entry
    p_fair_raw = desired_entry.get("p_fair")
    if p_fair_raw is None:
        return desired_entry
    try:
        p_fair = Decimal(str(p_fair_raw))
    except Exception:
        return desired_entry
    if not (lower <= p_fair <= upper):
        return desired_entry

    prior_multiplier = Decimal(str(desired_entry.get("size_multiplier", Decimal("1")) or "1"))
    adjusted_multiplier = max(Decimal("0"), prior_multiplier * multiplier)
    desired_entry["size_multiplier"] = adjusted_multiplier
    desired_entry["weak_pfair_size_adjustment"] = {
        "p_fair": p_fair,
        "lower": lower,
        "upper": upper,
        "multiplier": multiplier,
        "prior_size_multiplier": prior_multiplier,
        "adjusted_size_multiplier": adjusted_multiplier,
    }
    diag_reason = str(desired_entry.get("diag_reason", "") or "")
    adjustment_reason = (
        f"weak_pfair_size_adjust p_fair={float(p_fair):.4f} "
        f"in [{float(lower):.2f},{float(upper):.2f}] "
        f"size_mult={float(prior_multiplier):.3f}->{float(adjusted_multiplier):.3f}"
    )
    desired_entry["diag_reason"] = (
        f"{diag_reason}; {adjustment_reason}" if diag_reason else adjustment_reason
    )
    return desired_entry


def apply_high_entry_price_size_adjustment(
    *,
    desired_entry: dict[str, Any],
    side: str,
    enabled: bool,
    threshold: Decimal,
    multiplier: Decimal,
) -> dict[str, Any]:
    if side != "buy" or not enabled or not desired_entry.get("should_quote", False):
        return desired_entry
    if threshold <= 0 or multiplier <= 0 or multiplier >= 1:
        return desired_entry
    price_raw = desired_entry.get("price")
    if price_raw is None:
        return desired_entry
    try:
        entry_price = Decimal(str(price_raw))
    except Exception:
        return desired_entry
    if entry_price <= threshold:
        return desired_entry

    prior_multiplier = Decimal(str(desired_entry.get("size_multiplier", Decimal("1")) or "1"))
    adjusted_multiplier = max(Decimal("0"), prior_multiplier * multiplier)
    desired_entry["size_multiplier"] = adjusted_multiplier
    desired_entry["high_entry_price_size_adjustment"] = {
        "entry_price": entry_price,
        "threshold": threshold,
        "multiplier": multiplier,
        "prior_size_multiplier": prior_multiplier,
        "adjusted_size_multiplier": adjusted_multiplier,
    }
    diag_reason = str(desired_entry.get("diag_reason", "") or "")
    adjustment_reason = (
        f"high_entry_price_size_adjust entry={float(entry_price):.4f} "
        f"> {float(threshold):.2f} "
        f"size_mult={float(prior_multiplier):.3f}->{float(adjusted_multiplier):.3f}"
    )
    desired_entry["diag_reason"] = (
        f"{diag_reason}; {adjustment_reason}" if diag_reason else adjustment_reason
    )
    return desired_entry


def apply_fractional_kelly_sizing(
    *,
    desired_entry: dict[str, Any],
    side: str,
    enabled: bool,
    available_collateral_usdc: Decimal | None,
    fraction: Decimal,
    max_collateral_fraction: Decimal,
    base_quantity: Decimal | None = None,
) -> dict[str, Any]:
    """Cap a BUY by conservative fractional-Kelly collateral budget."""
    if side != "buy" or not enabled or not desired_entry.get("should_quote", False):
        return desired_entry
    if available_collateral_usdc is None or available_collateral_usdc <= 0:
        return desired_entry
    try:
        price = Decimal(str(desired_entry.get("price")))
        probability = Decimal(str(desired_entry.get("p_fair")))
        multiplier = max(Decimal("0"), Decimal(str(desired_entry.get("size_multiplier", "1"))))
    except Exception:
        return desired_entry
    if price <= 0:
        return desired_entry
    stake_fraction = fractional_kelly_stake_fraction(
        probability=probability,
        entry_price=price,
        fraction=fraction,
    )
    stake_fraction = min(stake_fraction, max(Decimal("0"), max_collateral_fraction))
    if stake_fraction <= 0:
        desired_entry["should_quote"] = False
        desired_entry["diag_reason"] = "kelly_no_positive_calibrated_edge"
        desired_entry["kelly_sizing"] = {"stake_fraction": Decimal("0")}
        return desired_entry
    max_qty = (available_collateral_usdc * stake_fraction) / price
    existing_target = desired_entry.get("target_qty_override")
    unadjusted_target = existing_target if existing_target is not None else base_quantity
    reduced_target = Decimal(str(unadjusted_target)) * multiplier if unadjusted_target is not None else None
    # Kelly is a risk cap, never a mechanism to increase configured size.
    desired_entry["target_qty_override"] = min(max_qty, reduced_target) if reduced_target is not None else max_qty
    # The previously applied high/weak multiplier is embodied in the target.
    desired_entry["size_multiplier"] = Decimal("1")
    desired_entry["kelly_sizing"] = {
        "stake_fraction": stake_fraction,
        "available_collateral_usdc": available_collateral_usdc,
        "target_qty": desired_entry["target_qty_override"],
        "model_fraction": fraction,
    }
    return desired_entry


def compute_loss_sell_policy(
    *,
    thesis_weakened: bool,
    offside_confirmed: bool,
    confirmed_adverse_exit_active: bool,
    spot_still_supports_position: bool,
    stop_loss_pending_active: bool,
    stop_loss_regime_armed: bool,
    decision_phase: str,
    decision_regime: str,
    hold_sec: float,
    loss_sell_min_hold_sec: float,
    emergency_window: bool,
    time_left_sec: float | None,
    absolute_last_resort_sec: float,
    true_last_resort_sec: float,
) -> tuple[bool, str]:
    thesis_bad = (
        thesis_weakened
        or offside_confirmed
        or stop_loss_pending_active
        or confirmed_adverse_exit_active
    )
    urgent_override = (
        decision_phase == "EXIT"
        or confirmed_adverse_exit_active
        or stop_loss_pending_active
        or (thesis_bad and stop_loss_regime_armed)
    )
    de_risk_active = decision_phase == "DE_RISK" and not spot_still_supports_position
    allow_regime_loss_sell = (
        (urgent_override or (de_risk_active and thesis_bad))
        and (urgent_override or hold_sec >= float(loss_sell_min_hold_sec))
    )
    allow_emergency_with_thesis = emergency_window and allow_regime_loss_sell
    thesis_good = (not thesis_bad) and decision_phase in ("HOLD", "PROBE", "")
    in_last_resort_window = (
        time_left_sec is not None
        and absolute_last_resort_sec > 0
        and time_left_sec < absolute_last_resort_sec
    )
    in_true_last_resort = (
        time_left_sec is not None
        and true_last_resort_sec > 0
        and time_left_sec < true_last_resort_sec
    )
    allow_absolute_last_resort = (
        (in_true_last_resort and not thesis_good)
        or (in_last_resort_window and not thesis_good)
    )
    allow_loss_sell = (
        allow_regime_loss_sell
        or allow_emergency_with_thesis
        or allow_absolute_last_resort
    )
    if not allow_loss_sell:
        return False, ""
    if decision_phase == "EXIT":
        return True, f"state_machine_exit:{decision_regime or 'n/a'}"
    if confirmed_adverse_exit_active:
        return True, "confirmed_adverse_exit"
    if allow_regime_loss_sell:
        if stop_loss_regime_armed:
            return True, "armed_thesis_bad"
        return True, "forced_exit_thesis_bad"
    if in_true_last_resort:
        return True, f"true_last_resort(<{true_last_resort_sec:.0f}s)"
    if allow_absolute_last_resort:
        return True, f"last_resort_thesis_bad(<{absolute_last_resort_sec:.0f}s)"
    return True, "emergency_with_thesis"


def resolve_quote_intent_state(
    *,
    side: str,
    desired_should_quote: bool,
    tail_inventory_exit_context: bool,
    adverse_exit_context: bool,
    stop_loss_pending_active: bool,
    recycle_sell_ready: bool,
    recycle_profit_candidate: bool,
    active_side_locked: bool,
    active_side_value: str,
    inst_id: Any,
    active_instrument_id: Any,
    locked_side_entry_blocked: bool,
) -> QuoteIntentState:
    if side == "sell":
        if tail_inventory_exit_context:
            return QuoteIntentState(
                quote_mode=QuoteMode.HARD_EXIT,
                sell_intent="TAIL_EXIT",
                hard_exit_allowed=True,
            )
        if adverse_exit_context or stop_loss_pending_active:
            return QuoteIntentState(
                quote_mode=QuoteMode.HARD_EXIT,
                sell_intent="FORCED_EXIT",
                hard_exit_allowed=True,
            )
        if recycle_sell_ready or recycle_profit_candidate:
            return QuoteIntentState(
                quote_mode=QuoteMode.RECYCLE_LOCKED_SIDE,
                sell_intent="RECYCLE_PROFIT",
                hard_exit_allowed=False,
            )
        return QuoteIntentState(quote_mode=QuoteMode.OBSERVE)
    if side == "buy":
        locked_side_matches_instrument = (
            active_side_locked
            and str(active_side_value or "NONE").upper() != "NONE"
            and active_instrument_id is not None
            and str(inst_id) == str(active_instrument_id)
        )
        if desired_should_quote and locked_side_matches_instrument and not locked_side_entry_blocked:
            return QuoteIntentState(quote_mode=QuoteMode.ACQUIRE_LOCKED_SIDE)
    return QuoteIntentState(quote_mode=QuoteMode.OBSERVE)


def shadow_opposes_locked_side(
    *,
    active_side_value: str,
    active_side_locked: bool,
    shadow_payload: dict[str, Any] | None,
) -> bool:
    if (
        not shadow_payload
        or not active_side_locked
        or str(active_side_value or "NONE").upper() == "NONE"
    ):
        return False
    shadow_bias_side = str(shadow_payload.get("shadow_bias_side") or "").upper()
    shadow_score = Decimal(str(shadow_payload.get("shadow_score") or "0"))
    shadow_min_abs = Decimal(str(shadow_payload.get("shadow_min_score_abs") or "0"))
    if not shadow_bias_side or abs(shadow_score) < shadow_min_abs:
        return False
    return shadow_bias_side != str(active_side_value or "NONE").upper()


def locked_side_signal_invalidated(
    *,
    active_side_value: str,
    active_side_locked: bool,
    side_score: Decimal,
    shadow_payload: dict[str, Any] | None,
) -> bool:
    active_side_txt = str(active_side_value or "NONE").upper()
    if not active_side_locked or active_side_txt == "NONE":
        return False
    if shadow_opposes_locked_side(
        active_side_value=active_side_txt,
        active_side_locked=active_side_locked,
        shadow_payload=shadow_payload,
    ):
        return True
    if active_side_txt == "UP":
        return side_score <= 0
    if active_side_txt == "DOWN":
        return side_score >= 0
    return False


def confirmed_adverse_exit(
    *,
    active_side_value: str,
    active_side_locked: bool,
    legacy_thesis_weakened: bool,
    market_consensus: Decimal,
    shadow_payload: dict[str, Any] | None,
    adverse_market_threshold: Decimal = Decimal("0.50"),
) -> bool:
    active_side_txt = str(active_side_value or "NONE").upper()
    if not legacy_thesis_weakened or not active_side_locked or active_side_txt == "NONE":
        return False
    if shadow_opposes_locked_side(
        active_side_value=active_side_txt,
        active_side_locked=active_side_locked,
        shadow_payload=shadow_payload,
    ):
        return True
    if active_side_txt == "UP":
        return market_consensus <= -adverse_market_threshold
    if active_side_txt == "DOWN":
        return market_consensus >= adverse_market_threshold
    return False


def evaluate_buy_entry_controls(
    *,
    side: str,
    bi_side_enabled: bool,
    active_side_locked: bool,
    active_side_value: str,
    side_score: Decimal,
    directional_entry_min_score_abs_new: Decimal,
    directional_first_entry_min_score_abs_new: Decimal,
    first_entry_max_time_left_sec: int = 0,
    locked_side_score_abs: Decimal = Decimal("0"),
    maker_min_expected_net_usdc: Decimal,
    maker_reload_min_expected_net_multiplier: Decimal,
    current_inst_inventory_qty: Decimal,
    maker_reload_inventory_threshold_shares: Decimal,
    current_slug: str,
    inst_id: Any,
    market_buy_count: int = 0,
    # --- Trend-buy params ---
    trend_buy_enabled: bool = False,
    trend_buy_min_score: Decimal = Decimal("0.20"),
    trend_buy_min_net_usdc: Decimal = Decimal("-0.005"),
    active_instrument_id: Any = None,
    time_left_sec: float | None = None,
    trend_buy_min_time_left_sec: float = 300.0,
    best_bid: Decimal | None = None,
    fair: Decimal | None = None,
    trend_buy_max_price_premium_ps: Decimal = Decimal("0.02"),
    candidate_entry_price: Decimal | None = None,
    spot_minus_strike_avg: Decimal | None = None,
    entry_spot_strike_avg_min_abs: Decimal = Decimal("0"),
    entry_fair_edge_min_ps: Decimal = Decimal("0"),
    robust_net_usdc: Decimal | None = None,
    down_high_price_threshold: Decimal = Decimal("1"),
    down_high_price_min_score_abs: Decimal = Decimal("0"),
    down_high_price_min_robust_net_usdc: Decimal = Decimal("0"),
    down_high_price_spot_strike_avg_max: Decimal = Decimal("0"),
    shadow_payload: dict[str, Any] | None = None,
    entry_quality_allow_size_down: bool = False,
    latest_observation_supports_locked_side: bool | None = None,
    locked_side_entry_blocked: bool = False,
    locked_side_entry_block_reason: str = "",
    max_locked_side_position: Decimal = Decimal("999999"),
    inventory_full_behavior: str = "STOP_BUY",
) -> BuyEntryEvaluation:
    min_expected_net_usdc = maker_min_expected_net_usdc
    entry_mode = "value"
    size_multiplier = Decimal("1")
    if side != "buy":
        return BuyEntryEvaluation(
            skip=False,
            min_expected_net_usdc=min_expected_net_usdc,
            entry_mode=entry_mode,
            size_multiplier=size_multiplier,
        )
    if (
        current_inst_inventory_qty >= max_locked_side_position
        and str(inventory_full_behavior or "STOP_BUY").upper() == "STOP_BUY"
    ):
        return BuyEntryEvaluation(
            skip=True,
            min_expected_net_usdc=min_expected_net_usdc,
            entry_mode=entry_mode,
            event_type="ORDER_SKIP_LOCKED_SIDE_POSITION_FULL",
            reason="locked_side_position_full",
            payload={
                "slug": current_slug,
                "instrument_id": str(inst_id),
                "inventory_qty": float(current_inst_inventory_qty),
                "max_locked_side_position": float(max_locked_side_position),
                "behavior": str(inventory_full_behavior or "STOP_BUY").upper(),
            },
        )
    if (
        bi_side_enabled
        and active_side_locked
        and str(active_side_value or "NONE").upper() != "NONE"
        and locked_side_entry_blocked
    ):
        return BuyEntryEvaluation(
            skip=True,
            min_expected_net_usdc=min_expected_net_usdc,
            entry_mode=entry_mode,
            event_type="ORDER_SKIP_LOCKED_SIDE_INVALIDATED",
            reason=str(locked_side_entry_block_reason or "locked_side_invalidated"),
            payload={
                "slug": current_slug,
                "instrument_id": str(inst_id),
                "active_side": active_side_value,
                "side_score": float(side_score),
                "engine": "locked_side_invalidation",
            },
        )
    if (
        current_inst_inventory_qty <= 0
        and int(market_buy_count) <= 0
        and max(abs(side_score), abs(locked_side_score_abs)) < directional_first_entry_min_score_abs_new
    ):
        return BuyEntryEvaluation(
            skip=True,
            min_expected_net_usdc=min_expected_net_usdc,
            entry_mode=entry_mode,
            event_type="ORDER_SKIP_DIRECTIONAL_FIRST_ENTRY_GATE",
            reason="directional_first_entry_gate",
            payload={
                "slug": current_slug,
                "instrument_id": str(inst_id),
                "side_score": float(side_score),
                "locked_side_score_abs": float(abs(locked_side_score_abs)),
                "required_score_abs": float(directional_first_entry_min_score_abs_new),
                "engine": "new_signal",
            },
        )
    if (
        current_inst_inventory_qty <= 0
        and int(market_buy_count) <= 0
        and first_entry_max_time_left_sec > 0
        and time_left_sec is not None
        and time_left_sec > float(first_entry_max_time_left_sec)
    ):
        return BuyEntryEvaluation(
            skip=True,
            min_expected_net_usdc=min_expected_net_usdc,
            entry_mode=entry_mode,
            event_type="ORDER_SKIP_FIRST_ENTRY_TIME_WINDOW",
            reason="first_entry_too_early",
            payload={
                "slug": current_slug,
                "instrument_id": str(inst_id),
                "time_left_sec": float(time_left_sec),
                "max_time_left_sec": int(first_entry_max_time_left_sec),
                "engine": "entry_timing",
            },
        )
    if abs(side_score) < directional_entry_min_score_abs_new:
        return BuyEntryEvaluation(
            skip=True,
            min_expected_net_usdc=min_expected_net_usdc,
            entry_mode=entry_mode,
            event_type="ORDER_SKIP_DIRECTIONAL_ENTRY_GATE",
            reason="directional_entry_gate",
            payload={
                "slug": current_slug,
                "instrument_id": str(inst_id),
                "side_score": float(side_score),
                "required_score_abs": float(directional_entry_min_score_abs_new),
                "engine": "new_signal",
            },
        )
    active_side_txt = str(active_side_value or "NONE").upper()
    if (
        active_side_txt in {"UP", "DOWN"}
        and spot_minus_strike_avg is not None
        and (
            entry_spot_strike_avg_min_abs > 0
            or (active_side_txt == "UP" and spot_minus_strike_avg < 0)
            or (active_side_txt == "DOWN" and spot_minus_strike_avg > 0)
        )
    ):
        spot_avg_supports = (
            spot_minus_strike_avg >= entry_spot_strike_avg_min_abs
            if active_side_txt == "UP"
            else spot_minus_strike_avg <= -entry_spot_strike_avg_min_abs
        )
        if not spot_avg_supports:
            return BuyEntryEvaluation(
                skip=True,
                min_expected_net_usdc=min_expected_net_usdc,
                entry_mode=entry_mode,
                event_type="ORDER_SKIP_ENTRY_SPOT_STRIKE_AVG_GATE",
                reason="entry_spot_strike_avg_gate",
                payload={
                    "slug": current_slug,
                    "instrument_id": str(inst_id),
                    "active_side": active_side_txt,
                    "spot_minus_strike_avg": float(spot_minus_strike_avg),
                    "required_abs": float(entry_spot_strike_avg_min_abs),
                    "engine": "entry_context",
                },
            )
    if (
        candidate_entry_price is not None
        and fair is not None
        and candidate_entry_price > 0
        and fair > 0
        and entry_fair_edge_min_ps > 0
        and (fair - candidate_entry_price) < entry_fair_edge_min_ps
    ):
        return BuyEntryEvaluation(
            skip=True,
            min_expected_net_usdc=min_expected_net_usdc,
            entry_mode=entry_mode,
            event_type="ORDER_SKIP_ENTRY_FAIR_EDGE_GATE",
            reason="entry_fair_edge_gate",
            payload={
                "slug": current_slug,
                "instrument_id": str(inst_id),
                "active_side": active_side_txt,
                "entry_price": float(candidate_entry_price),
                "fair": float(fair),
                "fair_minus_entry": float(fair - candidate_entry_price),
                "required_min": float(entry_fair_edge_min_ps),
                "engine": "entry_context",
            },
        )
    if (
        active_side_txt == "DOWN"
        and candidate_entry_price is not None
        and down_high_price_threshold < 1
        and candidate_entry_price >= down_high_price_threshold
        and (
            abs(side_score) < down_high_price_min_score_abs
            and (robust_net_usdc is None or robust_net_usdc < down_high_price_min_robust_net_usdc)
            and (
                spot_minus_strike_avg is None
                or spot_minus_strike_avg > down_high_price_spot_strike_avg_max
            )
        )
    ):
        return BuyEntryEvaluation(
            skip=True,
            min_expected_net_usdc=min_expected_net_usdc,
            entry_mode=entry_mode,
            event_type="ORDER_SKIP_DOWN_HIGH_PRICE_GATE",
            reason="down_high_price_gate",
            payload={
                "slug": current_slug,
                "instrument_id": str(inst_id),
                "entry_price": float(candidate_entry_price),
                "entry_threshold": float(down_high_price_threshold),
                "side_score": float(side_score),
                "required_score_abs": float(down_high_price_min_score_abs),
                "robust_net_usdc": float(robust_net_usdc) if robust_net_usdc is not None else None,
                "required_robust_net_usdc": float(down_high_price_min_robust_net_usdc),
                "spot_minus_strike_avg": float(spot_minus_strike_avg) if spot_minus_strike_avg is not None else None,
                "required_spot_minus_strike_avg_max": float(down_high_price_spot_strike_avg_max),
                "engine": "entry_context",
            },
        )
    # --- Trend-buy mode detection ---
    _active_side_txt = active_side_txt
    if (
        current_inst_inventory_qty >= max_locked_side_position
        and str(inventory_full_behavior or "STOP_BUY").upper() == "WIDEN_SPREAD"
    ):
        min_expected_net_usdc = (
            maker_min_expected_net_usdc * max(Decimal("1"), maker_reload_min_expected_net_multiplier)
        )
    if (
        trend_buy_enabled
        and active_side_locked
        and _active_side_txt not in ("NONE", "")
        and abs(side_score) >= trend_buy_min_score
        and current_inst_inventory_qty <= 0
        and active_instrument_id is not None
        and str(inst_id) == str(active_instrument_id)
        and (time_left_sec is None or time_left_sec >= trend_buy_min_time_left_sec)
        and _trend_price_premium_ok(
            best_bid=best_bid,
            fair=fair,
            max_premium=trend_buy_max_price_premium_ps,
        )
    ):
        entry_mode = "trend"
        min_expected_net_usdc = trend_buy_min_net_usdc

    if (
        maker_reload_min_expected_net_multiplier > Decimal("1")
        and current_inst_inventory_qty + Decimal("0.000001")
        >= maker_reload_inventory_threshold_shares
    ):
        min_expected_net_usdc = (
            maker_min_expected_net_usdc * maker_reload_min_expected_net_multiplier
        )
        # Reload always uses value mode regardless of trend detection.
        entry_mode = "value"
    quality_adjustment = evaluate_entry_quality_adjustment(
        candidate_entry_price=candidate_entry_price,
        side_score=side_score,
        fair=fair,
        robust_net_usdc=robust_net_usdc,
        spot_minus_strike_avg=spot_minus_strike_avg,
        active_side_value=active_side_txt,
        shadow_payload=shadow_payload,
        allow_size_down=entry_quality_allow_size_down,
    )
    min_expected_net_usdc += quality_adjustment.min_expected_net_uplift_usdc
    size_multiplier = quality_adjustment.size_multiplier
    return BuyEntryEvaluation(
        skip=False,
        min_expected_net_usdc=min_expected_net_usdc,
        entry_mode=entry_mode,
        size_multiplier=size_multiplier,
        payload=quality_adjustment.as_payload(),
    )


def _trend_price_premium_ok(
    best_bid: Decimal | None,
    fair: Decimal | None,
    max_premium: Decimal,
) -> bool:
    """Check that best_bid doesn't exceed fair by more than max_premium."""
    if best_bid is None or fair is None or fair <= 0:
        return False
    return best_bid <= fair + max_premium


def compute_trend_robust_net(
    expected_net: Decimal,
    exec_penalty: Decimal,
    taker_leakage: Decimal,
    trend_penalty_discount: Decimal,
) -> Decimal:
    """Recompute robust_net with discounted exec penalty for trend entries.

    In trend mode the bot accepts thinner edge because the entry thesis is
    directional conviction, not spread-capture.  The execution penalty —
    which models forced-liquidation cost — is discounted because trend
    entries are less likely to need immediate reversal.
    """
    discounted_penalty = exec_penalty * trend_penalty_discount
    return expected_net - discounted_penalty - taker_leakage


def attach_desired_entry_runtime_metadata(
    *,
    desired_entry: dict[str, Any],
    dynamic_fee_rate: Decimal | None,
    min_expected_net_usdc: Decimal,
    quote: tuple[Decimal, Decimal] | None,
    now_ts: float,
) -> dict[str, Any]:
    desired_entry["dynamic_fee_rate"] = dynamic_fee_rate
    desired_entry["min_expected_net_usdc"] = min_expected_net_usdc
    if quote is not None:
        desired_entry["planned_best_bid"] = quote[0]
        desired_entry["planned_best_ask"] = quote[1]
        desired_entry["planned_quote_ts"] = now_ts
    return desired_entry


def apply_entry_quality_quote_placement(
    *,
    desired_entry: dict[str, Any],
    side: str,
    quote: tuple[Decimal, Decimal] | None,
    tick: Decimal,
) -> dict[str, Any]:
    if side != "buy" or not desired_entry.get("should_quote", False) or quote is None:
        return desired_entry
    quality = desired_entry.get("entry_quality")
    quality_d = quality if isinstance(quality, dict) else {}
    placement_mode = str(quality_d.get("entry_quality_quote_placement_mode") or "default")
    if placement_mode not in {"join_bid", "one_tick_above_bid"}:
        return desired_entry

    best_bid, best_ask = quote
    current_price = Decimal(str(desired_entry.get("price", "0") or "0"))
    if current_price <= 0 or best_bid <= 0 or best_ask <= 0 or tick <= 0:
        return desired_entry

    if placement_mode == "join_bid":
        placement_price = best_bid
    else:
        placement_price = best_bid + tick
        if placement_price >= best_ask:
            placement_price = best_bid

    placement_price = max(Decimal("0.01"), placement_price)
    if placement_price >= current_price:
        return desired_entry

    desired_entry["price"] = placement_price
    desired_entry["entry_quality_quote_price_cap"] = placement_price
    diag_reason = str(desired_entry.get("diag_reason", "") or "")
    placement_diag = (
        f"entry_quality_quote_placement {placement_mode} "
        f"{float(current_price):.4f}->{float(placement_price):.4f}"
    )
    desired_entry["diag_reason"] = (
        f"{diag_reason} | {placement_diag}" if diag_reason else placement_diag
    )
    return desired_entry


def maybe_apply_trapped_inventory_recovery(
    *,
    desired_entry: dict[str, Any],
    side: str,
    trapped_inventory_recovery_enabled: bool,
    current_inst_inventory_qty: Decimal,
    trapped_inventory_recovery_min_qty: Decimal,
    maker_exchange_min_shares: Decimal,
    active_side_locked: bool,
    inst_id: Any,
    active_instrument_id: Any,
    latest_observation_supports_locked_side: bool,
    robust_net: Decimal | None,
    max_robust_net_deficit_usdc: Decimal,
    time_left_sec: float | None = None,
) -> dict[str, Any]:
    if (
        side != "buy"
        or not trapped_inventory_recovery_enabled
        or current_inst_inventory_qty <= 0
        or current_inst_inventory_qty + Decimal("0.000001") < max(Decimal("0"), trapped_inventory_recovery_min_qty)
        or current_inst_inventory_qty + Decimal("0.000001") >= maker_exchange_min_shares
        or not active_side_locked
        or inst_id != active_instrument_id
        or not latest_observation_supports_locked_side
    ):
        return desired_entry
    if time_left_sec is not None and time_left_sec < 180.0:
        return desired_entry
    diag_reason = str(desired_entry.get("diag_reason", "") or "")
    if not (
        diag_reason.startswith("side_disabled:post_fill_buy_cooldown")
        or diag_reason.startswith("econ_gate")
        or diag_reason.startswith("side_disabled:edge_gate_buy")
    ):
        return desired_entry
    if isinstance(robust_net, Decimal) and robust_net < -abs(max_robust_net_deficit_usdc):
        return desired_entry
    desired_entry["should_quote"] = True
    desired_entry["diag_reason"] = (
        f"trapped_inventory_recovery qty={float(current_inst_inventory_qty):.6f} "
        f"< min={float(maker_exchange_min_shares):.6f} prev={diag_reason or 'blocked'}"
    )
    desired_entry["entry_mode"] = "topup"
    desired_entry["size_multiplier"] = Decimal("1")
    return desired_entry


def apply_shadow_entry_veto(
    *,
    desired_entry: dict[str, Any],
    side: str,
    entry_mode: str,
    inst_id: Any,
    up_instrument_id: Any,
    down_instrument_id: Any,
    shadow_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    if (
        side != "buy"
        or not desired_entry.get("should_quote", False)
        or entry_mode == "topup"
        or not shadow_payload
    ):
        return desired_entry

    intended_side = None
    intended_bias = None
    if inst_id == up_instrument_id:
        intended_side = "BUY_UP"
        intended_bias = "UP"
    elif inst_id == down_instrument_id:
        intended_side = "BUY_DOWN"
        intended_bias = "DOWN"
    if intended_side is None:
        return desired_entry

    shadow_candidate_side = str(shadow_payload.get("shadow_candidate_side") or "")
    shadow_bias_side = str(shadow_payload.get("shadow_bias_side") or "")
    shadow_score = Decimal(str(shadow_payload.get("shadow_score") or "0"))
    shadow_min_abs = Decimal(str(shadow_payload.get("shadow_min_score_abs") or "0"))

    veto_reason = ""
    if shadow_candidate_side and shadow_candidate_side != intended_side:
        veto_reason = f"shadow_veto_opposite_candidate:{shadow_candidate_side}"
    elif (
        not shadow_candidate_side
        and shadow_bias_side
        and shadow_bias_side != intended_bias
        and abs(shadow_score) >= shadow_min_abs
    ):
        veto_reason = f"shadow_veto_opposite_bias:{shadow_bias_side}@{float(shadow_score):.4f}"
    if not veto_reason:
        return desired_entry

    desired_entry["should_quote"] = False
    desired_entry["diag_reason"] = veto_reason
    return desired_entry


def apply_confirmed_inventory_sell_guard(
    *,
    desired_entry: dict[str, Any],
    side: str,
    confirmed_inventory_qty: Decimal,
    other_held_inventory_qty: Decimal,
) -> dict[str, Any]:
    if side != "sell" or confirmed_inventory_qty > 0:
        return desired_entry
    desired_entry["should_quote"] = False
    if other_held_inventory_qty > 0:
        desired_entry["diag_reason"] = (
            f"confirmed_inventory_zero_current_leg other_held={float(other_held_inventory_qty):.6f}"
        )
    else:
        desired_entry["diag_reason"] = "confirmed_inventory_zero"
    return desired_entry


def preserve_profitable_existing_sell_order(
    *,
    desired_entry: dict[str, Any],
    side: str,
    existing_state: dict[str, Any] | None,
    avg_entry: Decimal,
    maker_sell_cost_protect_fee_buffer_ps: Decimal,
    maker_sell_min_profit_floor_ps: Decimal,
) -> dict[str, Any]:
    if side != "sell" or desired_entry.get("should_quote", False):
        return desired_entry
    diag = str(desired_entry.get("diag_reason", ""))
    if not any(token in diag for token in ("sell_cost_protect", "high_cost_exit_cooldown", "min_profit_floor")):
        return desired_entry
    if existing_state is None:
        return desired_entry
    existing_price = Decimal(str(existing_state.get("price", 0)))
    cost_floor = avg_entry + maker_sell_cost_protect_fee_buffer_ps + maker_sell_min_profit_floor_ps
    if existing_price < cost_floor:
        return desired_entry
    desired_entry["should_quote"] = True
    desired_entry["price"] = existing_price
    desired_entry["diag_reason"] = (
        f"sell_preserved existing={float(existing_price):.4f} "
        f">= floor={float(cost_floor):.4f} "
        f"(new_blocked: {diag})"
    )
    return desired_entry


def preserve_recent_loss_sell_order(
    *,
    desired_entry: dict[str, Any],
    side: str,
    existing_state: dict[str, Any] | None,
    now_ts: float,
    loss_sell_reprice_min_interval_sec: float,
) -> dict[str, Any]:
    if side != "sell" or not desired_entry.get("should_quote", False):
        return desired_entry
    loss_sell_reason = str(desired_entry.get("loss_sell_reason", "") or "")
    if not loss_sell_reason:
        return desired_entry
    if existing_state is None or not existing_state.get("loss_sell_reason"):
        return desired_entry
    existing_price = Decimal(str(existing_state.get("price", 0) or 0))
    new_price = Decimal(str(desired_entry.get("price", 0) or 0))
    created_ts = float(existing_state.get("created_ts", 0.0) or 0.0)
    if existing_price <= 0 or new_price <= 0 or created_ts <= 0:
        return desired_entry
    if new_price >= existing_price:
        return desired_entry
    if loss_sell_reprice_min_interval_sec <= 0:
        return desired_entry
    age_sec = max(0.0, now_ts - created_ts)
    if age_sec >= float(loss_sell_reprice_min_interval_sec):
        return desired_entry
    desired_entry["price"] = existing_price
    desired_entry["diag_reason"] = (
        f"loss_sell_reprice_hold existing={float(existing_price):.4f} "
        f"> new={float(new_price):.4f} age={age_sec:.1f}s"
    )
    return desired_entry


def apply_forced_exit_sell_pricing(
    *,
    desired_entry: dict[str, Any],
    side: str,
    avg_entry: Decimal,
    fair: Optional[Decimal],
    best_bid: Decimal,
    best_ask: Decimal,
    tick: Decimal,
    maker_sell_cost_protect_fee_buffer_ps: Decimal,
    maker_sell_min_profit_floor_ps: Decimal,
    exit_decision_reason: str,
    allow_loss_exit_below_cost_floor: bool = False,
) -> dict[str, Any]:
    if side != "sell" or not desired_entry.get("should_quote", False):
        return desired_entry
    if fair is None or fair <= 0 or best_bid <= 0 or best_ask <= 0 or avg_entry <= 0:
        return desired_entry
    if tick <= 0:
        tick = Decimal("0.01")

    try:
        limit_price = Decimal(str(desired_entry.get("price", "0")))
    except Exception:
        return desired_entry
    if limit_price <= 0:
        return desired_entry

    cost_floor = avg_entry + maker_sell_cost_protect_fee_buffer_ps + maker_sell_min_profit_floor_ps
    fair_edge = max(Decimal("0"), fair - best_bid)
    if allow_loss_exit_below_cost_floor:
        target_floor = max(Decimal("0.01"), best_bid + tick)
        target_ceiling = min(best_ask, max(Decimal("0.01"), fair))
        target_price = max(target_floor, target_ceiling)
    else:
        if fair_edge <= 0:
            return desired_entry
        # Tail exits can still be patient enough to monetize the observed edge,
        # but they should not fall back to generic recycle pricing.
        target_above_ask = best_ask + max(tick, fair_edge * Decimal("0.75"))
        fair_ceiling = max(cost_floor, fair - tick)
        target_price = min(target_above_ask, fair_ceiling)
        target_price = max(cost_floor, best_bid + tick, target_price)
    target_price = (target_price / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    target_price = max(Decimal("0.01"), min(Decimal("0.99"), target_price))
    if target_price == limit_price:
        return desired_entry

    prev_reason = str(desired_entry.get("diag_reason", "") or "")
    desired_entry["price"] = target_price
    desired_entry["diag_reason"] = (
        f"forced_exit_price old={float(limit_price):.4f} "
        f"new={float(target_price):.4f} fair={float(fair):.4f} "
        f"bid={float(best_bid):.4f} ask={float(best_ask):.4f} "
        f"edge={float(fair_edge):.4f} reason={exit_decision_reason}"
        f" below_cost={'1' if allow_loss_exit_below_cost_floor else '0'}"
        + (f" prev={prev_reason}" if prev_reason else "")
    )
    return desired_entry


def apply_locked_side_recycle_sell_pricing(
    *,
    desired_entry: dict[str, Any],
    side: str,
    avg_entry: Decimal,
    fair: Optional[Decimal],
    best_bid: Decimal,
    best_ask: Decimal,
    tick: Decimal,
    maker_sell_cost_protect_fee_buffer_ps: Decimal,
    maker_sell_min_profit_floor_ps: Decimal,
    recycle_sell_discount_ps: Decimal,
) -> dict[str, Any]:
    if side != "sell" or not desired_entry.get("should_quote", False):
        return desired_entry
    if avg_entry <= 0 or fair is None or fair <= 0 or best_bid <= 0 or best_ask <= 0:
        return desired_entry
    if str(desired_entry.get("loss_sell_reason", "") or ""):
        return desired_entry
    if tick <= 0:
        tick = Decimal("0.01")

    try:
        limit_price = Decimal(str(desired_entry.get("price", "0") or "0"))
    except Exception:
        return desired_entry
    if limit_price <= 0:
        return desired_entry

    cost_floor = avg_entry + maker_sell_cost_protect_fee_buffer_ps + maker_sell_min_profit_floor_ps
    passive_anchor = max(best_bid + tick, best_ask)
    recycle_price = max(cost_floor, passive_anchor, fair - max(Decimal("0"), recycle_sell_discount_ps))
    recycle_price = (recycle_price / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    recycle_price = max(Decimal("0.01"), min(Decimal("0.99"), recycle_price))
    if recycle_price == limit_price:
        return desired_entry

    prev_reason = str(desired_entry.get("diag_reason", "") or "")
    desired_entry["price"] = recycle_price
    desired_entry["diag_reason"] = (
        f"recycle_sell_price old={float(limit_price):.4f} new={float(recycle_price):.4f} "
        f"fair={float(fair):.4f} bid={float(best_bid):.4f} ask={float(best_ask):.4f}"
        + (f" prev={prev_reason}" if prev_reason else "")
    )
    return desired_entry


def apply_reload_edge_guard(
    *,
    desired_entry: dict[str, Any],
    side: str,
    current_inst_inventory_qty: Decimal,
    maker_reload_inventory_threshold_shares: Decimal,
    maker_reload_min_directional_edge_ps: Decimal,
) -> dict[str, Any]:
    if (
        side != "buy"
        or current_inst_inventory_qty + Decimal("0.000001") < maker_reload_inventory_threshold_shares
    ):
        return desired_entry
    directional_edge_ps = desired_entry.get("directional_edge_ps")
    desired_entry["reload_min_directional_edge_ps"] = maker_reload_min_directional_edge_ps
    if (
        isinstance(directional_edge_ps, Decimal)
        and directional_edge_ps < maker_reload_min_directional_edge_ps
    ):
        desired_entry["should_quote"] = False
        desired_entry["diag_reason"] = (
            f"reload_edge_gate directional_edge_ps={float(directional_edge_ps):.6f} "
            f"< min={float(maker_reload_min_directional_edge_ps):.6f}"
        )
    return desired_entry


def reconcile_unwanted_quotes(
    active_maker_orders: dict[str, Any],
    desired_quotes: dict[str, dict[str, Any]],
    target_inst_set: set[str],
    now_ts: float,
    cancel_cooldown_sec: float,
    gate_block_grace_sec: float,
    reason_family_fn: Callable[[str], str],
    cancel_order_fn: Callable[[str, str], None],
    gate_block_since_by_order_key: dict[str, float],
    gate_block_reason_by_order_key: dict[str, str],
    gate_last_cancel_ts_by_order_key: dict[str, float],
) -> None:
    for order_key, state in list(active_maker_orders.items()):
        state_inst = str(state.get("instrument_id", "") or "")
        if state_inst not in target_inst_set:
            continue
        desired = desired_quotes.get(order_key)
        if desired is None:
            cancel_order_fn(order_key, "risk:no_desired_quote")
            continue
        if bool(desired.get("should_quote", False)):
            gate_block_since_by_order_key.pop(order_key, None)
            gate_block_reason_by_order_key.pop(order_key, None)
            continue
        if bool(desired.get("force_cancel_existing", False)):
            gate_block_reason_by_order_key.pop(order_key, None)
            gate_block_since_by_order_key.pop(order_key, None)
            gate_last_cancel_ts_by_order_key[order_key] = now_ts
            cancel_order_fn(order_key, f"risk:{str(desired.get('diag_reason', 'hold_cancel'))}")
            continue

        reason = str(desired.get("diag_reason", "risk") or "risk")
        reason_family = reason_family_fn(reason)
        if reason_family == "sell_pause":
            continue

        soft_block = reason_family in {
            "econ_gate",
            "reduce_only",
            "reduce_only_tail_guard",
            "balance_forced_sell_only",
            "side_disabled",
        }
        if soft_block:
            prev_reason = gate_block_reason_by_order_key.get(order_key, "")
            if prev_reason != reason_family:
                gate_block_reason_by_order_key[order_key] = reason_family
                gate_block_since_by_order_key[order_key] = now_ts
            blocked_for = now_ts - float(gate_block_since_by_order_key.get(order_key, now_ts))
            if blocked_for < float(gate_block_grace_sec):
                continue
        else:
            gate_block_reason_by_order_key.pop(order_key, None)
            gate_block_since_by_order_key.pop(order_key, None)

        last_cancel = float(gate_last_cancel_ts_by_order_key.get(order_key, 0.0))
        if now_ts - last_cancel < float(cancel_cooldown_sec):
            continue
        gate_last_cancel_ts_by_order_key[order_key] = now_ts
        cancel_order_fn(order_key, f"risk:{reason}")


def log_no_quote_diagnostics(
    submitted_attempts: int,
    target_instruments: list[Any],
    desired_quotes: dict[str, dict[str, Any]],
    diag_context_by_inst: dict[str, dict[str, Any]],
    now_ts: float,
    no_quote_diag_interval_sec: float,
    phase_value: str,
    instrument_key_fn: Callable[[Any], str],
    active_order_keys_fn: Callable[..., list[str]],
    last_no_quote_diag_ts_by_inst: dict[str, float],
    logger_info_fn: Callable[[str], None],
    reason_family_fn: Optional[Callable[[str], str]] = None,
    strategy_event_fn: Optional[Callable[[str, dict[str, Any]], None]] = None,
) -> None:
    if submitted_attempts != 0:
        return
    for inst_id in target_instruments:
        inst_key = instrument_key_fn(inst_id)
        per_inst = [
            desired for desired in desired_quotes.values()
            if instrument_key_fn(desired.get("instrument_id")) == inst_key
        ]
        if any(bool(desired.get("should_quote", False)) for desired in per_inst):
            continue
        if active_order_keys_fn(instrument_id=inst_id):
            continue
        last_diag = float(last_no_quote_diag_ts_by_inst.get(inst_key, 0.0))
        if now_ts - last_diag < float(no_quote_diag_interval_sec):
            continue
        last_no_quote_diag_ts_by_inst[inst_key] = now_ts
        ctx = diag_context_by_inst.get(inst_key, {})
        blocked = ", ".join(
            f"{desired.get('side')}={desired.get('diag_reason') or 'blocked'}"
            for desired in per_inst
        ) if per_inst else str(ctx.get("reason", "no_quote_tick"))
        fair = ctx.get("fair")
        bid = ctx.get("bid")
        ask = ctx.get("ask")
        fee_rate = ctx.get("fee_rate")
        msg_parts = [
            f"NO_QUOTE diagnostic: inst={inst_key}",
            f"phase={phase_value}",
        ]
        if isinstance(fair, Decimal):
            msg_parts.append(f"fair={float(fair):.4f}")
        if isinstance(bid, Decimal) and isinstance(ask, Decimal):
            msg_parts.append(f"bid={float(bid):.4f}")
            msg_parts.append(f"ask={float(ask):.4f}")
        if isinstance(fee_rate, Decimal):
            msg_parts.append(f"fee_rate={float(fee_rate):.6f}")
        msg_parts.append(f"blocked={blocked}")
        logger_info_fn(" ".join(msg_parts))
        if strategy_event_fn is not None:
            buy_desired = next((desired for desired in per_inst if str(desired.get("side")) == "buy"), None)
            primary = buy_desired or (per_inst[0] if per_inst else None)
            primary_reason = str(primary.get("diag_reason", "")) if primary else ""
            family = reason_family_fn(primary_reason) if (reason_family_fn and primary_reason) else ""
            event_type = ""
            if family == "econ_gate":
                event_type = "NO_TRADE_ECON_GATE"
            elif family == "reduce_only":
                event_type = "NO_TRADE_REDUCE_ONLY"
            elif family == "reduce_only_tail_guard":
                event_type = "NO_TRADE_REDUCE_ONLY_TAIL_GUARD"
            elif family == "trend_protection":
                event_type = "NO_TRADE_TREND_PROTECTION"
            elif family == "side_disabled" and primary_reason.startswith("side_disabled:edge_gate_buy"):
                event_type = "NO_TRADE_DIRECTIONAL_EDGE_GATE"
            if event_type:
                payload = {
                    "instrument_id": str(inst_id),
                    "blocked": blocked,
                    "primary_reason": primary_reason,
                    "phase": phase_value,
                }
                if isinstance(fair, Decimal):
                    payload["fair"] = float(fair)
                if isinstance(bid, Decimal):
                    payload["bid"] = float(bid)
                if isinstance(ask, Decimal):
                    payload["ask"] = float(ask)
                if isinstance(fee_rate, Decimal):
                    payload["fee_rate"] = float(fee_rate)
                strategy_event_fn(event_type, payload)


def retreat_crossing_buy_quote(
    limit_price: Decimal,
    instrument: Any,
    quote_now: tuple[Decimal, Decimal] | None,
    align_price_fn: Callable[[Decimal, str, Any], Decimal],
    logger_warning_fn: Callable[[str], None],
    logger_info_fn: Callable[[str], None],
) -> Decimal | None:
    if quote_now is None:
        return limit_price
    best_bid_now, best_ask_now = quote_now
    tick = Decimal("0.01")
    try:
        raw_tick = getattr(instrument, "price_increment", None) if instrument is not None else None
        if raw_tick is not None:
            tick = Decimal(str(raw_tick.as_decimal() if hasattr(raw_tick, "as_decimal") else raw_tick))
        elif instrument is not None and hasattr(instrument, "info") and instrument.info:
            min_tick = instrument.info.get("minimum_tick_size")
            if min_tick is not None:
                tick = Decimal(str(min_tick))
    except Exception:
        tick = Decimal("0.01")
    if tick <= 0:
        tick = Decimal("0.01")
    old_limit_price = limit_price
    passive_cap = best_bid_now if best_bid_now > 0 else (best_ask_now - tick)
    if passive_cap <= 0:
        logger_warning_fn(
            f"Skip BUY quote: passive_cap non-positive "
            f"(bid={float(best_bid_now):.4f} ask={float(best_ask_now):.4f} tick={float(tick):.4f})"
        )
        return None
    if old_limit_price <= passive_cap:
        return old_limit_price
    limit_price = align_price_fn(passive_cap, "buy", instrument)
    if limit_price >= best_ask_now or limit_price > passive_cap:
        logger_warning_fn(
            f"Skip aggressive BUY quote {float(old_limit_price):.4f} "
            f"(bid={float(best_bid_now):.4f} ask={float(best_ask_now):.4f}) "
            f"(passive retreat failed -> {float(limit_price):.4f})"
        )
        return None
    logger_info_fn(
        "Adjusted BUY quote to passive maker level: "
        f"{float(old_limit_price):.4f} -> {float(limit_price):.4f} "
        f"(bid={float(best_bid_now):.4f} ask={float(best_ask_now):.4f})"
    )
    return limit_price


def maybe_apply_continuation_entry(
    *,
    desired_entry: dict[str, Any],
    side: str,
    active_side_locked: bool,
    active_side_value: str,
    inst_id: Any,
    active_instrument_id: Any,
    side_score: Decimal,
    locked_for_sec: float,
    time_left_sec: float | None,
    current_inventory_qty: Decimal,
    market_buy_count: int,
    best_bid: Decimal,
    fair: Decimal | None,
    continuation_enabled: bool,
    continuation_size_multiplier: Decimal,
    continuation_min_score_abs: Decimal = Decimal("0.32"),
    continuation_min_locked_sec: float = 20.0,
    continuation_min_time_left_sec: float = 300.0,
    continuation_max_price_premium_ps: Decimal = Decimal("0.02"),
    continuation_min_robust_net_usdc: Decimal = Decimal("-0.025"),
) -> dict[str, Any]:
    if not continuation_enabled or side != "buy":
        return desired_entry
    if bool(desired_entry.get("should_quote", False)):
        desired_entry.setdefault("entry_mode", "value")
        desired_entry.setdefault("size_multiplier", Decimal("1"))
        return desired_entry
    if not active_side_locked or str(active_side_value or "NONE").upper() == "NONE":
        return desired_entry
    if str(inst_id) != str(active_instrument_id):
        return desired_entry
    if current_inventory_qty > 0:
        return desired_entry
    if int(market_buy_count) <= 0:
        return desired_entry
    active_side_txt = str(active_side_value or "NONE").upper()
    if active_side_txt == "UP":
        if side_score < continuation_min_score_abs:
            return desired_entry
    elif active_side_txt == "DOWN":
        if side_score > -continuation_min_score_abs:
            return desired_entry
    else:
        return desired_entry
    if locked_for_sec < float(continuation_min_locked_sec):
        return desired_entry
    if time_left_sec is None or time_left_sec < float(continuation_min_time_left_sec):
        return desired_entry
    if best_bid <= 0 or fair is None or fair <= 0:
        return desired_entry
    robust_net = desired_entry.get("robust_net")
    if not isinstance(robust_net, Decimal) or robust_net < continuation_min_robust_net_usdc:
        return desired_entry
    if best_bid > fair + continuation_max_price_premium_ps:
        return desired_entry
    diag_reason = str(desired_entry.get("diag_reason", "") or "")
    if not (
        diag_reason.startswith("econ_gate")
        or diag_reason.startswith("side_disabled:edge_gate_buy")
    ):
        return desired_entry

    desired_entry["should_quote"] = True
    desired_entry["price"] = best_bid
    desired_entry["entry_mode"] = "continuation"
    desired_entry["size_multiplier"] = max(Decimal("0"), continuation_size_multiplier)
    desired_entry["diag_reason"] = (
        f"continuation_entry locked_for={locked_for_sec:.1f}s "
        f"score={float(side_score):+.4f} "
        f"robust_net={float(robust_net):.6f} "
        f"bid={float(best_bid):.4f} fair={float(fair):.4f}"
    )
    return desired_entry


def apply_sellable_inventory_guard(
    qty_dec: Decimal,
    precision: int,
    sellable_qty: Decimal,
    maker_exchange_min_shares: Decimal,
) -> tuple[Decimal | None, str | None]:
    if sellable_qty < Decimal("0.01"):
        return None, "no_sellable_inventory"
    if sellable_qty + Decimal("0.000001") < qty_dec:
        qty_dec = sellable_qty.quantize(Decimal(str(10 ** (-precision))))
    if qty_dec + Decimal("0.000001") < maker_exchange_min_shares:
        return None, "sellable_below_min_after_reduce"
    return qty_dec, None


def build_limit_order(
    order_factory: Any,
    order_kwargs: dict[str, Any],
    maker_use_post_only: bool,
    maker_post_only_strict: bool,
    logger_error_fn: Callable[[str], None],
    logger_warning_fn: Callable[[str], None],
) -> tuple[Any | None, bool]:
    if maker_use_post_only:
        try:
            return order_factory.limit(**order_kwargs, post_only=True), maker_use_post_only
        except TypeError:
            if maker_post_only_strict:
                logger_error_fn("Order factory does not support post_only while strict mode is enabled; skip quote.")
                return None, maker_use_post_only
            logger_warning_fn("Order factory post_only unsupported; falling back to normal limit order.")
            maker_use_post_only = False
    return order_factory.limit(**order_kwargs), maker_use_post_only


def violates_final_crossing_guard(
    side: str,
    limit_price: Decimal,
    quote: tuple[Decimal, Decimal] | None,
    maker_use_post_only: bool,
    maker_post_only_strict: bool,
    logger_warning_fn: Callable[[str], None],
) -> bool:
    if quote is None:
        return False
    best_bid, best_ask = quote
    if side == "buy" and limit_price >= best_ask:
        logger_warning_fn(f"Skip crossing BUY quote {float(limit_price):.4f} >= ask {float(best_ask):.4f}")
        return True
    if side == "sell" and limit_price <= best_bid:
        logger_warning_fn(f"Skip crossing SELL quote {float(limit_price):.4f} <= bid {float(best_bid):.4f}")
        return True
    return False


def build_active_maker_order_state(
    order: Any,
    econ: Any,
    directional_snapshot: dict[str, Any] | None,
    limit_price: Decimal,
    side: str,
    instrument_id: Any,
    token_id: str | None,
    token_qty: float,
    created_ts: float,
    target_version: int,
    loss_sell_reason: str = "",
) -> dict[str, Any]:
    return {
        "order": order,
        "econ": econ,
        "directional_snapshot": directional_snapshot or {},
        "price": limit_price,
        "side": side,
        "instrument_id": instrument_id,
        "token_id": token_id,
        "quantity": Decimal(str(token_qty)),
        "created_ts": created_ts,
        "target_version": target_version,
        "loss_sell_reason": loss_sell_reason or "",
    }


async def build_quote_instrument_context(
    inst_id: Any,
    normalize_instrument_id_fn: Callable[[Any], Any],
    instrument_key_fn: Callable[[Any], str],
    get_quote_for_instrument_fn: Callable[[Any], tuple[Decimal, Decimal] | None],
    compute_fair_probability_fn: Callable[..., Any],
    cache_instrument_fn: Callable[[Any], Any],
    extract_token_id_fn: Callable[[str], str | None],
    get_dynamic_fee_rate_fn: Callable[..., Any],
    get_orderbook_levels_fn: Callable[[str | None], Any],
    latest_quote_depth_by_inst: dict[str, tuple[Any, Any]],
    maker_econ_fee_rate_decimal: Decimal,
    latest_quote_ts_by_inst: dict[str, float] | None = None,
) -> QuoteInstrumentContext:
    inst_key = instrument_key_fn(inst_id)
    quote_ts = (latest_quote_ts_by_inst or {}).get(str(inst_id))
    quote = get_quote_for_instrument_fn(inst_id)
    if quote is None:
        return QuoteInstrumentContext(
            inst_id=inst_id,
            inst_key=inst_key,
            quote=None,
            fair=None,
            instrument=None,
            tick=Decimal("0.01"),
            token_id=None,
            quote_ts=quote_ts,
            dynamic_fee_rate=None,
            fee_rate_val=maker_econ_fee_rate_decimal,
            bid_levels=None,
            ask_levels=None,
            bid_depth=None,
            ask_depth=None,
            diag_context={"reason": "no_quote_tick"},
        )

    inst_bid, inst_ask = quote
    fair = await compute_fair_probability_fn((inst_bid + inst_ask) / 2, instrument_id=inst_id)
    instrument_for_tick = normalize_instrument_id_fn(inst_id)
    instrument = cache_instrument_fn(instrument_for_tick) if instrument_for_tick else None
    tick = extract_instrument_tick(instrument, default_tick="0.01")

    token_id = extract_token_id_fn(str(inst_id))
    dynamic_fee_rate = await get_dynamic_fee_rate_fn(token_id=token_id)
    bid_levels, ask_levels = await get_orderbook_levels_fn(token_id)
    bid_depth, ask_depth = latest_quote_depth_by_inst.get(str(inst_id), (None, None))
    return QuoteInstrumentContext(
        inst_id=inst_id,
        inst_key=inst_key,
        quote=quote,
        fair=fair,
        instrument=instrument,
        tick=tick,
        token_id=token_id,
        quote_ts=quote_ts,
        dynamic_fee_rate=dynamic_fee_rate,
        fee_rate_val=maker_econ_fee_rate_decimal,
        bid_levels=bid_levels,
        ask_levels=ask_levels,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        diag_context={
            "reason": "ok",
            "fair": fair,
            "bid": inst_bid,
            "ask": inst_ask,
            "fee_rate": maker_econ_fee_rate_decimal,
        },
    )


def build_desired_quote_entry(
    order_key: str,
    side: str,
    inst_id: Any,
    quote_data: tuple[Any, ...],
    side_disable_reason_by_side: dict[str, str],
    reduce_only_reason: str | None,
    reduce_only_tail_sell_block: bool,
    reduce_only_no_new_sell_last_sec: int,
    forced_sell_only: bool,
    min_expected_net_usdc: Decimal,
    now_ts: float,
    sell_pause_until: float,
    is_dry_run_mode: bool,
    sellable_qty: Decimal | None,
    maker_exchange_min_shares: Decimal,
    avg_entry: Decimal,
    emergency_window: bool,
    high_cost_exit_cooldown_enabled: bool,
    high_cost_exit_cooldown_sec: float,
    high_cost_exit_cooldown_until: float,
    maker_sell_cost_protect_enabled: bool,
    maker_sell_cost_protect_fee_buffer_ps: Decimal,
    maker_sell_min_profit_floor_ps: Decimal = Decimal("0"),
    thesis_weakened: bool = False,
    offside_confirmed: bool = False,
    confirmed_adverse_exit_active: bool = False,
    spot_still_supports_position: bool = False,
    stop_loss_pending_active: bool = False,
    stop_loss_regime_armed: bool = False,
    hold_sec: float = 0.0,
    loss_sell_min_hold_sec: float = 60.0,
    # Thesis-aware emergency exit: time_left_sec is used to compute the
    # absolute last-resort window. Pass None if unavailable.
    time_left_sec: float | None = None,
    # Seconds before expiry where loss-selling is thesis-aware: allowed
    # when thesis is bad (weakened/offside/adverse phase), blocked when
    # thesis still supports the position direction.
    absolute_last_resort_sec: float = 60.0,
    # True unconditional last resort: in the genuinely final seconds,
    # loss-selling is always allowed regardless of thesis to prevent
    # holding a dead position into settlement.
    true_last_resort_sec: float = 15.0,
    # --- Trend-buy params (orchestration passes down) ---
    entry_mode: str = "value",
    trend_buy_penalty_discount: Decimal = Decimal("0.50"),
    trend_buy_score: Decimal = Decimal("0"),
    trend_buy_size_multiplier: Decimal = Decimal("1"),
    entry_size_multiplier: Decimal = Decimal("1"),
    entry_quality: dict[str, Any] | None = None,
    decision_phase: str = "",
    decision_regime: str = "",
    decision_pressure: float | None = None,
) -> dict[str, Any]:
    limit_price = quote_data[0]
    econ = quote_data[1]
    should_quote = quote_data[2]
    robust_net = quote_data[3] if len(quote_data) > 3 else None
    exec_penalty = quote_data[4] if len(quote_data) > 4 else None
    directional_edge_ps = quote_data[5] if len(quote_data) > 5 else None
    directional_edge_usdc = quote_data[6] if len(quote_data) > 6 else None
    p_fair = quote_data[7] if len(quote_data) > 7 else None
    fee_ps = quote_data[8] if len(quote_data) > 8 else None
    other_cost_ps = quote_data[9] if len(quote_data) > 9 else None

    diag_reason = ""
    if not should_quote:
        robust_net_val = robust_net if isinstance(robust_net, Decimal) else None
        robust_net_display = float(robust_net) if isinstance(robust_net, Decimal) else float("nan")
        exec_penalty_display = float(exec_penalty) if isinstance(exec_penalty, Decimal) else 0.0
        if robust_net_val is not None and robust_net_val < min_expected_net_usdc:
            diag_reason = (
                f"econ_gate robust_net={float(robust_net_val):.6f} "
                f"(expected_net={float(econ.expected_net_usdc):.6f}, "
                f"exec_penalty={exec_penalty_display:.6f}) "
                f"< min={float(min_expected_net_usdc):.6f}"
            )
        else:
            side_disable_reason = side_disable_reason_by_side.get(side, "unspecified")
            diag_reason = (
                f"side_disabled:{side_disable_reason} robust_net={robust_net_display:.6f} "
                f"(expected_net={float(econ.expected_net_usdc):.6f}, "
                f"exec_penalty={exec_penalty_display:.6f})"
            )

    # --- Trend-buy override: re-evaluate econ gate with discounted penalty ---
    if (
        side == "buy"
        and not should_quote
        and entry_mode == "trend"
        and isinstance(robust_net, Decimal)
        and isinstance(exec_penalty, Decimal)
    ):
        # Separate taker_leakage from the MakerEngine robust_net.
        # MakerEngine computes: robust_net = expected_net - exec_penalty - taker_leakage
        # So: taker_leakage = expected_net - exec_penalty - robust_net
        taker_leakage = econ.expected_net_usdc - exec_penalty - robust_net
        trend_robust = compute_trend_robust_net(
            expected_net=econ.expected_net_usdc,
            exec_penalty=exec_penalty,
            taker_leakage=taker_leakage,
            trend_penalty_discount=trend_buy_penalty_discount,
        )
        if trend_robust >= min_expected_net_usdc:
            should_quote = True
            robust_net = trend_robust
            entry_mode = "trend"
            diag_reason = (
                f"trend_buy_entry score={float(trend_buy_score):+.4f} "
                f"trend_robust_net={float(trend_robust):.6f} "
                f"(discount={float(trend_buy_penalty_discount):.2f} "
                f"orig_penalty={exec_penalty_display:.6f}) "
                f">= min={float(min_expected_net_usdc):.6f}"
            )

    if side == "sell":
        if now_ts < sell_pause_until:
            should_quote = False
            diag_reason = f"sell_pause {sell_pause_until - now_ts:.1f}s"
        if should_quote and not is_dry_run_mode and sellable_qty is not None:
            if sellable_qty + Decimal("0.000001") < maker_exchange_min_shares:
                should_quote = False
                diag_reason = (
                    f"sellable_below_min sellable={float(sellable_qty):.6f} "
                    f"< min={float(maker_exchange_min_shares):.6f}"
                )
        # Allow loss-selling with thesis-aware logic:
        #
        # 1) urgent_override: EXIT phase or an armed thesis-bad stop-loss.
        #    This can bypass the minimum hold timer because the thesis is broken.
        # 2) de_risk_active: DE_RISK phase can loss-sell only after the minimum hold
        #    timer AND only when the thesis is also bad. DE_RISK alone should not
        #    bypass cost protection for a merely noisy/choppy position.
        # 3) emergency_with_thesis: we are in the emergency time window (e.g.
        #    last 120s) AND thesis is also bad. The emergency window alone is
        #    NOT sufficient — it must be confirmed by signal state.
        # 4) absolute_last_resort: genuinely final seconds (<60s). We always
        #    allow loss-selling here to prevent holding a dead position to
        #    settlement, but the window is intentionally narrow.
        #
        # Previously: allow_loss_sell = emergency_window or thesis_weakened or offside_confirmed
        # Problem: emergency_window was purely time-based and would override
        # HOLD_IN_BAND decisions, causing loss sells when direction was correct.
        #
        allow_loss_sell, _loss_sell_reason = compute_loss_sell_policy(
            thesis_weakened=thesis_weakened,
            offside_confirmed=offside_confirmed,
            confirmed_adverse_exit_active=confirmed_adverse_exit_active,
            spot_still_supports_position=spot_still_supports_position,
            stop_loss_pending_active=stop_loss_pending_active,
            stop_loss_regime_armed=stop_loss_regime_armed,
            decision_phase=decision_phase,
            decision_regime=decision_regime,
            hold_sec=hold_sec,
            loss_sell_min_hold_sec=loss_sell_min_hold_sec,
            emergency_window=emergency_window,
            time_left_sec=time_left_sec,
            absolute_last_resort_sec=absolute_last_resort_sec,
            true_last_resort_sec=true_last_resort_sec,
        )
        if (
            should_quote
            and high_cost_exit_cooldown_enabled
            and high_cost_exit_cooldown_sec > 0
            and now_ts < high_cost_exit_cooldown_until
            and avg_entry > 0
            and limit_price < avg_entry
            and not allow_loss_sell
        ):
            should_quote = False
            diag_reason = (
                f"high_cost_exit_cooldown sell={float(limit_price):.4f} "
                f"< avg_entry={float(avg_entry):.4f}"
            )
        if (
            should_quote
            and maker_sell_cost_protect_enabled
            and avg_entry > 0
            and limit_price < (avg_entry + maker_sell_cost_protect_fee_buffer_ps)
            and not allow_loss_sell
        ):
            should_quote = False
            diag_reason = (
                f"sell_cost_protect sell={float(limit_price):.4f} "
                f"< min={float(avg_entry + maker_sell_cost_protect_fee_buffer_ps):.4f} "
                f"phase={decision_phase or '-'} regime={decision_regime or '-'}"
                + (f" pressure={decision_pressure:+.4f}" if decision_pressure is not None else "")
            )
        # Minimum profit floor — block sells that are technically above cost but
        # yield too little profit to justify using a buy quota slot.
        if (
            should_quote
            and maker_sell_min_profit_floor_ps > 0
            and avg_entry > 0
            and limit_price < (avg_entry + maker_sell_cost_protect_fee_buffer_ps + maker_sell_min_profit_floor_ps)
            and not allow_loss_sell
        ):
            should_quote = False
            min_sell = avg_entry + maker_sell_cost_protect_fee_buffer_ps + maker_sell_min_profit_floor_ps
            diag_reason = (
                f"min_profit_floor sell={float(limit_price):.4f} "
                f"< min={float(min_sell):.4f} "
                f"(entry={float(avg_entry):.4f}+fee={float(maker_sell_cost_protect_fee_buffer_ps):.4f}"
                f"+floor={float(maker_sell_min_profit_floor_ps):.4f}) "
                f"phase={decision_phase or '-'} regime={decision_regime or '-'}"
                + (f" pressure={decision_pressure:+.4f}" if decision_pressure is not None else "")
            )

    if reduce_only_reason and side == "buy":
        should_quote = False
        diag_reason = f"reduce_only: {reduce_only_reason}"
    if reduce_only_tail_sell_block and side == "sell":
        has_inventory_to_exit = (
            sellable_qty is not None
            and sellable_qty > Decimal("0")
        )
        if not has_inventory_to_exit:
            should_quote = False
            diag_reason = f"reduce_only_tail_guard: <= {reduce_only_no_new_sell_last_sec}s"
    if forced_sell_only and side == "buy":
        should_quote = False
        diag_reason = "balance_forced_sell_only"

    return {
        "order_key": order_key,
        "side": side,
        "instrument_id": inst_id,
        "price": limit_price,
        "econ": econ,
        "should_quote": should_quote,
        "diag_reason": diag_reason,
        "robust_net": robust_net,
        "exec_penalty": exec_penalty,
        "directional_edge_ps": directional_edge_ps,
        "directional_edge_usdc": directional_edge_usdc,
        "p_fair": p_fair,
        "fee_ps": fee_ps,
        "other_cost_ps": other_cost_ps,
        "entry_mode": entry_mode if side == "buy" else "",
        "size_multiplier": (
            max(Decimal("0"), entry_size_multiplier)
            * (
                max(Decimal("0"), trend_buy_size_multiplier)
                if entry_mode == "trend"
                else Decimal("1")
            )
            if side == "buy"
            else Decimal("1")
        ),
        "entry_quality": entry_quality if side == "buy" else None,
        # Observability: non-empty only when a loss-sell was gated/allowed.
        # Values: "thesis_bad" | "emergency_with_thesis" |
        #         "absolute_last_resort(<Ns)" | "" (no loss-sell override)
        "loss_sell_reason": _loss_sell_reason if side == "sell" else "",
    }


def compute_requote_target_version(
    order_key: str,
    limit_price: Decimal,
    tick: Decimal,
    maker_requote_hysteresis_ticks: int,
    target_anchor_price_by_order_key: dict[str, Decimal],
    target_version_by_order_key: dict[str, int],
) -> int:
    prev_anchor = target_anchor_price_by_order_key.get(order_key)
    target_version = int(target_version_by_order_key.get(order_key, 0))
    if prev_anchor is None:
        target_version += 1
        target_anchor_price_by_order_key[order_key] = limit_price
        target_version_by_order_key[order_key] = target_version
        return target_version
    if abs(limit_price - prev_anchor) >= (maker_requote_hysteresis_ticks * tick):
        target_version += 1
        target_anchor_price_by_order_key[order_key] = limit_price
        target_version_by_order_key[order_key] = target_version
    return target_version


def should_requote_existing_order(
    current: dict[str, Any] | None,
    target_version: int,
    now_ts: float,
    maker_requote_min_age_sec: float,
    side: str = "",
    maker_requote_min_age_sec_sell: float = 0,
    desired_loss_sell_reason: str = "",
) -> bool:
    if not current:
        return False
    if current.get("pending_cancel"):
        return False
    current_loss_sell_reason = str(current.get("loss_sell_reason", "") or "")
    desired_loss_sell_reason = str(desired_loss_sell_reason or "")
    if current_loss_sell_reason and not desired_loss_sell_reason:
        return True
    if current_loss_sell_reason and desired_loss_sell_reason and current_loss_sell_reason != desired_loss_sell_reason:
        return True
    current_target_version = int(current.get("target_version", 0) or 0)
    if current_target_version >= target_version:
        return False
    created_ts = float(current.get("created_ts", 0.0))
    # Use sell-specific min age if this is a sell order and one is configured.
    effective_min_age = maker_requote_min_age_sec
    if side.lower() == "sell" and maker_requote_min_age_sec_sell > 0:
        effective_min_age = maker_requote_min_age_sec_sell
    if effective_min_age > 0 and created_ts > 0 and (now_ts - created_ts) < effective_min_age:
        return False
    return True
