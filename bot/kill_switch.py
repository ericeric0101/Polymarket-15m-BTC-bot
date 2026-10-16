"""Kill-switch classification and policy (single source of truth).

A kill switch always stops NEW BUY authority. What it does to SELL authority
depends on what the trigger says about the venue/order state:

* ``operational_entry`` -- the trigger is about entry eligibility only and every
  affected order reached a known terminal state. Resting BUYs are cancelled,
  resting protective SELLs are kept, and held inventory may still be closed by
  the hard protective breakers (``HARD_PROTECTIVE_EXIT_REASONS``) only.
* ``execution_integrity`` -- the order state itself is uncertain (unknown after
  reconcile retries, or an order we cannot cancel). Sending another SELL could
  duplicate exposure, so no new SELL is submitted. Existing behaviour is kept
  (cancel all resting orders), held inventory is reported DEGRADED, and the
  watchdog's pending-cancel reconciliation keeps running.
* ``unresolved`` -- the trigger's semantics for held inventory have not been
  decided. Treated exactly like ``execution_integrity`` (most restrictive).

Classification is per trigger and never inferred from free-text reasons.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

KILL_CLASS_OPERATIONAL_ENTRY = "operational_entry"
KILL_CLASS_EXECUTION_INTEGRITY = "execution_integrity"
KILL_CLASS_UNRESOLVED = "unresolved"

# Higher rank = more restrictive. A later trigger can only escalate.
_CLASS_RANK = {
    KILL_CLASS_OPERATIONAL_ENTRY: 0,
    KILL_CLASS_EXECUTION_INTEGRITY: 1,
    KILL_CLASS_UNRESOLVED: 1,
}

# Trigger codes (one per _activate_maker_kill_switch call site).
KILL_REASON_CANCEL_RECONCILE_UNKNOWN = "cancel_reconcile_unknown"
KILL_REASON_CANCEL_RECONCILE_FAILED = "cancel_reconcile_failed"
KILL_REASON_REGION_RESTRICTED = "region_restricted"
KILL_REASON_CONSECUTIVE_DENIED = "consecutive_denied"
KILL_REASON_UNSPECIFIED = "unspecified"

# Exit-engine reasons that remain allowed under an operational/entry kill.
# The same set is the STOP_LOSS=0 exemption in bot.taker_exit.
HARD_PROTECTIVE_EXIT_REASONS = frozenset({
    "absolute_max_loss_breaker",
    "catastrophic_stop_loss_confirming",
    "catastrophic_stop_loss_confirmed",
})

DEGRADED_REASON_BY_CLASS = {
    KILL_CLASS_EXECUTION_INTEGRITY: "kill_switch_execution_integrity_reconcile_first",
    # Unchanged label: the exit policy for these triggers is still undecided.
    KILL_CLASS_UNRESOLVED: "kill_switch_active_exit_policy_unresolved",
}


def classify_kill_reason(reason_code: str, *, denied_sides: Optional[Iterable[str]] = None) -> str:
    """Map a trigger to its class. Anything not explicitly decided is UNRESOLVED."""
    code = str(reason_code or "")
    if code in (KILL_REASON_CANCEL_RECONCILE_UNKNOWN, KILL_REASON_CANCEL_RECONCILE_FAILED):
        # Unknown state, or an order we tried and failed to cancel that may
        # still be live: a new SELL could duplicate exposure.
        return KILL_CLASS_EXECUTION_INTEGRITY
    if code == KILL_REASON_CONSECUTIVE_DENIED:
        # Denied/rejected orders are terminal and known. If the whole streak was
        # BUY denials, nothing about held inventory or SELL authority is in
        # doubt. Any SELL (or unknown-side) denial: semantics undecided.
        sides = {str(s or "").strip().lower() for s in (denied_sides or ())}
        if sides == {"buy"}:
            return KILL_CLASS_OPERATIONAL_ENTRY
        return KILL_CLASS_UNRESOLVED
    # KILL_REASON_REGION_RESTRICTED: the venue refuses this deployment; whether
    # a protective SELL could still be accepted is not established.
    return KILL_CLASS_UNRESOLVED


def escalate(current: Optional[str], new: str) -> str:
    """Return the more restrictive class; never relax an active kill."""
    if current not in _CLASS_RANK:
        return new
    return new if _CLASS_RANK[new] > _CLASS_RANK[current] else current


def effective_kill_class(strategy: Any) -> Optional[str]:
    """None when no kill switch is active; UNRESOLVED when active but unclassified."""
    if not bool(getattr(strategy, "maker_kill_switch", False)):
        return None
    kill_class = getattr(strategy, "maker_kill_switch_class", None)
    return kill_class if kill_class in _CLASS_RANK else KILL_CLASS_UNRESOLVED


def allows_hard_protective_sell(kill_class: Optional[str]) -> bool:
    return kill_class is None or kill_class == KILL_CLASS_OPERATIONAL_ENTRY


def keeps_resting_sells(kill_class: str) -> bool:
    return kill_class == KILL_CLASS_OPERATIONAL_ENTRY


def order_side_name(order: Any) -> str:
    """'BUY' / 'SELL' / '' for a Nautilus order or a test double."""
    for attr in ("side", "order_side"):
        side = getattr(order, attr, None)
        if side is None:
            continue
        name = str(getattr(side, "name", side) or "").upper()
        if name.endswith("BUY"):
            return "BUY"
        if name.endswith("SELL"):
            return "SELL"
    return ""


def submission_blocked_by_kill_switch(strategy: Any, order: Any) -> str:
    """Return a block reason ('' = allowed) for a real submission under a kill."""
    kill_class = effective_kill_class(strategy)
    if kill_class is None:
        return ""
    side = order_side_name(order)
    if side == "SELL" and kill_class == KILL_CLASS_OPERATIONAL_ENTRY:
        return ""
    return f"kill_switch_{kill_class}_blocks_{side.lower() or 'unknown_side'}"


def rollover_reset_allowed(kill_class: Optional[str], active_maker_orders: Mapping[str, Any]) -> bool:
    """An integrity/unresolved kill may only auto-reset once reconciliation is done."""
    if kill_class is None or kill_class == KILL_CLASS_OPERATIONAL_ENTRY:
        return True
    for state in (active_maker_orders or {}).values():
        if isinstance(state, Mapping) and (
            state.get("pending_cancel") or int(state.get("reconcile_unknown_retries", 0) or 0) > 0
        ):
            return False
    return True
