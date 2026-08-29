from datetime import datetime, timezone

from bot.entry_session_policy import is_taipei_weeknight_entry_session, new_buy_session_decision


def test_weeknight_entry_session_boundaries_in_taipei_time():
    # Friday 19:00 Taipei is open; its session remains open until Saturday 07:00.
    assert is_taipei_weeknight_entry_session(datetime(2026, 8, 28, 11, 0, tzinfo=timezone.utc))
    assert is_taipei_weeknight_entry_session(datetime(2026, 8, 28, 22, 59, tzinfo=timezone.utc))
    assert not is_taipei_weeknight_entry_session(datetime(2026, 8, 28, 23, 0, tzinfo=timezone.utc))
    # Saturday night and Sunday night do not open a new session.
    assert not is_taipei_weeknight_entry_session(datetime(2026, 8, 29, 11, 0, tzinfo=timezone.utc))
    assert not is_taipei_weeknight_entry_session(datetime(2026, 8, 30, 11, 0, tzinfo=timezone.utc))


def test_new_buy_session_decision_blocks_taipei_daytime_after_enforcement():
    decision = new_buy_session_decision(datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc).timestamp())

    assert decision.allowed is False
    assert decision.reason == "taipei_day_or_weekend"
