"""Taipei calendar policy for opening new BUY positions.

This deliberately controls entries only.  The process stays alive outside the
entry session so it can cancel orders, exit inventory, reconcile and redeem.

Default: new BUYs are blocked all of Saturday and Sunday, Asia/Taipei calendar
days (Friday 16:00 UTC -> Sunday 16:00 UTC).  An operator may unlock weekend
BUYs only with an explicit ``ENTRY_ALLOW_TAIPEI_WEEKEND_BUYS=true`` (resolved at
startup into ``OperationsConfig.allow_taipei_weekend_buys`` and applied here
once via ``configure_entry_session_policy``).  SELL authority is never gated.
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
ALLOW_TAIPEI_WEEKEND_BUYS_ENV = "ENTRY_ALLOW_TAIPEI_WEEKEND_BUYS"

# Process-wide effective value; conservative default (weekend BUYs blocked).
_allow_taipei_weekend_buys = False


def configure_entry_session_policy(*, allow_taipei_weekend_buys: bool) -> None:
    """Apply the startup-resolved weekend switch for every BUY boundary."""
    global _allow_taipei_weekend_buys
    _allow_taipei_weekend_buys = bool(allow_taipei_weekend_buys)


def entry_session_policy_summary() -> str:
    value = "true" if _allow_taipei_weekend_buys else "false"
    effect = ("new BUYs allowed every Taipei day" if _allow_taipei_weekend_buys
              else "new BUYs blocked Saturday/Sunday Asia/Taipei")
    return f"Entry session policy: {ALLOW_TAIPEI_WEEKEND_BUYS_ENV}={value} ({effect}; SELL authority unaffected)"


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


def new_buy_session_decision(now_ts: float, *, allow_taipei_weekend_buys: bool | None = None) -> EntrySessionDecision:
    when = datetime.fromtimestamp(float(now_ts), tz=timezone.utc)
    local = when.astimezone(TAIPEI)
    if when < ENTRY_SESSION_ENFORCED_FROM_UTC:
        return EntrySessionDecision(True, "entry_session_policy_not_yet_enforced", local)
    allow_weekend = _allow_taipei_weekend_buys if allow_taipei_weekend_buys is None else bool(allow_taipei_weekend_buys)
    if local.weekday() >= 5:
        if allow_weekend:
            return EntrySessionDecision(True, "taipei_weekend_unlocked_by_env", local)
        return EntrySessionDecision(False, "taipei_weekend_observation_only", local)
    return EntrySessionDecision(True, "taipei_weekday_entry_session", local)
