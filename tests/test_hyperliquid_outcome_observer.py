from bot.hyperliquid_outcome_observer import discover_btc_daily_outcome
from scripts.hyperliquid_outcome_lead_lag_report import build_report


def test_discovers_nearest_btc_daily_outcome_and_uses_canonical_side_coins():
    market = discover_btc_daily_outcome({"outcomes": [
        {"outcome": 100, "description": "class:priceBinary|underlying:BTC|expiry:20260903-0300|targetPrice:80000|period:1d"},
        {"outcome": 99, "description": "class:priceBinary|underlying:BTC|expiry:20260902-0300|targetPrice:79000|period:1d"},
        {"outcome": 98, "description": "class:priceBinary|underlying:ETH|expiry:20260902-0300|targetPrice:3000|period:1d"},
    ]})

    assert market is not None
    assert market.outcome_id == 99
    assert market.yes_coin == "#990"
    assert market.no_coin == "#991"
    assert str(market.target_price) == "79000"


def test_lead_lag_report_compares_current_outcome_move_to_future_polymarket_move():
    snapshots = [
        {"ts": float(index * 5), "outcome_yes": 0.50 + index * 0.01, "polymarket_up": 0.40 + max(0, index - 1) * 0.01}
        for index in range(8)
    ]
    report = build_report(snapshots)

    assert report["horizons"]["5"]["sample_count"] == 6
    assert report["horizons"]["5"]["sign_agreement_rate"] == 1.0
