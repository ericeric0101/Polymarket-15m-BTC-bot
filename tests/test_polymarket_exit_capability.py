from bot.polymarket_exit_capability import inspect_exit_order_capability


def test_installed_market_order_path_is_explicit_about_partial_fill_semantics() -> None:
    capability = inspect_exit_order_capability()

    assert capability.requested_tif == "IOC"
    assert capability.limit_ioc_order_type == "FAK"
    assert capability.market_order_type == "FOK"
    assert capability.market_orders_allow_partial_fill is False
