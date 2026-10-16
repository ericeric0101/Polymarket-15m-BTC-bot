"""Polymarket Protocol V2 support (Binary markets only).

Gamma reports ``version`` per market: ``"v1"`` (legacy CTF; trade ``clobTokenIds``)
or ``"v2"`` (PositionManager; trade ``positionIds``).  Everything else that
differs by protocol keys off the asset id, so discovery registers each asset's
protocol here and signing, balance checks, fee bookkeeping, merge and redeem
look it up instead of re-deriving it.

V2 id layout (polymarket-v2-external docs/position-ids.md):
    positionId (uint256) = [moduleId(8) | baseHash(128) | arity(16) | reserved(64)
                            | resolutionChain(16) | conditionIndex(16) | outcomeIndex(8)]
    conditionId (bytes31) = positionId without the outcome byte
so ``positionId == (conditionId << 8) | outcomeIndex``.  Only the Binary module
(moduleId 1, outcomes 0 and 1) is supported; anything else is rejected rather
than traded with guessed ids.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Any, Optional

PROTOCOL_V1 = "v1"
PROTOCOL_V2 = "v2"
SUPPORTED_PROTOCOLS = frozenset({PROTOCOL_V1, PROTOCOL_V2})

# Polygon proxies (polymarket-v2-external README "Deployed contracts").
POSITION_MANAGER_ADDRESS = "0x006F54F7f9A22e0000CC2AB60031000000ae9fEF"
EXCHANGE_V3_ADDRESS = "0xe3333700cA9d93003F00f0F71f8515005F6c00Aa"
ROUTER_ADDRESS = "0x12121212006e4CD160D18e3f00711DA5c3372600"
# EIP-712 domain of the V2 Exchange (Exchange.sol _domainNameAndVersion; API migration guide).
EXCHANGE_V3_DOMAIN_NAME = "Polymarket CTF Exchange"
EXCHANGE_V3_DOMAIN_VERSION = "3"
POLYGON_CHAIN_ID = 137

BINARY_MODULE_ID = 1
ASSET_TYPE_CONDITIONAL_V1 = "CONDITIONAL"
ASSET_TYPE_CONDITIONAL_V2 = "CONDITIONAL-V2"

_DECIMAL_ID = re.compile(r"[0-9]{1,78}")
_UINT256_MAX = (1 << 256) - 1


class UnsupportedMarketProtocol(ValueError):
    """The market cannot be traded safely by this bot (unknown version, bad ids, non-binary V2)."""


@dataclass(frozen=True)
class MarketAssets:
    protocol: str
    condition_id: str          # as reported by Gamma (used for instrument ids and user-stream subscriptions)
    outcomes: tuple[str, ...]
    asset_ids: tuple[str, ...]  # decimal strings, aligned with outcomes
    condition_bytes31: Optional[bytes] = None  # V2 only: Router conditionId

    def asset_for(self, outcome: str) -> str:
        wanted = str(outcome).strip().lower()
        for name, asset_id in zip(self.outcomes, self.asset_ids):
            if name.strip().lower() == wanted:
                return asset_id
        raise UnsupportedMarketProtocol(f"outcome {outcome!r} not in {self.outcomes}")


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def market_protocol(market: dict[str, Any]) -> str:
    """The market's protocol; missing or unknown versions are rejected (API migration guide)."""
    version = str(market.get("version") or "").strip().lower()
    if version not in SUPPORTED_PROTOCOLS:
        raise UnsupportedMarketProtocol(f"unsupported market version {market.get('version')!r}")
    return version


def _decimal_ids(raw: Any, field: str) -> list[str]:
    ids = [str(v).strip() for v in _json_list(raw)]
    if not ids or any(not _DECIMAL_ID.fullmatch(v) or int(v) > _UINT256_MAX for v in ids):
        raise UnsupportedMarketProtocol(f"{field} missing or not decimal uint256 ids")
    return ids


def condition_bytes31_from_position(position_id: int) -> bytes:
    return (position_id >> 8).to_bytes(31, "big")


def position_id(condition_bytes31: bytes, outcome_index: int) -> int:
    if len(condition_bytes31) != 31 or outcome_index not in (0, 1):
        raise UnsupportedMarketProtocol("binary position needs a bytes31 condition and outcome 0/1")
    return (int.from_bytes(condition_bytes31, "big") << 8) | outcome_index


def narrow_condition_id(condition_hex: str) -> bytes:
    """Gamma/Router condition id -> bytes31.  A padded bytes32 must end in a zero byte."""
    raw = bytes.fromhex(str(condition_hex).lower().removeprefix("0x"))
    if len(raw) == 31:
        return raw
    if len(raw) == 32 and raw[-1] == 0:
        return raw[:31]
    raise UnsupportedMarketProtocol(f"condition id {condition_hex!r} is not a bytes31 (or zero-padded bytes32)")


def select_market_assets(market: dict[str, Any]) -> MarketAssets:
    """Pick the tradable ids for ``market`` by its version (never by field presence)."""
    protocol = market_protocol(market)
    condition_id = str(market.get("conditionId") or market.get("condition_id") or "").strip()
    if not condition_id:
        raise UnsupportedMarketProtocol("market has no conditionId")
    outcomes = tuple(str(o) for o in _json_list(market.get("outcomes")))
    if protocol == PROTOCOL_V1:
        ids = _decimal_ids(market.get("clobTokenIds") or market.get("clob_token_ids"), "clobTokenIds")
        if outcomes and len(outcomes) != len(ids):
            raise UnsupportedMarketProtocol("clobTokenIds and outcomes differ in length")
        return MarketAssets(PROTOCOL_V1, condition_id, outcomes or tuple(f"outcome_{i}" for i in range(len(ids))),
                            tuple(ids))
    ids = _decimal_ids(market.get("positionIds") or market.get("position_ids"), "positionIds")
    if len(outcomes) != 2 or len(ids) != 2:
        raise UnsupportedMarketProtocol("only binary V2 markets (two outcomes, two positionIds) are supported")
    values = [int(v) for v in ids]
    condition = condition_bytes31_from_position(values[0])
    if condition[0] != BINARY_MODULE_ID:
        raise UnsupportedMarketProtocol(f"V2 module {condition[0]} is not the Binary module")
    for index, value in enumerate(values):
        if value != position_id(condition, index):
            raise UnsupportedMarketProtocol("positionIds do not follow conditionId|outcomeIndex for outcomes 0/1")
    try:
        gamma_condition = narrow_condition_id(condition_id)
    except (ValueError, UnsupportedMarketProtocol):
        gamma_condition = None
    if gamma_condition is not None and gamma_condition != condition:
        raise UnsupportedMarketProtocol("Gamma conditionId does not match the positionIds")
    return MarketAssets(PROTOCOL_V2, condition_id, outcomes, tuple(ids), condition)


# ---------------------------------------------------------------------------------------------------
# asset id -> protocol registry (filled at discovery; read by signing, balances, fees, merge, redeem)
# ---------------------------------------------------------------------------------------------------

_REGISTRY_LOCK = threading.Lock()
_ASSET_PROTOCOL: dict[str, str] = {}
_ASSET_CONDITION: dict[str, bytes] = {}
_REGISTRY_MAX = 4096


def register_market_assets(assets: MarketAssets) -> None:
    with _REGISTRY_LOCK:
        if len(_ASSET_PROTOCOL) > _REGISTRY_MAX:
            _ASSET_PROTOCOL.clear()
            _ASSET_CONDITION.clear()
        for asset_id in assets.asset_ids:
            _ASSET_PROTOCOL[asset_id] = assets.protocol
            if assets.condition_bytes31 is not None:
                _ASSET_CONDITION[asset_id] = assets.condition_bytes31


def asset_protocol(asset_id: Any) -> Optional[str]:
    with _REGISTRY_LOCK:
        return _ASSET_PROTOCOL.get(str(asset_id or "").strip())


def is_v2_asset(asset_id: Any) -> bool:
    return asset_protocol(asset_id) == PROTOCOL_V2


def v2_condition_for_asset(asset_id: Any) -> Optional[bytes]:
    with _REGISTRY_LOCK:
        return _ASSET_CONDITION.get(str(asset_id or "").strip())


def conditional_asset_type(asset_id: Any) -> str:
    """CLOB /balance-allowance asset_type for an outcome asset."""
    return ASSET_TYPE_CONDITIONAL_V2 if is_v2_asset(asset_id) else ASSET_TYPE_CONDITIONAL_V1


def _reset_registry_for_tests() -> None:
    with _REGISTRY_LOCK:
        _ASSET_PROTOCOL.clear()
        _ASSET_CONDITION.clear()


# ---------------------------------------------------------------------------------------------------
# Minimal ABIs (polymarket-v2-external src/routers/Router.sol, PositionManager.sol; ERC1155/ERC20)
# ---------------------------------------------------------------------------------------------------

ROUTER_ABI = [
    {"type": "function", "name": "merge", "stateMutability": "nonpayable", "outputs": [],
     "inputs": [{"name": "_conditionId", "type": "bytes31"}, {"name": "_amount", "type": "uint256"}]},
    {"type": "function", "name": "redeem", "stateMutability": "nonpayable", "outputs": [],
     "inputs": [{"name": "_conditionId", "type": "bytes31"}, {"name": "_outcomeIndex", "type": "uint256"},
                {"name": "_amount", "type": "uint256"}]},
    {"type": "function", "name": "split", "stateMutability": "nonpayable", "outputs": [],
     "inputs": [{"name": "_conditionId", "type": "bytes31"}, {"name": "_amount", "type": "uint256"}]},
]
POSITION_MANAGER_ABI = [
    {"type": "function", "name": "balanceOf", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "isApprovedForAll", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}, {"name": "operator", "type": "address"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"type": "function", "name": "setApprovalForAll", "stateMutability": "nonpayable", "outputs": [],
     "inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}]},
    {"type": "function", "name": "getPayout", "stateMutability": "view",
     "inputs": [{"name": "positionId", "type": "uint256"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]
