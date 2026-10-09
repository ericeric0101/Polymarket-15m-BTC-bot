"""Runtime hooks that route Protocol V2 assets to the V2 Exchange (installed once at startup).

* Order signing: py-clob-client-v2 1.0.0 only knows CTF exchanges (EIP-712 domain
  version "1"/"2").  For an asset registered as V2 (bot.protocol_v2) orders keep
  the CTFExchangeV2 struct but are signed for ExchangeV3: domain
  ("Polymarket CTF Exchange", "3", 137, EXCHANGE_V3_ADDRESS).  The CLOB's global
  order version never decides this; the market's protocol does.  V1 assets are
  untouched.  Neg-risk V2 is not supported and is refused.
* Gamma instrument loading: the Nautilus provider builds instruments from
  ``clobTokenIds`` only.  For V2 markets the tokens are rebuilt from
  ``positionIds``; markets with an unknown version get no tokens (not tradable).
"""
from __future__ import annotations

import time
from typing import Any

from loguru import logger

from bot.protocol_v2 import (
    EXCHANGE_V3_ADDRESS,
    EXCHANGE_V3_DOMAIN_NAME,
    EXCHANGE_V3_DOMAIN_VERSION,
    UnsupportedMarketProtocol,
    is_v2_asset,
    register_market_assets,
    select_market_assets,
)


def _exchange_v3_builder_class():
    from py_clob_client_v2.order_utils import ExchangeOrderBuilderV2

    class ExchangeOrderBuilderV3(ExchangeOrderBuilderV2):
        """CTFExchangeV2 order struct, signed for the Protocol V2 Exchange (domain version "3")."""

        def build_order_typed_data(self, order) -> dict:
            typed = super().build_order_typed_data(order)
            typed["domain"] = {
                "name": EXCHANGE_V3_DOMAIN_NAME,
                "version": EXCHANGE_V3_DOMAIN_VERSION,
                "chainId": self.chain_id,
                "verifyingContract": EXCHANGE_V3_ADDRESS,
            }
            return typed

    return ExchangeOrderBuilderV3


def build_v3_signed_order(order_builder, order_args, options, *, market: bool):
    """Sign ``order_args`` for ExchangeV3 using the SDK's own amount rounding."""
    from py_clob_client_v2.constants import BYTES32_ZERO
    from py_clob_client_v2.order_builder.builder import ROUNDING_CONFIG
    from py_clob_client_v2.order_utils.model.order_data_v2 import OrderDataV2

    if options.neg_risk:
        raise UnsupportedMarketProtocol("neg-risk Protocol V2 markets are not supported")
    round_config = ROUNDING_CONFIG[options.tick_size]
    if market:
        side, maker_amount, taker_amount = order_builder.get_market_order_amounts(
            order_args.side, order_args.amount, order_args.price, round_config)
        expiration = "0"
    else:
        side, maker_amount, taker_amount = order_builder.get_order_amounts(
            order_args.side, order_args.size, order_args.price, round_config)
        expiration = str(getattr(order_args, "expiration", 0))
    order_data = OrderDataV2(
        maker=order_builder.funder,
        tokenId=order_args.token_id,
        makerAmount=str(maker_amount),
        takerAmount=str(taker_amount),
        side=side,
        signer=order_builder.signer.address(),
        signatureType=order_builder.signature_type,
        timestamp=str(time.time_ns() // 1_000_000),
        metadata=getattr(order_args, "metadata", BYTES32_ZERO),
        builder=getattr(order_args, "builder_code", None) or BYTES32_ZERO,
        expiration=expiration,
    )
    builder = _exchange_v3_builder_class()(EXCHANGE_V3_ADDRESS, order_builder.signer.get_chain_id(),
                                           order_builder.signer)
    return builder.build_signed_order(order_data)


def install_order_signing_override() -> None:
    from py_clob_client_v2.order_builder.builder import OrderBuilder

    if getattr(OrderBuilder, "_btc15m_protocol_v2_patched", False):
        return
    original_build_order = OrderBuilder.build_order
    original_build_market_order = OrderBuilder.build_market_order

    def build_order(self, order_args, options, version: int = 2, fee_rate_bps: int = None):
        if is_v2_asset(order_args.token_id):
            return build_v3_signed_order(self, order_args, options, market=False)
        return original_build_order(self, order_args, options, version=version, fee_rate_bps=fee_rate_bps)

    def build_market_order(self, order_args, options, version: int = 2, fee_rate_bps: int = None):
        if is_v2_asset(order_args.token_id):
            return build_v3_signed_order(self, order_args, options, market=True)
        return original_build_market_order(self, order_args, options, version=version, fee_rate_bps=fee_rate_bps)

    OrderBuilder.build_order = build_order
    OrderBuilder.build_market_order = build_market_order
    OrderBuilder._btc15m_protocol_v2_patched = True


def normalize_gamma_market_for_protocol(original_normalize, gamma_market: dict[str, Any]) -> dict[str, Any]:
    normalized = original_normalize(gamma_market)
    try:
        assets = select_market_assets(gamma_market)
    except UnsupportedMarketProtocol as exc:
        logger.warning(f"Gamma market not loaded as instruments: slug={gamma_market.get('slug')} reason={exc}")
        normalized["tokens"] = []
        normalized["protocol_version"] = None
        return normalized
    register_market_assets(assets)
    normalized["protocol_version"] = assets.protocol
    if assets.protocol == "v2":
        prices = {t.get("outcome"): t.get("price", 0.5) for t in normalized.get("tokens", [])}
        normalized["tokens"] = [
            {"token_id": asset_id, "outcome": outcome, "price": prices.get(outcome, 0.5), "winner": False}
            for outcome, asset_id in zip(assets.outcomes, assets.asset_ids)
        ]
    return normalized


def install_gamma_instrument_override() -> None:
    import nautilus_trader.adapters.polymarket.common.gamma_markets as gamma_mod
    import nautilus_trader.adapters.polymarket.providers as providers_mod

    if getattr(gamma_mod, "_btc15m_protocol_v2_patched", False):
        return
    original = gamma_mod.normalize_gamma_market_to_clob_format

    def normalize(gamma_market):
        return normalize_gamma_market_for_protocol(original, gamma_market)

    gamma_mod.normalize_gamma_market_to_clob_format = normalize
    providers_mod.normalize_gamma_market_to_clob_format = normalize  # imported by name there
    gamma_mod._btc15m_protocol_v2_patched = True


def install_protocol_v2_overrides() -> None:
    install_order_signing_override()
    install_gamma_instrument_override()
