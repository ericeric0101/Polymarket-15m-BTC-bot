from types import SimpleNamespace

from bot.market_runtime import quote_tick_event_timestamp


def test_quote_tick_event_timestamp_converts_nanoseconds():
    tick = SimpleNamespace(ts_event=1_700_000_000_000_000_000)

    assert quote_tick_event_timestamp(tick, 99.0) == 1_700_000_000.0


def test_quote_tick_event_timestamp_falls_back_for_missing_or_invalid_value():
    assert quote_tick_event_timestamp(SimpleNamespace(ts_event=None), 99.0) == 99.0
    assert quote_tick_event_timestamp(SimpleNamespace(ts_event=-1), 99.0) == 99.0
