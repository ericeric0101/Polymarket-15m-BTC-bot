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


def test_missing_entry_fair_stays_unavailable_and_depth_is_explicit():
    recorder = StopForensicsShadow()
    base = dict(slug="s", instrument_id="i", position_side="UP", entry_price=Decimal(".6"), qty=Decimal("10"),
                signal_side="DOWN", signal_score=Decimal("-.2"), official_strike=Decimal("100"), spot=Decimal("99"),
                fair_probability=Decimal(".4"), fair_at_entry=None, leader_side="DOWN", best_bid=Decimal(".5"),
                best_bid_size=Decimal("5"), time_left_sec=100.0, bid_levels=[(Decimal(".5"), Decimal("5")), (Decimal(".49"), Decimal("5"))])
    recorder.observe(raw_adverse=True, now_ts=0, **base)
    recorder.observe(raw_adverse=True, now_ts=5.1, **base)
    row = next(event["payload"] for event in recorder.events if event["event_type"] == "STOP_SHADOW_CHECKPOINT")
    assert row["fair_at_entry"] is None
    assert row["thesis_component_availability"]["fair"] is False
    assert row["coverage_1c"] == 1.0


def test_only_explicit_opposite_signal_is_reversal_and_leader_never_fills_twap_vote():
    recorder = StopForensicsShadow()
    base = dict(raw_adverse=True, slug="s", instrument_id="i", position_side="UP", entry_price=Decimal(".60"),
                qty=Decimal("10"), signal_score=Decimal("0"), official_strike=Decimal("100"), spot=Decimal("99"),
                fair_probability=Decimal(".60"), fair_at_entry=Decimal(".60"), leader_side="DOWN",
                best_bid=Decimal(".50"), best_bid_size=Decimal("10"), time_left_sec=100.0)
    recorder.observe(now_ts=0, signal_side="NONE", **base)
    recorder.observe(now_ts=5.1, signal_side="NONE", **base)
    checkpoint = next(e["payload"] for e in recorder.events if e["event_type"] == "STOP_SHADOW_CHECKPOINT")
    assert checkpoint["signal_available"] is False
    assert checkpoint["signal_reversal"] is None
    assert checkpoint["thesis_component_availability"]["twap"] is False
    assert checkpoint["thesis_weakening_count"] == 0
    assert not [e for e in recorder.events if e["event_type"] == "STOP_SHADOW_CANDIDATE"]

    recorder = StopForensicsShadow()
    recorder.observe(now_ts=0, signal_side="DOWN", **base)
    recorder.observe(now_ts=5.1, signal_side="DOWN", **base)
    checkpoint = next(e["payload"] for e in recorder.events if e["event_type"] == "STOP_SHADOW_CHECKPOINT")
    assert checkpoint["signal_available"] is True
    assert checkpoint["signal_reversal"] is True


def test_unknown_signal_and_incomplete_twap_are_unavailable_not_adverse_votes():
    recorder = StopForensicsShadow()
    base = dict(raw_adverse=True, slug="s", instrument_id="i", position_side="DOWN", entry_price=Decimal(".60"),
                qty=Decimal("10"), signal_side="UNKNOWN", signal_score=Decimal("0"), official_strike=Decimal("100"),
                spot=Decimal("101"), fair_probability=Decimal(".60"), fair_at_entry=Decimal(".60"), leader_side="UP",
                best_bid=Decimal(".50"), best_bid_size=Decimal("10"), time_left_sec=100.0,
                twap_features={"official_current_twap": 100.0, "projected_settlement_side_trend": "UNKNOWN"})
    recorder.observe(now_ts=0, **base)
    recorder.observe(now_ts=5.1, **base)
    row = next(e["payload"] for e in recorder.events if e["event_type"] == "STOP_SHADOW_CHECKPOINT")
    assert row["thesis_component_availability"] == {"signal": False, "twap": False, "fair": True}
    assert row["thesis_weakening_count"] == 0


def test_bounded_depth_reports_partial_vwap_without_claiming_full_exit():
    recorder = StopForensicsShadow()
    base = dict(raw_adverse=True, slug="s", instrument_id="i", position_side="UP", entry_price=Decimal(".60"),
                qty=Decimal("10"), signal_side="DOWN", signal_score=Decimal("-.2"), official_strike=Decimal("100"),
                spot=Decimal("99"), fair_probability=Decimal(".40"), fair_at_entry=Decimal(".60"), leader_side="UP",
                best_bid=Decimal(".50"), best_bid_size=None, time_left_sec=100.0,
                bid_levels=[(Decimal(".50"), Decimal("4")), (Decimal(".48"), Decimal("3")), (Decimal(".40"), Decimal("3"))])
    recorder.observe(now_ts=0, **base)
    recorder.observe(now_ts=5.1, **base)
    candidate = next(e["payload"] for e in recorder.events if e["event_type"] == "STOP_SHADOW_CANDIDATE")
    assert candidate["coverage_top"] == 0.4
    assert candidate["coverage_1c"] == 0.4
    assert candidate["coverage_2c"] == 0.7
    assert candidate["coverage_5c"] == 0.7
    assert candidate["execution_feasible_5c"] is False
    assert candidate["execution_feasible_full_book"] is True
    assert candidate["execution_feasible"] is False  # compatibility field means bounded 5c
    assert candidate["filled_qty_5c"] == 7.0 and candidate["remaining_qty_5c"] == 3.0
    assert candidate["depth_weighted_exit_gross_pnl_5c"] == -0.76
    assert candidate["depth_weighted_exit_net_pnl_5c"] is None
