from decimal import Decimal

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
