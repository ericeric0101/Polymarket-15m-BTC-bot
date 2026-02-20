#!/usr/bin/env python3
"""
Check or refresh Polymarket allowance on Polygon using py-clob-client.

Examples:
  venv/bin/python scripts/check_allowance.py --check-only
  venv/bin/python scripts/check_allowance.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from dotenv import load_dotenv

USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
SPENDERS = [
    "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
]

ERC20_APPROVE_ABI = [
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

ERC1155_SET_APPROVAL_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "operator", "type": "address"},
            {"internalType": "bool", "name": "approved", "type": "bool"},
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


def _pretty(obj: Any) -> str:
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


def _all_allowances_zero(payload: Any) -> bool:
    try:
        allowances = payload.get("allowances", {})
        if not isinstance(allowances, dict) or not allowances:
            return True
        return all(str(v) == "0" for v in allowances.values())
    except Exception:
        return True


def _onchain_approve_all(
    private_key: str,
    owner_address: str,
    chain_id: int,
    rpc_url: str,
) -> None:
    try:
        from web3 import Web3
        from web3.constants import MAX_INT
        from web3.middleware import ExtraDataToPOAMiddleware
    except Exception as e:
        raise RuntimeError(
            "web3 is required for --onchain mode. Install with: venv/bin/pip install web3==7.12.1"
        ) from e

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    # Polygon PoS blocks require POA extraData middleware in web3.py
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    owner = Web3.to_checksum_address(owner_address)
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_APPROVE_ABI)
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=ERC1155_SET_APPROVAL_ABI)

    nonce = w3.eth.get_transaction_count(owner, "pending")

    for spender in SPENDERS:
        spender_cs = Web3.to_checksum_address(spender)

        tx1 = usdc.functions.approve(spender_cs, int(MAX_INT, 16)).build_transaction(
            {"chainId": chain_id, "from": owner, "nonce": nonce},
        )
        signed1 = w3.eth.account.sign_transaction(tx1, private_key=private_key)
        txh1 = w3.eth.send_raw_transaction(signed1.raw_transaction)
        rcpt1 = w3.eth.wait_for_transaction_receipt(txh1, timeout=600)
        print(f"USDC approve -> {spender_cs}: tx={txh1.hex()} status={rcpt1.status}")
        nonce += 1

        tx2 = ctf.functions.setApprovalForAll(spender_cs, True).build_transaction(
            {"chainId": chain_id, "from": owner, "nonce": nonce},
        )
        signed2 = w3.eth.account.sign_transaction(tx2, private_key=private_key)
        txh2 = w3.eth.send_raw_transaction(signed2.raw_transaction)
        rcpt2 = w3.eth.wait_for_transaction_receipt(txh2, timeout=600)
        print(f"CTF setApprovalForAll -> {spender_cs}: tx={txh2.hex()} status={rcpt2.status}")
        nonce += 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Check/update Polymarket allowance")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to environment file (default: .env)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply/update allowance on-chain",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only check current allowance (default behavior)",
    )
    parser.add_argument(
        "--asset-type",
        choices=["COLLATERAL", "CONDITIONAL"],
        default="COLLATERAL",
        help="Allowance type to check/update (default: COLLATERAL)",
    )
    parser.add_argument(
        "--token-id",
        default=None,
        help="Required for CONDITIONAL asset type",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="CLOB host override (default from POLYMARKET_CLOB_BASE_URL)",
    )
    parser.add_argument(
        "--rpc-url",
        default=None,
        help="Polygon RPC URL for --onchain mode",
    )
    parser.add_argument(
        "--chain-id",
        type=int,
        default=None,
        help="Chain ID override (default from POLYMARKET_CHAIN_ID or 137)",
    )
    parser.add_argument(
        "--onchain",
        action="store_true",
        help="Use on-chain approve flow (requires web3 and MATIC gas)",
    )
    args = parser.parse_args()

    if args.apply and args.check_only:
        print("Use either --apply or --check-only, not both.")
        return 2

    if args.env_file:
        load_dotenv(args.env_file, override=False)
    else:
        load_dotenv(override=False)

    private_key = os.getenv("POLYMARKET_PK")
    if not private_key:
        print("Missing POLYMARKET_PK in environment.")
        return 2

    funder = (
        os.getenv("POLYMARKET_FUNDER")
        or os.getenv("POLYMARKET_WALLET_ADDRESS")
        or os.getenv("WALLET_ADDRESS")
    )

    host = args.host or os.getenv("POLYMARKET_CLOB_BASE_URL", "https://clob.polymarket.com")
    rpc_url = args.rpc_url or os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    chain_id = args.chain_id or int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
    signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams
    except Exception as e:
        print(f"py-clob-client import failed: {e}")
        print("Run with the project virtualenv, e.g. venv/bin/python ...")
        return 2

    kwargs: dict[str, Any] = {
        "host": host,
        "key": private_key,
        "chain_id": chain_id,
        "signature_type": signature_type,
    }
    if funder:
        kwargs["funder"] = funder
    client = ClobClient(**kwargs)

    # Some endpoints require L2 API credentials.
    api_key = os.getenv("POLYMARKET_API_KEY")
    api_secret = os.getenv("POLYMARKET_API_SECRET")
    api_passphrase = os.getenv("POLYMARKET_PASSPHRASE")
    try:
        if api_key and api_secret and api_passphrase:
            client.set_api_creds(
                ApiCreds(
                    api_key=api_key,
                    api_secret=api_secret,
                    api_passphrase=api_passphrase,
                ),
            )
        else:
            derived = client.create_or_derive_api_creds()
            client.set_api_creds(derived)
    except Exception as e:
        print(f"Failed to set API credentials for allowance endpoint: {e}")
        return 1

    params = BalanceAllowanceParams(
        asset_type=(AssetType.COLLATERAL if args.asset_type == "COLLATERAL" else AssetType.CONDITIONAL),
        token_id=args.token_id,
        signature_type=signature_type,
    )

    print("=== Polymarket Allowance Check ===")
    print(f"Host: {host}")
    print(f"RPC URL: {rpc_url}")
    print(f"Chain ID: {chain_id}")
    print(f"Asset Type: {args.asset_type}")
    print(f"Address: {client.get_address()}")
    if funder:
        print(f"Funder: {funder}")

    try:
        before = client.get_balance_allowance(params)
        print("\n[Before]")
        print(_pretty(before))
    except Exception as e:
        print(f"Failed to fetch current allowance: {e}")
        return 1

    if not args.apply:
        print("\nCheck-only mode complete.")
        return 0

    if args.onchain:
        try:
            _onchain_approve_all(
                private_key=private_key,
                owner_address=client.get_address(),
                chain_id=chain_id,
                rpc_url=rpc_url,
            )
        except Exception as e:
            print(f"On-chain approve failed: {e}")
            return 1

    try:
        tx = client.update_balance_allowance(params)
        print("\n[Apply]")
        print(_pretty(tx))
    except Exception as e:
        print(f"Failed to update allowance: {e}")
        if not args.onchain:
            return 1

    try:
        after = client.get_balance_allowance(params)
        print("\n[After]")
        print(_pretty(after))
    except Exception as e:
        print(f"Updated but failed to re-fetch allowance: {e}")
        return 1

    if _all_allowances_zero(after):
        print(
            "\nWarning: allowances are still zero. "
            "Run with --onchain (and install web3) to force Polygon approvals."
        )
    else:
        print("\nAllowance update flow complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
