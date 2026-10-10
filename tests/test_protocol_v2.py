"""Polymarket Protocol V2 (Binary) support: ids, signing domain, balances, fees, settlement, redeem routing."""
import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from bot import protocol_v2 as pv2
from bot.fill_ledger import interpret_fill_liquidity
from bot.inventory import InventoryLedger
from bot.post_trade import compute_settlement_summary
from bot.protocol_v2_runtime import install_order_signing_override, normalize_gamma_market_for_protocol

CONDITION = bytes([pv2.BINARY_MODULE_ID]) + bytes(range(1, 31))  # moduleId 1 + 30 bytes
UP_ID = str(pv2.position_id(CONDITION, 0))
DOWN_ID = str(pv2.position_id(CONDITION, 1))
V1_UP, V1_DOWN = "1" * 77, "2" * 77


def _v2_market(**overrides):
    market = {"version": "v2", "slug": "btc-updown-15m-1", "conditionId": "0x" + CONDITION.hex() + "00",
              "outcomes": json.dumps(["Up", "Down"]), "positionIds": [UP_ID, DOWN_ID],
              "clobTokenIds": json.dumps([V1_UP, V1_DOWN])}  # present but must be ignored for v2
    market.update(overrides)
    return market


def _v1_market(**overrides):
    market = {"version": "v1", "slug": "btc-updown-15m-2", "conditionId": "0x" + "ab" * 32,
              "outcomes": json.dumps(["Up", "Down"]), "clobTokenIds": json.dumps([V1_UP, V1_DOWN])}
    market.update(overrides)
    return market


@pytest.fixture(autouse=True)
def _clean_registry():
    pv2._reset_registry_for_tests()
    yield
    pv2._reset_registry_for_tests()


# --- market version and id selection ---------------------------------------------------------------

def test_ids_are_chosen_by_version_not_by_field_presence():
    v2 = pv2.select_market_assets(_v2_market())
    assert v2.protocol == "v2" and v2.asset_ids == (UP_ID, DOWN_ID) and v2.condition_bytes31 == CONDITION
    assert v2.asset_for("Up") == UP_ID and v2.asset_for("down") == DOWN_ID
    v1 = pv2.select_market_assets(_v1_market(positionIds=[UP_ID, DOWN_ID]))
    assert v1.protocol == "v1" and v1.asset_ids == (V1_UP, V1_DOWN) and v1.condition_bytes31 is None


@pytest.mark.parametrize("market", [
    _v1_market(version=None),                                   # missing version
    _v1_market(version="v3"),                                   # unknown version
    _v2_market(positionIds=None),                               # v2 without positionIds
    _v2_market(positionIds=[UP_ID, "12ab"]),                    # non-decimal id
    _v2_market(positionIds=[UP_ID, UP_ID]),                     # outcome byte not 0/1
    _v2_market(positionIds=[UP_ID, str(int(DOWN_ID) + 256)]),   # different condition
    _v2_market(conditionId="0x" + "cd" * 31),                   # Gamma condition disagrees with ids
    _v2_market(outcomes=json.dumps(["A", "B", "C"])),           # not binary
    _v1_market(clobTokenIds=json.dumps([V1_UP])),               # ids/outcomes mismatch
])
def test_unsupported_or_inconsistent_markets_are_rejected(market):
    with pytest.raises(pv2.UnsupportedMarketProtocol):
        pv2.select_market_assets(market)


def test_non_binary_v2_module_is_rejected():
    neg_risk_condition = bytes([2]) + CONDITION[1:]
    market = _v2_market(conditionId="0x" + neg_risk_condition.hex(),
                        positionIds=[str(pv2.position_id(neg_risk_condition, i)) for i in (0, 1)])
    with pytest.raises(pv2.UnsupportedMarketProtocol, match="Binary"):
        pv2.select_market_assets(market)


def test_condition_id_narrowing_requires_a_zero_final_byte():
    assert pv2.narrow_condition_id("0x" + CONDITION.hex()) == CONDITION
    assert pv2.narrow_condition_id("0x" + CONDITION.hex() + "00") == CONDITION
    with pytest.raises(pv2.UnsupportedMarketProtocol):
        pv2.narrow_condition_id("0x" + CONDITION.hex() + "01")


def test_discovery_builds_instruments_from_position_ids_and_registers_protocol():
    from bot.market_discovery import extract_instrument_ids_from_gamma_market

    ids = extract_instrument_ids_from_gamma_market(_v2_market())
    assert [str(i) for i in ids] == [f"0x{CONDITION.hex()}00-{UP_ID}.POLYMARKET",
                                     f"0x{CONDITION.hex()}00-{DOWN_ID}.POLYMARKET"]
    assert pv2.is_v2_asset(UP_ID) and pv2.v2_condition_for_asset(DOWN_ID) == CONDITION
    assert pv2.conditional_asset_type(UP_ID) == "CONDITIONAL-V2"
    assert pv2.conditional_asset_type(V1_UP) == "CONDITIONAL"
    assert extract_instrument_ids_from_gamma_market(_v1_market(version="v9")) == []


def test_gamma_instrument_normalization_uses_position_ids_for_v2():
    def original(market):
        tokens = json.loads(market["clobTokenIds"]) if market.get("clobTokenIds") else []
        return {"tokens": [{"token_id": t, "outcome": o, "price": 0.5, "winner": False}
                           for t, o in zip(tokens, json.loads(market["outcomes"]))]}

    v2 = normalize_gamma_market_for_protocol(original, _v2_market())
    assert [t["token_id"] for t in v2["tokens"]] == [UP_ID, DOWN_ID] and v2["protocol_version"] == "v2"
    v1 = normalize_gamma_market_for_protocol(original, _v1_market())
    assert [t["token_id"] for t in v1["tokens"]] == [V1_UP, V1_DOWN] and v1["protocol_version"] == "v1"
    unknown = normalize_gamma_market_for_protocol(original, _v1_market(version=None))
    assert unknown["tokens"] == []  # never loaded as tradable instruments


# --- order signing ---------------------------------------------------------------------------------

def _signer_and_builder():
    from py_clob_client_v2.order_builder.builder import OrderBuilder
    from py_clob_client_v2.signer import Signer

    install_order_signing_override()
    account = Account.create()  # throwaway test key
    return account, OrderBuilder(Signer(account.key.hex(), 137))


def _recover(order, domain_version, contract):
    from py_clob_client_v2.order_utils.model.ctf_exchange_v2_typed_data import (
        CTF_EXCHANGE_V2_ORDER_STRUCT, EIP712_DOMAIN)

    typed = {
        "primaryType": "Order", "types": {"EIP712Domain": EIP712_DOMAIN, "Order": CTF_EXCHANGE_V2_ORDER_STRUCT},
        "domain": {"name": "Polymarket CTF Exchange", "version": domain_version, "chainId": 137,
                   "verifyingContract": contract},
        "message": {"salt": int(order.salt), "maker": order.maker, "signer": order.signer,
                    "tokenId": int(order.tokenId), "makerAmount": int(order.makerAmount),
                    "takerAmount": int(order.takerAmount), "side": int(order.side),
                    "signatureType": int(order.signatureType), "timestamp": int(order.timestamp),
                    "metadata": bytes.fromhex(order.metadata.removeprefix("0x").zfill(64)),
                    "builder": bytes.fromhex(order.builder.removeprefix("0x").zfill(64))},
    }
    return Account.recover_message(encode_typed_data(full_message=typed), signature=order.signature)


def test_v2_orders_are_signed_for_exchange_v3_domain_3_and_v1_orders_are_unchanged():
    from py_clob_client_v2.clob_types import CreateOrderOptions, MarketOrderArgsV2, OrderArgsV2
    from py_clob_client_v2.config import get_contract_config

    pv2.register_market_assets(pv2.select_market_assets(_v2_market()))
    pv2.register_market_assets(pv2.select_market_assets(_v1_market()))
    account, builder = _signer_and_builder()
    options = CreateOrderOptions(tick_size="0.01", neg_risk=False)

    v2_order = builder.build_order(OrderArgsV2(token_id=UP_ID, price=0.8, size=5, side="BUY"), options, version=1)
    assert v2_order.tokenId == UP_ID and v2_order.makerAmount == "4000000" and v2_order.takerAmount == "5000000"
    assert _recover(v2_order, "3", pv2.EXCHANGE_V3_ADDRESS) == account.address
    assert _recover(v2_order, "2", get_contract_config(137).exchange_v2) != account.address

    v2_market_order = builder.build_market_order(
        MarketOrderArgsV2(token_id=DOWN_ID, amount=5, price=0.6, side="SELL"), options)
    assert _recover(v2_market_order, "3", pv2.EXCHANGE_V3_ADDRESS) == account.address

    v1_order = builder.build_order(OrderArgsV2(token_id=V1_UP, price=0.8, size=5, side="BUY"), options, version=2)
    assert _recover(v1_order, "2", get_contract_config(137).exchange_v2) == account.address


def test_neg_risk_v2_orders_are_refused():
    from py_clob_client_v2.clob_types import CreateOrderOptions, OrderArgsV2

    pv2.register_market_assets(pv2.select_market_assets(_v2_market()))
    _account, builder = _signer_and_builder()
    with pytest.raises(pv2.UnsupportedMarketProtocol):
        builder.build_order(OrderArgsV2(token_id=UP_ID, price=0.8, size=5, side="BUY"),
                            CreateOrderOptions(tick_size="0.01", neg_risk=True))


# --- fees, inventory and settlement ----------------------------------------------------------------

def _taker_buy(protocol):
    return interpret_fill_liquidity(liquidity_side="TAKER", raw_commission_dec=Decimal("0"), maker_matched=False,
                                    side_for_ledger="buy", fill_price_dec=Decimal("0.8"), fill_qty_dec=Decimal("10"),
                                    filled_limit_price=Decimal("0.8"), filled_id="o", protocol=protocol)


def test_taker_buy_fee_is_collateral_not_shares_on_both_protocols():
    # Venue evidence: v1 taker BUYs were also charged in USDC with full shares delivered.
    for protocol in ("v1", "v2"):
        fill = _taker_buy(protocol)
        assert fill.effective_fee_shares_dec == 0 and fill.effective_fee_usdc_dec > 0
    assert _taker_buy("v1").effective_fee_usdc_dec == _taker_buy("v2").effective_fee_usdc_dec


def test_v2_buy_fee_reaches_inventory_cost_and_settlement_pnl():
    fee = _taker_buy("v2").effective_fee_usdc_dec
    ledger = {}
    InventoryLedger.update_from_fill(ledger, "up", "buy", Decimal("0.8"), Decimal("10"), fee, Decimal("0"), 1.0)
    assert ledger["up"]["qty"] == Decimal("10")  # full shares received
    summary = compute_settlement_summary(outcome="UP", inventory_shares=10.0, live_inventory_cost=ledger,
                                         market_cycle_realized_net_usdc=Decimal("0"))
    assert summary.settlement_pnl == pytest.approx(10 * (1 - 0.8) - float(fee))


# --- settlement evidence and redeem routing --------------------------------------------------------

def test_startup_winner_for_v2_requires_resolved_status_and_uses_position_ids():
    from bot.recovery import StrategyRecoveryMixin

    closed = _v2_market(closed=True, outcomePrices=json.dumps(["1", "0"]))
    assert StrategyRecoveryMixin._resolved_gamma_winner_token_id({**closed, "resolutionStatus": "active"}) is None
    assert StrategyRecoveryMixin._resolved_gamma_winner_token_id(
        {**closed, "resolutionStatus": "resolved"}) == ("UP", UP_ID)
    v1 = _v1_market(closed=True, outcomePrices=json.dumps(["0", "1"]))
    assert StrategyRecoveryMixin._resolved_gamma_winner_token_id(v1) == ("DOWN", V1_DOWN)
    assert StrategyRecoveryMixin._resolved_gamma_winner_token_id({**v1, "version": None}) is None


def test_redeem_conditions_are_split_by_market_protocol():
    spec = importlib.util.spec_from_file_location("redeem", Path("scripts/check_positions_and_redeem.py"))
    redeem = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(redeem)
    markets = {"c1": _v1_market(), "c2": _v2_market(), "c3": _v1_market(version="v7")}
    v1, v2, skipped = redeem.split_conditions_by_protocol(["c1", "c2", "c3", "c4"], market_lookup=markets.get)
    assert v1 == ["c1"] and list(v2) == ["c2"] and v2["c2"].condition_bytes31 == CONDITION
    assert set(skipped) == {"c3", "c4"}


def test_router_abi_matches_v2_signatures():
    by_name = {f["name"]: [i["type"] for i in f["inputs"]] for f in pv2.ROUTER_ABI}
    assert by_name == {"merge": ["bytes31", "uint256"], "redeem": ["bytes31", "uint256", "uint256"],
                       "split": ["bytes31", "uint256"]}
