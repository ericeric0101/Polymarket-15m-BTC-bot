from decimal import Decimal
from types import SimpleNamespace

import pytest

from bot.order_events import _build_markout_entry_context
from bot.trade_telemetry import TradeTelemetry


def test_trade_telemetry_records_signed_markouts_for_buy_and_sell():
    tracker = TradeTelemetry()
    tracker.record_fill(
        fill_id="buy-1", instrument_key="up", side="buy",
        fill_price=Decimal("0.50"), qty=Decimal("5"), filled_ts=100.0,
        reference_mid=Decimal("0.50"), model_probability=Decimal("0.60"),
        edge_ps=Decimal("0.08"), liquidity_class="maker",
        entry_context={"entry_regime_bucket": "10_30", "entry_side_score": 0.42},
    )
    tracker.record_fill(
        fill_id="sell-1", instrument_key="up", side="sell",
        fill_price=Decimal("0.60"), qty=Decimal("5"), filled_ts=100.0,
        reference_mid=Decimal("0.60"), model_probability=Decimal("0.40"),
        edge_ps=Decimal("0.03"), liquidity_class="maker",
    )
    rows = tracker.observe("up", Decimal("0.55"), 101.0)
    by_id = {row["fill_id"]: row for row in rows}
    assert by_id["buy-1"]["signed_markout_ps"] == 0.05
    assert by_id["buy-1"]["entry_regime_bucket"] == "10_30"
    assert by_id["buy-1"]["entry_side_score"] == 0.42
    assert by_id["sell-1"]["signed_markout_ps"] == 0.05
    assert by_id["buy-1"]["horizon_sec"] == 1


def test_markout_context_records_regime_features_at_fill_time():
    host = SimpleNamespace(
        current_market_slug="btc-updown-15m-test",
        latest_external_spot=Decimal("101"),
        market_strike_cache_by_slug={"btc-updown-15m-test": Decimal("100")},
        current_market_end_timestamp=1100.0,
        side_decision_score=Decimal("0.4"),
        latest_external_spot_source="polymarket_chainlink_twap_60s_ws",
        external_spot_history=[(960.0, Decimal("98")), (990.0, Decimal("100"))],
        latest_quote_by_inst={"inst": (Decimal("0.50"), Decimal("0.52"))},
        latest_quote_depth_by_inst={"inst": (Decimal("20"), Decimal("30"))},
        _side_for_instrument_id=lambda _inst: SimpleNamespace(value="UP"),
        _compute_recent_volatility=lambda _inst: Decimal("0.01"),
    )

    payload = _build_markout_entry_context(host, "inst", 1000.0)

    assert payload["markout_context_schema_version"] == 2
    assert payload["entry_spot_continuation_10s"] == 0.01
    assert payload["entry_spot_continuation_30s"] == pytest.approx(0.0306122449)
    assert payload["entry_bbo_spread"] == pytest.approx(0.02)
    assert payload["entry_bid_depth"] == 20.0
    assert payload["entry_is_weekend_utc"] is False
