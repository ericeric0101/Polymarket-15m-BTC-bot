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
from collections import defaultdict
from typing import Any

import requests
from dotenv import load_dotenv

DATA_API = "https://data-api.polymarket.com"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
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
]

GWEI = 10**9


def _is_hex_address(v: str | None) -> bool:
    if not v:
        return False
    s = v.strip()
    return s.startswith("0x") and len(s) == 42


def _to_checksum_address(addr: str) -> str:
    s = addr.strip()
    try:
        from web3 import Web3

        return Web3.to_checksum_address(s)
    except Exception:
        # Allow read-only position checks even when web3 is not installed.
        return s


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
    nonce = w3.eth.get_transaction_count(owner, "pending")

    for cid in condition_ids:
        cid_txt = str(cid).strip()
        if not cid_txt.startswith("0x") or len(cid_txt) != 66:
            print(f"Skip invalid conditionId: {cid_txt}")
            continue

        tx_base = {
            "chainId": chain_id,
            "from": owner,
            "nonce": nonce,
        }
        tx_base.update(_build_fee_params(w3))

        tx = contract.functions.redeemPositions(
            Web3.to_checksum_address(USDC_ADDRESS),
            b"\x00" * 32,
            Web3.to_bytes(hexstr=cid_txt),
            [1, 2],
        ).build_transaction(tx_base)
        if "gas" not in tx:
            gas_estimate = w3.eth.estimate_gas(tx)
            tx["gas"] = int(gas_estimate * 1.20)
        signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
        txh = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=600)
        fee_msg = (
            f"maxFeePerGas={tx.get('maxFeePerGas')} maxPriorityFeePerGas={tx.get('maxPriorityFeePerGas')}"
            if "maxFeePerGas" in tx
            else f"gasPrice={tx.get('gasPrice')}"
        )
        print(f"redeemPositions condition={cid_txt} tx={txh.hex()} status={receipt.status} {fee_msg}")
        nonce += 1


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
    )
    print("Redeem flow complete.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(130)
