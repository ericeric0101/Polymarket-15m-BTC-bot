from decimal import Decimal
import asyncio
import time

from bot.pricing_runtime import PricingRuntimeMixin


class _Host(PricingRuntimeMixin):
    maker_vol_warmup_quotes = 3
    maker_vol_rolling_window = 5
    maker_vol_return_clip = Decimal("1")
    maker_vol_ewma_alpha = Decimal("0.35")
    real_price_history = [Decimal("0.50"), Decimal("0.50"), Decimal("0.50")]
    real_price_history_by_inst = {
        "UP": [Decimal("0.50"), Decimal("0.51"), Decimal("0.52")],
        # This is what used to contaminate the global series: complementary
        # DOWN mids interleaved with UP mids.
        "DOWN": [Decimal("0.50"), Decimal("0.49"), Decimal("0.48")],
    }

    def _momentum_history_for_instrument(self, instrument_id):
        return self.real_price_history_by_inst.get(str(instrument_id), self.real_price_history)


def test_recent_volatility_uses_only_the_requested_outcome_history():
    host = _Host()

    up_vol = host._compute_recent_volatility("UP")
    down_vol = host._compute_recent_volatility("DOWN")

    assert up_vol is not None
    assert down_vol is not None
    # Both monotonic histories have the same magnitude of return volatility.
    assert abs(up_vol - down_vol) < Decimal("0.01")


class _BookLevel:
    def __init__(self, price, size):
        self.price = Decimal(str(price))
        self._size = Decimal(str(size))

    def size(self):
        return self._size


class _LiveBook:
    def bids(self):
        return [_BookLevel("0.88", "120")]

    def asks(self):
        return [_BookLevel("0.89", "140")]


class _DepthHost(PricingRuntimeMixin):
    quote_max_delivery_delay_sec = 2.0

    def __init__(self, *, l2_ts=None):
        self.cache = type("Cache", (), {"order_book": lambda _self, inst: _LiveBook() if inst == "UP" else None})()
        self.fast_follow_l2_update_ts_by_inst = {"UP": l2_ts} if l2_ts is not None else {}
        self._balance_clob_client = type(
            "RestClient",
            (),
            {"get_order_book": lambda *_args: (_ for _ in ()).throw(AssertionError("REST book must not be used"))},
        )()

    def _normalize_instrument_id(self, instrument_id):
        return instrument_id


def test_maker_depth_reads_fresh_native_l2_book_instead_of_rest_snapshot():
    host = _DepthHost(l2_ts=time.time())

    bids, asks = asyncio.run(host._get_orderbook_levels_for_instrument("UP"))

    assert bids == [(Decimal("0.88"), Decimal("120"))]
    assert asks == [(Decimal("0.89"), Decimal("140"))]


def test_maker_depth_fails_closed_when_native_l2_book_is_stale():
    host = _DepthHost(l2_ts=time.time() - 3.0)

    bids, asks = asyncio.run(host._get_orderbook_levels_for_instrument("UP"))

    assert bids is None
    assert asks is None
