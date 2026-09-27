from bot.polymarket_exit_capability import inspect_exit_order_capability


def test_installed_market_order_path_is_explicit_about_partial_fill_semantics() -> None:
    capability = inspect_exit_order_capability()

    assert capability.requested_tif == "IOC"
    assert capability.limit_ioc_order_type == "FAK"
    assert capability.market_order_type == "FOK"
    assert capability.market_orders_allow_partial_fill is False
    assert capability.insufficient_depth_can_reject_entire_order is True
    assert capability.limit_ioc_uses_adapter_tif_converter is True
    assert capability.price_bounded_limit_ioc_avoids_fok_requirement is True
    assert capability.aggressive_partial_exit_proven is False
    assert capability.verdict == "NOT_PROVEN_MARKET_PATH_IS_FOK"
