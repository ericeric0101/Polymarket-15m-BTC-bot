"""Explicit Taipei-weekend BUY switch (pre-12-market engineering pass, F-B).

Default = last committed behaviour before the 2026-10-10 dirty patch: new
BUYs are blocked all of Saturday/Sunday Taipei time.  Only an explicit
ENTRY_ALLOW_TAIPEI_WEEKEND_BUYS=true reproduces the 2026-10-10 LIVE run.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import bot.entry_session_policy as policy
from bot.app_config import AppConfig

TPE = ZoneInfo("Asia/Taipei")


def _ts(*args):
    return datetime(*args, tzinfo=TPE).timestamp()


@pytest.fixture(autouse=True)
def _reset_policy():
    policy.configure_entry_session_policy(allow_taipei_weekend_buys=False)
    yield
    policy.configure_entry_session_policy(allow_taipei_weekend_buys=False)


def test_default_blocks_taipei_weekend_and_allows_weekdays():
    sat = policy.new_buy_session_decision(_ts(2026, 10, 10, 7, 30))
    assert (sat.allowed, sat.reason) == (False, "taipei_weekend_observation_only")
    mon = policy.new_buy_session_decision(_ts(2026, 10, 12, 7, 30))
    assert (mon.allowed, mon.reason) == (True, "taipei_weekday_entry_session")


def test_explicit_unlock_allows_weekend_with_distinct_reason():
    policy.configure_entry_session_policy(allow_taipei_weekend_buys=True)
    sat = policy.new_buy_session_decision(_ts(2026, 10, 10, 7, 30))
    assert (sat.allowed, sat.reason) == (True, "taipei_weekend_unlocked_by_env")
    fri = policy.new_buy_session_decision(_ts(2026, 10, 9, 23, 0))
    assert (fri.allowed, fri.reason) == (True, "taipei_weekday_entry_session")


def test_per_call_override_wins_over_configured_value():
    policy.configure_entry_session_policy(allow_taipei_weekend_buys=True)
    d = policy.new_buy_session_decision(_ts(2026, 10, 11, 12, 0), allow_taipei_weekend_buys=False)
    assert d.allowed is False


@pytest.mark.parametrize(("when", "blocked"), [
    ((2026, 10, 9, 23, 59, 59), False),   # Friday last second, Taipei
    ((2026, 10, 10, 0, 0, 0), True),      # Saturday 00:00 Taipei = Friday 16:00 UTC
    ((2026, 10, 11, 23, 59, 59), True),   # Sunday last second
    ((2026, 10, 12, 0, 0, 0), False),     # Monday 00:00 Taipei
])
def test_day_boundaries_are_taipei_calendar_days(when, blocked):
    assert policy.new_buy_session_decision(_ts(*when)).allowed is (not blocked)


def test_policy_not_enforced_before_its_approval_date():
    d = policy.new_buy_session_decision(_ts(2026, 8, 22, 12, 0))  # Saturday, before 2026-08-29
    assert (d.allowed, d.reason) == (True, "entry_session_policy_not_yet_enforced")


def test_summary_reports_effective_value_without_secrets():
    assert "ENTRY_ALLOW_TAIPEI_WEEKEND_BUYS=false" in policy.entry_session_policy_summary()
    policy.configure_entry_session_policy(allow_taipei_weekend_buys=True)
    assert "ENTRY_ALLOW_TAIPEI_WEEKEND_BUYS=true" in policy.entry_session_policy_summary()


@pytest.mark.parametrize(("raw", "expected"), [(None, False), ("false", False), ("0", False),
                                                ("true", True), ("1", True), ("yes", True)])
def test_app_config_resolves_env_switch(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(policy.ALLOW_TAIPEI_WEEKEND_BUYS_ENV, raising=False)
    else:
        monkeypatch.setenv(policy.ALLOW_TAIPEI_WEEKEND_BUYS_ENV, raw)
    assert AppConfig.from_env(enable_terminal_dashboard=False).operations.allow_taipei_weekend_buys is expected
