from decimal import Decimal
from types import SimpleNamespace
import pytest

from bot.trend_entry_shadow import TrendEntryShadow, record_strategy_quote


class ResearchDB:
    def __init__(self):
        self.decisions = []
        self.markouts = []

    def enqueue_decision(self, **kwargs):
        self.decisions.append(kwargs)

    def enqueue_markout(self, **kwargs):
        self.markouts.append(kwargs)


def quote(shadow, *, ts, side="UP", spot="100100", bid="0.59", ask="0.60"):
    bid_decimal, ask_decimal = Decimal(bid), Decimal(ask)
    if ask_decimal < bid_decimal:
        ask_decimal = bid_decimal + Decimal("0.01")
    return shadow.on_quote(
        slug="btc-updown-15m-1000000000",
        market_start_ts=1_000_000_000,
        market_end_ts=1_000_000_900,
        now_ts=ts,
        instrument_id=f"{side}.TOKEN",
        instrument_side=side,
        reference_spot=Decimal(spot),
        reference_ts=ts,
        reference_source="chainlink_twap",
        strike=Decimal("100000"),
        best_bid=bid_decimal,
        best_ask=ask_decimal,
        bid_size=Decimal("12"),
        ask_size=Decimal("18"),
        bid_levels=[(bid_decimal, Decimal("12")), (Decimal("0.58"), Decimal("20"))],
        ask_levels=[(ask_decimal, Decimal("18")), (Decimal("0.61"), Decimal("25"))],
        quote_source_ts=ts - 0.05,
    )


def test_candidate_is_sampled_once_on_signal_side_and_has_price_depth_context():
    db = ResearchDB()
    shadow = TrendEntryShadow(db=db, run_id="run-test")

    assert quote(shadow, ts=1_000_000_060.0, side="DOWN") == 0
    assert db.decisions == []
    assert quote(shadow, ts=1_000_000_060.1, side="UP") == 1
    assert quote(shadow, ts=1_000_000_060.2, side="UP") == 0

    payload = db.decisions[0]["payload"]
    assert payload["event_type"] == "TREND_ENTRY_SHADOW_CANDIDATE"
    assert payload["scheduled_window_sec"] == 60
    assert payload["threshold_bps"] == 0
    assert payload["signal_side"] == "UP"
    assert payload["qualified"] is True
    assert payload["entry_ask"] == 0.60
    assert payload["entry_ask_size"] == 18.0
    assert payload["ask_depth_within_1c"] == 43.0
    assert payload["ask_depth_within_2c"] == 43.0
    assert payload["quote_age_sec"] == pytest.approx(0.05)


def test_threshold_variants_share_direction_but_keep_independent_candidate_rows():
    db = ResearchDB()
    shadow = TrendEntryShadow(db=db, run_id="run-test")

    # 3 bps at T+120: both the 0 and 2 bps candidates qualify; 5 bps does not.
    assert quote(shadow, ts=1_000_000_060.0, spot="100030") == 1
    assert quote(shadow, ts=1_000_000_120.0, spot="100030") == 2
    assert quote(shadow, ts=1_000_000_180.0, spot="100030") == 3
    assert [row["payload"]["threshold_bps"] for row in db.decisions[3:]] == [0, 2, 5]
    assert [row["payload"]["qualified"] for row in db.decisions[3:]] == [True, True, False]
    quote(shadow, ts=1_000_000_181.1, spot="100030")
    t180_marks = [
        row for row in db.markouts
        if row["payload"]["candidate_id"].startswith("btc-updown-15m-1000000000:t180:")
    ]
    assert len({row["candidate_epoch_ns"] for row in t180_marks}) == 3


def test_candidate_records_short_horizon_bid_markouts_and_late_windows():
    db = ResearchDB()
    shadow = TrendEntryShadow(db=db, run_id="run-test", markout_lateness_sec=2.0)

    quote(shadow, ts=1_000_000_060.0)
    quote(shadow, ts=1_000_000_061.1, bid="0.62")
    quote(shadow, ts=1_000_000_065.1, bid="0.64")
    # The 10-second horizon missed its bounded window and is explicitly marked.
    quote(shadow, ts=1_000_000_072.1, bid="0.63")

    by_horizon = {row["horizon_ms"]: row["payload"] for row in db.markouts}
    assert by_horizon[1000]["status"] == "observed"
    assert by_horizon[1000]["markout_bid"] == 0.62
    assert by_horizon[1000]["gross_markout_per_share"] == 0.02
    assert by_horizon[5000]["status"] == "observed"
    assert by_horizon[5000]["markout_bid"] == 0.64
    assert by_horizon[10000]["status"] == "missed_quote_window"


def test_stale_reference_is_not_used_to_create_a_shadow_candidate():
    db = ResearchDB()
    shadow = TrendEntryShadow(db=db, run_id="run-test", max_reference_age_sec=2.0)

    result = shadow.on_quote(
        slug="btc-updown-15m-1000000000",
        market_start_ts=1_000_000_000,
        market_end_ts=1_000_000_900,
        now_ts=1_000_000_060.0,
        instrument_id="UP.TOKEN",
        instrument_side="UP",
        reference_spot=Decimal("100100"),
        reference_ts=1_000_000_050.0,
        reference_source="stale_reference",
        strike=Decimal("100000"),
        best_bid=Decimal("0.59"),
        best_ask=Decimal("0.60"),
        bid_size=Decimal("10"),
        ask_size=Decimal("10"),
        quote_source_ts=1_000_000_059.9,
    )

    assert result == 0
    assert db.decisions == []


def test_market_settlement_joins_outcome_without_creating_order_authority():
    db = ResearchDB()
    shadow = TrendEntryShadow(db=db, run_id="run-test")
    quote(shadow, ts=1_000_000_060.0)

    shadow.on_settlement(slug="btc-updown-15m-1000000000", outcome="UP", settlement_ts=1_000_000_900)

    settlement = next(row for row in db.decisions if row["payload"]["event_type"] == "TREND_ENTRY_SHADOW_SETTLEMENT")
    assert settlement["payload"]["signal_side"] == "UP"
    assert settlement["payload"]["direction_correct"] is True
    assert settlement["payload"]["would_win_if_entered"] is True
    assert not hasattr(shadow, "submit_order")
    assert not hasattr(shadow, "cancel_order")


def test_fresh_strategy_quote_is_forwarded_to_research_recorder_only():
    db = ResearchDB()
    shadow = TrendEntryShadow(db=db, run_id="run-test")

    class Book:
        def bids(self):
            return [(Decimal("0.59"), Decimal("12")), (Decimal("0.58"), Decimal("20"))]

        def asks(self):
            return [(Decimal("0.60"), Decimal("18")), (Decimal("0.61"), Decimal("25"))]

    strategy = SimpleNamespace(
        trend_entry_shadow=shadow,
        current_market_slug="btc-updown-15m-1000000000",
        market_start_ts_by_slug={"btc-updown-15m-1000000000": 1_000_000_000},
        current_market_end_timestamp=1_000_000_900,
        latest_external_spot=Decimal("100100"),
        latest_external_spot_source_ts=1_000_000_060.0,
        latest_external_spot_source="chainlink_twap",
        market_strike_cache_by_slug={"btc-updown-15m-1000000000": Decimal("100000")},
        cache=SimpleNamespace(order_book=lambda _instrument_id: Book()),
        _side_for_instrument_id=lambda _instrument_id: SimpleNamespace(value="UP"),
    )

    emitted = record_strategy_quote(
        strategy,
        instrument_id="UP.TOKEN",
        now_ts=1_000_000_060.0,
        bid=Decimal("0.59"),
        ask=Decimal("0.60"),
        bid_size=Decimal("12"),
        ask_size=Decimal("18"),
        quote_source_ts=1_000_000_059.95,
    )

    assert emitted == 1
    assert db.decisions[0]["payload"]["instrument_id"] == "UP.TOKEN"
    assert db.decisions[0]["payload"]["ask_depth_within_1c"] == 43.0
    assert not hasattr(strategy, "submit_order")
    assert not hasattr(strategy, "cancel_order")
