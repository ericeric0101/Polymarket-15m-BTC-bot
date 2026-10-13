"""Effective exit-policy state for startup logs, run headers and research telemetry.

Pure and read-only.  It makes explicit that the Breaker V2 canonical-TWAP gate only
governs the *conditional absolute-loss breaker*: when that breaker is disabled
(e.g. the Entry V3 forward research profile sets ABSOLUTE_MAX_LOSS_ENABLED=0), the
V2 gate is configured but operationally irrelevant for exits.  The catastrophic
stop and the tail TP are independent of both.
"""
from __future__ import annotations

from typing import Any


def _flag(obj: Any, name: str) -> bool:
    return bool(getattr(obj, name, False))


def effective_exit_policy(strategy: Any) -> dict[str, Any]:
    conditional = _flag(strategy, "absolute_max_loss_enabled")
    v2_gate = _flag(strategy, "absolute_max_loss_twap_authority_gate_enabled")
    return {
        "conditional_breaker_enabled": conditional,
        "conditional_breaker_usdc": str(getattr(strategy, "absolute_max_loss_usdc", "")) or None,
        "breaker_v2_gate_enabled": v2_gate,
        "breaker_v2_effective_for_conditional_exit": bool(conditional and v2_gate),
        "catastrophic_breaker_enabled": _flag(strategy, "catastrophic_stop_loss_enabled"),
        "catastrophic_breaker_usdc": str(getattr(strategy, "catastrophic_stop_loss_usdc", "")) or None,
        "tail_tp_enabled": _flag(strategy, "tail_protect_tp_enabled"),
        "tail_tp_price": str(getattr(strategy, "tail_protect_tp_price", "")) or None,
        "hold_to_redeem_enabled": _flag(strategy, "hold_to_redeem_enabled"),
        "stop_loss_enabled": _flag(strategy, "stop_loss_enabled"),
        "endgame_twap_exit_enabled": _flag(strategy, "endgame_twap_exit_enabled"),
        "taker_exit_enabled": _flag(strategy, "taker_exit_enabled"),
        "invalidation_recovery_enabled": _flag(strategy, "taker_exit_only_after_invalidation"),
        "recovery_exit_ladder_enabled": _flag(strategy, "recovery_exit_ladder_enabled"),
        "maker_urgent_exit_enabled": _flag(strategy, "maker_urgent_exit_enabled"),
    }


def exit_policy_summary(state: dict[str, Any]) -> str:
    return ("Exit policy (effective): "
            f"conditional_breaker_enabled={int(state['conditional_breaker_enabled'])} "
            f"breaker_v2_gate_enabled={int(state['breaker_v2_gate_enabled'])} "
            f"breaker_v2_effective_for_conditional_exit={int(state['breaker_v2_effective_for_conditional_exit'])} "
            f"catastrophic_breaker_enabled={int(state['catastrophic_breaker_enabled'])} "
            f"tail_tp_enabled={int(state['tail_tp_enabled'])} tail_tp_price={state['tail_tp_price']} "
            f"endgame_twap_exit_enabled={int(state['endgame_twap_exit_enabled'])} "
            f"recovery_exit_enabled={int(state['taker_exit_enabled'])} "
            f"maker_urgent_exit_enabled={int(state['maker_urgent_exit_enabled'])} "
            f"hold_to_redeem_enabled={int(state['hold_to_redeem_enabled'])}")
