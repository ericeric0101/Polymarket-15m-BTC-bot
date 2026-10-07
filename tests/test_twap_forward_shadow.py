from decimal import Decimal
import pytest

from bot.twap_forward_shadow import TwapForwardShadow
from bot.spot_pricer import SpotPricerMixin
from bot.settings import build_twap_research_db
import inspect


class FakeDb:
    db_path = "/tmp/no-such-research.db"
    def __init__(self): self.rows = []
    def enqueue_decision(self, **kwargs): self.rows.append(kwargs); return True


def sample(model, ts, spot="101", twap="100", strike="100", left=60):
    return model.observe(
        slug="m", now_ts=ts, source_ts=ts, fast_spot=Decimal(spot), official_twap=Decimal(twap),
        strike=Decimal(strike), time_left_sec=left, best_bid=Decimal("0.50"), best_ask=Decimal("0.51"),
    )


def test_primary_features_and_bounded_buffer():
    model = TwapForwardShadow(max_samples=3)
    for ts, twap in enumerate(("100", "100.02", "100.04", "100.06", "100.08")):
        result = sample(model, float(ts), twap=twap)
    assert model.sample_count("m") == 3
    assert result["twap_minus_strike_bps"] == 8.0
    assert result["spot_minus_twap_bps"] > 0


def test_actual_span_slope_and_insufficient_history_are_not_zero():
    model = TwapForwardShadow()
    first = sample(model, 0, twap="100")
    assert first["twap_slope_5s_bps_per_sec"] is None
    sample(model, 5, twap="100.05")
    result = sample(model, 10, twap="100.10")
    assert result["twap_slope_5s_bps_per_sec"] is not None
    assert result["twap_slope_10s_bps_per_sec"] is not None


def test_flat_and_capped_trend_projection_and_crossing_eta():
    model = TwapForwardShadow(trend_cap_bps=10)
    sample(model, 0, spot="99", twap="99", strike="100", left=30)
    sample(model, 10, spot="101", twap="99.5", strike="100", left=20)
    result = sample(model, 20, spot="102", twap="100", strike="100", left=10)
    assert result["projected_settlement_side_flat"] == "UP"
    assert result["projected_settlement_side_trend"] == "UP"
    assert result["projected_crossing_eta_sec"] is None  # already at strike, not a future crossing
    # The trend increment is capped; the absolute level can already be far
    # from strike because the current fast spot itself is far from strike.
    flat = result["projected_settlement_twap_flat"]
    trend = result["projected_settlement_twap_trend"]
    assert abs((trend - flat) / flat * 10_000) <= 10


def test_moving_away_has_no_crossing_eta_and_rollover_summarizes_then_clears():
    model = TwapForwardShadow()
    sample(model, 0, spot="98", twap="99", strike="100", left=30)
    result = sample(model, 10, spot="97", twap="98", strike="100", left=20)
    assert result["projected_crossing_eta_sec"] is None
    summary = model.finalize_market("m", settlement_side="DOWN")
    assert summary["market_slug"] == "m"
    assert model.sample_count("m") == 0


def test_checkpoints_never_emit_after_settlement_or_fabricate_skipped_horizons():
    db = FakeDb(); model = TwapForwardShadow(db=db)
    sample(model, 100, left=0)
    sample(model, 101, left=-1)
    assert not [r for r in db.rows if r["payload"]["event_type"] == "TMINUS_CHECKPOINT"]

    sample(model, 102, left=58)
    first = [r["payload"] for r in db.rows if r["payload"]["event_type"] == "TMINUS_CHECKPOINT"]
    assert len(first) == 1
    assert first[0]["checkpoint_sec"] == 60
    assert first[0]["time_left_sec"] == 58
    assert first[0]["checkpoint_capture_lag_sec"] == 2

    sample(model, 103, left=29)
    checkpoints = [r["payload"]["checkpoint_sec"] for r in db.rows
                   if r["payload"]["event_type"] == "TMINUS_CHECKPOINT"]
    assert checkpoints == [60, 30]


def test_summary_uses_settlement_timestamp_not_epoch_zero():
    db = FakeDb(); model = TwapForwardShadow(db=db, run_id="r")
    sample(model, 10, twap="100")
    model.finalize_market("m", settlement_side="UP", settlement_ts=20.0,
                          settlement_reference_source="polymarket_chainlink_twap_60s_ws",
                          settlement_reference_is_canonical=True, settlement_reference_age_sec=2.0)
    row = next(row for row in db.rows if row["payload"]["event_type"] == "MARKET_TWAP_SUMMARY")
    assert row["decision_epoch_ns"] == 20_000_000_000
    assert row["payload"]["summary_ts"] == 20.0
    assert row["payload"]["settlement_reference_is_canonical"] is True
    assert row["payload"]["settlement_reference_source"] == "polymarket_chainlink_twap_60s_ws"


def test_storage_guard_preserves_observed_crossing_checkpoint_and_summary(monkeypatch, tmp_path):
    db = FakeDb(); db.db_path = str(tmp_path / "research.db"); (tmp_path / "research.db").write_bytes(b"x")
    model = TwapForwardShadow(db=db, max_db_mb=0.0, min_free_disk_gb=0, storage_check_interval_sec=1)
    sample(model, 100, twap="101")
    sample(model, 101, twap="99")
    assert model.storage_guard_status()["triggered"] is True
    emitted = {row["payload"]["event_type"] for row in db.rows}
    assert {"TWAP_STRIKE_CROSS", "TMINUS_CHECKPOINT"} <= emitted
    assert any(row["payload"]["event_type"] == "RESEARCH_STORAGE_GUARD_TRIGGERED" for row in db.rows)
    model.finalize_market("m", settlement_side="DOWN", settlement_ts=102)
    assert any(row["payload"]["event_type"] == "MARKET_TWAP_SUMMARY" for row in db.rows)


def test_storage_guard_counts_wal_and_shm_in_total_disk_usage(tmp_path):
    db = FakeDb(); db.db_path = str(tmp_path / "research.db")
    (tmp_path / "research.db").write_bytes(b"")
    (tmp_path / "research.db-wal").write_bytes(b"x" * 2048)
    (tmp_path / "research.db-shm").write_bytes(b"x" * 1024)
    model = TwapForwardShadow(db=db, max_db_mb=0.002, min_free_disk_gb=0, storage_check_interval_sec=1)
    sample(model, 100, twap="100")
    status = model.storage_guard_status()
    assert status["triggered"] is True
    guard = next(row["payload"] for row in db.rows if row["payload"]["event_type"] == "RESEARCH_STORAGE_GUARD_TRIGGERED")
    assert guard["db_main_mb"] == 0.0
    assert guard["db_wal_mb"] > 0 and guard["db_shm_mb"] > 0
    assert guard["db_total_disk_mb"] > guard["db_wal_mb"]


def test_twap_ingress_binds_current_tick_timestamp_before_shadow_observe():
    source = inspect.getsource(SpotPricerMixin._polymarket_chainlink_ws_loop)
    block = source[source.index('if self._is_twap_spot_source(tick.source):'):]
    assert block.index("observation_ts = chainlink_observation_ts(tick)") < block.index("twap_shadow.observe(")


def test_twap_research_market_context_uses_wall_clock_market_during_rollover_gap():
    # Runtime market selection can lag the 15-minute boundary. Research must
    # still attribute the first incoming ticks to the market actually open.
    assert SpotPricerMixin._twap_research_market_context(
        1_790_812_815.0, current_slug="btc-updown-15m-1790811900",
        current_end=1_790_812_800.0,
    ) == ("btc-updown-15m-1790812800", 1_790_813_700.0)


def test_twap_research_market_context_keeps_matching_runtime_market():
    assert SpotPricerMixin._twap_research_market_context(
        1_790_812_815.0, current_slug="btc-updown-15m-1790812800",
        current_end=1_790_813_700.0,
    ) == ("btc-updown-15m-1790812800", 1_790_813_700.0)


def test_opening_twenty_second_observation_coverage_is_in_market_summary():
    db = FakeDb()
    model = TwapForwardShadow(db=db)
    start = 1_790_812_800
    for age in (1.0, 8.0, 18.0, 22.0):
        model.observe(
            slug=f"btc-updown-15m-{start}", now_ts=start + age,
            source_ts=start + age - 1.0, fast_spot=Decimal("101"),
            official_twap=Decimal("100"), strike=None,
            time_left_sec=900.0 - age,
        )
    summary = model.finalize_market(
        f"btc-updown-15m-{start}", settlement_side="DOWN", settlement_ts=start + 900,
    )
    assert summary["opening_20s_observation_count"] == 3
    assert summary["opening_20s_first_age_sec"] == 0.0
    assert summary["opening_20s_last_age_sec"] == 17.0
    assert summary["opening_20s_first_receive_age_sec"] == 1.0
    assert summary["opening_20s_last_receive_age_sec"] == 18.0
    assert summary["opening_20s_coverage_span_sec"] == 17.0
    assert summary["opening_20s_capture_observed"] is True
    stored = db.rows[-1]["payload"]
    assert stored["event_type"] == "MARKET_TWAP_SUMMARY"
    assert stored["opening_20s_observation_count"] == 3
    opening_rows = [row["payload"] for row in db.rows
                    if row["payload"].get("event_type") == "MARKET_OPENING_TWAP_SAMPLE"]
    assert len(opening_rows) == 3
    assert [row["market_age_source_sec"] for row in opening_rows] == [0.0, 7.0, 17.0]
    assert all(row["market_age_receive_sec"] - row["market_age_source_sec"] == 1.0 for row in opening_rows)


def test_raw_chainlink_history_uses_source_clock_and_rejects_invalid_timestamps():
    class Host(SpotPricerMixin):
        pass
    host = Host()
    host.polymarket_chainlink_history = []
    host.polymarket_chainlink_history_max = 20
    assert host._record_polymarket_chainlink_observation(Decimal("100"), 100.0, received_ts=100.1)
    assert host._record_polymarket_chainlink_observation(Decimal("110"), 101.0, received_ts=101.8)
    assert host._record_polymarket_chainlink_observation(Decimal("100"), 102.0, received_ts=102.2)
    assert not host._record_polymarket_chainlink_observation(Decimal("999"), None, received_ts=102.3)
    assert not host._record_polymarket_chainlink_observation(Decimal("999"), 103.0, received_ts=102.2)
    average, seconds = host._final_twap_observation(now_ts=102.0, end_ts=103.0, window_sec=3)
    assert average == Decimal("105")
    assert seconds == 2.0
    assert [ts for ts, _ in host.polymarket_chainlink_history] == [100.0, 101.0, 102.0]
    receipt_average, _ = host._final_twap_observation(
        now_ts=102.2, end_ts=103.0, window_sec=3,
        history=[(99.9, Decimal("100")), (100.1, Decimal("100")), (101.8, Decimal("110")), (102.2, Decimal("100"))],
    )
    assert receipt_average != average


def test_twap_research_uses_a_dedicated_writer_not_the_shared_lead_lag_db(monkeypatch, tmp_path):
    path = tmp_path / "twap_forward_shadow.db"
    monkeypatch.setenv("TWAP_RESEARCH_DB_PATH", str(path))

    db = build_twap_research_db()
    try:
        assert db.db_path == str(path)
        assert path.is_file()
        assert "hyperliquid_lead_lag" not in db.db_path
    finally:
        db.stop()


def test_probability_path_only_persists_material_thresholds_and_enriches_checkpoints():
    db = FakeDb(); model = TwapForwardShadow(db=db)
    diagnostics = {
        "p_up_ex_market": .96, "p_down_ex_market": .04,
        "p_up_market_conditioned": .95, "p_down_market_conditioned": .05,
        "market_mid_probability_up": .90, "best_bid_up": .89, "best_ask_up": .91,
        "model_minus_market_mid_up": .06, "model_minus_best_ask_up": .05,
        "required_future_avg_to_flip": 101.0, "required_move_sigma": 2.2,
        "probability_model_mode": "PRE_FINAL_WINDOW_APPROX", "sigma_ex_market_fresh": True,
    }
    sample(model, 100, left=121)
    observed = model.observe(slug="m", now_ts=101, source_ts=101, fast_spot=Decimal("101"),
                             official_twap=Decimal("100"), strike=Decimal("100"), time_left_sec=119,
                             settlement_diagnostics=diagnostics)
    checkpoint = next(row["payload"] for row in db.rows if row["payload"]["event_type"] == "TMINUS_CHECKPOINT")
    assert checkpoint["p_up_ex_market"] == .96
    assert checkpoint["market_mid_probability_up"] == .90
    assert checkpoint["required_move_sigma"] == 2.2
    assert any(row["payload"]["event_type"] == "SETTLEMENT_PATH_THRESHOLD_CROSS" for row in db.rows)
    # Identical follow-up values add no threshold-cross event or per-tick row.
    before = len([row for row in db.rows if row["payload"]["event_type"] == "SETTLEMENT_PATH_THRESHOLD_CROSS"])
    model.observe(slug="m", now_ts=102, source_ts=102, fast_spot=Decimal("101"),
                  official_twap=Decimal("100"), strike=Decimal("100"), time_left_sec=118,
                  settlement_diagnostics=diagnostics)
    after = len([row for row in db.rows if row["payload"]["event_type"] == "SETTLEMENT_PATH_THRESHOLD_CROSS"])
    assert after == before
    summary = model.finalize_market("m", settlement_side="UP", settlement_ts=103,
                                    settlement_reference_is_canonical=True)
    assert summary["max_p_up_ex_market"] == .96
    assert summary["canonical_settlement_side"] == "UP"
    assert summary["first_p90_up_ts"] == 101.0
    assert summary["first_market_p90_up_ts"] == 101.0


def test_unavailable_probability_does_not_emit_fake_threshold_exit_or_reentry():
    db = FakeDb(); model = TwapForwardShadow(db=db)
    base = {"p_up_ex_market": .96, "market_mid_probability_up": .9,
            "sigma_ex_market_fresh": True}
    sample(model, 100, left=121)
    model.observe(slug="m", now_ts=101, source_ts=101, fast_spot=Decimal("101"),
                  official_twap=Decimal("100"), strike=Decimal("100"), time_left_sec=119,
                  settlement_diagnostics=base)
    initial = [r["payload"] for r in db.rows if r["payload"]["event_type"] == "SETTLEMENT_PATH_THRESHOLD_CROSS"
               and r["payload"].get("measure") == "p_up_ex_market"]
    unavailable = {**base, "p_up_ex_market": None, "sigma_ex_market_fresh": False}
    model.observe(slug="m", now_ts=102, source_ts=102, fast_spot=Decimal("101"),
                  official_twap=Decimal("100"), strike=Decimal("100"), time_left_sec=118,
                  settlement_diagnostics=unavailable)
    model.observe(slug="m", now_ts=103, source_ts=103, fast_spot=Decimal("101"),
                  official_twap=Decimal("100"), strike=Decimal("100"), time_left_sec=117,
                  settlement_diagnostics=base)
    final = [r["payload"] for r in db.rows if r["payload"]["event_type"] == "SETTLEMENT_PATH_THRESHOLD_CROSS"
             and r["payload"].get("measure") == "p_up_ex_market"]
    assert len(final) == len(initial)
    assert all(row["crossing_direction"] != "left" for row in final)


def test_probability_report_uses_canonical_labels_and_retains_negative_lead():
    from scripts.twap_forward_report import probability_research_rows

    rows = [
        {"event_type": "MARKET_TWAP_SUMMARY", "market_slug": "canonical", "settlement_reference_is_canonical": True,
         "settlement_side": "UP", "first_p90_up_ts": 120.0, "first_market_p90_up_ts": 110.0},
        {"event_type": "MARKET_TWAP_SUMMARY", "market_slug": "proxy", "settlement_reference_is_canonical": False,
         "settlement_side": "DOWN"},
        {"event_type": "TMINUS_CHECKPOINT", "market_slug": "canonical", "checkpoint_sec": 60,
         "p_up_ex_market": .8, "sigma_ex_market_fresh": True,
         "market_mid_probability_up": .7, "time_left_sec": 59, "observed_ts": 101},
        {"event_type": "TMINUS_CHECKPOINT", "market_slug": "canonical", "checkpoint_sec": 120,
         "p_up_ex_market": .8, "sigma_ex_market_fresh": True,
         "market_mid_probability_up": .7, "time_left_sec": 119, "observed_ts": 102},
        {"event_type": "TMINUS_CHECKPOINT", "market_slug": "canonical", "checkpoint_sec": 30,
         "p_up_ex_market": .8, "sigma_ex_market_fresh": True,
         "market_mid_probability_up": .7, "time_left_sec": 29, "observed_ts": 103},
        {"event_type": "TMINUS_CHECKPOINT", "market_slug": "canonical", "checkpoint_sec": 60,
         "p_up_ex_market": .8, "sigma_ex_market_fresh": True,
         "market_mid_probability_up": .7, "time_left_sec": 58, "observed_ts": 104},
        # Stale and legacy rows do not enter provenance-strict calibration.
        {"event_type": "TMINUS_CHECKPOINT", "market_slug": "canonical", "checkpoint_sec": 60,
         "p_up_ex_market": .99, "sigma_ex_market_fresh": False, "time_left_sec": 60, "observed_ts": 105},
        {"event_type": "TMINUS_CHECKPOINT", "market_slug": "canonical", "checkpoint_sec": 60,
         "p_up_ex_market": .99, "time_left_sec": 60, "observed_ts": 106},
        {"event_type": "TMINUS_CHECKPOINT", "market_slug": "proxy", "checkpoint_sec": 60,
         "p_up_ex_market": .1, "sigma_ex_market_fresh": True, "market_mid_probability_up": .2},
        {"event_type": "SETTLEMENT_PATH_THRESHOLD_CROSS", "market_slug": "canonical", "observed_ts": 120,
         "measure": "p_up_ex_market", "threshold": .9, "p_up_ex_market": .96,
         "crossing_direction": "first_observed_beyond", "sigma_ex_market_fresh": True},
        {"event_type": "SETTLEMENT_PATH_THRESHOLD_CROSS", "market_slug": "canonical", "observed_ts": 110,
         "measure": "market_mid_probability_up", "threshold": .9, "market_mid_probability_up": .91,
         "crossing_direction": "first_observed_beyond", "sigma_ex_market_fresh": False,
         "market_bbo_up_source_age_sec": .4, "market_bbo_up_received_age_sec": .2,
         "market_bbo_max_age_sec": 2.0},
    ]
    metrics, buckets, leads = probability_research_rows(rows)
    assert next(row for row in metrics if row["checkpoint_sec"] == 60)["canonical_n"] == 1
    assert sum(row["n_markets"] for row in buckets if row["probability_low"] == .75 and row["probability_high"] == .9) == 3
    assert {row["checkpoint_sec"] for row in buckets if row["n_markets"]} == {30, 60, 120}
    assert leads[0]["model_lead_sec"] == -10.0
    assert leads[0]["model_first_observed_ts"] == 120.0


def _probability_host(now=1000.0):
    class Host(SpotPricerMixin):
        pass
    host = Host()
    host._polymarket_chainlink_twap_window_sec = 60
    host._polymarket_chainlink_twap_price = Decimal("100")
    host.current_market_end_timestamp = now + 121
    host._binance_ws_price = Decimal("100")
    host._binance_ws_price_ts = now - 1
    host._polymarket_chainlink_price = Decimal("102")
    host._polymarket_chainlink_price_ts = now - 1
    host._polymarket_chainlink_price_observation_ts = now - 1
    host.polymarket_chainlink_history = [(now-30, Decimal("99")), (now-20, Decimal("101")), (now-1, Decimal("100"))]
    host.maker_digital_vol_min_points = 3
    host.maker_digital_vol_window = 30
    host.maker_digital_sigma_default = Decimal("0.05")
    host.maker_digital_vol_scale = Decimal("1")
    host.maker_digital_sigma_floor = Decimal("0.05")
    host.maker_digital_sigma_ceiling = Decimal("2")
    host.maker_digital_sigma_time_decay_enabled = False
    host.maker_digital_sigma_time_decay_ref_sec = 600
    host.maker_digital_sigma_time_decay_min = .3
    host.maker_implied_sigma_enabled = False
    host.latest_quote_by_inst = {}
    host.last_quote_update_ts_by_inst = {}
    host.last_quote_source_ts_by_inst = {}
    host.last_quote_received_ts_by_inst = {}
    return host


def test_stale_raw_sigma_fails_closed_even_with_fresh_binance_path_spot():
    host = _probability_host()
    host.polymarket_chainlink_history = [(970.0, Decimal("99")), (975.0, Decimal("101")), (980.0, Decimal("100"))]
    out = host._settlement_probability_shadow_inputs(
        slug="m", official_twap=Decimal("99"), strike=Decimal("100"), time_left_sec=121, now_ts=1000.0)
    assert out["path_spot_source"] == "polymarket_chainlink_spot"
    assert out["sigma_ex_market"] is not None
    assert out["sigma_ex_market_age_sec"] == 19.0
    assert out["sigma_ex_market_fresh"] is False
    assert out["p_up_ex_market"] is None and out["p_down_ex_market"] is None
    assert out["required_move_sigma"] is None
    assert out["model_minus_market_mid_up"] is None
    assert out["data_quality"] == "stale_raw_spot_sigma"
    assert out["p_up_market_conditioned"] is not None


def test_fresh_raw_sigma_has_age_freshness_and_ex_market_probability():
    host = _probability_host()
    out = host._settlement_probability_shadow_inputs(
        slug="m", official_twap=Decimal("99"), strike=Decimal("100"), time_left_sec=121, now_ts=1000.0)
    assert out["sigma_ex_market_age_sec"] == 1.0
    assert out["sigma_ex_market_fresh"] is True
    assert out["p_up_ex_market"] is not None
    assert out["required_move_sigma"] is not None


def test_settlement_state_and_path_spot_sides_are_distinct_and_alias_is_settlement():
    host = _probability_host()
    # Raw Chainlink path spot is UP of strike while official TWAP remains DOWN.
    out = host._settlement_probability_shadow_inputs(
        slug="m", official_twap=Decimal("99"), strike=Decimal("100"), time_left_sec=121, now_ts=1000.0)
    assert out["settlement_state_side"] == "DOWN"
    assert out["path_spot_side"] == "UP"
    assert out["settlement_path_side_divergence"] is True
    assert out["currently_dominant_side"] == out["settlement_state_side"]
    host._polymarket_chainlink_price = Decimal("98")
    reverse = host._settlement_probability_shadow_inputs(
        slug="m", official_twap=Decimal("101"), strike=Decimal("100"), time_left_sec=121, now_ts=1000.0)
    assert reverse["settlement_state_side"] == "UP"
    assert reverse["path_spot_side"] == "DOWN"
    assert reverse["settlement_path_side_divergence"] is True


def test_required_move_modes_distinguish_exact_proxy_and_unavailable():
    host = _probability_host()
    prefinal = host._settlement_probability_shadow_inputs(
        slug="m", official_twap=Decimal("100"), strike=Decimal("101"), time_left_sec=121, now_ts=1000.0)
    assert prefinal["required_move_mode"] == "PRE_FINAL_STRIKE_PROXY"
    assert prefinal["path_boundary_proxy"] == 101.0
    assert prefinal["remaining_avg_decision_boundary"] is None
    assert prefinal["required_future_avg_to_flip"] is None
    host.current_market_end_timestamp = 1030.0
    host.polymarket_chainlink_history = []
    final_missing = host._settlement_probability_shadow_inputs(
        slug="m", official_twap=Decimal("100"), strike=Decimal("101"), time_left_sec=30, now_ts=1000.0)
    assert final_missing["required_move_mode"] == "UNAVAILABLE"
    assert final_missing["required_move_usd"] is None
    assert final_missing["required_move_bps"] is None
    assert final_missing["required_move_sigma"] is None
    assert final_missing["required_future_avg_to_flip"] is None


def test_spot_shadow_probability_reuses_forecast_without_mutating_live_state():
    from types import SimpleNamespace

    class ShadowHost(SpotPricerMixin):
        pass

    host = ShadowHost()
    now = 1000.0
    host.current_market_slug = "m"
    host.current_up_instrument_id = "up"
    host.current_down_instrument_id = "down"
    host.latest_quote_by_inst = {"up": (Decimal("0.09"), Decimal("0.11")),
                                 "down": (Decimal("0.89"), Decimal("0.91"))}
    host.last_quote_update_ts_by_inst = {"up": now - .1, "down": now - .2}
    host.last_quote_source_ts_by_inst = {"up": now - .1, "down": now - .2}
    host.last_quote_received_ts_by_inst = {"up": now - .1, "down": now - .2}
    host.quote_stale_sec = 3.0
    host._polymarket_chainlink_twap_window_sec = 60
    host._polymarket_chainlink_twap_observation_ts = now - .1
    host.current_market_end_timestamp = now + 121
    host._binance_ws_price = Decimal("100.002")
    host._binance_ws_price_ts = now - .1
    host._polymarket_chainlink_price = Decimal("102")
    host._polymarket_chainlink_price_ts = now - .1
    host._polymarket_chainlink_price_observation_ts = now - .1
    host._polymarket_chainlink_twap_price = Decimal("100")
    host.polymarket_chainlink_history = [(now - 3, Decimal("99.9")), (now - 2, Decimal("100.1")), (now - 1, Decimal("99.8"))]
    host.external_spot_history = [(997.0, Decimal("100")), (998.0, Decimal("102")), (999.0, Decimal("99"))]
    host.maker_digital_vol_min_points = 3
    host.maker_digital_vol_window = 30
    host.maker_digital_sigma_default = Decimal("0.05")
    host.maker_digital_vol_scale = Decimal("1")
    host.maker_digital_sigma_floor = Decimal("0.05")
    host.maker_digital_sigma_ceiling = Decimal("2")
    host.maker_digital_sigma_time_decay_enabled = False
    host.maker_digital_sigma_time_decay_ref_sec = 600
    host.maker_digital_sigma_time_decay_min = .3
    host.maker_implied_sigma_enabled = True
    host.last_forecast_state = SimpleNamespace(marker="must remain live-owned")
    original_state = host.last_forecast_state

    first = host._settlement_probability_shadow_inputs(
        slug="m", official_twap=Decimal("100"), strike=Decimal("101"), time_left_sec=121, now_ts=now
    )
    host.latest_quote_by_inst["up"] = (Decimal("0.89"), Decimal("0.91"))
    second = host._settlement_probability_shadow_inputs(
        slug="m", official_twap=Decimal("100"), strike=Decimal("101"), time_left_sec=121, now_ts=now
    )
    assert first["market_mid_probability_up"] == .1
    assert first["best_ask_up"] == .11
    assert first["p_down_ex_market"] == 1 - first["p_up_ex_market"]
    assert first["official_current_twap"] == 100
    assert first["fast_spot"] == 102
    assert first["path_spot_source"] == "polymarket_chainlink_spot"
    assert first["sigma_ex_market_source"] == "polymarket_chainlink_spot_history"
    assert first["sigma_ex_market"] == float(host._estimate_polymarket_raw_spot_sigma_annualized())
    assert first["sigma_ex_market"] != float(host._estimate_external_spot_sigma_annualized())
    assert first["remaining_avg_decision_boundary"] is None
    assert first["required_future_avg_to_flip"] is None
    assert first["path_boundary_proxy"] == 101.0
    assert first["p_up_ex_market"] == second["p_up_ex_market"]
    host._polymarket_chainlink_price = Decimal("100.5")
    path_changed = host._settlement_probability_shadow_inputs(
        slug="m", official_twap=Decimal("100"), strike=Decimal("101"), time_left_sec=121, now_ts=now
    )
    assert path_changed["p_up_ex_market"] != first["p_up_ex_market"]
    assert first["market_mid_probability_up"] != second["market_mid_probability_up"]
    assert first["sigma_ex_market_available"] is True
    assert host.last_forecast_state is original_state

    host.current_market_end_timestamp = now + 30
    no_raw = host._settlement_probability_shadow_inputs(
        slug="m", official_twap=Decimal("100"), strike=Decimal("100"), time_left_sec=30, now_ts=now
    )
    assert no_raw["data_quality"] == "insufficient_raw_final_window_history"
    assert no_raw["required_future_avg_to_flip"] is None

    host.current_market_end_timestamp = 1060
    host.polymarket_chainlink_history = [(999.0, Decimal("99")), (1010.0, Decimal("101"))]
    host._polymarket_chainlink_price = Decimal("100")
    host._polymarket_chainlink_price_ts = 1019.9
    host._polymarket_chainlink_price_observation_ts = 1019.9
    host._binance_ws_price_ts = 1019.9
    observed = host._settlement_probability_shadow_inputs(
        slug="m", official_twap=Decimal("100"), strike=Decimal("100"), time_left_sec=40, now_ts=1020,
        source_observed_ts=1020,
    )
    assert observed["observed_final_window_avg"] == 100.0
    assert observed["observed_final_window_sec"] == 20.0
    assert observed["required_move_mode"] == "EXACT_FINAL_WINDOW_BOUNDARY"
    assert observed["required_future_avg_to_flip"] == 100.0
    assert observed["remaining_avg_decision_boundary"] == 100.0
    assert observed["required_avg_for_up"] == observed["required_avg_for_down"] == 100.0


def test_probability_shadow_exposes_existing_bbo_freshness_and_unavailable_reasons():
    host = _probability_host(now=1000.0)
    host.current_market_slug = "m"
    host.current_up_instrument_id = "up"
    host.current_down_instrument_id = "down"
    host.latest_quote_by_inst = {"up": (Decimal("0.49"), Decimal("0.51")),
                                 "down": (Decimal("0.48"), Decimal("0.52"))}
    host.last_quote_update_ts_by_inst = {"up": 999.0, "down": 990.0}
    host.last_quote_source_ts_by_inst = {"up": 999.0, "down": 990.0}
    host.last_quote_received_ts_by_inst = {"up": 999.0, "down": 990.0}
    host.quote_stale_sec = 3.0
    out = host._settlement_probability_shadow_inputs(
        slug="m", official_twap=Decimal("100"), strike=Decimal("100"), time_left_sec=121, now_ts=1000.0)
    assert out["market_mid_probability_up"] == .5
    assert out["market_bbo_up_age_sec"] == 1.0
    assert out["market_bbo_up_unavailable_reason"] is None
    assert out["market_mid_probability_down"] is None
    assert out["market_bbo_down_age_sec"] == 10.0
    assert out["market_bbo_down_unavailable_reason"] == "quote_stale"


def test_opening_shadow_keeps_fresh_bbo_when_strike_is_not_ready():
    host = _probability_host(now=1000.0)
    host.current_market_slug = "m"
    host.current_up_instrument_id = "up"
    host.current_down_instrument_id = "down"
    host.latest_quote_by_inst = {
        "up": (Decimal("0.49"), Decimal("0.51")),
        "down": (Decimal("0.48"), Decimal("0.52")),
    }
    host.last_quote_update_ts_by_inst = {"up": 999.9, "down": 999.8}
    host.last_quote_source_ts_by_inst = {"up": 999.9, "down": 999.8}
    host.last_quote_received_ts_by_inst = {"up": 999.9, "down": 999.8}
    host.quote_stale_sec = 3.0

    out = host._settlement_probability_shadow_inputs(
        slug="m", official_twap=Decimal("100"), strike=None,
        time_left_sec=899.0, now_ts=1000.0,
    )

    assert out["probability_model_mode"] == "UNAVAILABLE"
    assert out["p_up_ex_market"] is None
    assert out["best_bid_up"] == .49
    assert out["best_ask_up"] == .51
    assert abs(out["market_bbo_up_age_sec"] - .1) < 1e-9

    host.current_up_instrument_id = "missing"
    missing = host._settlement_probability_shadow_inputs(
        slug="m", official_twap=Decimal("100"), strike=Decimal("100"), time_left_sec=121, now_ts=1000.0)
    assert missing["market_mid_probability_up"] is None
    assert missing["market_bbo_up_unavailable_reason"] == "quote_missing"


def test_canonical_twap_settlement_label_uses_direct_source_timestamp_and_rejects_stale_or_future():
    from bot.lifecycle_runtime import _canonical_twap_shadow_label

    fresh = _canonical_twap_shadow_label(
        twap_price=99.0, source_ts=995.0, window_sec=60, strike=100.0,
        settlement_ts=1000.0, freshness_sec=10.0)
    assert fresh["source"] == "polymarket_chainlink_twap_60s_ws"
    assert fresh["age_sec"] == 5.0
    assert fresh["canonical"] is True
    assert fresh["side"] == "DOWN"

    stale = _canonical_twap_shadow_label(
        twap_price=101.0, source_ts=765.0, window_sec=60, strike=100.0,
        settlement_ts=1000.0, freshness_sec=10.0)
    assert stale["age_sec"] == 235.0
    assert stale["canonical"] is False
    assert stale["side"] is None

    future = _canonical_twap_shadow_label(
        twap_price=101.0, source_ts=1001.0, window_sec=60, strike=100.0,
        settlement_ts=1000.0, freshness_sec=10.0)
    assert future["canonical"] is False
    assert future["age_sec"] == -1.0


def test_probability_report_excludes_post_settlement_checkpoint_rows():
    from scripts.twap_forward_report import probability_research_rows

    rows = [
        {"event_type": "MARKET_TWAP_SUMMARY", "market_slug": "m",
         "settlement_reference_is_canonical": True, "settlement_side": "UP"},
        {"event_type": "TMINUS_CHECKPOINT", "market_slug": "m", "checkpoint_sec": 5,
         "time_left_sec": 0, "p_up_ex_market": .99, "sigma_ex_market_fresh": True,
         "market_mid_probability_up": .95, "observed_ts": 100},
        {"event_type": "TMINUS_CHECKPOINT", "market_slug": "m", "checkpoint_sec": 5,
         "time_left_sec": 4.8, "p_up_ex_market": .8, "sigma_ex_market_fresh": True,
         "market_mid_probability_up": .7, "observed_ts": 99},
    ]
    metrics, buckets, leads = probability_research_rows(rows)
    row = next(r for r in metrics if r["checkpoint_sec"] == 5)
    assert row["canonical_n"] == 1
    assert row["mean_p_up_ex_market"] == .8


def test_probability_report_excludes_stale_or_unproven_market_mid_quotes():
    from scripts.twap_forward_report import probability_research_rows

    rows = [
        {"event_type": "MARKET_TWAP_SUMMARY", "market_slug": "stale",
         "settlement_reference_is_canonical": True, "settlement_side": "UP"},
        {"event_type": "TMINUS_CHECKPOINT", "market_slug": "stale", "checkpoint_sec": 30,
         "time_left_sec": 29.5, "p_up_ex_market": .7, "sigma_ex_market_fresh": True,
         "market_mid_probability_up": .65, "market_bbo_up_source_age_sec": 30.0,
         "market_bbo_up_received_age_sec": .2, "market_bbo_max_age_sec": 2.0, "observed_ts": 100},
        {"event_type": "MARKET_TWAP_SUMMARY", "market_slug": "fresh",
         "settlement_reference_is_canonical": True, "settlement_side": "DOWN"},
        {"event_type": "TMINUS_CHECKPOINT", "market_slug": "fresh", "checkpoint_sec": 30,
         "time_left_sec": 29.5, "p_up_ex_market": .3, "sigma_ex_market_fresh": True,
         "market_mid_probability_up": .35, "market_bbo_up_source_age_sec": .8,
         "market_bbo_up_received_age_sec": .2, "market_bbo_max_age_sec": 2.0, "observed_ts": 101},
        {"event_type": "MARKET_TWAP_SUMMARY", "market_slug": "legacy",
         "settlement_reference_is_canonical": True, "settlement_side": "UP"},
        {"event_type": "TMINUS_CHECKPOINT", "market_slug": "legacy", "checkpoint_sec": 30,
         "time_left_sec": 29.5, "p_up_ex_market": .6, "sigma_ex_market_fresh": True,
         "market_mid_probability_up": .55, "observed_ts": 102},
    ]

    metrics, _, _ = probability_research_rows(rows)
    row = next(item for item in metrics if item["checkpoint_sec"] == 30)
    assert row["canonical_n"] == 3
    assert row["market_n"] == 1
    assert row["brier_market_mid"] == .35 ** 2

def test_probability_path_uses_fresh_binance_if_raw_chainlink_stale_and_never_twap_fallback():
    class Host(SpotPricerMixin):
        pass
    host = Host()
    now = 1000.0
    host._polymarket_chainlink_twap_window_sec = 60
    host._polymarket_chainlink_twap_price = Decimal("100")
    host._polymarket_chainlink_price = Decimal("102")
    host._polymarket_chainlink_price_ts = now - 20
    host._polymarket_chainlink_price_observation_ts = now - 20
    host._binance_ws_price = Decimal("103")
    host._binance_ws_price_ts = now - 1
    host.polymarket_chainlink_history = []
    host.maker_digital_vol_min_points = 3
    host.maker_digital_vol_window = 30
    host.maker_digital_sigma_default = Decimal("0.05")
    host.maker_digital_vol_scale = Decimal("1")
    host.maker_digital_sigma_floor = Decimal("0.05")
    host.maker_digital_sigma_ceiling = Decimal("2")
    host.maker_digital_sigma_time_decay_enabled = False
    host.maker_digital_sigma_time_decay_ref_sec = 600
    host.maker_digital_sigma_time_decay_min = .3
    host.maker_implied_sigma_enabled = False
    host.latest_quote_by_inst = {}
    host.last_quote_update_ts_by_inst = {}
    out = host._settlement_probability_shadow_inputs(slug="m", official_twap=Decimal("100"), strike=Decimal("101"), time_left_sec=121, now_ts=now)
    assert out["path_spot_source"] == "binance_ws"
    assert out["fast_spot"] == 103
    assert out["p_up_ex_market"] is None
    assert out["required_move_sigma"] is None
    host._binance_ws_price_ts = now - 20
    out = host._settlement_probability_shadow_inputs(slug="m", official_twap=Decimal("100"), strike=Decimal("101"), time_left_sec=121, now_ts=now)
    assert out["path_spot_source"] == "unavailable"
    assert out["fast_spot"] is None
    assert out["p_up_ex_market"] is None


@pytest.mark.parametrize("offset", [.5, -.5])
def test_research_probability_freshness_uses_receipt_and_same_chainlink_clock(offset):
    host = _probability_host()
    host._polymarket_chainlink_price_observation_ts = 1000 + offset
    host._polymarket_chainlink_price_ts = 1000
    host.polymarket_chainlink_history = [(970 + offset, Decimal("99")),
        (980 + offset, Decimal("101")), (1000 + offset, Decimal("100"))]
    out = host._settlement_probability_shadow_inputs(slug="m", official_twap=Decimal("99"),
        strike=Decimal("100"), time_left_sec=121, now_ts=1000.2)
    assert out["path_spot_source"] == "polymarket_chainlink_spot"
    assert out["path_spot_age_sec"] == pytest.approx(.2)
    assert out["sigma_ex_market_age_sec"] == pytest.approx(.2)
    assert out["sigma_ex_market_fresh"] is True
    assert out["p_up_ex_market"] is not None


def test_research_sigma_still_rejects_old_value_and_missing_local_receipt():
    host = _probability_host()
    host._polymarket_chainlink_price_observation_ts = 1020
    out = host._settlement_probability_shadow_inputs(slug="m", official_twap=Decimal("99"),
        strike=Decimal("100"), time_left_sec=121, now_ts=1000)
    assert out["sigma_ex_market_age_sec"] == 21
    assert out["sigma_ex_market_fresh"] is False
    host._polymarket_chainlink_price_ts = 0
    out = host._settlement_probability_shadow_inputs(slug="m", official_twap=Decimal("99"),
        strike=Decimal("100"), time_left_sec=121, now_ts=1000)
    assert out["sigma_ex_market_fresh"] is False


def test_research_sigma_negative_same_domain_order_is_invalid_not_clamped():
    host = _probability_host()
    host._polymarket_chainlink_price_observation_ts = 998
    out = host._settlement_probability_shadow_inputs(slug="m", official_twap=Decimal("99"),
        strike=Decimal("100"), time_left_sec=121, now_ts=1000)
    assert out["sigma_ex_market_value_age_sec"] == -1
    assert out["sigma_ex_market_age_sec"] is None
    assert out["sigma_ex_market_fresh"] is False
    assert out["p_up_ex_market"] is None

@pytest.mark.parametrize('event', sorted(__import__('bot.twap_forward_shadow', fromlist=['REQUIRED_EVENT_TYPES']).REQUIRED_EVENT_TYPES))
def test_cap_never_suppresses_required_evidence(tmp_path, event):
    from bot.research.health import component_health
    db = FakeDb(); db.db_path = str(tmp_path / 'research.db')
    (tmp_path / 'research.db').write_bytes(b'x')
    model = TwapForwardShadow(db=db, max_db_mb=0, min_free_disk_gb=0)
    model._storage_health(100)
    model._persist('m', 101, event, {'marker': event})
    assert db.rows[-1]['payload']['marker'] == event
    model._persist('m', 102, 'OPTIONAL_DIAGNOSTIC', {})
    assert db.rows[-1]['payload']['event_type'] == event
    status = model.storage_guard_status()
    assert status['persistence_counters']['optional_suppressed'] == 1
    assert component_health(status, domain='Storage')['state'] == 'DEGRADED'

@pytest.mark.parametrize('failure', ['reject', 'exception', 'worker'])
def test_canonical_persistence_failure_is_critical_not_optional_degradation(failure):
    from bot.research.health import component_health
    class FailingDb(FakeDb):
        def enqueue_decision(self, **kwargs):
            if failure == 'exception':
                raise OSError('disk full')
            return failure != 'reject'
        def research_health(self):
            return {'write_errors': int(failure == 'worker')}
    model = TwapForwardShadow(db=FailingDb())
    model._persist('m', 100, 'TWAP_STRIKE_CROSS', {})
    assert component_health(model.storage_guard_status(), domain='Storage')['state'] == 'CRITICAL'


def test_free_space_warning_overrides_cap_and_recovers(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from bot.research.health import component_health
    db = FakeDb(); db.db_path = str(tmp_path / 'research.db')
    (tmp_path / 'research.db').write_bytes(b'x')
    model = TwapForwardShadow(db=db, max_db_mb=0, storage_check_interval_sec=1)
    monkeypatch.setattr('bot.twap_forward_shadow.shutil.disk_usage', lambda _: SimpleNamespace(free=9 * 1024**3))
    model._storage_health(100)
    assert model.storage_guard_status()['reason'] == 'free_disk_low'
    assert component_health(model.storage_guard_status(), domain='Storage')['state'] == 'CRITICAL'
    # Required evidence is attempted, never silently suppressed under pressure.
    model._persist('m', 101, 'TWAP_STRIKE_CROSS', {})
    assert db.rows[-1]['payload']['event_type'] == 'TWAP_STRIKE_CROSS'
    monkeypatch.setattr('bot.twap_forward_shadow.shutil.disk_usage', lambda _: SimpleNamespace(free=20 * 1024**3))
    model._storage_health(102)
    assert component_health(model.storage_guard_status(), domain='Storage')['state'] == 'DEGRADED'
    assert len([r for r in db.rows if r['payload']['event_type'] == 'RESEARCH_STORAGE_GUARD_TRIGGERED']) == 2
