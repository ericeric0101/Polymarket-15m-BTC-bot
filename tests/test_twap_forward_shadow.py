from decimal import Decimal

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


def test_storage_guard_suppresses_optional_events_but_keeps_summary(monkeypatch, tmp_path):
    db = FakeDb(); db.db_path = str(tmp_path / "research.db"); (tmp_path / "research.db").write_bytes(b"x")
    model = TwapForwardShadow(db=db, max_db_mb=0.0, min_free_disk_gb=0, storage_check_interval_sec=1)
    sample(model, 100, twap="100")
    sample(model, 101, twap="99")
    assert model.storage_guard_status()["triggered"] is True
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
        "probability_model_mode": "PRE_FINAL_WINDOW_APPROX",
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


def test_probability_report_uses_canonical_labels_and_retains_negative_lead():
    from scripts.twap_forward_report import probability_research_rows

    rows = [
        {"event_type": "MARKET_TWAP_SUMMARY", "market_slug": "canonical", "settlement_reference_is_canonical": True,
         "settlement_side": "UP", "first_p90_up_ts": 120.0, "first_market_p90_up_ts": 110.0},
        {"event_type": "MARKET_TWAP_SUMMARY", "market_slug": "proxy", "settlement_reference_is_canonical": False,
         "settlement_side": "DOWN"},
        {"event_type": "TMINUS_CHECKPOINT", "market_slug": "canonical", "checkpoint_sec": 60,
         "p_up_ex_market": .8, "market_mid_probability_up": .7},
        {"event_type": "TMINUS_CHECKPOINT", "market_slug": "proxy", "checkpoint_sec": 60,
         "p_up_ex_market": .1, "market_mid_probability_up": .2},
    ]
    metrics, buckets, leads = probability_research_rows(rows)
    assert next(row for row in metrics if row["checkpoint_sec"] == 60)["canonical_n"] == 1
    assert sum(row["n_checkpoint_observations"] for row in buckets) == 1
    assert leads[0]["model_lead_sec"] == -10.0


def test_spot_shadow_probability_reuses_forecast_without_mutating_live_state():
    from types import SimpleNamespace

    class ShadowHost(SpotPricerMixin):
        pass

    host = ShadowHost()
    now = 1000.0
    host.current_up_instrument_id = "up"
    host.current_down_instrument_id = "down"
    host.latest_quote_by_inst = {"up": (Decimal("0.09"), Decimal("0.11")),
                                 "down": (Decimal("0.89"), Decimal("0.91"))}
    host.last_quote_update_ts_by_inst = {"up": now - .1, "down": now - .2}
    host.quote_stale_sec = 3.0
    host._polymarket_chainlink_twap_window_sec = 60
    host._polymarket_chainlink_twap_observation_ts = now - .1
    host.current_market_end_timestamp = now + 121
    host._binance_ws_price = Decimal("100.002")
    host.external_spot_history = []
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
        slug="m", spot=Decimal("100.001"), strike=Decimal("100"), time_left_sec=121, now_ts=now
    )
    host.latest_quote_by_inst["up"] = (Decimal("0.89"), Decimal("0.91"))
    second = host._settlement_probability_shadow_inputs(
        slug="m", spot=Decimal("100.001"), strike=Decimal("100"), time_left_sec=121, now_ts=now
    )
    assert first["market_mid_probability_up"] == .1
    assert first["best_ask_up"] == .11
    assert first["p_down_ex_market"] == 1 - first["p_up_ex_market"]
    assert first["p_up_ex_market"] == second["p_up_ex_market"]
    assert first["p_up_market_conditioned"] != second["p_up_market_conditioned"]
    assert first["sigma_implied_floor_applied"] is True
    assert host.last_forecast_state is original_state

    host.current_market_end_timestamp = now + 30
    no_raw = host._settlement_probability_shadow_inputs(
        slug="m", spot=Decimal("100.001"), strike=Decimal("100"), time_left_sec=30, now_ts=now
    )
    assert no_raw["data_quality"] == "insufficient_raw_final_window_history"
    assert no_raw["required_future_avg_to_flip"] is None

    host.current_market_end_timestamp = 1060
    host.polymarket_chainlink_history = [(999.0, Decimal("99")), (1010.0, Decimal("101"))]
    observed = host._settlement_probability_shadow_inputs(
        slug="m", spot=Decimal("100.5"), strike=Decimal("100"), time_left_sec=40, now_ts=1020
    )
    assert observed["observed_final_window_avg"] == 100.0
    assert observed["observed_final_window_sec"] == 20.0
    assert observed["required_future_avg_to_flip"] == 100.0
