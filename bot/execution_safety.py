"""Single fail-closed boundary between strategy orders and real execution."""
from __future__ import annotations

from loguru import logger

from bot.kill_switch import submission_blocked_by_kill_switch


def real_order_submission_allowed(strategy) -> bool:
    # An explicitly initialized live flag is required; missing/failed mode
    # inspection must never default to a live authenticated client.
    if getattr(strategy, 'test_mode', None) is not False:
        return False
    try:
        check = getattr(strategy, '_is_dry_run_mode', None)
        return not bool(check()) if callable(check) else True
    except Exception:
        return False


class ExecutionSafetyMixin:
    def submit_order(self, order, *args, **kwargs):
        if not real_order_submission_allowed(self):
            return False
        # Defence in depth for the kill-switch classes (bot.kill_switch): an
        # active kill never lets a BUY through, and only an operational/entry
        # kill lets a SELL through. Only ever stricter than the mode check.
        blocked = submission_blocked_by_kill_switch(self, order)
        if blocked:
            logger.warning(f"Real order submission blocked: {blocked}")
            return False
        return super().submit_order(order, *args, **kwargs)
