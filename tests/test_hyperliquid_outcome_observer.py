import json
import time

from bot.hyperliquid_outcome_observer import HyperliquidOutcomeObserver, outcome_coins
from scripts.hyperliquid_outcome_lead_lag_report import build_report


def _feed_valid_stream(observer):
    observer._merge(stream_connected=True)
    observer._on_message({"channel": "allMids", "data": {"mids": {
        "#13130": "0.044", "#13131": "0.956", "BTC": "77499.5",
    }}})
    for coin in ("#13130", "#13131"):
        observer._on_message({"channel": "l2Book", "data": {
            "coin": coin, "time": 123, "levels": [[{"px": "0.044", "sz": "5"}], [{"px": "0.04662", "sz": "6"}]],
        }})


def test_ws_observer_requires_connected_fresh_mids_and_both_fresh_books():
    assert outcome_coins(1313) == ("#13130", "#13131")
    observer = HyperliquidOutcomeObserver(market_id=1313, ws_url="wss://example.invalid/ws")
    _feed_valid_stream(observer)
    snapshot = observer.snapshot()

    assert snapshot["analysis_available"] is True
    assert snapshot["side0_bbo_mid"] == 0.04531
    assert snapshot["side0_bid_depth"] == 5.0
    observer._merge(stream_connected=False, reason="ConnectionClosed")
    disconnected = observer.snapshot()
    assert disconnected["available"] is False
    assert disconnected["analysis_available"] is False
    assert disconnected["side0_all_mid"] == 0.044


def test_l2_update_cannot_make_an_old_all_mids_value_fresh():
    observer = HyperliquidOutcomeObserver(market_id=1313)
    _feed_valid_stream(observer)
    observer._merge(mids_received_ts=time.time() - 10.0)
    observer._on_message({"channel": "l2Book", "data": {
        "coin": "#13130", "time": 124, "levels": [[{"px": "0.045", "sz": "5"}], [{"px": "0.047", "sz": "6"}]],
    }})

    snapshot = observer.snapshot()
    assert snapshot["mids_age_sec"] > 5.0
    assert snapshot["analysis_available"] is False


def test_recent_daily_selection_from_other_bot_is_rollover_authority(tmp_path):
    authority_path = tmp_path / "outcome_market_authority.json"
    authority_path.write_text(json.dumps({
        "market_id": 1314, "period": "1d", "side0_coin": "#13140",
        "side1_coin": "#13141", "updated_at_ms": int(time.time() * 1000),
    }), encoding="utf-8")
    observer = HyperliquidOutcomeObserver(market_id=1313, authority_path=str(authority_path))

    assert observer._authority_market_id() == 1314


def test_rollover_authority_rejects_stale_or_mismatched_state(tmp_path):
    authority_path = tmp_path / "outcome_market_authority.json"
    authority_path.write_text(json.dumps({
        "market_id": 1314, "period": "1d", "side0_coin": "#bad",
        "side1_coin": "#13141", "updated_at_ms": int(time.time() * 1000),
    }), encoding="utf-8")
    observer = HyperliquidOutcomeObserver(authority_path=str(authority_path))
    assert observer._authority_market_id() is None


def test_lead_lag_report_keeps_groups_separate_and_counts_non_following_moves():
    snapshots = [
        {"run_id": "r", "slug": "a", "market_id": 1, "ts": float(index * 5), "outcome_side0": 0.50 + index * 0.01, "polymarket_up": 0.40}
        for index in range(4)
    ] + [
        {"run_id": "r", "slug": "b", "market_id": 2, "ts": float(index * 5), "outcome_side0": 0.50, "polymarket_up": 0.60 + index * 0.01}
        for index in range(4)
    ]
    report = build_report(snapshots)

    assert report["group_count"] == 2
    assert report["horizons"]["5"]["sample_count"] == 4
    assert report["horizons"]["5"]["outcome_move_sample_count"] == 2
    assert report["horizons"]["5"]["sign_agreement_rate"] == 0.0
