"""Telemetry inputs for the next early-warning study (pre-12-market pass, S3).

E1 entry executable-bid baseline on POSITION_OPENED; E3 held-token exit-size
depth on transition rows; E4 Binance spot + age on the same wall clock.
Transition-only, fail-safe, no new event types.
"""
from decimal import Decimal
from types import SimpleNamespace

from bot.stop_timing_telemetry import StopTimingTelemetry, exit_depth_metrics
from test_live_path_regressions import DummyStrategyForFill
from test_stop_timing_telemetry import END, SLUG, Sink, _decision, _make_config, _signal


def _obs(tel, now, *, levels=None, l2_age=0.3, binance=100050.0, binance_age=0.2, state=None, twap=100020.0):
    tel.observe_safe(
        now_ts=now, slug=SLUG, instrument_id="up-token", held_side="UP",
        state=state or {"qty": Decimal("5.5"), "avg_entry_price": Decimal("0.82"), "opened_ts": END - 600,
                        "entry_fee_remaining": Decimal("0"), "entry_bid_at_fill": Decimal("0.82"),
                        "entry_ask_at_fill": Decimal("0.83"), "entry_quote_age_at_fill_sec": 0.4},
        qty=Decimal("5.5"), sellable_qty=Decimal("5.4945"), best_bid=Decimal("0.60"), best_ask=Decimal("0.61"),
        time_left_sec=END - now, market_end_ts=END, strike=Decimal("100000"), reference_spot=Decimal("100010"),
        reference_source="polymarket_chainlink_twap_60s_ws", reference_ts=now - 0.4,
        twap_features={"official_current_twap": twap, "source_ts": now - 0.5, "required_move_sigma": 0.8},
        exit_decision=_decision(), engine_config=_make_config(), signal_decision=_signal(),
        locked_side_invalidated=False, adverse_persistence_sec=0.0, thesis_votes={}, hold_sec=60.0,
        binance_spot=binance, binance_spot_ts=(now - binance_age) if binance is not None else None,
        exit_bid_levels=levels, l2_age_sec=l2_age,
    )


LEVELS = [(Decimal("0.23"), Decimal("2")), (Decimal("0.22"), Decimal("3")), (Decimal("0.20"), Decimal("10"))]


def test_exit_depth_walks_bids_for_the_requested_qty():
    m = exit_depth_metrics(LEVELS, Decimal("5.4945"), l2_age_sec=0.3)
    assert m["exit_l2_state"] == "FRESH"
    assert m["exit_top_bid_size"] == 2.0
    assert m["exit_depth_covers_qty"] is True
    assert m["exit_depth_levels_used"] == 3
    expected_vwap = (0.23 * 2 + 0.22 * 3 + 0.20 * 0.4945) / 5.4945
    assert abs(m["exit_vwap_for_qty"] - expected_vwap) < 1e-9
    assert m["exit_depth_total_size"] == 15.0


def test_exit_depth_reports_insufficient_and_unknown():
    thin = exit_depth_metrics(LEVELS[:1], Decimal("5.4945"), l2_age_sec=0.3)
    assert thin["exit_depth_covers_qty"] is False and thin["exit_vwap_for_qty"] is None
    missing = exit_depth_metrics(None, Decimal("5.4945"), l2_age_sec=None)
    assert missing["exit_l2_state"] == "UNKNOWN"
    assert missing["exit_top_bid_size"] is None and missing["exit_depth_covers_qty"] is None
    stale = exit_depth_metrics(LEVELS, Decimal("5"), l2_age_sec=9.0)
    assert stale["exit_l2_state"] == "STALE"


def test_position_opened_carries_entry_bid_baseline_binance_and_depth():
    sink = Sink()
    tel = StopTimingTelemetry(emit=sink, run_id="r1")
    _obs(tel, END - 590, levels=LEVELS)
    (opened,) = sink.of("STOP_TIMING_POSITION_OPENED")
    assert opened["entry_bid_at_fill"] == 0.82 and opened["entry_ask_at_fill"] == 0.83
    assert opened["entry_quote_age_at_fill_sec"] == 0.4
    assert opened["instrument_id"] == "up-token" and opened["held_side"] == "UP"
    assert opened["binance_spot"] == 100050.0 and abs(opened["binance_spot_age_sec"] - 0.2) < 1e-6
    assert opened["exit_depth_covers_qty"] is True


def test_missing_inputs_are_none_not_invented():
    sink = Sink()
    tel = StopTimingTelemetry(emit=sink, run_id="r1")
    state = {"qty": Decimal("5.5"), "avg_entry_price": Decimal("0.82"), "opened_ts": END - 600}
    _obs(tel, END - 590, levels=None, l2_age=None, binance=None, state=state)
    (opened,) = sink.of("STOP_TIMING_POSITION_OPENED")
    assert opened["entry_bid_at_fill"] is None
    assert opened["binance_spot"] is None and opened["binance_spot_age_sec"] is None
    assert opened["exit_l2_state"] == "UNKNOWN"


def test_depth_changes_alone_do_not_create_rows():
    def run(level_seq):
        sink = Sink()
        tel = StopTimingTelemetry(emit=sink, run_id="r1")
        for i, levels in enumerate(level_seq):
            _obs(tel, END - 590 + 5 * i, levels=levels)
        return sink.types()

    constant = run([LEVELS, LEVELS, LEVELS])
    varying = run([LEVELS, LEVELS[:1], [(Decimal("0.5"), Decimal("100"))]])
    assert varying == constant


def test_broken_order_book_never_breaks_the_row():
    class Boom:
        def __iter__(self):
            raise RuntimeError("book gone")

    sink = Sink()
    tel = StopTimingTelemetry(emit=sink, run_id="r1")
    _obs(tel, END - 590, levels=Boom())
    (opened,) = sink.of("STOP_TIMING_POSITION_OPENED")
    assert opened["exit_l2_state"] == "UNKNOWN"
    assert tel.counters["exceptions"] == 0


def test_first_buy_fill_captures_entry_quote_once():
    strategy = DummyStrategyForFill()
    quotes = iter([(Decimal("0.45"), Decimal("0.46")), (Decimal("0.30"), Decimal("0.31"))])
    strategy._get_quote_for_instrument = lambda _inst: next(quotes)
    strategy.last_quote_update_ts_by_inst = {}
    strategy._update_live_inventory_cost_from_fill(instrument_id="inst-1", side="buy", fill_price=Decimal("0.46"),
                                                   fill_qty=Decimal("5.2"), fee_usdc=Decimal("0"), fee_shares=Decimal("0"))
    strategy._update_live_inventory_cost_from_fill(instrument_id="inst-1", side="buy", fill_price=Decimal("0.31"),
                                                   fill_qty=Decimal("1"), fee_usdc=Decimal("0"), fee_shares=Decimal("0"))
    state = strategy.live_inventory_cost["inst-1"]
    assert state["entry_bid_at_fill"] == Decimal("0.45") and state["entry_ask_at_fill"] == Decimal("0.46")


def test_entry_quote_capture_failure_never_blocks_the_fill():
    strategy = DummyStrategyForFill()

    def _boom(_inst):
        raise RuntimeError("quote cache gone")

    strategy._get_quote_for_instrument = _boom
    strategy._update_live_inventory_cost_from_fill(instrument_id="inst-1", side="buy", fill_price=Decimal("0.46"),
                                                   fill_qty=Decimal("5.2"), fee_usdc=Decimal("0"), fee_shares=Decimal("0"))
    state = strategy.live_inventory_cost["inst-1"]
    assert state["qty"] == Decimal("5.2")
    assert state.get("entry_bid_at_fill") is None


def test_exit_depth_small_negative_l2_age_is_same_cycle_skew_not_stale():
    # LIVE run_1791621743: the L2 callback landed after the protective cycle
    # captured now_ts, giving ages of -0.77..-0.88 s.  That is a same-cycle
    # clock-read ordering artefact, not an old book.
    for raw in (-0.77, -0.88, -1.0, -0.0):
        m = exit_depth_metrics(LEVELS, Decimal("5"), l2_age_sec=raw)
        assert m["exit_l2_state"] == "FRESH", raw
        assert m["exit_l2_age_sec"] == 0.0, raw
    # Beyond the documented bound the reading is not trusted as fresh.
    far = exit_depth_metrics(LEVELS, Decimal("5"), l2_age_sec=-1.5)
    assert far["exit_l2_state"] == "STALE" and far["exit_l2_age_sec"] == -1.5
    # Genuinely old books stay STALE; the boundary stays FRESH.
    assert exit_depth_metrics(LEVELS, Decimal("5"), l2_age_sec=2.5)["exit_l2_state"] == "STALE"
    assert exit_depth_metrics(LEVELS, Decimal("5"), l2_age_sec=2.0)["exit_l2_state"] == "FRESH"
    # No book: age is still normalised, state stays UNKNOWN.
    none_book = exit_depth_metrics(None, Decimal("5"), l2_age_sec=-0.8)
    assert none_book["exit_l2_state"] == "UNKNOWN" and none_book["exit_l2_age_sec"] == 0.0
