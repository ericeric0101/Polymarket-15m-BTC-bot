import json
import math
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.analytics.weekend_liquidity import (
    classify_weekend,
    compute_liquidity_metrics,
    compare_samples,
    deduplicate_fills,
    load_public_cache,
    market_seconds_to_resolution,
    percentile,
    price_slippage,
    public_data_source_label,
    resolution_time_bin,
    save_public_cache,
    summarize_pnl,
)
from scripts.analyze_weekend_liquidity import load_local_journal, _market_weekend
from scripts.record_polymarket_l2 import apply_market_event, subscription_message, _DailyParquetSink


def test_et_weekday_weekend_classification_and_friday_saturday_edge():
    tz = ZoneInfo("America/New_York")
    friday_late = datetime(2026, 9, 25, 23, 59, tzinfo=tz).timestamp()
    saturday_early = datetime(2026, 9, 26, 0, 1, tzinfo=tz).timestamp()
    assert classify_weekend(friday_late, "America/New_York") is False
    assert classify_weekend(saturday_early, "America/New_York") is True


def test_sunday_monday_timezone_edge_is_not_system_timezone_dependent():
    tz = ZoneInfo("America/New_York")
    sunday_late = datetime(2026, 9, 27, 23, 59, tzinfo=tz).timestamp()
    monday_early = datetime(2026, 9, 28, 0, 1, tzinfo=tz).timestamp()
    assert classify_weekend(sunday_late, "America/New_York") is True
    assert classify_weekend(monday_early, "America/New_York") is False


def test_market_weekend_classification_uses_market_start_epoch():
    # A market opened Friday 23:59 ET stays a weekday even if an event lands Saturday.
    friday_start = int(datetime(2026, 9, 25, 23, 59, tzinfo=ZoneInfo("America/New_York")).timestamp())
    assert _market_weekend(f"btc-updown-15m-{friday_start}", "America/New_York") is False


def test_slug_seconds_to_resolution_and_lifecycle_bins():
    market_start = 1_790_347_500
    assert market_seconds_to_resolution(f"btc-updown-15m-{market_start}", market_start + 840) == 60
    assert resolution_time_bin(840) == "T-15m_to_T-10m"
    assert resolution_time_bin(90) == "T-2m_to_T-1m"
    assert resolution_time_bin(10) == "T-15s_to_settlement"


def test_buy_and_sell_slippage_signs_are_adverse_positive():
    assert math.isclose(price_slippage("BUY", intended_price=0.50, actual_price=0.53), 0.03)
    assert math.isclose(price_slippage("SELL", intended_price=0.97, actual_price=0.94), 0.03)
    assert math.isclose(price_slippage("BUY", intended_price=0.50, actual_price=0.48), -0.02)


def test_missing_depth_degrades_gracefully_and_score_is_unavailable():
    metrics = compute_liquidity_metrics([{"spread": 0.02, "quote_age_sec": 1.0}])
    assert metrics[0]["spread"] == 0.02
    assert metrics[0]["depth_1c"] is None
    assert metrics[0]["liquidity_score"] is None


def test_empty_db_load_is_valid_empty_dataset(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    data = load_local_journal(str(path), days=90)
    assert data["orders"] == []
    assert data["fills"] == []
    assert data["quality"]["status"] == "empty"
    assert data["quality"]["observed_start_utc"] is None


def test_partial_fills_are_preserved_and_duplicate_fill_id_removed():
    fills = [
        {"run_id": "r1", "client_order_id": "o1", "event_id": 1, "fill_id": "f1", "qty": 2},
        {"run_id": "r1", "client_order_id": "o1", "event_id": 2, "fill_id": "f2", "qty": 3},
        {"run_id": "r1", "client_order_id": "o1", "event_id": 3, "fill_id": "f2", "qty": 3},
    ]
    actual = deduplicate_fills(fills)
    assert len(actual) == 2
    assert sum(row["qty"] for row in actual) == 5


def test_missing_pnl_is_not_silently_treated_as_zero():
    pnl = summarize_pnl([{"pnl": 1.0}, {"pnl": None}, {"pnl": -0.25}])
    assert pnl["sample_size"] == 2
    assert pnl["total_pnl"] == 0.75
    assert pnl["mean_pnl"] == 0.375


def test_percentile_and_small_sample_statistics():
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    pnl = summarize_pnl([{"pnl": 1.0}])
    assert pnl["pnl_p10"] == 1.0
    assert pnl["sample_warning"] is True


def test_comparison_reports_single_group_mean_without_claiming_difference():
    result = compare_samples([1.0, 2.0, 3.0], [])
    assert result["weekday_mean"] == 2.0
    assert result["weekend_mean"] is None
    assert result["mean_difference"] is None
    assert result["sample_warning"] is True


def test_cache_round_trip_and_public_source_labels(tmp_path):
    cache_dir = tmp_path / "cache"
    payload = {"markets": [{"slug": "btc-updown-15m-1"}], "trades": []}
    save_public_cache(str(cache_dir), "btc-updown-15m-1", payload)
    assert load_public_cache(str(cache_dir), "btc-updown-15m-1") == payload
    assert public_data_source_label("cache") == "PUBLIC_HISTORICAL"
    assert public_data_source_label("journal") == "LOCAL_RECORDED"
    assert public_data_source_label("l2") == "FORWARD_L2_ONLY"


def test_analysis_loader_canonicalizes_orders_and_excludes_shadow_from_fills(tmp_path):
    path = tmp_path / "journal.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE order_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT, "
            "client_order_id TEXT, side TEXT, price REAL, qty REAL, payload_json TEXT, instrument_id TEXT);"
            "CREATE TABLE strategy_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT, payload_json TEXT);"
        )
        conn.executemany(
            "INSERT INTO order_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "2026-09-25T13:00:00+00:00", "r", "ORDER_SUBMIT", "o", "BUY", .5, 10, json.dumps({"slug": "btc-updown-15m-1790347500"}), "token"),
                (2, "2026-09-25T13:00:01+00:00", "r", "ORDER_FILLED", "o", "BUY", .5, 4, json.dumps({"fill_id": "f1"}), "token"),
                (3, "2026-09-25T13:00:02+00:00", "r", "ORDER_FILLED", "o", "BUY", .51, 6, json.dumps({"fill_id": "f2"}), "token"),
                (4, "2026-09-25T13:00:03+00:00", "r", "FILL_MARKOUT", "o", "BUY", .5, 10, "{}", "token"),
            ],
        )
    loaded = load_local_journal(str(path), days=90)
    assert len(loaded["orders"]) == 1
    assert len(loaded["fills"]) == 2
    assert loaded["fills"][0]["pnl"] is None
    assert loaded["quality"]["shadow_rows_excluded"] == 1


def test_l2_recorder_parses_full_book_and_computes_spread_depth_imbalance():
    rows = apply_market_event({}, {
        "event_type": "book", "market": "condition-1", "asset_id": "token-1",
        "timestamp": "1790000000000",
        "bids": [{"price": "0.60", "size": "10"}, {"price": "0.59", "size": "5"}],
        "asks": [{"price": "0.62", "size": "8"}, {"price": "0.63", "size": "12"}],
    }, {"token-1": {"slug": "btc-updown-15m-1790000000", "condition_id": "condition-1"}})
    assert len(rows) == 1
    assert rows[0]["best_bid"] == 0.60
    assert rows[0]["best_ask"] == 0.62
    assert math.isclose(rows[0]["spread"], 0.02)
    assert rows[0]["depth_1c_bid"] == 15
    assert rows[0]["depth_1c_ask"] == 20
    assert rows[0]["source"] == "FORWARD_L2_ONLY"


def test_l2_recorder_applies_price_change_and_handles_missing_book():
    metadata = {"token-1": {"slug": "btc-updown-15m-1790000000", "condition_id": "condition-1"}}
    state = {"token-1": {"bids": {0.60: 10.0}, "asks": {0.62: 8.0}}}
    rows = apply_market_event(state, {
        "event_type": "price_change", "market": "condition-1", "timestamp": "1790000001000",
        "price_changes": [{"asset_id": "token-1", "price": "0.61", "size": "4", "side": "BUY"}],
    }, metadata)
    assert rows[0]["best_bid"] == 0.61
    assert rows[0]["bid_size"] == 4
    assert apply_market_event({}, {"event_type": "price_change", "price_changes": []}, metadata) == []


def test_l2_subscription_message_is_public_market_channel_only():
    message = subscription_message(["token-a", "token-b"])
    assert message["type"] == "market"
    assert message["assets_ids"] == ["token-a", "token-b"]
    assert message["custom_feature_enabled"] is True


def test_l2_parquet_sink_appends_rows_without_rewriting_previous_rows(tmp_path):
    import pytest
    pq = pytest.importorskip("pyarrow.parquet")
    sink = _DailyParquetSink(tmp_path, "testsession")
    sink.write([{"received_at_utc": "2026-09-25T23:59:59+00:00", "best_bid": 0.5}])
    sink.write([{"received_at_utc": "2026-09-25T23:59:59.500000+00:00", "best_bid": 0.51}])
    sink.close()
    path = tmp_path / "2026-09-25" / "part-testsession.parquet"
    table = pq.read_table(path)
    assert table.num_rows == 2
    assert table.column("best_bid").to_pylist() == [0.5, 0.51]
