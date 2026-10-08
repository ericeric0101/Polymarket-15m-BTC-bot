"""Single fail-closed boundary between strategy orders and real execution."""
from __future__ import annotations


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
        return super().submit_order(order, *args, **kwargs)
