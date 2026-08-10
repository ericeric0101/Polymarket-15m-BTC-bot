from decimal import Decimal

from bot.entry_confirmation import EntryConfirmationConfig, EntryConfirmationEngine


def test_extreme_book_conflict_skips_live_entry_even_when_probability_is_not_neutral():
    engine = EntryConfirmationEngine(
        EntryConfirmationConfig(enabled=True, skip_strong_conflict=True)
    )

    signal = engine.evaluate(
        active_side="UP",
        p_fair=Decimal("0.35"),
        fair=Decimal("0.35"),
        best_bid=Decimal("0.06"),
        best_ask=Decimal("0.07"),
        ref_spot=Decimal("65000"),
        ref_spot_source="polymarket_chainlink_twap_60s_ws",
        ref_spot_age_sec=0.5,
        strike=Decimal("64990"),
        binance_spot=Decimal("65000"),
        binance_age_sec=0.5,
    )

    assert signal.state == "book_conflict"
    assert signal.confidence == Decimal("1")
    assert signal.action == "skip"
    assert signal.shadow_only is False
