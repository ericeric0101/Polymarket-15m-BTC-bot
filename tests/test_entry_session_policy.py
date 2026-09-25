from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bot.entry_session_policy import (
    is_taipei_weekday_entry_session,
    is_taipei_weeknight_entry_session,
    new_buy_session_decision,
)


def test_weeknight_entry_session_boundaries_in_taipei_time():
    # Friday 19:00 Taipei is open; its session remains open until Saturday 07:00.
    assert is_taipei_weeknight_entry_session(datetime(2026, 8, 28, 11, 0, tzinfo=timezone.utc))
    assert is_taipei_weeknight_entry_session(datetime(2026, 8, 28, 22, 59, tzinfo=timezone.utc))
    assert not is_taipei_weeknight_entry_session(datetime(2026, 8, 28, 23, 0, tzinfo=timezone.utc))
    # Saturday night and Sunday night do not open a new session.
    assert not is_taipei_weeknight_entry_session(datetime(2026, 8, 29, 11, 0, tzinfo=timezone.utc))
    assert not is_taipei_weeknight_entry_session(datetime(2026, 8, 30, 11, 0, tzinfo=timezone.utc))
    # Friday's overnight session ends Saturday 07:00; Saturday night cannot
    # leak into the calibration cohort through Sunday morning.
    assert is_taipei_weeknight_entry_session(
        datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc)
    )  # Saturday 02:00 Taipei, continuation of Friday night.
    assert not is_taipei_weeknight_entry_session(
        datetime(2026, 8, 29, 18, 0, tzinfo=timezone.utc)
    )  # Sunday 02:00 Taipei, continuation of Saturday night.


def test_new_buy_session_decision_blocks_taipei_weekend_after_enforcement():
    decision = new_buy_session_decision(datetime(2026, 8, 29, 2, 0, tzinfo=timezone.utc).timestamp())

    assert decision.allowed is False
    assert decision.reason == "taipei_weekend"


def test_live_entry_session_allows_every_weekday_hour_but_blocks_all_weekend_hours():
    # Monday 00:30 and 10:00 Taipei are both open; Friday 23:30 is open.
    for local in (
        datetime(2026, 8, 31, 0, 30, tzinfo=ZoneInfo("Asia/Taipei")),
        datetime(2026, 8, 31, 10, 0, tzinfo=ZoneInfo("Asia/Taipei")),
        datetime(2026, 9, 4, 23, 30, tzinfo=ZoneInfo("Asia/Taipei")),
    ):
        decision = new_buy_session_decision(local.timestamp())
        assert decision.allowed is True
        assert decision.reason == "taipei_weekday_entry_session"

    # Saturday/Sunday are closed throughout, including the old Sunday-morning loophole.
    for local in (
        datetime(2026, 9, 5, 2, 0, tzinfo=ZoneInfo("Asia/Taipei")),
        datetime(2026, 9, 5, 10, 0, tzinfo=ZoneInfo("Asia/Taipei")),
        datetime(2026, 9, 6, 2, 0, tzinfo=ZoneInfo("Asia/Taipei")),
        datetime(2026, 9, 6, 23, 30, tzinfo=ZoneInfo("Asia/Taipei")),
    ):
        decision = new_buy_session_decision(local.timestamp())
        assert decision.allowed is False
        assert decision.reason == "taipei_weekend"


def test_historical_weeknight_classifier_stays_unchanged_for_markout_calibration():
    assert is_taipei_weeknight_entry_session(
        datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)
    ) is False  # 10:00 Taipei remains outside the historical markout population.
    assert is_taipei_weekday_entry_session(
        datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)
    ) is True
