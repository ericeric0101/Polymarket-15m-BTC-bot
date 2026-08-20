from decimal import Decimal

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
