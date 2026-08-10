from decimal import Decimal
from types import SimpleNamespace

from run_bot import IntegratedBTCStrategy


def test_quote_watchdog_skips_reduce_only_market_without_risk():
    strategy = SimpleNamespace(
        market_phase=SimpleNamespace(value="REDUCE_ONLY"),
        inventory_delta_shares=Decimal("0"),
        active_maker_orders={},
    )

    assert IntegratedBTCStrategy._quote_watchdog_recovery_is_needed(strategy) is False


def test_quote_watchdog_keeps_recovery_for_inventory_or_active_market():
    active = SimpleNamespace(
        market_phase=SimpleNamespace(value="ACTIVE"),
        inventory_delta_shares=Decimal("0"),
        active_maker_orders={},
    )
    held = SimpleNamespace(
        market_phase=SimpleNamespace(value="REDUCE_ONLY"),
        inventory_delta_shares=Decimal("1"),
        active_maker_orders={},
    )

    assert IntegratedBTCStrategy._quote_watchdog_recovery_is_needed(active) is True
    assert IntegratedBTCStrategy._quote_watchdog_recovery_is_needed(held) is True
