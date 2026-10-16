from decimal import Decimal

from bot.side_decision import SideDecisionMixin


class _Host(SideDecisionMixin):
    def __init__(self, now: float, *, source_ts: float | None, receive_ts: float | None):
        self.current_up_instrument_id = "up-token"
        self.latest_quote_by_inst = {"up-token": (Decimal("0.40"), Decimal("0.42"))}
        self.last_quote_source_ts_by_inst = (
            {"up-token": source_ts} if source_ts is not None else {}
        )
        self.last_quote_received_ts_by_inst = (
            {"up-token": receive_ts} if receive_ts is not None else {}
        )
        self.quote_max_delivery_delay_sec = 2.0


def test_side_decision_ignores_source_quote_older_than_existing_freshness_limit():
    host = _Host(100.0, source_ts=70.0, receive_ts=99.5)
    diagnostics = {}

    mid = host._get_up_token_mid_for_side_decision(now_ts=100.0, diagnostics=diagnostics)

    assert mid is None
    assert diagnostics["market_mid_unavailable_reason"] == "source_quote_stale"
    assert diagnostics["market_mid_source_age_sec"] == 30.0


def test_side_decision_ignores_recent_source_if_local_quote_delivery_is_stale():
    host = _Host(100.0, source_ts=99.8, receive_ts=96.0)
    diagnostics = {}

    mid = host._get_up_token_mid_for_side_decision(now_ts=100.0, diagnostics=diagnostics)

    assert mid is None
    assert diagnostics["market_mid_unavailable_reason"] == "received_quote_stale"
    assert diagnostics["market_mid_received_age_sec"] == 4.0


def test_side_decision_uses_fresh_timestamped_quote_and_reports_ages():
    host = _Host(100.0, source_ts=99.2, receive_ts=99.5)
    diagnostics = {}

    mid = host._get_up_token_mid_for_side_decision(now_ts=100.0, diagnostics=diagnostics)

    assert mid == Decimal("0.41")
    assert diagnostics["market_mid_unavailable_reason"] is None
    assert abs(diagnostics["market_mid_source_age_sec"] - 0.8) < 1e-9
    assert abs(diagnostics["market_mid_received_age_sec"] - 0.5) < 1e-9


def test_side_decision_does_not_fallback_to_untimestamped_price_history():
    host = _Host(100.0, source_ts=None, receive_ts=None)
    host.real_price_history_by_inst = {"up-token": [Decimal("0.80")]}
    host.real_price_history = [Decimal("0.80")]
    diagnostics = {}

    mid = host._get_up_token_mid_for_side_decision(now_ts=100.0, diagnostics=diagnostics)

    assert mid is None
    assert diagnostics["market_mid_unavailable_reason"] == "quote_timestamp_missing"
