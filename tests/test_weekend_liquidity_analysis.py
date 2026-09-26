import json
import math
import socket
import sqlite3
from urllib.error import HTTPError, URLError
from io import BytesIO
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
from scripts.analyze_weekend_liquidity import (
    load_local_journal, generate_report, _market_weekend, parse_polymarket_instrument_id,
    fetch_market_public_history, _classify_public_error, _public_market_metrics,
)
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
                (1, "2026-09-25T13:00:00+00:00", "r", "ORDER_SUBMIT", "o", "BUY", .5, 10, json.dumps({"slug": "btc-updown-15m-1790347500"}), "0x" + "ab" * 32 + "-123456789.POLYMARKET"),
                (2, "2026-09-25T13:00:01+00:00", "r", "ORDER_FILLED", "o", "BUY", .5, 4, json.dumps({"fill_id": "f1", "slug": "btc-updown-15m-1790347500"}), "0x" + "ab" * 32 + "-123456789.POLYMARKET"),
                (3, "2026-09-25T13:00:02+00:00", "r", "ORDER_FILLED", "o", "BUY", .51, 6, json.dumps({"fill_id": "f2", "slug": "btc-updown-15m-1790347500"}), "0x" + "ab" * 32 + "-123456789.POLYMARKET"),
                (4, "2026-09-25T13:00:03+00:00", "r", "FILL_MARKOUT", "o", "BUY", .5, 10, "{}", "0x" + "ab" * 32 + "-123456789.POLYMARKET"),
            ],
        )
    loaded = load_local_journal(str(path), days=90)
    assert len(loaded["orders"]) == 1
    assert len(loaded["fills"]) == 2
    assert loaded["fills"][0]["pnl"] is None
    assert loaded["quality"]["shadow_rows_excluded"] == 1
    from scripts.analyze_weekend_liquidity import _market_identity_from_local
    identity = _market_identity_from_local(
        "btc-updown-15m-1790347500", loaded["identities"]["btc-updown-15m-1790347500"]
    )
    assert identity.condition_id == "0x" + "ab" * 32
    assert identity.token_ids == ("123456789",)
    assert identity.source == "LOCAL_INSTRUMENT_ID"


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


def test_parse_canonical_instrument_id_to_condition_and_token():
    result = parse_polymarket_instrument_id(
        "0x" + "a1" * 32 + "-12345678901234567890.POLYMARKET"
    )
    assert result == {"condition_id": "0x" + "a1" * 32, "token_id": "12345678901234567890"}
    assert parse_polymarket_instrument_id("not-an-instrument") is None


def test_gamma_slug_resolver_accepts_single_object_and_prices_use_market_window(monkeypatch, tmp_path):
    from scripts import analyze_weekend_liquidity as analysis
    market_start = 1_790_347_500
    slug = f"btc-updown-15m-{market_start}"
    condition = "0x" + "ab" * 32
    token = "123456789"
    requests = []

    def fake_get(url, params=None):
        requests.append((url, params))
        if "/markets/slug/" in url:
            return {"slug": slug, "conditionId": condition, "clobTokenIds": [token, "987654321"]}
        if url.endswith("/trades"):
            if params.get("cursor") == "page-2":
                return {"data": [{"timestamp": market_start + 840, "conditionId": condition},
                                 {"timestamp": market_start + 901, "conditionId": condition}],
                        "pagination": {}}
            return {"data": [{"timestamp": market_start - 1, "conditionId": condition},
                             {"timestamp": market_start + 300, "conditionId": condition}],
                    "pagination": {"next_cursor": "page-2"}}
        if url.endswith("/prices-history"):
            return {"data": [{"t": market_start + 10, "p": 0.5}]}
        raise AssertionError(url)

    monkeypatch.setattr(analysis, "_http_json", fake_get)
    result, error = fetch_market_public_history(slug, cache_dir=str(tmp_path), force_refresh=True)
    assert error is None
    assert result["gamma"]["status"] == "success"
    assert len(result["trades"]) == 2
    assert result["trade_rows_raw"] == 4
    assert result["trade_pages"] == 2
    assert result["price_history"][token] == [{"t": market_start + 10, "p": 0.5}]
    trade_call = next(item for item in requests if item[0].endswith("/trades"))
    price_call = next(item for item in requests if item[0].endswith("/prices-history"))
    assert trade_call[1]["condition"] == condition
    assert price_call[1] == {"token_id": token, "start": market_start, "end": market_start + 900}
    assert "markets/slug/" + slug in requests[0][0]


def test_local_identifiers_continue_when_gamma_dns_fails(monkeypatch, tmp_path):
    from scripts import analyze_weekend_liquidity as analysis
    market_start = 1_790_347_500
    slug = f"btc-updown-15m-{market_start}"
    condition = "0x" + "cd" * 32
    token = "1234567890123"
    calls = []

    def fake_get(url, params=None):
        calls.append(url)
        if "/markets/slug/" in url:
            raise URLError(socket.gaierror(-2, "Name or service not known"))
        if url.endswith("/trades"):
            return {"data": [{"timestamp": market_start + 50, "conditionId": condition,
                              "asset": token, "slug": slug}], "pagination": {}}
        return {"data": [{"t": market_start + 20, "p": 0.6}]}

    monkeypatch.setattr(analysis, "_http_json", fake_get)
    result, error = fetch_market_public_history(
        slug, cache_dir=str(tmp_path), force_refresh=True,
        local_identity={"condition_id": condition, "token_ids": [token], "instrument_ids": []},
    )
    assert error is None
    assert result["identity"]["source"] == "LOCAL_JOURNAL"
    assert result["gamma"]["status"] == "dns_error"
    assert result["trade_fetch_status"] == "success"
    assert result["price_fetch_status"] == "success"
    assert result["status"] == "SUCCESS_PUBLIC_HISTORY"
    from scripts.analyze_weekend_liquidity import _public_diagnostic_row
    diagnostics = _public_diagnostic_row(slug, result)
    assert diagnostics["gamma_error_code"] == "dns_error"
    assert diagnostics["trade_error_code"] is None
    assert diagnostics["price_error_code"] is None
    assert calls[1].endswith("/trades")


def test_no_local_identifiers_resolves_from_gamma(monkeypatch, tmp_path):
    from scripts import analyze_weekend_liquidity as analysis
    condition, tokens = "0x" + "ef" * 32, ["111", "222"]
    monkeypatch.setattr(analysis, "_http_json", lambda url, params=None: (
        {"slug": "btc-updown-15m-1790347500", "conditionId": condition, "clobTokenIds": tokens}
        if "/markets/slug/" in url else {"data": [], "pagination": {}}
    ))
    result, _ = fetch_market_public_history(
        "btc-updown-15m-1790347500", cache_dir=str(tmp_path), force_refresh=True,
    )
    assert result["identity"]["source"] == "GAMMA"
    assert result["identity"]["condition_id"] == condition
    assert result["identity"]["token_ids"] == tokens


def test_gamma_identifier_mismatch_preserves_local_values_and_is_reported(monkeypatch, tmp_path):
    from scripts import analyze_weekend_liquidity as analysis
    local_condition, gamma_condition = "0x" + "11" * 32, "0x" + "22" * 32
    local_token, gamma_token = "11111", "22222"

    def fake_get(url, params=None):
        if "/markets/slug/" in url:
            return {"slug": "btc-updown-15m-1790347500", "conditionId": gamma_condition,
                    "clobTokenIds": [gamma_token, "33333"]}
        if url.endswith("/trades"):
            return {"data": [], "pagination": {}}
        return {"data": []}

    monkeypatch.setattr(analysis, "_http_json", fake_get)
    result, _ = fetch_market_public_history(
        "btc-updown-15m-1790347500", cache_dir=str(tmp_path), force_refresh=True,
        local_identity={"condition_id": local_condition, "token_ids": [local_token]},
    )
    assert result["identity"]["condition_id"] == local_condition
    assert result["identity"]["token_ids"] == [local_token]
    assert result["gamma"]["status"] == "identifier_mismatch"
    assert result["gamma"]["local_condition_id"] == local_condition
    assert result["gamma"]["gamma_condition_id"] == gamma_condition


def test_dns_errors_are_structured_and_not_reported_as_market_not_found():
    error = _classify_public_error(URLError(socket.gaierror(-2, "Name or service not known")),
                                   stage="gamma_lookup", endpoint="https://gamma.example")
    assert error["code"] == "dns_error"
    assert error["stage"] == "gamma_lookup"
    assert error["retryable"] is True
    assert error["endpoint"] == "https://gamma.example"


def test_gamma_http_404_is_gamma_not_found_and_nonretryable():
    error = _classify_public_error(
        HTTPError("https://gamma.example", 404, "not found", {}, BytesIO(b"")),
        stage="gamma_lookup", endpoint="https://gamma.example",
    )
    assert error["code"] == "gamma_not_found"
    assert error["retryable"] is False
    assert error["http_status"] == 404


def test_gamma_list_response_is_invalid_for_single_market_slug_endpoint(monkeypatch, tmp_path):
    from scripts import analyze_weekend_liquidity as analysis
    monkeypatch.setattr(analysis, "_http_json", lambda *_args, **_kwargs: [])
    result, _ = fetch_market_public_history(
        "btc-updown-15m-1790347500", cache_dir=str(tmp_path), force_refresh=True,
    )
    assert result["gamma"]["status"] == "gamma_invalid_response"
    assert result["fetch_errors"][0]["code"] == "gamma_invalid_response"
    assert result["trade_fetch_status"] == "identifier_missing"
    assert result["price_fetch_status"] == "identifier_missing"


def test_trade_identity_mismatch_rows_are_excluded(monkeypatch, tmp_path):
    from scripts import analyze_weekend_liquidity as analysis
    market_start = 1_790_347_500
    slug, condition, token = f"btc-updown-15m-{market_start}", "0x" + "33" * 32, "1001"

    def fake_get(url, params=None):
        if "/markets/slug/" in url:
            return {"slug": slug, "conditionId": condition, "clobTokenIds": [token, "1002"]}
        if url.endswith("/trades"):
            return {"data": [
                {"timestamp": market_start + 10, "slug": slug, "conditionId": condition, "asset": token},
                {"timestamp": market_start + 20, "slug": "wrong-slug", "conditionId": condition, "asset": token},
                {"timestamp": market_start + 30, "slug": slug, "conditionId": "0x" + "44" * 32, "asset": token},
            ], "pagination": {}}
        return {"data": []}

    monkeypatch.setattr(analysis, "_http_json", fake_get)
    result, _ = fetch_market_public_history(slug, cache_dir=str(tmp_path), force_refresh=True)
    assert len(result["trades"]) == 1
    assert result["trade_rows_raw"] == 3
    assert result["trade_rows_matched"] == 1
    assert result["trade_identity_mismatch_count"] == 2
    assert result["trade_fetch_status"] == "partial"
    assert any(error["code"] == "trade_identity_mismatch" for error in result["fetch_errors"])


def test_prices_failure_preserves_successful_trades_as_partial(monkeypatch, tmp_path):
    from scripts import analyze_weekend_liquidity as analysis
    condition, tokens = "0x" + "55" * 32, ["555", "666"]

    def fake_get(url, params=None):
        if "/markets/slug/" in url:
            return {"slug": "btc-updown-15m-1790347500", "conditionId": condition, "clobTokenIds": tokens}
        if url.endswith("/trades"):
            return {"data": [{"timestamp": 1790347510, "conditionId": condition, "asset": "555",
                              "size": 3, "price": .55}],
                    "pagination": {}}
        raise TimeoutError("price history timeout")

    monkeypatch.setattr(analysis, "_http_json", fake_get)
    result, _ = fetch_market_public_history(
        "btc-updown-15m-1790347500", cache_dir=str(tmp_path), force_refresh=True,
    )
    assert result["status"] == "PARTIAL_PUBLIC_HISTORY"
    assert result["trade_fetch_status"] == "success"
    assert result["price_fetch_status"] == "failed"
    from scripts.analyze_weekend_liquidity import _public_diagnostic_row
    diagnostics = _public_diagnostic_row("btc-updown-15m-1790347500", result)
    assert diagnostics["price_error_code"] == "timeout"
    assert diagnostics["price_error_endpoint"].endswith("/prices-history")
    market, _ = _public_market_metrics("btc-updown-15m-1790347500", result, "America/New_York")
    assert market["public_trade_count"] == 1
    assert market["price_history_points"] is None


def test_structured_public_history_cache_round_trip(tmp_path, monkeypatch):
    from scripts import analyze_weekend_liquidity as analysis
    condition, tokens = "0x" + "77" * 32, ["777", "888"]
    monkeypatch.setattr(analysis, "_http_json", lambda url, params=None: (
        {"slug": "btc-updown-15m-1790347500", "conditionId": condition, "clobTokenIds": tokens}
        if "/markets/slug/" in url else {"data": [], "pagination": {}}
    ))
    first, _ = fetch_market_public_history(
        "btc-updown-15m-1790347500", cache_dir=str(tmp_path), force_refresh=True,
    )
    second, _ = fetch_market_public_history(
        "btc-updown-15m-1790347500", cache_dir=str(tmp_path),
    )
    assert second["identity"] == first["identity"]
    assert second["gamma"]["market"]["slug"] == "btc-updown-15m-1790347500"
    assert second["cache_used"] is True


def test_offline_report_never_attempts_network_and_writes_fetch_diagnostics(tmp_path, monkeypatch):
    from scripts import analyze_weekend_liquidity as analysis
    db_path = tmp_path / "journal.db"
    slug = "btc-updown-15m-1790347500"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            "CREATE TABLE order_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT, "
            "client_order_id TEXT, side TEXT, price REAL, qty REAL, payload_json TEXT, instrument_id TEXT);"
            "CREATE TABLE strategy_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT, payload_json TEXT);"
        )
        conn.execute(
            "INSERT INTO order_events VALUES (1, ?, 'r', 'ORDER_SUBMIT', 'o', 'BUY', .5, 2, ?, ?)",
            ("2026-09-25T13:00:00+00:00", json.dumps({"slug": slug}),
             "0x" + "ab" * 32 + "-123456789.POLYMARKET"),
        )

    def forbidden_network(*_args, **_kwargs):
        raise AssertionError("--offline must never try public network")

    monkeypatch.setattr(analysis, "_http_json", forbidden_network)
    report = generate_report(
        db_path=str(db_path), output_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "cache"),
        days=90, offline=True,
    )
    assert report["public_diagnostics"][0]["error_code"] == "not_fetched_offline"
    assert report["public_diagnostics"][0]["identity_source"] == "LOCAL_INSTRUMENT_ID"
    assert report["public_diagnostics"][0]["condition_id"] == "0x" + "ab" * 32
    with (tmp_path / "out" / "public_fetch_diagnostics.csv").open() as handle:
        assert "trade_fetch_status" in handle.readline()
    assert "Public historical retrieval" in (tmp_path / "out" / "summary.md").read_text()
