"""RTDS Chainlink freshness flag (observation only; callers decide to fail closed)."""
from bot.price_streams import chainlink_feed_health
from bot.spot_pricer import SpotPricerMixin


def test_fresh_connected_feed_is_not_stale():
    h = chainlink_feed_health(now_ts=100.0, connected=True, spot_received_ts=99.5, twap_received_ts=99.0)
    assert h["stale"] is False and h["reasons"] == []
    assert h["spot_age_sec"] == 0.5 and h["twap_age_sec"] == 1.0


def test_disconnect_missing_and_old_ticks_are_reported():
    h = chainlink_feed_health(now_ts=100.0, connected=False, spot_received_ts=None, twap_received_ts=90.0)
    assert h["stale"] is True
    assert h["reasons"] == ["ws_disconnected", "spot_missing", "twap_stale"]


def test_age_threshold_is_inclusive_of_max_age():
    h = chainlink_feed_health(now_ts=100.0, connected=True, spot_received_ts=97.0, twap_received_ts=97.0,
                              max_age_sec=3.0)
    assert h["stale"] is False


class _Strategy(SpotPricerMixin):
    pass


def test_mixin_reads_receipt_clocks_and_connection_flag():
    s = _Strategy()
    assert s._chainlink_feed_health(now_ts=100.0)["reasons"] == ["ws_disconnected", "spot_missing", "twap_missing"]
    s._polymarket_chainlink_ws_connected = True
    s._polymarket_chainlink_price_ts = 99.0
    s._polymarket_chainlink_twap_price_ts = 95.0
    s.chainlink_feed_max_age_sec = 2.0
    h = s._chainlink_feed_health(now_ts=100.0)
    assert h["reasons"] == ["twap_stale"] and h["max_age_sec"] == 2.0
