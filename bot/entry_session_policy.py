"""Taipei calendar policy for opening new BUY positions.

This deliberately controls entries only.  The process stays alive outside the
entry session so it can cancel orders, exit inventory, reconcile and redeem.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")
# The policy was approved on 2026-08-29.  Keeping an explicit deployment
# boundary makes replaying older journal timestamps deterministic.
ENTRY_SESSION_ENFORCED_FROM_UTC = datetime(2026, 8, 29, tzinfo=timezone.utc)
MARKOUT_CALIBRATION_START_UTC = datetime(2026, 8, 22, tzinfo=timezone.utc)


@dataclass(frozen=True)
class EntrySessionDecision:
    allowed: bool
    reason: str
    local_time: datetime


def is_taipei_weeknight_entry_session(when: datetime) -> bool:
    """Historical weekday-night classifier retained for calibration cohorts."""
    local = when.astimezone(TAIPEI)
    if 19 <= local.hour:
        return local.weekday() < 5
    if local.hour < 7:
        # After midnight belongs to the previous calendar day's session.
        # Thus Tuesday-Saturday early morning can continue a weekday session;
        # Monday/Sunday early morning follows a weekend night and is closed.
        return 0 < local.weekday() < 6
    return False


def is_taipei_weekday_entry_session(when: datetime) -> bool:
    """Allow every hour Monday-Friday Taipei time; block all Saturday/Sunday."""
    return when.astimezone(TAIPEI).weekday() < 5


def new_buy_session_decision(now_ts: float) -> EntrySessionDecision:
    when = datetime.fromtimestamp(float(now_ts), tz=timezone.utc)
    local = when.astimezone(TAIPEI)
    if when < ENTRY_SESSION_ENFORCED_FROM_UTC:
        return EntrySessionDecision(True, "entry_session_policy_not_yet_enforced", local)
    return EntrySessionDecision(True, "taipei_all_days_entry_session", local)
