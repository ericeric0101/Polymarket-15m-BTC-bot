from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass
class FillFollowupResult:
    buy_cooldown_until_ts: float
    market_cycle_realized_net_usdc: Decimal
    recent_fill_pnl_results: list[Any]
    quote_pause_until_ts: float
    triggered_loss_pause: bool
    total_loss: float


@dataclass
class SettlementSummary:
    outcome: str
    active_side: str
    redeem_per_share: float
    redeem_value: float
    inventory_cost: float
    settlement_pnl: float
    cycle_fill_realized: float
    cycle_combined_pnl: float


def build_fill_order_event_payload(
    liquidity_side_raw: Any,
    inventory_delta_shares: Decimal,
    raw_commission_dec: Decimal,
    fill_commission_dec: Decimal,
    filled_econ: Any,
    filled_directional_snapshot: dict[str, Any],
    realized_net_usdc: Decimal | None,
) -> dict[str, Any]:
    return {
        "liquidity_side": str(liquidity_side_raw),
        "inventory_delta_shares": float(inventory_delta_shares),
        "raw_commission_usdc": float(raw_commission_dec),
        "effective_commission_usdc": float(fill_commission_dec),
        "expected_rebate_usdc": (
            float(getattr(filled_econ, "expected_rebate_usdc", 0.0))
            if filled_econ is not None
            else None
        ),
        "expected_spread_capture_usdc": (
            float(getattr(filled_econ, "expected_spread_capture_usdc", 0.0))
            if filled_econ is not None
            else None
        ),
        "directional_edge_ps_submit": (
            float(filled_directional_snapshot.get("directional_edge_ps"))
            if filled_directional_snapshot.get("directional_edge_ps") is not None
            else None
        ),
        "directional_edge_usdc_submit": (
            float(filled_directional_snapshot.get("directional_edge_usdc"))
            if filled_directional_snapshot.get("directional_edge_usdc") is not None
            else None
        ),
        "p_fair_submit": (
            float(filled_directional_snapshot.get("p_fair"))
            if filled_directional_snapshot.get("p_fair") is not None
            else None
        ),
        "fee_ps_submit": (
            float(filled_directional_snapshot.get("fee_ps"))
            if filled_directional_snapshot.get("fee_ps") is not None
            else None
        ),
        "other_cost_ps_submit": (
            float(filled_directional_snapshot.get("other_cost_ps"))
            if filled_directional_snapshot.get("other_cost_ps") is not None
            else None
        ),
        "exec_penalty_usdc_submit": (
            float(filled_directional_snapshot.get("exec_penalty_usdc"))
            if filled_directional_snapshot.get("exec_penalty_usdc") is not None
            else None
        ),
        "robust_net_usdc_submit": (
            float(filled_directional_snapshot.get("robust_net_usdc"))
            if filled_directional_snapshot.get("robust_net_usdc") is not None
            else None
        ),
        "realized_net_usdc": (float(realized_net_usdc) if realized_net_usdc is not None else None),
    }


def apply_fill_followup(
    fill_side_norm: str | None,
    post_fill_buy_cooldown_sec: float,
    buy_cooldown_until_ts: float,
    fill_cooldown_policy: Any,
    realized_net_usdc: Decimal | None,
    market_cycle_realized_net_usdc: Decimal,
    recent_fill_pnl_results: list[Any],
    quote_pause_until_ts: float,
    now_ts: float,
) -> FillFollowupResult:
    triggered_loss_pause = False
    total_loss = 0.0
    if fill_side_norm == "buy" and post_fill_buy_cooldown_sec > 0:
        buy_cooldown_until_ts = fill_cooldown_policy.next_buy_cooldown_until(now_ts)

    if realized_net_usdc is not None:
        market_cycle_realized_net_usdc += Decimal(str(float(realized_net_usdc)))
        (
            recent_fill_pnl_results,
            quote_pause_until_ts,
            triggered_loss_pause,
            total_loss,
        ) = fill_cooldown_policy.register_realized_pnl(
            recent_fill_pnl_results=recent_fill_pnl_results,
            realized_net_usdc=float(realized_net_usdc),
            now_ts=now_ts,
            current_quote_pause_until_ts=quote_pause_until_ts,
        )

    return FillFollowupResult(
        buy_cooldown_until_ts=buy_cooldown_until_ts,
        market_cycle_realized_net_usdc=market_cycle_realized_net_usdc,
        recent_fill_pnl_results=recent_fill_pnl_results,
        quote_pause_until_ts=quote_pause_until_ts,
        triggered_loss_pause=triggered_loss_pause,
        total_loss=total_loss,
    )


def compute_settlement_summary(
    spot: float,
    strike: float,
    inventory_shares: float,
    live_inventory_cost: dict[str, dict[str, Any]],
    market_cycle_realized_net_usdc: Decimal,
    active_side: str = "UP",
) -> SettlementSummary:
    outcome = "UP" if spot >= strike else "DOWN"
    side_txt = str(active_side or "UP").strip().upper()
    redeem_per_share = 1.0 if outcome == side_txt else 0.0
    redeem_value = inventory_shares * redeem_per_share
    inventory_cost = 0.0
    for state in live_inventory_cost.values():
        qty = float(state.get("qty", 0))
        avg_entry = float(state.get("avg_entry_price", 0))
        if qty > 0 and avg_entry > 0:
            inventory_cost += qty * avg_entry
    settlement_pnl = redeem_value - inventory_cost
    cycle_fill_realized = float(market_cycle_realized_net_usdc)
    cycle_combined_pnl = cycle_fill_realized + settlement_pnl
    return SettlementSummary(
        outcome=outcome,
        active_side=side_txt,
        redeem_per_share=redeem_per_share,
        redeem_value=redeem_value,
        inventory_cost=inventory_cost,
        settlement_pnl=settlement_pnl,
        cycle_fill_realized=cycle_fill_realized,
        cycle_combined_pnl=cycle_combined_pnl,
    )
