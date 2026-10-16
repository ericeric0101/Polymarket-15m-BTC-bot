"""LIVE-only early-warning fields on STOP_TIMING transition rows.

* Executable entry baseline (FIRST BUY fill, same clock, never a midpoint).
* Chainlink spot + age and a Chainlink-vs-strike cross state (settlement
  family; tie settles UP).  No Binance-vs-strike cross exists anywhere.
* Binance relative move from the Binance spot at entry.
* Held-side bid state FRESH_BID / STALE / BOOK_EMPTY and TOKEN_DD.
Observation only: execution outputs are identical with telemetry on/off.
"""
import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bot.enums import ActiveSide
from bot.stop_timing_telemetry import StopTimingTelemetry, entry_bid_baseline
from test_absolute_max_loss_breaker import _BreakerExitHost
from test_live_path_regressions import DummyStrategyForFill
from test_stop_timing_telemetry import END, SLUG, Sink, _decision, _make_config, _signal


def _state(**updates):
    state = {"qty": Decimal("5.5"), "avg_entry_price": Decimal("0.76"), "opened_ts": END - 600,
             "entry_fill_price_first": Decimal("0.76"), "entry_bid_at_fill": Decimal("0.75"),
             "entry_ask_at_fill": Decimal("0.76"), "entry_quote_age_at_fill_sec": 0.4,
             "entry_bid_size_at_fill": Decimal("20"), "entry_binance_spot": Decimal("100000"),
             "entry_binance_spot_age_sec": 0.2}
    state.update(updates)
    return state


def _obs(tel, now, *, side="UP", state=None, bid="0.60", chainlink=100000.0, chainlink_age=0.3,
         binance=100010.0, held_bid_age=0.5, held_bid_size="30", strike="100000"):
    tel.observe_safe(
        now_ts=now, slug=SLUG, instrument_id=f"{side.lower()}-token", held_side=side,
        state=state if state is not None else _state(), qty=Decimal("5.5"), sellable_qty=Decimal("5.5"),
        best_bid=Decimal(bid), best_ask=Decimal("0.99"), time_left_sec=END - now, market_end_ts=END,
        strike=Decimal(strike), reference_spot=Decimal("100000"), reference_source="twap", reference_ts=now - 0.2,
        twap_features={"official_current_twap": 100001.0, "source_ts": now - 0.5, "required_move_sigma": 0.8,
                       "required_move_z_diffusion": 0.6},
        exit_decision=_decision(), engine_config=_make_config(), signal_decision=_signal(),
        locked_side_invalidated=False, adverse_persistence_sec=0.0, thesis_votes={}, hold_sec=60.0,
        binance_spot=binance, binance_spot_ts=now - 0.2 if binance is not None else None,
        chainlink_spot=chainlink, chainlink_spot_ts=(now - chainlink_age) if chainlink is not None else None,
        held_bid_ts=(now - held_bid_age) if held_bid_age is not None else None,
        held_bid_size=Decimal(held_bid_size) if held_bid_size is not None else None,
        quote_fresh_max_age_sec=2.0, spot_fresh_max_age_sec=10.0,
    )


def _opened(**kwargs):
    sink = Sink()
    tel = StopTimingTelemetry(emit=sink, run_id="r")
    _obs(tel, END - 590, **kwargs)
    (row,) = sink.of("STOP_TIMING_POSITION_OPENED")
    assert tel.counters["exceptions"] == 0
    return row


# ------------------------------------------------------------ entry baseline
def test_position_opened_carries_executable_entry_baseline():
    row = _opened()
    assert row["entry_fill_price"] == 0.76
    assert row["entry_executable_bid"] == 0.75 and row["entry_executable_ask"] == 0.76
    assert row["entry_bid_age_sec"] == 0.4 and row["entry_bid_state"] == "FRESH_BID"
    assert row["entry_binance_spot"] == 100000.0


@pytest.mark.parametrize("updates,state", [
    ({"entry_quote_age_at_fill_sec": 5.0}, "STALE"),
    ({"entry_bid_size_at_fill": Decimal("0")}, "BOOK_EMPTY"),
    ({"entry_quote_synthesis": "missing_bid"}, "BOOK_EMPTY"),
    ({"entry_quote_synthesis": "crossed_book"}, "UNKNOWN"),
    ({"entry_bid_at_fill": None}, "UNKNOWN"),
    ({"entry_quote_age_at_fill_sec": None}, "UNKNOWN"),
])
def test_unusable_entry_baseline_is_unknown_never_substituted(updates, state):
    row = _opened(state=_state(**updates))
    assert row["entry_executable_bid"] is None
    assert row["entry_bid_state"] == state


def test_baseline_helper_is_pure_and_failsafe():
    assert entry_bid_baseline(None, 2.0)["entry_bid_state"] == "UNKNOWN"
    assert entry_bid_baseline({"entry_bid_at_fill": "x"}, 2.0)["entry_executable_bid"] is None
    assert entry_bid_baseline(_state(), None)["entry_executable_bid"] is None


# ------------------------------------------------------- Chainlink vs strike
def test_tie_settles_up_for_chainlink_cross_and_binance_is_not_a_strike_cross():
    up = _opened(side="UP")
    assert up["chainlink_spot"] == 100000.0 and up["chainlink_spot_fresh"] is True
    assert abs(up["chainlink_spot_age_sec"] - 0.3) < 1e-6
    assert up["chainlink_cross_state"] == "FAVORABLE" and up["chainlink_signed_distance_bps"] == 0.0
    down = _opened(side="DOWN")
    assert down["chainlink_cross_state"] == "ADVERSE"   # equal price: tie is adverse for DOWN
    # Binance (100010) is +1 bp vs the Chainlink-family strike, but no row ever
    # carries a Binance-vs-strike cross.
    assert not [key for key in up if "binance" in key and "cross" in key]


@pytest.mark.parametrize("side,chainlink,expected", [
    ("UP", 99990.0, "ADVERSE"), ("UP", 100010.0, "FAVORABLE"),
    ("DOWN", 99990.0, "FAVORABLE"), ("DOWN", 100010.0, "ADVERSE"),
])
def test_chainlink_cross_sign_convention(side, chainlink, expected):
    assert _opened(side=side, chainlink=chainlink)["chainlink_cross_state"] == expected


def test_stale_or_missing_chainlink_never_forms_a_cross():
    stale = _opened(chainlink_age=12.0)
    assert stale["chainlink_spot_fresh"] is False and stale["chainlink_cross_state"] == "UNKNOWN"
    assert stale["chainlink_spot"] == 100000.0 and stale["chainlink_spot_age_sec"] == pytest.approx(12.0)
    missing = _opened(chainlink=None)
    assert missing["chainlink_spot"] is None and missing["chainlink_cross_state"] == "UNKNOWN"


# ------------------------------------------------------------ Binance move
def test_binance_relative_move_uses_binance_entry_baseline():
    up = _opened(side="UP", binance=99970.0)
    assert up["binance_adverse_move_bps"] == pytest.approx(3.0)
    down = _opened(side="DOWN", binance=99970.0)
    assert down["binance_adverse_move_bps"] == pytest.approx(-3.0)
    assert _opened(state=_state(entry_binance_spot=None))["binance_adverse_move_bps"] is None
    assert _opened(state=_state(entry_binance_spot_age_sec=30.0))["binance_adverse_move_bps"] is None


# ------------------------------------------------------- held side / token DD
def test_held_side_fresh_bid_gives_exact_token_dd():
    row = _opened(bid="0.60")
    assert row["held_side_bid_state"] == "FRESH_BID" and row["held_side_bid_fresh"] is True
    assert row["held_side_bid_age_sec"] == pytest.approx(0.5)
    assert row["token_dd_state"] == "VALUE" and row["token_dd"] == 0.15  # 0.75 - 0.60 exactly


def test_held_side_stale_is_unknown_and_book_empty_is_exit_unavailable():
    stale = _opened(held_bid_age=5.0)
    assert stale["held_side_bid_state"] == "STALE" and stale["held_side_bid_fresh"] is False
    assert stale["token_dd"] is None and stale["token_dd_state"] == "UNKNOWN"
    empty = _opened(bid="0.001", held_bid_size="0")
    assert empty["held_side_bid_state"] == "BOOK_EMPTY"
    assert empty["token_dd"] is None and empty["token_dd_state"] == "EXIT_UNAVAILABLE"
    unknown = _opened(held_bid_age=None)
    assert unknown["held_side_bid_state"] == "UNKNOWN" and unknown["token_dd_state"] == "UNKNOWN"


def test_no_entry_baseline_means_token_dd_unknown():
    row = _opened(state=_state(entry_quote_age_at_fill_sec=9.0))
    assert row["token_dd"] is None and row["token_dd_state"] == "UNKNOWN"


def test_exit_depth_and_l2_freshness_are_explicit():
    row = _opened()
    for key in ("exit_l2_state", "exit_l2_fresh", "exit_top_bid_size", "exit_depth_total_size",
                "exit_depth_covers_qty", "exit_vwap_for_qty", "sellable_qty", "best_bid"):
        assert key in row
    assert row["exit_l2_fresh"] is False  # no book passed -> UNKNOWN, not fresh


# --------------------------------------------------------- LIVE-path harness
def _live_host(telemetry):
    host = _BreakerExitHost()
    host.active_side = ActiveSide.DOWN
    host.side_decision_score = Decimal("-0.30")
    host.market_strike_cache_by_slug = {"breaker-test": Decimal("100")}
    now = 10_000.0
    host._polymarket_chainlink_price = 99.5
    host._polymarket_chainlink_price_ts = now - 0.4
    host._binance_ws_price = 99.6
    host._binance_ws_price_ts = now - 0.2
    host.quote_max_delivery_delay_sec = 2.0
    host._RAW_SPOT_FRESHNESS_SEC = 10.0
    host.last_quote_update_ts_by_inst = {"up": now - 0.3}
    host.latest_quote_depth_by_inst = {"up": (Decimal("40"), Decimal("10"))}
    host.live_inventory_cost["up"].update(_state(entry_bid_at_fill=Decimal("0.68"), entry_ask_at_fill=Decimal("0.69"),
                                                 entry_fill_price_first=Decimal("0.69"), qty=Decimal("5.3"),
                                                 avg_entry_price=Decimal("0.69"), opened_ts=now - 90))
    if telemetry is not None:
        host.stop_timing_telemetry = telemetry
    asyncio.run(host._maybe_taker_exit_positions(now, is_simulation=False))
    return host


def test_live_path_would_emit_every_research_field_and_execution_is_identical():
    sink = Sink()
    baseline = _live_host(None)
    observed = _live_host(StopTimingTelemetry(emit=sink, run_id="r"))
    assert baseline.submissions == observed.submissions and len(baseline.submissions) == 1
    (opened,) = sink.of("STOP_TIMING_POSITION_OPENED")
    expected = {
        "entry_executable_bid": 0.68, "entry_bid_state": "FRESH_BID", "entry_fill_price": 0.69,
        "held_side": "UP", "held_side_bid_state": "FRESH_BID", "held_side_bid_fresh": True,
        "chainlink_spot": 99.5, "chainlink_spot_fresh": True, "chainlink_cross_state": "ADVERSE",
        "binance_spot": 99.6, "binance_spot_fresh": True, "settlement_reference_state": "FRESH",
    }
    for key, value in expected.items():
        assert opened[key] == value, key
    for key in ("sellable_qty", "best_bid", "exit_l2_state", "exit_depth_covers_qty", "time_left_sec",
                "required_move_sigma_legacy", "required_move_z_diffusion", "settlement_reference_age_sec",
                "chainlink_spot_age_sec", "binance_spot_age_sec", "held_side_bid_age_sec", "token_dd_state"):
        assert key in opened, key
    assert opened["token_dd"] == pytest.approx(0.48)  # 0.68 - 0.20 executable bid


class _ExplodingTelemetry:
    disabled = False

    def observe_safe(self, **_kwargs):
        raise RuntimeError("telemetry crashed")


def test_live_path_inputs_that_explode_cannot_change_execution():
    baseline = _live_host(None)
    host = _BreakerExitHost()
    host.active_side = ActiveSide.DOWN
    host.side_decision_score = Decimal("-0.30")
    host.market_strike_cache_by_slug = {"breaker-test": Decimal("100")}
    host.latest_quote_depth_by_inst = SimpleNamespace()          # not a dict
    host.last_quote_update_ts_by_inst = None
    host._polymarket_chainlink_price = object()
    host.stop_timing_telemetry = StopTimingTelemetry(emit=Sink(), run_id="r")
    asyncio.run(host._maybe_taker_exit_positions(10_000.0, is_simulation=False))
    assert host.submissions == baseline.submissions


def test_dry_run_claims_no_live_fields():
    host = _BreakerExitHost()
    sink = Sink()
    host.stop_timing_telemetry = StopTimingTelemetry(emit=sink, run_id="r")
    asyncio.run(host._maybe_taker_exit_positions(10_000.0, is_simulation=True))
    assert sink.rows == [] and host.submissions == []


# ------------------------------------------------------- first-fill capture
def test_first_buy_fill_captures_baseline_inputs_once():
    strategy = DummyStrategyForFill()
    quotes = iter([(Decimal("0.45"), Decimal("0.46")), (Decimal("0.30"), Decimal("0.31"))])
    strategy._get_quote_for_instrument = lambda _inst: next(quotes)
    strategy._quote_synthesis_reason = lambda _inst: None
    strategy.last_quote_update_ts_by_inst = {}
    strategy.latest_quote_depth_by_inst = {"inst-1": (Decimal("25"), Decimal("9"))}
    strategy._binance_ws_price = 100_000.0
    strategy._binance_ws_price_ts = 1.0
    strategy._polymarket_chainlink_price = 99_990.0
    strategy._polymarket_chainlink_price_ts = 1.0
    for price, qty in (("0.46", "5.2"), ("0.31", "1")):
        strategy._update_live_inventory_cost_from_fill(instrument_id="inst-1", side="buy", fill_price=Decimal(price),
                                                       fill_qty=Decimal(qty), fee_usdc=Decimal("0"),
                                                       fee_shares=Decimal("0"))
        strategy.latest_quote_depth_by_inst = {"inst-1": (Decimal("0"), Decimal("9"))}
        strategy._binance_ws_price = 1.0
    state = strategy.live_inventory_cost["inst-1"]
    assert state["entry_fill_price_first"] == Decimal("0.46")
    assert state["entry_bid_size_at_fill"] == Decimal("25")
    assert state["entry_quote_synthesis"] is None
    assert state["entry_binance_spot"] == Decimal("100000.0")
    assert state["entry_chainlink_spot"] == Decimal("99990.0")
    assert state["entry_binance_spot_age_sec"] is not None


def test_first_fill_research_capture_failure_never_blocks_the_fill():
    strategy = DummyStrategyForFill()
    strategy._get_quote_for_instrument = lambda _inst: (Decimal("0.45"), Decimal("0.46"))

    def boom(_inst):
        raise RuntimeError("synthesis map gone")

    strategy._quote_synthesis_reason = boom
    strategy.latest_quote_depth_by_inst = None
    strategy._binance_ws_price = "garbage"
    strategy._update_live_inventory_cost_from_fill(instrument_id="inst-1", side="buy", fill_price=Decimal("0.46"),
                                                   fill_qty=Decimal("5.2"), fee_usdc=Decimal("0"),
                                                   fee_shares=Decimal("0"))
    state = strategy.live_inventory_cost["inst-1"]
    assert state["qty"] == Decimal("5.2") and state["entry_bid_at_fill"] == Decimal("0.45")
