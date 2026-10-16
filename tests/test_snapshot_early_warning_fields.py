"""Early-warning research fields on the 1 s prediction snapshot (schema v2).

Fix A: Chainlink spot (the settlement reference family) is recorded beside the
Binance spot so a strike cross never has to mix the two sources.
Fix B: per-side and held-side bid freshness with FRESH_BID / STALE /
BOOK_EMPTY, independent of the opposite token.  Legacy ``market_quote_fresh``
is unchanged.  All fields are observation-only and additive.
"""
import time
from collections import deque
from decimal import Decimal

import pytest

from bot.prediction_research_snapshot import (
    BID_BOOK_EMPTY,
    BID_FRESH,
    BID_STALE,
    BID_UNKNOWN,
    SNAPSHOT_SCHEMA_VERSION,
    PredictionResearchSnapshotter,
    build_prediction_snapshot,
    side_bid_state,
)
from bot.research.provenance import build_run_manifest
from test_prediction_research_snapshot import _context

NEW_KEYS = ("snapshot_schema_version", "chainlink_spot", "chainlink_spot_age_sec", "chainlink_spot_fresh",
            "chainlink_source_ts", "up_bid_state", "down_bid_state", "up_bid_fresh", "down_bid_fresh",
            "held_side")


def _ew(**updates):
    base = {"chainlink_spot": 100_000.0, "chainlink_received_ts": 99.7, "chainlink_source_ts": 99.0,
            "chainlink_max_age_sec": 10.0, "market_up_bid_size": 50.0, "market_down_bid_size": 40.0}
    base.update(updates)
    return _context(**base)


def test_schema_version_is_in_every_row_and_in_the_run_manifest():
    row = build_prediction_snapshot(_ew())
    assert row["snapshot_schema_version"] == SNAPSHOT_SCHEMA_VERSION == 2
    manifest = build_run_manifest(run_id="r", config={}, mode="TEST", test_mode=True, maker_mode=True)
    assert manifest["schema_versions"]["snapshot_schema_version"] == SNAPSHOT_SCHEMA_VERSION


def test_fresh_chainlink_spot_uses_local_receipt_age():
    row = build_prediction_snapshot(_ew())
    assert row["chainlink_spot"] == 100_000.0
    assert row["chainlink_spot_age_sec"] == pytest.approx(0.3)
    assert row["chainlink_spot_fresh"] is True
    assert row["chainlink_source_ts"] == 99.0  # source clock is metadata only


def test_stale_chainlink_is_never_reported_as_fresh():
    row = build_prediction_snapshot(_ew(chainlink_received_ts=90.0))  # 10 s old == bound -> stale
    assert row["chainlink_spot"] is None
    assert row["chainlink_spot_fresh"] is False
    assert row["chainlink_spot_age_sec"] == pytest.approx(10.0)


def test_missing_chainlink_is_unknown_not_invented():
    row = build_prediction_snapshot(_context())
    assert row["chainlink_spot"] is None and row["chainlink_spot_fresh"] is False
    assert row["chainlink_spot_age_sec"] is None


def test_binance_spot_fields_are_unchanged():
    legacy = build_prediction_snapshot(_context())
    row = build_prediction_snapshot(_ew())
    for key in ("btc_spot", "btc_age_sec", "btc_fresh", "official_twap", "twap_fresh", "market_quote_fresh",
                "best_bid_up", "best_bid_down", "required_move_sigma", "joint_fresh"):
        assert row[key] == legacy[key]


@pytest.mark.parametrize("fresh,observed,bid,size,expected", [
    (True, True, 0.60, 50, BID_FRESH),
    (False, True, 0.60, 50, BID_STALE),
    (False, False, None, None, BID_UNKNOWN),
    (True, True, 0.001, 0, BID_BOOK_EMPTY),     # adapter placeholder for an empty bid side
    (True, True, 0.0, None, BID_BOOK_EMPTY),
    (True, True, 0.60, None, BID_FRESH),          # size unknown: a positive bid is still a bid
])
def test_side_bid_state(fresh, observed, bid, size, expected):
    assert side_bid_state(fresh=fresh, observed=observed, bid=bid, bid_size=size) == expected


def test_held_up_fresh_while_down_book_is_stale():
    row = build_prediction_snapshot(_ew(market_down_received_ts=90.0, held_instrument_id="up",
                                        held_side="UP", held_qty=5.5, held_quote_value_ts=99.6,
                                        held_entry_executable_bid=0.75))
    assert row["market_quote_fresh"] is False          # legacy both-sides flag is unchanged
    assert row["down_bid_state"] == BID_STALE
    assert row["held_side"] == "UP"
    assert row["held_side_bid_state"] == BID_FRESH
    assert row["held_side_bid_fresh"] is True
    assert row["held_side_bid"] == 0.60
    assert row["held_side_quote_age_sec"] == pytest.approx(0.4)
    assert row["held_entry_executable_bid"] == 0.75


def test_held_side_stale_and_book_empty_are_distinct():
    stale = build_prediction_snapshot(_ew(market_up_received_ts=90.0, held_instrument_id="up", held_side="UP"))
    assert stale["held_side_bid_state"] == BID_STALE
    assert stale["held_side_bid_fresh"] is False and stale["held_side_bid"] is None
    empty = build_prediction_snapshot(_ew(best_bid_up=0.001, market_up_bid_size=0.0,
                                          held_instrument_id="up", held_side="UP"))
    assert empty["held_side_bid_state"] == BID_BOOK_EMPTY
    assert empty["held_side_bid_fresh"] is False
    assert empty["held_side_bid"] == 0.001  # recorded, never treated as an executable bid


def test_no_position_writes_no_held_side_claims():
    row = build_prediction_snapshot(_ew())
    assert row["held_side"] is None
    assert "held_side_bid_state" not in row and "held_entry_executable_bid" not in row


def test_field_builder_fault_never_drops_the_snapshot(monkeypatch):
    import bot.prediction_research_snapshot as module

    def boom(**_kwargs):
        raise RuntimeError("telemetry bug")

    monkeypatch.setattr(module, "side_bid_state", boom)
    row = build_prediction_snapshot(_ew())
    legacy = build_prediction_snapshot(_context())
    assert row["early_warning_fields_error"] == "RuntimeError"
    assert {k: v for k, v in row.items() if k in legacy} == legacy


def test_none_inputs_are_tolerated():
    row = build_prediction_snapshot(_ew(chainlink_spot=None, chainlink_received_ts=None,
                                        market_up_bid_size=None, held_instrument_id="up", held_side="UP",
                                        held_qty=None, held_quote_value_ts=None, held_entry_executable_bid=None))
    assert row["chainlink_spot_fresh"] is False
    assert row["held_side_quote_age_sec"] is None and row["held_entry_executable_bid"] is None
    assert "early_warning_fields_error" not in row


# ----------------------------------------------------------------- capture()
class _DB:
    def __init__(self):
        self.rows = []

    def enqueue_decision(self, **kwargs):
        self.rows.append(kwargs)
        return True


def _strategy(**updates):
    from types import SimpleNamespace
    values = dict(
        current_market_slug="btc-updown-15m-0", current_market_end_timestamp=900,
        market_start_ts_by_slug={}, market_strike_cache_by_slug={"btc-updown-15m-0": 100_000},
        _polymarket_chainlink_twap_price=100_001, _polymarket_chainlink_twap_observation_ts=99.5,
        _polymarket_chainlink_twap_price_ts=99.5,
        _polymarket_chainlink_price=100_002.5, _polymarket_chainlink_price_ts=99.8,
        _polymarket_chainlink_price_observation_ts=99.0,
        _binance_ws_price_source_ts=99.5, _binance_ws_price_ts=99.6,
        _prediction_btc_research_history=deque([(99.5, 100_010, 99.6)]),
        _RAW_SPOT_FRESHNESS_SEC=10.0, quote_max_delivery_delay_sec=2.0,
        last_quote_source_ts_by_inst={"up": 99.5, "down": 99.5},
        last_quote_received_ts_by_inst={"up": 99.6, "down": 99.6},
        last_quote_update_ts_by_inst={"up": 99.6, "down": 99.6},
        latest_quote_by_inst={"up": (Decimal("0.60"), Decimal("0.62")), "down": (Decimal("0.001"), Decimal("0.40"))},
        latest_quote_depth_by_inst={"up": (Decimal("50"), Decimal("20")), "down": (Decimal("0"), Decimal("30"))},
        live_inventory_cost={},
        _research_market_quote_instruments=lambda **_: ("up", "down"),
        _settlement_probability_shadow_inputs=lambda **_: {
            "path_spot_source": "binance_ws", "p_up_ex_market": .7,
            "sigma_ex_market_fresh": True, "sigma_ex_market_age_sec": .2},
    )
    values.update(updates)
    return SimpleNamespace(**values)


def test_capture_records_chainlink_and_per_side_book_state():
    db = _DB()
    row = PredictionResearchSnapshotter(db=db, run_id="r").capture(_strategy(), now_ts=100.0, force=True)
    assert row["chainlink_spot"] == 100_002.5 and row["chainlink_spot_fresh"] is True
    assert row["chainlink_spot_age_sec"] == pytest.approx(0.2)
    assert row["btc_spot"] == 100_010  # Binance, unchanged
    assert row["up_bid_state"] == BID_FRESH and row["down_bid_state"] == BID_BOOK_EMPTY
    assert row["held_side"] is None  # DRY-RUN / flat: no live inventory, no held-side claims
    assert "held_side_bid_state" not in row


def test_capture_held_side_comes_from_live_inventory_not_price():
    inventory = {"down": {"qty": Decimal("6"), "avg_entry_price": Decimal("0.70"),
                          "entry_bid_at_fill": Decimal("0.69"), "entry_ask_at_fill": Decimal("0.71"),
                          "entry_quote_age_at_fill_sec": 0.3, "entry_bid_size_at_fill": Decimal("12")}}
    row = PredictionResearchSnapshotter(db=_DB(), run_id="r").capture(
        _strategy(live_inventory_cost=inventory), now_ts=100.0, force=True)
    assert row["held_side"] == "DOWN" and row["down_instrument_id"] == "down"
    assert "held_instrument_id" not in row  # identity = down_instrument_id in the same row
    assert row["held_qty"] == 6.0
    assert row["held_side_bid_state"] == BID_BOOK_EMPTY
    assert row["held_entry_executable_bid"] == 0.69


def test_capture_survives_hostile_new_inputs():
    class Hostile(dict):
        def items(self):
            raise RuntimeError("inventory unavailable")

    row = PredictionResearchSnapshotter(db=_DB(), run_id="r").capture(
        _strategy(live_inventory_cost=Hostile(), latest_quote_depth_by_inst=None,
                  _polymarket_chainlink_price="garbage"), now_ts=100.0, force=True)
    assert row is not None
    assert row["chainlink_spot_fresh"] is False
    assert row["btc_spot"] == 100_010


def test_added_capture_cost_is_small():
    snapper = PredictionResearchSnapshotter(db=_DB(), run_id="r")
    inventory = {"down": {"qty": Decimal("6"), "avg_entry_price": Decimal("0.70")}}
    strategy = _strategy(live_inventory_cost=inventory)
    samples = []
    for i in range(300):
        started = time.perf_counter()
        assert snapper.capture(strategy, now_ts=100.0 + i * 0.001, trigger="entry_decision", force=True)
        samples.append(time.perf_counter() - started)
    samples.sort()
    assert samples[len(samples) // 2] < 0.005  # whole capture (legacy + new) well under 5 ms p50


def test_capture_keeps_legacy_row_when_new_input_gathering_itself_fails(monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("context gathering bug")

    monkeypatch.setattr(PredictionResearchSnapshotter, "_early_warning_context", staticmethod(broken))
    snapper = PredictionResearchSnapshotter(db=_DB(), run_id="r")
    row = snapper.capture(_strategy(), now_ts=100.0, force=True)
    assert row is not None and row["btc_spot"] == 100_010
    assert row["chainlink_spot_fresh"] is False and row["held_side"] is None
    assert snapper._counters["early_warning_context_errors"] == 1
