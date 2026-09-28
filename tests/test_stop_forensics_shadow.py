from decimal import Decimal

from bot.stop_forensics_shadow import StopForensicsShadow


def test_adverse_episode_records_only_after_raw_adverse_and_emits_p10_candidate():
    recorder = StopForensicsShadow()
    base = dict(
        slug="btc-updown-15m-x", instrument_id="inst", position_side="UP",
        entry_price=Decimal("0.60"), qty=Decimal("10"), signal_side="DOWN",
        signal_score=Decimal("-0.3"), official_strike=Decimal("100"), spot=Decimal("99"),
        fair_probability=Decimal("0.50"), fair_at_entry=Decimal("0.60"),
        leader_side="DOWN", best_bid=Decimal("0.50"), best_bid_size=Decimal("10"),
        time_left_sec=200.0,
    )
    recorder.observe(raw_adverse=False, now_ts=100.0, **base)
    assert recorder.events == []
    recorder.observe(raw_adverse=True, now_ts=101.0, **base)
    recorder.observe(raw_adverse=True, now_ts=111.1, **base)
    candidates = [event for event in recorder.events if event["event_type"] == "STOP_SHADOW_CANDIDATE"]
    assert {event["payload"]["candidate"] for event in candidates} >= {"STOP_SHADOW_P5", "STOP_SHADOW_P10"}
    assert all(event["payload"]["thesis_weakening_count"] >= 2 for event in candidates)


def test_checkpoint_never_invents_bbo_when_quote_unavailable():
    recorder = StopForensicsShadow()
    recorder.observe(
        raw_adverse=True, now_ts=0.0, slug="s", instrument_id="i", position_side="UP",
        entry_price=Decimal("0.60"), qty=Decimal("10"), signal_side="UP", signal_score=Decimal("0.2"),
        official_strike=Decimal("100"), spot=Decimal("99"), fair_probability=Decimal("0.60"),
        fair_at_entry=Decimal("0.60"), leader_side="UP", best_bid=None, best_bid_size=None,
        time_left_sec=200.0,
    )
    recorder.observe(
        raw_adverse=False, now_ts=6.0, slug="s", instrument_id="i", position_side="UP",
        entry_price=Decimal("0.60"), qty=Decimal("10"), signal_side="UP", signal_score=Decimal("0.2"),
        official_strike=Decimal("100"), spot=Decimal("101"), fair_probability=Decimal("0.60"),
        fair_at_entry=Decimal("0.60"), leader_side="UP", best_bid=None, best_bid_size=None,
        time_left_sec=200.0,
    )
    checkpoint = next(event for event in recorder.events if event["event_type"] == "STOP_SHADOW_CHECKPOINT")
    assert checkpoint["payload"]["checkpoint_quote_available"] is False
    assert checkpoint["payload"]["best_bid"] is None
