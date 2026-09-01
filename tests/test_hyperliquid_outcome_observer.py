from bot.hyperliquid_outcome_observer import HyperliquidOutcomeObserver, outcome_coins
from scripts.hyperliquid_outcome_lead_lag_report import build_report


def test_mainnet_ws_observer_uses_canonical_side_coins_and_stream_updates():
    assert outcome_coins(1313) == ("#13130", "#13131")
    observer = HyperliquidOutcomeObserver(market_id=1313, ws_url="wss://example.invalid/ws")
    observer._on_message({"channel": "allMids", "data": {"mids": {
        "#13130": "0.044", "#13131": "0.956", "BTC": "77499.5",
    }}})
    observer._on_message({"channel": "l2Book", "data": {
        "coin": "#13130", "time": 123, "levels": [[{"px": "0.044"}], [{"px": "0.04662"}]],
    }})
    snapshot = observer.snapshot()

    assert snapshot["available"] is True
    assert snapshot["source"] == "hyperliquid_outcome_mainnet_ws"
    assert snapshot["yes_mid"] == 0.044
    assert snapshot["yes_bid"] == 0.044
    assert snapshot["yes_ask"] == 0.04662


def test_lead_lag_report_compares_current_outcome_move_to_future_polymarket_move():
    snapshots = [
        {"ts": float(index * 5), "outcome_yes": 0.50 + index * 0.01, "polymarket_up": 0.40 + max(0, index - 1) * 0.01}
        for index in range(8)
    ]
    report = build_report(snapshots)

    assert report["horizons"]["5"]["sample_count"] == 6
    assert report["horizons"]["5"]["sign_agreement_rate"] == 1.0
