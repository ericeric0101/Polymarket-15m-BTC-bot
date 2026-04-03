from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional


def normalize_quote_mode(raw_mode: str) -> str:
    mode = str(raw_mode or "").strip().lower()
    if mode in {"up_only", "buy_and_sell_up", "up_inventory_exit"}:
        return "both"
    if mode in {"buy_only", "up_buy_only"}:
        return "buy"
    if mode in {"sell", "both_buy"}:
        return "both"
    if mode not in {"both", "buy"}:
        return "both"
    return mode


def initial_side_disable_reasons(quote_mode: str) -> dict[str, str]:
    if quote_mode == "buy":
        return {"sell": "quote_mode_buy_only"}
    return {}


@dataclass
class ReduceOnlyDecision:
    reason: Optional[str]
    tail_sell_block: bool
    tail_sec_left: Optional[float]


@dataclass
class QuotePlanGuardOutcome:
    side_disable_reason_by_side: dict[str, str]
    reduce_only: ReduceOnlyDecision
    buy_cooldown_remaining: Optional[float]
    momentum_trend_pct: Optional[Decimal]
    momentum_buy_threshold_pct: Optional[Decimal]
    momentum_sell_threshold_pct: Optional[Decimal]
    momentum_buy_blocked: bool
    momentum_sell_blocked: bool


def compute_reduce_only_decision(
    phase_value: str,
    fair: Decimal,
    min_fair_price: Decimal,
    max_fair_price: Decimal,
    end_ts: Optional[float],
    now_ts: float,
    min_minutes_to_close: float,
    no_new_sell_last_sec: int,
) -> ReduceOnlyDecision:
    reason: Optional[str] = None
    tail_sell_block = False
    tail_sec_left: Optional[float] = None
    if phase_value == "REDUCE_ONLY":
        time_left_min = ((end_ts - now_ts) / 60.0) if end_ts else 0.0
        reason = f"lifecycle REDUCE_ONLY ({time_left_min:.1f}m left)"
        if end_ts is not None and no_new_sell_last_sec > 0:
            time_left_sec = end_ts - now_ts
            if time_left_sec <= float(no_new_sell_last_sec):
                tail_sell_block = True
                tail_sec_left = max(0.0, time_left_sec)
        return ReduceOnlyDecision(reason=reason, tail_sell_block=tail_sell_block, tail_sec_left=tail_sec_left)
    if fair < min_fair_price:
        return ReduceOnlyDecision(
            reason=f"fair {float(fair):.4f} < min {float(min_fair_price):.4f}",
            tail_sell_block=False,
            tail_sec_left=None,
        )
    if fair > max_fair_price:
        return ReduceOnlyDecision(
            reason=f"fair {float(fair):.4f} > max {float(max_fair_price):.4f}",
            tail_sell_block=False,
            tail_sec_left=None,
        )
    if end_ts is not None:
        time_left_min = (end_ts - now_ts) / 60.0
        if time_left_min < min_minutes_to_close:
            return ReduceOnlyDecision(
                reason=f"only {time_left_min:.1f}m until close",
                tail_sell_block=False,
                tail_sec_left=None,
            )
    return ReduceOnlyDecision(reason=None, tail_sell_block=False, tail_sec_left=None)


def set_side_should_quote(
    side_plan: dict[str, tuple[Any, ...]],
    side_disable_reason_by_side: dict[str, str],
    plan_side: str,
    new_should_quote: bool,
    disable_reason: Optional[str] = None,
) -> None:
    existing = side_plan.get(plan_side)
    if not existing:
        return
    lp = existing[0]
    ec = existing[1]
    robust_net = existing[3] if len(existing) > 3 else None
    exec_penalty = existing[4] if len(existing) > 4 else None
    directional_edge_ps = existing[5] if len(existing) > 5 else None
    directional_edge_usdc = existing[6] if len(existing) > 6 else None
    p_fair = existing[7] if len(existing) > 7 else None
    fee_ps = existing[8] if len(existing) > 8 else None
    other_cost_ps = existing[9] if len(existing) > 9 else None
    side_plan[plan_side] = (
        lp,
        ec,
        new_should_quote,
        robust_net,
        exec_penalty,
        directional_edge_ps,
        directional_edge_usdc,
        p_fair,
        fee_ps,
        other_cost_ps,
    )
    if not new_should_quote and disable_reason:
        side_disable_reason_by_side[plan_side] = str(disable_reason)
    elif new_should_quote:
        side_disable_reason_by_side.pop(plan_side, None)


def apply_quote_plan_guards(
    side_plan: dict[str, tuple[Any, ...]],
    quote_mode: str,
    phase_value: str,
    inventory_delta_shares: Decimal,
    early_sell_only_sec: float,
    time_left_sec_global: Optional[float],
    directional_edge_gate_enabled: bool,
    regime_guard_active: bool,
    min_directional_edge_ps: Decimal,
    min_directional_edge_ps_conservative: Decimal,
    now_ts: float,
    buy_cooldown_until_ts: float,
    momentum_buy_filter_pct: Decimal,
    momentum_sell_filter_pct: Decimal,
    momentum_window_ticks: int,
    momentum_history: list[Decimal],
    fair: Decimal,
    min_fair_price: Decimal,
    max_fair_price: Decimal,
    end_ts: Optional[float],
    min_minutes_to_close: float,
    reduce_only_no_new_sell_last_sec: int,
    forced_sell_only: bool,
    active_side: str = "",
    min_directional_edge_ps_down: Optional[Decimal] = None,
) -> QuotePlanGuardOutcome:
    side_disable_reason_by_side: dict[str, str] = initial_side_disable_reasons(quote_mode)

    if (
        "buy" in side_plan
        and phase_value == "ACTIVE"
        and inventory_delta_shares > 0
        and early_sell_only_sec > 0
        and time_left_sec_global is not None
        and time_left_sec_global <= float(early_sell_only_sec)
    ):
        set_side_should_quote(side_plan, side_disable_reason_by_side, "buy", False, "early_sell_only")

    if directional_edge_gate_enabled and "buy" in side_plan and phase_value == "ACTIVE":
        buy_tuple = side_plan.get("buy")
        buy_edge_ps = buy_tuple[5] if (buy_tuple and len(buy_tuple) > 5) else None
        # Use side-specific edge threshold: DOWN side can have a higher bar
        if min_directional_edge_ps_down is not None and active_side.upper() == "DOWN":
            min_edge_gate = min_directional_edge_ps_down
        elif regime_guard_active:
            min_edge_gate = min_directional_edge_ps_conservative
        else:
            min_edge_gate = min_directional_edge_ps
        if isinstance(buy_edge_ps, Decimal) and buy_edge_ps < min_edge_gate:
            set_side_should_quote(side_plan, side_disable_reason_by_side, "buy", False, "edge_gate_buy")

    buy_cooldown_remaining: Optional[float] = None
    if "buy" in side_plan and now_ts < buy_cooldown_until_ts:
        buy_cooldown_remaining = buy_cooldown_until_ts - now_ts
        set_side_should_quote(
            side_plan,
            side_disable_reason_by_side,
            "buy",
            False,
            f"post_fill_buy_cooldown_{buy_cooldown_remaining:.0f}s",
        )


    momentum_trend_pct: Optional[Decimal] = None
    momentum_buy_blocked = False
    momentum_sell_blocked = False
    momentum_buy_threshold_pct: Optional[Decimal] = None
    momentum_sell_threshold_pct: Optional[Decimal] = None
    if (
        (momentum_buy_filter_pct > 0 or momentum_sell_filter_pct > 0)
        and len(momentum_history) >= momentum_window_ticks
    ):
        recent_px = momentum_history[-1]
        old_px = momentum_history[-momentum_window_ticks]
        momentum_trend_pct = (recent_px - old_px) / old_px if old_px > 0 else Decimal("0")
        if momentum_buy_filter_pct > 0:
            momentum_buy_threshold_pct = -momentum_buy_filter_pct
        if momentum_sell_filter_pct > 0:
            momentum_sell_threshold_pct = momentum_sell_filter_pct
        if (
            momentum_buy_filter_pct > 0
            and momentum_trend_pct <= -momentum_buy_filter_pct
            and "buy" in side_plan
        ):
            momentum_buy_blocked = True
            set_side_should_quote(side_plan, side_disable_reason_by_side, "buy", False, "momentum_buy_block")
        if (
            momentum_sell_filter_pct > 0
            and momentum_trend_pct >= momentum_sell_filter_pct
            and "sell" in side_plan
        ):
            # Only block SELL when there is no existing inventory to exit.
            # If the bot holds shares (inventory_delta_shares > 0), this SELL is
            # reduce-only (exiting a position), and should NOT be blocked by
            # momentum — blocking it traps the bot in a losing position until settlement.
            if inventory_delta_shares <= 0:
                momentum_sell_blocked = True
                set_side_should_quote(side_plan, side_disable_reason_by_side, "sell", False, "momentum_sell_block")

    reduce_only = compute_reduce_only_decision(
        phase_value=phase_value,
        fair=fair,
        min_fair_price=min_fair_price,
        max_fair_price=max_fair_price,
        end_ts=end_ts,
        now_ts=now_ts,
        min_minutes_to_close=min_minutes_to_close,
        no_new_sell_last_sec=reduce_only_no_new_sell_last_sec,
    )
    if reduce_only.reason:
        if "buy" in side_plan:
            set_side_should_quote(side_plan, side_disable_reason_by_side, "buy", False, "reduce_only_buy_block")
        if "sell" in side_plan and reduce_only.tail_sell_block:
            set_side_should_quote(
                side_plan,
                side_disable_reason_by_side,
                "sell",
                False,
                "reduce_only_tail_sell_block",
            )

    if forced_sell_only and "buy" in side_plan:
        set_side_should_quote(
            side_plan,
            side_disable_reason_by_side,
            "buy",
            False,
            "balance_forced_sell_only",
        )

    return QuotePlanGuardOutcome(
        side_disable_reason_by_side=side_disable_reason_by_side,
        reduce_only=reduce_only,
        buy_cooldown_remaining=buy_cooldown_remaining,
        momentum_trend_pct=momentum_trend_pct,
        momentum_buy_threshold_pct=momentum_buy_threshold_pct,
        momentum_sell_threshold_pct=momentum_sell_threshold_pct,
        momentum_buy_blocked=momentum_buy_blocked,
        momentum_sell_blocked=momentum_sell_blocked,
    )
