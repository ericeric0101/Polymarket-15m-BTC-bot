from collections import deque
import pytest

from bot.prediction_research_snapshot import (
    PredictionResearchSnapshotter,
    build_prediction_snapshot,
)
from scripts.prediction_snapshot_analysis import (
    attach_future_repricing,
    cluster_episodes,
    summarize_residual_bins,
)


def _context(**updates):
    value = {
        "snapshot_ts": 100.0,
        "trigger": "periodic",
        "identity": {"market_slug": "btc-updown-15m-100", "run_id": "test"},
        "p_up_ex_market": 0.7,
        "p_ex_age_sec": 0.5,
        "p_ex_source_ts": 99.5,
        "sigma_ex_market_fresh": True,
        "sigma_ex_market": 0.5,
        "market_max_age_sec": 2,
        "market_up_source_ts": 99.5,
        "market_up_received_ts": 99.6,
        "market_up_source_age_sec": 0.5,
        "market_up_receive_age_sec": 0.4,
        "best_bid_up": 0.60,
        "best_ask_up": 0.62,
        "market_down_source_ts": 99.5,
        "market_down_received_ts": 99.6,
        "market_down_source_age_sec": 0.5,
        "market_down_receive_age_sec": 0.4,
        "best_bid_down": 0.38,
        "best_ask_down": 0.40,
        "btc_spot": 100_000,
        "btc_source_ts": 99.5,
        "btc_received_ts": 99.6,
        "btc_source_reference_ts": 99.5,
        "market_source_reference_ts": 100.0,
        "btc_max_age_sec": 10,
        "btc_return_1s_bps": 1,
        "btc_return_1s_prior_age_sec": 0.1,
        "btc_return_5s_bps": 2,
        "btc_return_5s_prior_age_sec": 0.3,
        "btc_return_10s_bps": 3,
        "btc_return_10s_prior_age_sec": 0.3,
        "btc_return_30s_bps": 4,
        "btc_return_30s_prior_age_sec": 1,
        "btc_return_60s_bps": 5,
        "btc_return_60s_prior_age_sec": 2,
        "twap_source_ts": 99.5,
        "twap_received_ts": 99.5,
        "official_twap": 100_001,
        "settlement_state_side": "UP",
        "active_side": "UP",
        "required_move_mode": "PRE_FINAL_STRIKE_PROXY",
        "required_move_sigma": 0.4,
    }
    value.update(updates)
    return value


def test_fresh_probability_and_quote_form_synchronized_residual_and_ask_edge():
    row = build_prediction_snapshot(_context())
    assert row["p_ex_fresh"] is True
    assert row["market_mid_up"] == 0.61
    assert row["residual_up"] == pytest.approx(0.09)
    assert row["edge_vs_ask_up"] == pytest.approx(0.08)
    assert row["joint_fresh"] is True


def test_stale_probability_or_market_quote_cannot_form_residual():
    stale_probability = build_prediction_snapshot(_context(p_ex_age_sec=11))
    assert stale_probability["p_up_ex_market"] is None
    assert stale_probability["residual_up"] is None
    assert stale_probability["p_ex_unavailable_reason"] == "p_ex_source_stale"

    stale_quote = build_prediction_snapshot(_context(market_up_source_ts=97))
    assert stale_quote["market_mid_up"] is None
    assert stale_quote["residual_up"] is None
    assert stale_quote["market_mid_down"] == 0.39


def test_stale_sigma_invalidates_ex_market_probability():
    row = build_prediction_snapshot(_context(sigma_ex_market_fresh=False))
    assert row["p_up_ex_market"] is None
    assert row["p_down_ex_market"] is None
    assert row["residual_up"] is None
    assert row["p_ex_unavailable_reason"] == "sigma_stale_or_unavailable"


def test_btc_disagreement_is_research_only_side_relative_diagnostic():
    assert build_prediction_snapshot(_context(active_side="UP", btc_return_10s_bps=-1))["btc_disagree_10s"] is True
    assert build_prediction_snapshot(_context(active_side="DOWN", btc_return_10s_bps=1))["btc_disagree_10s"] is True
    assert build_prediction_snapshot(_context(active_side="NONE"))["btc_disagree_10s"] is None


def test_weekday_and_weekend_labels_do_not_change_prediction_snapshot_semantics():
    weekend = build_prediction_snapshot(_context(identity={"market_slug": "btc-updown-15m-1791072000", "run_id": "r"}))
    weekday = build_prediction_snapshot(_context(identity={"market_slug": "btc-updown-15m-1791244800", "run_id": "r"}))
    # Regime is derived offline from market-open time.  The capture layer must
    # not branch its price, freshness, or probability semantics by weekday.
    assert {key: value for key, value in weekend.items() if key != "market_slug"} == {
        key: value for key, value in weekday.items() if key != "market_slug"
    }


def test_btc_return_requires_recent_enough_prior_sample():
    row = build_prediction_snapshot(_context(btc_return_10s_prior_age_sec=0.3))
    assert row["btc_return_10s_bps"] == 3
    row = build_prediction_snapshot(_context(btc_return_10s_prior_age_sec=3))
    assert row["btc_return_10s_bps"] is None


def test_snapshotter_forces_entry_snapshot_and_caps_periodic_rate_without_live_authority():
    class DB:
        def __init__(self):
            self.rows = []

        def enqueue_decision(self, **kwargs):
            self.rows.append(kwargs)
            return True

        def research_health(self):
            return {"queue_depth": 0, "queue_drops": 0}

    class Side:
        value = "UP"

    class Strategy:
        current_market_slug = "btc-updown-15m-100"
        current_market_end_timestamp = 1000
        market_start_ts_by_slug = {}
        market_strike_cache_by_slug = {current_market_slug: 100_000}
        _polymarket_chainlink_twap_price = 100_001
        _polymarket_chainlink_twap_observation_ts = 99.5
        _polymarket_chainlink_twap_price_ts = 99.5
        _binance_ws_price = 100_000
        _binance_ws_price_source_ts = 99.5
        _binance_ws_price_ts = 99.6
        _prediction_btc_research_history = deque([(99.5, 100_000, 99.6)])
        _RAW_SPOT_FRESHNESS_SEC = 10
        quote_max_delivery_delay_sec = 2
        side_signal_btc_trend_primary_stale_sec = 10
        current_up_instrument_id = "up"
        current_down_instrument_id = "down"
        active_side = Side()
        side_decision_score = 0.2
        side_decision_inputs = {}
        last_quote_source_ts_by_inst = {"up": 99.5, "down": 99.5}
        last_quote_received_ts_by_inst = {"up": 99.6, "down": 99.6}
        latest_quote_by_inst = {"up": (0.6, 0.62), "down": (0.38, 0.4)}

        @staticmethod
        def _research_market_quote_instruments(**_kwargs):
            return "up", "down"

        @staticmethod
        def _settlement_probability_shadow_inputs(**kwargs):
            return {"p_up_ex_market": .7, "sigma_ex_market_fresh": True,
                    "sigma_ex_market_age_sec": .2, "sigma_ex_market": .4,
                    "probability_model_mode": "PRE_FINAL_WINDOW_APPROX",
                    "probability_model_version": "test", "required_move_mode": "PRE_FINAL_STRIKE_PROXY",
                    "settlement_state_side": "UP", "required_move_sigma": .3,
                    "path_spot_source": "binance_ws", "path_spot_age_sec": .5,
                    "official_current_twap": kwargs["official_twap"]}

    db = DB()
    snapshotter = PredictionResearchSnapshotter(db=db, run_id="r", interval_sec=1)
    strategy = Strategy()
    markers = []
    strategy.run_id = "r"
    strategy.collection_identity = {"process_instance_id": "p", "cycle_idx": 1}
    class Journal:
        def enqueue_strategy_event(self, *args):
            markers.append(args)
            return True
    strategy.trade_db = Journal()
    first = snapshotter.capture(strategy, now_ts=100, trigger="periodic")
    assert first is not None
    assert snapshotter.capture(strategy, now_ts=100.5, trigger="periodic") is None
    forced = snapshotter.capture(strategy, now_ts=100.5, trigger="entry_decision", force=True,
                                 entry_context={"entry_side": "UP", "entry_price": .61})
    assert forced["snapshot_trigger"] == "entry_decision"
    assert forced["entry_side"] == "UP"
    assert len(markers) == 1
    assert markers[0][2]["transition"] == "first_prediction_snapshot"
    assert markers[0][2]["snapshot_ts"] == 100
    assert len([row for row in db.rows if row["payload"]["event_type"] == "PREDICTION_RESEARCH_SNAPSHOT"]) == 2
    assert not hasattr(strategy, "live_authority_changed")

    class BrokenDB:
        @staticmethod
        def enqueue_decision(**_kwargs):
            raise OSError("research queue unavailable")

    broken_snapshotter = PredictionResearchSnapshotter(db=BrokenDB(), run_id="r")
    assert broken_snapshotter.capture(strategy, now_ts=102, trigger="entry_decision",
                                      force=True) is None
    assert strategy.active_side.value == "UP"


def test_analysis_joins_nearest_fresh_future_quote_and_clusters_repeated_extreme_rows():
    base = {"market_slug": "m1", "market_mid_up_fresh": True,
            "snapshot_trigger": "periodic"}
    rows = [
        {**base, "snapshot_ts": 100.0, "market_mid_up": .60, "residual_up": .12},
        {**base, "snapshot_ts": 110.0, "market_mid_up": .61, "residual_up": .13},
        {**base, "snapshot_ts": 130.8, "market_mid_up": .68, "residual_up": .11},
    ]
    joined = attach_future_repricing(rows, horizons=(30,), tolerance_sec=1.5)
    assert joined[0]["future_mid_up_30s"] == .68
    assert joined[0]["mid_repricing_30s"] == pytest.approx(.08)
    episodes = cluster_episodes(joined, value_key="residual_up",
                                predicate=lambda row: row["residual_up"] >= .10)
    assert len(episodes) == 2
    assert episodes[0]["episode_observations"] == 2


def test_residual_analysis_uses_fixed_bins_and_reports_independent_clusters():
    rows = []
    for market in ("m1", "m2"):
        for idx in range(2):
            rows.append({"market_slug": market, "snapshot_ts": 100 + idx,
                         "residual_up": .12, "mid_repricing_30s": .03})
    bins = summarize_residual_bins(rows)
    positive = next(row for row in bins if row["residual_bin"] == ">= +0.10")
    assert positive["observations"] == 4
    assert positive["episodes"] == 2
    assert positive["markets"] == 2
    assert positive["directional_hit_rate"] == 1


@pytest.mark.parametrize("source_ts", [100.50, 99.50])
def test_remote_clock_offset_does_not_define_transport_age(source_ts):
    row = build_prediction_snapshot(_context(
        snapshot_ts=100.20, market_up_received_ts=100.0, market_down_received_ts=100.0,
        market_up_source_ts=source_ts, market_down_source_ts=source_ts,
        market_source_reference_ts=source_ts, btc_source_ts=source_ts,
        btc_source_reference_ts=source_ts, btc_received_ts=100.0,
        twap_source_ts=source_ts, twap_received_ts=100.0))
    assert row["market_quote_up_receive_age_sec"] == pytest.approx(.20)
    assert row["market_quote_up_source_age_sec"] == 0
    assert row["btc_transport_age_sec"] == pytest.approx(.20)
    assert row["btc_value_age_sec"] == 0
    assert row["twap_age_sec"] == pytest.approx(.20)
    assert row["joint_fresh"] is True
    assert row["freshness_clock_semantics_version"] == 2


def test_behind_remote_clock_is_not_transport_latency():
    row = build_prediction_snapshot(_context(snapshot_ts=100.3, btc_received_ts=100,
        btc_source_ts=99.5, btc_source_reference_ts=99.5, market_up_received_ts=100))
    assert row["btc_age_sec"] == pytest.approx(.3)
    assert row["market_quote_up_receive_age_sec"] == pytest.approx(.3)


@pytest.mark.parametrize("updates,component", [
    ({"market_up_received_ts": 95, "market_down_received_ts": 95}, "market_mid_fresh"),
    ({"btc_received_ts": 89}, "btc_fresh"),
    ({"btc_received_ts": 101}, "btc_fresh"),
    ({"btc_source_reference_ts": 110, "btc_source_ts": 95}, "btc_fresh"),
    ({"btc_source_reference_ts": 95, "btc_source_ts": 110}, "btc_fresh"),
    ({"market_source_reference_ts": 110}, "market_mid_fresh"),
    ({"market_source_reference_ts": 95}, "market_mid_fresh"),
    ({"twap_received_ts": 89}, "twap_fresh"),
    ({"sigma_ex_market_fresh": False}, "p_ex_fresh"),
    ({"p_ex_age_sec": -0.3}, "p_ex_fresh"),
    ({"btc_received_ts": None}, "btc_fresh"),
    ({"btc_source_reference_ts": None}, "btc_fresh"),
    ({"twap_received_ts": None}, "twap_fresh"),
])
def test_each_component_remains_required_without_clamping(updates, component):
    row = build_prediction_snapshot(_context(**updates))
    assert row[component] is False
    assert row["joint_fresh"] is False
    if updates.get("btc_source_reference_ts") == 95:
        assert row["btc_value_age_sec"] == -15


def test_no_market_source_reference_is_explicitly_unavailable_not_offset_fitted():
    row = build_prediction_snapshot(_context(market_source_reference_ts=None,
        market_up_source_ts=100.5, market_down_source_ts=100.5))
    assert row["market_source_age_sec"] is None
    assert row["market_quote_up_fresh"] is True  # validated receipt + BBO; no invented source clock
    missing = build_prediction_snapshot(_context(market_up_source_ts=None))
    assert missing["market_quote_up_fresh"] is False


def _clock_capture_strategy(history):
    from types import SimpleNamespace
    return SimpleNamespace(
        current_market_slug="btc-updown-15m-1", current_market_end_timestamp=901,
        market_start_ts_by_slug={}, market_strike_cache_by_slug={"btc-updown-15m-1": 100},
        _polymarket_chainlink_twap_price=101, _polymarket_chainlink_twap_observation_ts=100.5,
        _polymarket_chainlink_twap_price_ts=100,
        _binance_ws_price_source_ts=100.5, _binance_ws_price_ts=100,
        _prediction_btc_research_history=deque(history),
        last_quote_source_ts_by_inst={"up":100.5,"down":100.5},
        last_quote_received_ts_by_inst={"up":100,"down":100},
        latest_quote_by_inst={"up":(.6,.62),"down":(.38,.4)},
        _research_market_quote_instruments=lambda **_: ("up","down"),
        _settlement_probability_shadow_inputs=lambda **_: {
            "path_spot_source":"binance_ws", "p_up_ex_market":.7,
            "sigma_ex_market_fresh":True, "sigma_ex_market_age_sec":.2},
    )


def test_capture_uses_received_ticks_not_remote_wall_clock_and_keeps_denominator():
    class DB:
        def __init__(self): self.accept = True
        def enqueue_decision(self, **kwargs): return self.accept
    db = DB()
    strategy = _clock_capture_strategy([(95.5, 99, 95), (100.5, 100, 100), (200, 999, 101)])
    snapper = PredictionResearchSnapshotter(db=db, run_id="r")
    row = snapper.capture(strategy, now_ts=100.2, force=True)
    assert row["btc_source_ts"] == 100.5
    assert row["btc_source_reference_ts"] == 100.5  # future receipt excluded
    assert row["btc_return_5s_bps"] == pytest.approx((100/99 - 1)*10000)
    assert row["joint_fresh"] is True
    assert row["p_ex_age_sec"] == pytest.approx(.2)
    assert snapper._counters["written"] == 1 and snapper._fresh["joint"] == 1
    db.accept = False
    assert snapper.capture(strategy, now_ts=101.2, force=True) is None
    assert snapper._counters["written"] == 1 and snapper._fresh["joint"] == 1
    assert snapper._counters["dropped"] == 1
    db.accept = True
    strategy.last_quote_received_ts_by_inst = {"up":95,"down":95}
    assert snapper.capture(strategy, now_ts=102.2, force=True)["joint_fresh"] is False
    assert snapper._counters["written"] == 2 and snapper._fresh["joint"] == 1
    assert snapper.recent_health(102.2)["persisted_joint_fresh_pct"] == 50


def test_capture_out_of_order_btc_value_is_stale_with_recent_receipt():
    strategy = _clock_capture_strategy([(110, 100, 99), (95, 99, 100)])
    class DB:
        def enqueue_decision(self, **_): return True
    row = PredictionResearchSnapshotter(db=DB(), run_id="r").capture(strategy, now_ts=100.2, force=True)
    assert row["btc_transport_age_sec"] == pytest.approx(.2)
    assert row["btc_value_age_sec"] == 15
    assert row["btc_fresh"] is False


def test_source_reference_never_admits_future_local_receipt():
    from bot.research.clocks import observed_source_reference
    assert observed_source_reference({"up":100.5,"down":200},
        {"up":100,"down":101}, ("up","down"), 100.2) == 100.5
