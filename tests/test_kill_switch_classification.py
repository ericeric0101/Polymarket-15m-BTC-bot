"""Kill-switch classes: operational/entry vs execution-integrity vs unresolved.

Hermetic: reuses the protective-exit harness (fake venue behind the real
ExecutionSafetyMixin chokepoint, real quote-cycle gate, real taker-exit path).
"""
import dataclasses
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bot.execution_safety import ExecutionSafetyMixin
from bot.kill_switch import (
    HARD_PROTECTIVE_EXIT_REASONS,
    KILL_CLASS_EXECUTION_INTEGRITY,
    KILL_CLASS_OPERATIONAL_ENTRY,
    KILL_CLASS_UNRESOLVED,
    KILL_REASON_CANCEL_RECONCILE_FAILED,
    KILL_REASON_CANCEL_RECONCILE_UNKNOWN,
    KILL_REASON_CONSECUTIVE_DENIED,
    KILL_REASON_REGION_RESTRICTED,
    KILL_REASON_UNSPECIFIED,
    classify_kill_reason,
    escalate,
    rollover_reset_allowed,
)
from bot.order_runtime import OrderRuntimeMixin
from test_protective_exit_availability import _ProtectiveHost, _cycle


def _activate(host, reason_code, **kwargs):
    OrderRuntimeMixin._activate_maker_kill_switch(host, f"test {reason_code}", reason_code=reason_code, **kwargs)


# --- classification ----------------------------------------------------------

@pytest.mark.parametrize("code,sides,expected", [
    (KILL_REASON_CANCEL_RECONCILE_UNKNOWN, None, KILL_CLASS_EXECUTION_INTEGRITY),
    (KILL_REASON_CANCEL_RECONCILE_FAILED, None, KILL_CLASS_EXECUTION_INTEGRITY),
    (KILL_REASON_REGION_RESTRICTED, None, KILL_CLASS_UNRESOLVED),
    (KILL_REASON_CONSECUTIVE_DENIED, {"buy"}, KILL_CLASS_OPERATIONAL_ENTRY),
    (KILL_REASON_CONSECUTIVE_DENIED, {"sell"}, KILL_CLASS_UNRESOLVED),
    (KILL_REASON_CONSECUTIVE_DENIED, {"buy", "sell"}, KILL_CLASS_UNRESOLVED),
    (KILL_REASON_CONSECUTIVE_DENIED, {"buy", ""}, KILL_CLASS_UNRESOLVED),
    (KILL_REASON_CONSECUTIVE_DENIED, set(), KILL_CLASS_UNRESOLVED),
    (KILL_REASON_UNSPECIFIED, None, KILL_CLASS_UNRESOLVED),
    ("something_new", None, KILL_CLASS_UNRESOLVED),
])
def test_every_trigger_has_an_explicit_class_and_unknown_is_unresolved(code, sides, expected):
    assert classify_kill_reason(code, denied_sides=sides) == expected


def test_escalation_never_relaxes_an_active_kill():
    assert escalate(None, KILL_CLASS_OPERATIONAL_ENTRY) == KILL_CLASS_OPERATIONAL_ENTRY
    assert escalate(KILL_CLASS_OPERATIONAL_ENTRY, KILL_CLASS_EXECUTION_INTEGRITY) == KILL_CLASS_EXECUTION_INTEGRITY
    assert escalate(KILL_CLASS_EXECUTION_INTEGRITY, KILL_CLASS_OPERATIONAL_ENTRY) == KILL_CLASS_EXECUTION_INTEGRITY
    assert escalate(KILL_CLASS_UNRESOLVED, KILL_CLASS_OPERATIONAL_ENTRY) == KILL_CLASS_UNRESOLVED


def test_activation_escalates_on_the_strategy_and_is_journaled():
    host = _ProtectiveHost()
    host.consecutive_denied_sides = {"buy"}
    _activate(host, KILL_REASON_CONSECUTIVE_DENIED)
    assert host.maker_kill_switch_class == KILL_CLASS_OPERATIONAL_ENTRY
    _activate(host, KILL_REASON_CANCEL_RECONCILE_UNKNOWN)
    assert host.maker_kill_switch_class == KILL_CLASS_EXECUTION_INTEGRITY
    host.consecutive_denied_sides = {"buy"}
    _activate(host, KILL_REASON_CONSECUTIVE_DENIED)
    assert host.maker_kill_switch_class == KILL_CLASS_EXECUTION_INTEGRITY
    events = host.event_names("MAKER_KILL_SWITCH_ACTIVATED")
    assert [e[2]["kill_class"] for e in events] == [
        KILL_CLASS_OPERATIONAL_ENTRY, KILL_CLASS_EXECUTION_INTEGRITY, KILL_CLASS_EXECUTION_INTEGRITY,
    ]
    assert events[-1][2]["trigger_class"] == KILL_CLASS_OPERATIONAL_ENTRY


# --- A: operational / entry kill --------------------------------------------

def test_operational_kill_cancels_only_buys_and_keeps_resting_protective_sell():
    host = _ProtectiveHost()
    host.consecutive_denied_sides = {"buy"}
    _activate(host, KILL_REASON_CONSECUTIVE_DENIED)
    assert "buy:down" in host.cancelled
    assert "sell:up" in host.active_maker_orders and "sell:up" not in host.cancelled


def test_operational_kill_allows_hard_breaker_sell_through_the_chokepoint():
    host = _ProtectiveHost()
    host.consecutive_denied_sides = {"buy"}
    _activate(host, KILL_REASON_CONSECUTIVE_DENIED)
    assert _cycle(host) is None
    assert len(host.venue_submissions) == 1
    assert host.venue_submissions[0].order_side.name == "SELL"
    assert all(e[1] != "ORDER_SUBMIT" for e in host.events)  # no maker quote


def test_operational_kill_blocks_non_hard_protective_exits():
    host = _ProtectiveHost()
    real_evaluate = host.exit_policy_engine.evaluate

    def adaptive_only(*args, **kwargs):
        return dataclasses.replace(real_evaluate(*args, **kwargs), reason="stop_loss_confirmed")

    host.exit_policy_engine = SimpleNamespace(evaluate=adaptive_only, config=host.exit_policy_engine.config)
    assert "stop_loss_confirmed" not in HARD_PROTECTIVE_EXIT_REASONS
    # Sanity: without a kill switch the adaptive decision does reach the venue
    # (the adaptive stop has a spread guard the hard breaker bypasses; open it).
    host.taker_exit_stop_loss_max_spread_pct = Decimal("1")
    control = _ProtectiveHost()
    control.exit_policy_engine = host.exit_policy_engine
    control.taker_exit_stop_loss_max_spread_pct = Decimal("1")
    _cycle(control)
    assert len(control.venue_submissions) == 1
    host.consecutive_denied_sides = {"buy"}
    _activate(host, KILL_REASON_CONSECUTIVE_DENIED)
    _cycle(host)
    assert host.venue_submissions == []


# --- B: execution-integrity kill ---------------------------------------------

@pytest.mark.parametrize("code", [KILL_REASON_CANCEL_RECONCILE_UNKNOWN, KILL_REASON_CANCEL_RECONCILE_FAILED])
def test_integrity_kill_submits_no_sell_and_reports_degraded(code):
    host = _ProtectiveHost()
    _activate(host, code)
    assert {"buy:down", "sell:up"} <= set(host.cancelled)  # unchanged cancel-all semantics
    host.active_maker_orders = {}
    assert _cycle(host) is None
    assert host.venue_submissions == []
    degraded = host.event_names("PROTECTIVE_EXIT_DEGRADED")
    assert degraded and degraded[0][2]["reason"] == "kill_switch_execution_integrity_reconcile_first"


@pytest.mark.parametrize("code,sides", [
    (KILL_REASON_REGION_RESTRICTED, None),
    (KILL_REASON_CONSECUTIVE_DENIED, {"buy", "sell"}),
])
def test_unresolved_kill_behaves_like_integrity(code, sides):
    host = _ProtectiveHost()
    host.consecutive_denied_sides = set(sides or ())
    _activate(host, code)
    assert host.maker_kill_switch_class == KILL_CLASS_UNRESOLVED
    assert "sell:up" in host.cancelled
    _cycle(host)
    assert host.venue_submissions == []
    degraded = host.event_names("PROTECTIVE_EXIT_DEGRADED")
    assert degraded and degraded[0][2]["reason"] == "kill_switch_active_exit_policy_unresolved"


# --- chokepoint defence in depth ---------------------------------------------

class _Venue:
    def submit_order(self, order, *args, **kwargs):
        self.sent.append(order)
        return True


class _ChokeHost(ExecutionSafetyMixin, _Venue):
    def __init__(self, kill_class, *, flag=True):
        self.test_mode = False
        self.maker_kill_switch = flag
        self.maker_kill_switch_class = kill_class
        self.sent = []

    def _is_dry_run_mode(self):
        return False


def _order(side):
    return SimpleNamespace(side=SimpleNamespace(name=side))


@pytest.mark.parametrize("kill_class,side,allowed", [
    (None, "BUY", True),
    (None, "SELL", True),
    (KILL_CLASS_OPERATIONAL_ENTRY, "BUY", False),
    (KILL_CLASS_OPERATIONAL_ENTRY, "SELL", True),
    (KILL_CLASS_EXECUTION_INTEGRITY, "SELL", False),
    (KILL_CLASS_EXECUTION_INTEGRITY, "BUY", False),
    (KILL_CLASS_UNRESOLVED, "SELL", False),
    (KILL_CLASS_OPERATIONAL_ENTRY, "UNKNOWN", False),
])
def test_chokepoint_enforces_kill_class(kill_class, side, allowed):
    host = _ChokeHost(kill_class, flag=kill_class is not None)
    result = host.submit_order(_order(side))
    assert (result is not False) is allowed
    assert (len(host.sent) == 1) is allowed


def test_chokepoint_treats_unclassified_active_kill_as_unresolved():
    host = _ChokeHost(None, flag=True)
    assert host.submit_order(_order("SELL")) is False
    assert host.sent == []


# --- rollover ----------------------------------------------------------------

def test_rollover_reset_requires_completed_reconciliation_for_integrity_kills():
    pending = {"sell:up": {"pending_cancel": True}}
    unknown = {"sell:up": {"pending_cancel": False, "reconcile_unknown_retries": 2}}
    assert rollover_reset_allowed(KILL_CLASS_OPERATIONAL_ENTRY, pending) is True
    assert rollover_reset_allowed(KILL_CLASS_EXECUTION_INTEGRITY, pending) is False
    assert rollover_reset_allowed(KILL_CLASS_EXECUTION_INTEGRITY, unknown) is False
    assert rollover_reset_allowed(KILL_CLASS_UNRESOLVED, pending) is False
    assert rollover_reset_allowed(KILL_CLASS_EXECUTION_INTEGRITY, {}) is True


def test_denied_side_streak_is_tracked_and_reset_by_a_fill():
    from bot.order_events import record_denied_order_side, reset_denied_order_streak
    host = SimpleNamespace(consecutive_denied_orders=0)
    record_denied_order_side(host, "buy")
    record_denied_order_side(host, "BUY")
    assert host.consecutive_denied_sides == {"buy"}
    record_denied_order_side(host, "")
    assert host.consecutive_denied_sides == {"buy", ""}
    reset_denied_order_streak(host)
    assert host.consecutive_denied_orders == 0 and host.consecutive_denied_sides == set()
