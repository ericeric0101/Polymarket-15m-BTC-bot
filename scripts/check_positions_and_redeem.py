#!/usr/bin/env python3
"""
Check Polymarket positions by user address and optionally redeem resolved winners.

Official references:
- Data API /positions (user, redeemable): https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user
- CTF redeemPositions: https://docs.polymarket.com/trading/ctf/redeem

Examples:
  venv/bin/python scripts/check_positions_and_redeem.py
  venv/bin/python scripts/check_positions_and_redeem.py --slug btc-updown-15m
  venv/bin/python scripts/check_positions_and_redeem.py --apply --slug btc-updown-15m
  venv/bin/python scripts/check_positions_and_redeem.py --user 0xYourAddress
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.collateral_tokens import (
    COLLATERAL_ONRAMP_ADDRESS,
    PUSD_ADDRESS,
    USDCE_ADDRESS,
    get_ctf_collateral,
)

DATA_API = "https://data-api.polymarket.com"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

CTF_REDEEM_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "collateralToken", "type": "address"},
            {"internalType": "bytes32", "name": "parentCollectionId", "type": "bytes32"},
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256[]", "name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "bytes32", "name": "parentCollectionId", "type": "bytes32"},
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256", "name": "indexSet", "type": "uint256"},
        ],
        "name": "getCollectionId",
        "outputs": [{"internalType": "bytes32", "name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "collateralToken", "type": "address"},
            {"internalType": "bytes32", "name": "collectionId", "type": "bytes32"},
        ],
        "name": "getPositionId",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "account", "type": "address"},
            {"internalType": "uint256", "name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]
ERC20_APPROVE_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}],
        "name": "allowance",
        "outputs": [{"name": "remaining", "type": "uint256"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    },
]
COLLATERAL_ONRAMP_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "_asset", "type": "address"},
            {"internalType": "address", "name": "_to", "type": "address"},
            {"internalType": "uint256", "name": "_amount", "type": "uint256"},
        ],
        "name": "wrap",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

CTF_COLLATERAL_CANDIDATES = [
    ("pUSD", PUSD_ADDRESS),
    ("USDC.e", USDCE_ADDRESS),
]

GWEI = 10**9


def _gwei_to_wei(w3, value: float | int) -> int:
    return int(w3.to_wei(value, "gwei"))


def _to_checksum_address(addr: str) -> str:
    s = addr.strip()
    try:
        from web3 import Web3

        return Web3.to_checksum_address(s)
    except Exception:
        # Allow read-only position checks even when web3 is not installed.
        return s


def _build_eip1559_fees(w3, *, bump_multiplier: float = 1.0) -> dict[str, int]:
    try:
        latest_block = w3.eth.get_block("latest")
        base_fee = int(latest_block.get("baseFeePerGas") or 0)
    except Exception:
        base_fee = 0
    try:
        priority_fee = int(w3.eth.max_priority_fee)
    except Exception:
        priority_fee = 0

    # Polygon often needs materially higher tips than vanilla defaults.
    priority_floor = _gwei_to_wei(w3, float(os.getenv("AUTO_REDEEM_PRIORITY_FEE_GWEI", "35")))
    priority_fee = max(priority_fee, priority_floor)
    max_fee = max(priority_fee * 2, (base_fee * 2) + priority_fee)

    if bump_multiplier > 1.0:
        priority_fee = int(priority_fee * bump_multiplier)
        max_fee = int(max(max_fee, (base_fee * 2) + priority_fee) * bump_multiplier)

    return {
        "maxPriorityFeePerGas": priority_fee,
        "maxFeePerGas": max_fee,
    }


def _estimate_redeem_gas(tx_func, tx_params: dict[str, Any]) -> int:
    estimate = int(tx_func.estimate_gas(tx_params))
    return max(estimate, int(estimate * 1.2))


def _wait_for_receipt_with_replacement_check(w3, txh, owner: str, nonce: int, *, timeout_sec: int) -> Any:
    from web3.exceptions import TimeExhausted, TransactionNotFound

    started = time.time()
    while time.time() - started < timeout_sec:
        try:
            receipt = w3.eth.get_transaction_receipt(txh)
            if receipt is not None:
                return receipt
        except TransactionNotFound:
            pass
        current_nonce = int(w3.eth.get_transaction_count(owner, "latest"))
        if current_nonce > nonce:
            raise RuntimeError(
                f"nonce_advanced_without_receipt current_nonce={current_nonce} nonce={nonce} tx={txh.hex()}"
            )
        time.sleep(3)
    raise TimeExhausted(f"Transaction {txh.hex()} not confirmed within {timeout_sec}s")


def _is_hex_address(v: str | None) -> bool:
    if not v:
        return False
    s = v.strip()
    return s.startswith("0x") and len(s) == 42


def _address_from_private_key(private_key: str) -> str:
    from eth_account import Account

    acct = Account.from_key(private_key)
    return acct.address


def _fetch_positions(user: str, redeemable: bool | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "user": user,
        "sizeThreshold": 0,
        "limit": 500,
        "offset": 0,
    }
    if redeemable is not None:
        params["redeemable"] = str(redeemable).lower()
    r = requests.get(f"{DATA_API}/positions", params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def _slug_match(rec: dict[str, Any], slug_filter: str | None) -> bool:
    if not slug_filter:
        return True
    slug = str(rec.get("slug") or "")
    title = str(rec.get("title") or "")
    needle = slug_filter.lower()
    return needle in slug.lower() or needle in title.lower()


def _print_user_report(user: str, positions: list[dict[str, Any]], slug_filter: str | None) -> None:
    subset = [p for p in positions if _slug_match(p, slug_filter)]
    redeemable_count = sum(1 for p in subset if bool(p.get("redeemable")))
    print(f"\n=== User {user} ===")
    print(f"positions_total={len(positions)} filtered={len(subset)} redeemable={redeemable_count}")

    # Group by condition to avoid noisy duplicate lines.
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in subset:
        grouped[str(p.get("conditionId") or "")].append(p)

    for condition_id, rows in grouped.items():
        rows = [r for r in rows if condition_id]
        if not rows:
            continue
        title = str(rows[0].get("title") or "")
        slug = str(rows[0].get("slug") or "")
        redeemable = any(bool(r.get("redeemable")) for r in rows)
        total_size = sum(float(r.get("size") or 0) for r in rows)
        outcomes = ", ".join(sorted({str(r.get("outcome") or "") for r in rows}))
        print(
            f"- condition={condition_id} redeemable={redeemable} size={total_size:.6f} "
            f"outcomes=[{outcomes}] slug={slug} title={title}"
        )


def _redeem_conditions(
    private_key: str,
    owner_address: str,
    chain_id: int,
    rpc_url: str,
    condition_ids: list[str],
    condition_sizes: dict[str, float],
    wrap_existing_usdce: bool = False,
) -> None:
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware

    if not condition_ids:
        print("No redeemable condition IDs found.")
        return

    def _safe_int(v: Any, default: int = 0) -> int:
        try:
            if v is None:
                return default
            return int(v)
        except Exception:
            return default

    def _build_fee_params(w3: Any) -> dict[str, int]:
        min_priority_fee_gwei = float(os.getenv("AUTO_REDEEM_MIN_PRIORITY_FEE_GWEI", "25"))
        max_priority_fee_gwei = float(os.getenv("AUTO_REDEEM_MAX_PRIORITY_FEE_GWEI", "60"))
        fee_buffer_gwei = float(os.getenv("AUTO_REDEEM_MAX_FEE_BUFFER_GWEI", "5"))

        min_priority_fee_wei = int(max(0.0, min_priority_fee_gwei) * GWEI)
        max_priority_fee_wei = int(max(min_priority_fee_gwei, max_priority_fee_gwei) * GWEI)
        fee_buffer_wei = int(max(0.0, fee_buffer_gwei) * GWEI)

        rpc_priority_fee_wei = 0
        try:
            rpc_priority_fee_wei = _safe_int(w3.eth.max_priority_fee, 0)
        except Exception:
            rpc_priority_fee_wei = 0

        priority_fee_wei = max(min_priority_fee_wei, rpc_priority_fee_wei)
        priority_fee_wei = min(priority_fee_wei, max_priority_fee_wei)

        latest_block = w3.eth.get_block("latest")
        base_fee_wei = _safe_int(latest_block.get("baseFeePerGas"), 0)

        if base_fee_wei > 0:
            max_fee_wei = max((base_fee_wei * 2) + priority_fee_wei + fee_buffer_wei, priority_fee_wei)
            return {
                "maxPriorityFeePerGas": priority_fee_wei,
                "maxFeePerGas": max_fee_wei,
            }

        gas_price_wei = 0
        try:
            gas_price_wei = _safe_int(w3.eth.gas_price, 0)
        except Exception:
            gas_price_wei = 0
        gas_price_wei = max(gas_price_wei, priority_fee_wei)
        return {"gasPrice": gas_price_wei}

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    owner = Web3.to_checksum_address(owner_address)
    contract = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_REDEEM_ABI)
    ctf_collateral = get_ctf_collateral()
    ctf_collateral_address = Web3.to_checksum_address(ctf_collateral.address)
    usdce = w3.eth.contract(address=Web3.to_checksum_address(USDCE_ADDRESS), abi=ERC20_APPROVE_ABI)
    onramp = w3.eth.contract(
        address=Web3.to_checksum_address(COLLATERAL_ONRAMP_ADDRESS),
        abi=COLLATERAL_ONRAMP_ABI,
    )
    nonce = w3.eth.get_transaction_count(owner, "pending")
    max_attempts = max(1, int(os.getenv("AUTO_REDEEM_MAX_SEND_ATTEMPTS", "3")))
    receipt_timeout_sec = max(30, int(os.getenv("AUTO_REDEEM_RECEIPT_TIMEOUT_SEC", "120")))

    def _send_wrap_usdce(amount_base_units: int, *, reason: str) -> bool:
        nonlocal nonce
        if amount_base_units <= 0:
            print(f"wrapToPUSD skipped reason={reason} amount=0.000000")
            return False

        onramp_address = Web3.to_checksum_address(COLLATERAL_ONRAMP_ADDRESS)
        usdce_balance = int(usdce.functions.balanceOf(owner).call())
        wrap_amount = min(int(amount_base_units), usdce_balance)
        if wrap_amount <= 0:
            print(
                f"wrapToPUSD skipped reason={reason} "
                f"wallet_USDC.e={usdce_balance / 1_000_000:.6f} requested={amount_base_units / 1_000_000:.6f}"
            )
            return False
        if wrap_amount < amount_base_units:
            print(
                f"wrapToPUSD amount reduced reason={reason} "
                f"requested={amount_base_units / 1_000_000:.6f} wallet_USDC.e={usdce_balance / 1_000_000:.6f} "
                f"selected={wrap_amount / 1_000_000:.6f}"
            )

        allowance = int(usdce.functions.allowance(owner, onramp_address).call())
        if allowance < wrap_amount:
            approve_tx = usdce.functions.approve(
                onramp_address,
                wrap_amount,
            ).build_transaction({
                "chainId": chain_id,
                "from": owner,
                "nonce": nonce,
            })
            approve_signed = w3.eth.account.sign_transaction(approve_tx, private_key=private_key)
            approve_hash = w3.eth.send_raw_transaction(approve_signed.raw_transaction)
            approve_receipt = w3.eth.wait_for_transaction_receipt(approve_hash, timeout=120)
            print(f"wrapApprove reason={reason} tx={approve_hash.hex()} status={approve_receipt.status}")
            nonce += 1

        try:
            wrap_tx = onramp.functions.wrap(
                Web3.to_checksum_address(USDCE_ADDRESS),
                owner,
                wrap_amount,
            ).build_transaction({
                "chainId": chain_id,
                "from": owner,
                "nonce": nonce,
            })
            wrap_signed = w3.eth.account.sign_transaction(wrap_tx, private_key=private_key)
            wrap_hash = w3.eth.send_raw_transaction(wrap_signed.raw_transaction)
            wrap_receipt = w3.eth.wait_for_transaction_receipt(wrap_hash, timeout=120)
            print(
                f"wrapToPUSD reason={reason} amount={wrap_amount / 1_000_000:.6f} "
                f"tx={wrap_hash.hex()} status={wrap_receipt.status}"
            )
            nonce += 1
            return bool(wrap_receipt.status == 1)
        except Exception as exc:
            print(
                f"wrapToPUSD failed/skipped reason={reason} amount={wrap_amount / 1_000_000:.6f} "
                f"wallet_USDC.e={usdce_balance / 1_000_000:.6f} error={type(exc).__name__}: {exc}"
            )
            return False

    for cid in condition_ids:
        cid_txt = str(cid).strip()
        if not cid_txt.startswith("0x") or len(cid_txt) != 66:
            print(f"Skip invalid conditionId: {cid_txt}")
            continue

        configured_collateral = get_ctf_collateral()
        collateral_candidates = [
            (configured_collateral.symbol, Web3.to_checksum_address(configured_collateral.address)),
            *[
                (symbol, Web3.to_checksum_address(address))
                for symbol, address in CTF_COLLATERAL_CANDIDATES
                if Web3.to_checksum_address(address) != Web3.to_checksum_address(configured_collateral.address)
            ],
        ]
        condition_bytes = Web3.to_bytes(hexstr=cid_txt)
        zero_parent_collection = b"\x00" * 32
        collateral_balances: list[tuple[str, str, int]] = []
        for symbol, collateral_address in collateral_candidates:
            total_balance = 0
            for index_set in (1, 2):
                collection_id = contract.functions.getCollectionId(
                    zero_parent_collection,
                    condition_bytes,
                    index_set,
                ).call()
                position_id = contract.functions.getPositionId(collateral_address, collection_id).call()
                total_balance += int(contract.functions.balanceOf(owner, position_id).call())
            collateral_balances.append((symbol, collateral_address, total_balance))

        selected_symbol, selected_collateral_address, selected_balance = max(
            collateral_balances,
            key=lambda item: item[2],
        )
        balance_report = ", ".join(
            f"{symbol}={balance / 1_000_000:.6f}" for symbol, _, balance in collateral_balances
        )
        if selected_balance <= 0:
            print(f"Skip condition={cid_txt}: no on-chain CTF balance found ({balance_report})")
            continue
        if selected_collateral_address != ctf_collateral_address:
            print(
                f"collateral override condition={cid_txt} "
                f"configured={ctf_collateral.symbol} selected={selected_symbol} "
                f"balances=({balance_report})"
            )
        else:
            print(f"collateral selected condition={cid_txt} {selected_symbol} balances=({balance_report})")

        tx_base = {
            "chainId": chain_id,
            "from": owner,
            "nonce": nonce,
        }
        tx_base.update(_build_fee_params(w3))

        tx_func = contract.functions.redeemPositions(
            selected_collateral_address,
            zero_parent_collection,
            condition_bytes,
            [1, 2],
        )

        last_error: Exception | None = None
        receipt = None
        for attempt in range(1, max_attempts + 1):
            tx_params = dict(tx_base)
            fee_params = _build_fee_params(w3)
            if "maxPriorityFeePerGas" in fee_params and "maxFeePerGas" in fee_params:
                bump_multiplier = 1.0 + (attempt - 1) * 0.20
                fee_params["maxPriorityFeePerGas"] = int(fee_params["maxPriorityFeePerGas"] * bump_multiplier)
                fee_params["maxFeePerGas"] = max(
                    int(fee_params["maxFeePerGas"] * bump_multiplier),
                    fee_params["maxPriorityFeePerGas"],
                )
            elif "gasPrice" in fee_params:
                fee_params["gasPrice"] = int(fee_params["gasPrice"] * (1.0 + (attempt - 1) * 0.20))
            tx_params.update(fee_params)
            tx_params["gas"] = _estimate_redeem_gas(tx_func, tx_params)
            tx = tx_func.build_transaction(tx_params)
            signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
            txh = w3.eth.send_raw_transaction(signed.raw_transaction)
            print(
                "submit redeemPositions "
                f"condition={cid_txt} attempt={attempt}/{max_attempts} tx={txh.hex()} "
                f"nonce={nonce} gas={tx_params['gas']} "
                + (
                    f"maxFeePerGas={tx_params['maxFeePerGas']} "
                    f"maxPriorityFeePerGas={tx_params['maxPriorityFeePerGas']}"
                    if "maxFeePerGas" in tx_params
                    else f"gasPrice={tx_params['gasPrice']}"
                )
            )
            try:
                receipt = _wait_for_receipt_with_replacement_check(
                    w3,
                    txh,
                    owner,
                    nonce,
                    timeout_sec=receipt_timeout_sec,
                )
                break
            except Exception as exc:
                last_error = exc
                latest_nonce = int(w3.eth.get_transaction_count(owner, "latest"))
                print(
                    "redeem wait timeout/retry "
                    f"condition={cid_txt} attempt={attempt}/{max_attempts} "
                    f"tx={txh.hex()} latest_nonce={latest_nonce} error={exc}"
                )
                if latest_nonce > nonce:
                    raise RuntimeError(
                        f"nonce advanced for redeem tx but no receipt was found. "
                        f"condition={cid_txt} nonce={nonce} latest_nonce={latest_nonce}"
                    ) from exc
                if attempt >= max_attempts:
                    raise
                time.sleep(2)

        if receipt is None:
            raise RuntimeError(
                f"Failed to confirm redeem tx for condition={cid_txt}"
            ) from last_error

        print(f"redeemPositions condition={cid_txt} tx={receipt.transactionHash.hex()} status={receipt.status}")
        nonce += 1

        expected_base_units = int(max(0.0, float(condition_sizes.get(cid_txt, 0.0))) * 1_000_000)
        amount_base_units = expected_base_units if expected_base_units > 0 else selected_balance
        if amount_base_units > 0 and selected_symbol == "USDC.e":
            _send_wrap_usdce(amount_base_units, reason=f"condition={cid_txt}")
        elif amount_base_units > 0:
            print(
                f"wrapToPUSD skipped condition={cid_txt} "
                f"ctf_collateral={selected_symbol} amount={amount_base_units / 1_000_000:.6f}"
            )

    if wrap_existing_usdce:
        usdce_balance = int(usdce.functions.balanceOf(owner).call())
        _send_wrap_usdce(usdce_balance, reason="existing_usdce_balance")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Polymarket positions and redeem winners")
    parser.add_argument("--env-file", default=".env", help="Path to env file (default: .env)")
    parser.add_argument("--slug", default=None, help="Filter by slug/title contains keyword")
    parser.add_argument(
        "--user",
        action="append",
        default=[],
        help="User address to check (can pass multiple times)",
    )
    parser.add_argument("--apply", action="store_true", help="Redeem winners on-chain")
    parser.add_argument("--rpc-url", default=None, help="Polygon RPC URL")
    parser.add_argument("--chain-id", type=int, default=None, help="Chain ID override")
    parser.add_argument(
        "--min-condition-size",
        type=float,
        default=None,
        help="Skip redeem for a condition when total redeemable size is below this threshold",
    )
    parser.add_argument(
        "--min-total-size",
        type=float,
        default=None,
        help="Skip the whole redeem run when selected redeemable total size is below this threshold",
    )
    parser.add_argument(
        "--wrap-existing-usdce",
        action="store_true",
        help="After redeem attempts, wrap any existing wallet USDC.e balance to pUSD.",
    )
    args = parser.parse_args()

    load_dotenv(args.env_file, override=False)

    private_key = os.getenv("POLYMARKET_PK", "").strip()
    signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
    chain_id = args.chain_id or int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
    rpc_url = args.rpc_url or os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    min_condition_size = (
        float(os.getenv("AUTO_REDEEM_MIN_CONDITION_SIZE", "0.10"))
        if args.min_condition_size is None
        else max(0.0, float(args.min_condition_size))
    )
    min_total_size = (
        float(os.getenv("AUTO_REDEEM_MIN_TOTAL_SIZE", "0.50"))
        if args.min_total_size is None
        else max(0.0, float(args.min_total_size))
    )

    env_addresses = [
        os.getenv("POLYMARKET_FUNDER", "").strip(),
        os.getenv("POLYMARKET_WALLET_ADDRESS", "").strip(),
        os.getenv("WALLET_ADDRESS", "").strip(),
    ]
    if private_key:
        try:
            env_addresses.append(_address_from_private_key(private_key))
        except Exception:
            pass

    all_candidates = [*args.user, *env_addresses]
    users: list[str] = []
    for a in all_candidates:
        if _is_hex_address(a):
            try:
                users.append(_to_checksum_address(a))
            except Exception:
                continue
    users = sorted(set(users))

    if not users:
        print("No valid user addresses found. Set POLYMARKET_WALLET_ADDRESS/POLYMARKET_FUNDER or pass --user.")
        return 2

    print("=== Polymarket Position Checker ===")
    print(f"Data API: {DATA_API}")
    print(f"Slug filter: {args.slug or '(none)'}")
    print(f"Users: {json.dumps(users)}")

    by_user: dict[str, list[dict[str, Any]]] = {}
    for user in users:
        try:
            positions = _fetch_positions(user=user, redeemable=None)
        except Exception as e:
            print(f"\nUser {user} fetch failed: {e}")
            continue
        by_user[user] = positions
        _print_user_report(user=user, positions=positions, slug_filter=args.slug)

    # Stop after reporting unless apply requested.
    if not args.apply:
        print("\nCheck complete. Use --apply to redeem redeemable positions.")
        return 0

    if not private_key:
        print("Missing POLYMARKET_PK for --apply.")
        return 2

    if signature_type != 0:
        print(
            "Detected POLYMARKET_SIGNATURE_TYPE != 0 (proxy/safe wallet mode). "
            "This script only sends direct EOA on-chain tx. "
            "Use Polymarket relayer flow for proxy/safe wallets."
        )
        return 2

    owner = (
        os.getenv("POLYMARKET_FUNDER", "").strip()
        or os.getenv("POLYMARKET_WALLET_ADDRESS", "").strip()
        or _address_from_private_key(private_key)
    )
    if not _is_hex_address(owner):
        print("Could not determine owner address for --apply.")
        return 2
    owner = _to_checksum_address(owner)

    owner_positions = by_user.get(owner) or []
    if not owner_positions:
        try:
            owner_positions = _fetch_positions(owner, redeemable=None)
        except Exception as e:
            print(f"Failed to fetch owner positions for redeem: {e}")
            return 1

    redeemable_conditions: list[str] = []
    redeemable_condition_sizes: dict[str, float] = defaultdict(float)
    seen: set[str] = set()
    for p in owner_positions:
        if not _slug_match(p, args.slug):
            continue
        if not bool(p.get("redeemable")):
            continue
        cid = str(p.get("conditionId") or "")
        if not cid:
            continue
        redeemable_condition_sizes[cid] += float(p.get("size") or 0.0)
        if cid not in seen:
            seen.add(cid)
            redeemable_conditions.append(cid)

    filtered_redeemable_conditions: list[str] = []
    skipped_small_conditions: list[tuple[str, float]] = []
    for cid in redeemable_conditions:
        total_size = redeemable_condition_sizes.get(cid, 0.0)
        if total_size + 1e-12 < min_condition_size:
            skipped_small_conditions.append((cid, total_size))
            continue
        filtered_redeemable_conditions.append(cid)

    selected_total_size = sum(redeemable_condition_sizes.get(cid, 0.0) for cid in filtered_redeemable_conditions)

    print("\n=== Redeem Plan ===")
    print(f"Owner: {owner}")
    print(f"Chain ID: {chain_id}")
    print(f"RPC URL: {rpc_url}")
    ctf_collateral = get_ctf_collateral()
    print(f"CTF collateral: {ctf_collateral.symbol} {ctf_collateral.address}")
    print(f"Min condition size: {min_condition_size:.6f}")
    print(f"Min total size: {min_total_size:.6f}")
    print(f"Redeemable conditions (raw): {json.dumps(redeemable_conditions)}")
    if skipped_small_conditions:
        skipped_fmt = ", ".join(f"{cid}:{size:.6f}" for cid, size in skipped_small_conditions)
        print(f"Skipped small conditions: {skipped_fmt}")
    print(f"Redeemable conditions (selected): {json.dumps(filtered_redeemable_conditions)}")
    print(f"Selected total redeemable size: {selected_total_size:.6f}")

    if selected_total_size + 1e-12 < min_total_size:
        print(
            f"Skip redeem run: selected_total_size={selected_total_size:.6f} "
            f"< min_total_size={min_total_size:.6f}"
        )
        print("Redeem flow complete.")
        return 0

    _redeem_conditions(
        private_key=private_key,
        owner_address=owner,
        chain_id=chain_id,
        rpc_url=rpc_url,
        condition_ids=filtered_redeemable_conditions,
        condition_sizes=redeemable_condition_sizes,
        wrap_existing_usdce=bool(args.wrap_existing_usdce),
    )
    print("Redeem flow complete.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(130)
