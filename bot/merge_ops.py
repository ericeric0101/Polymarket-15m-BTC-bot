from __future__ import annotations

import os
from collections import defaultdict
from decimal import Decimal
from typing import Any, Callable


def execute_merge_on_chain(
    *,
    pk: str,
    condition_id: str,
    amount: int,
    rpc_url: str,
    chain_id: int,
    logger_info_fn: Callable[[str], None],
    logger_warning_fn: Callable[[str], None],
) -> bool:
    try:
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware

        ctf_address = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
        usdc_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        ctf_merge_abi = [{
            "inputs": [
                {"internalType": "address", "name": "collateralToken", "type": "address"},
                {"internalType": "bytes32", "name": "parentCollectionId", "type": "bytes32"},
                {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
                {"internalType": "uint256[]", "name": "partition", "type": "uint256[]"},
                {"internalType": "uint256", "name": "amount", "type": "uint256"},
            ],
            "name": "mergePositions",
            "outputs": [],
            "stateMutability": "nonpayable",
            "type": "function",
        }]

        w3 = Web3(Web3.HTTPProvider(rpc_url))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        from eth_account import Account
        acct = Account.from_key(pk)
        owner = w3.to_checksum_address(acct.address)
        contract = w3.eth.contract(address=w3.to_checksum_address(ctf_address), abi=ctf_merge_abi)
        tx = contract.functions.mergePositions(
            w3.to_checksum_address(usdc_address),
            b"\x00" * 32,
            Web3.to_bytes(hexstr=condition_id),
            [1, 2],
            amount,
        ).build_transaction({
            "chainId": chain_id,
            "from": owner,
            "nonce": w3.eth.get_transaction_count(owner, "pending"),
        })
        signed = w3.eth.account.sign_transaction(tx, private_key=pk)
        txh = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=120)
        usdc_recovered = amount / 1_000_000
        logger_info_fn(
            f"✓ Merge SUCCESS: condition={condition_id[:16]}... "
            f"recovered={usdc_recovered:.4f} USDC tx={txh.hex()} status={receipt.status}"
        )
        return receipt.status == 1
    except Exception as e:
        logger_warning_fn(f"Merge on-chain failed: {e}")
        return False


def try_merge_yes_no_positions(
    *,
    strategy: Any,
    logger_info_fn: Callable[[str], None],
    logger_debug_fn: Callable[[str], None],
    logger_warning_fn: Callable[[str], None],
    adjust_inventory_after_merge_fn: Callable[..., tuple[Any, Any]],
) -> None:
    try:
        pk = os.getenv("POLYMARKET_PK", "").strip()
        if not pk or int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")) != 0:
            return

        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
        chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
        instruments = strategy.cache.instruments() if hasattr(strategy, "cache") else []
        if not instruments or not getattr(strategy, "_balance_clob_client", None):
            return

        condition_pairs: dict[str, list] = defaultdict(list)
        for inst in instruments:
            if hasattr(inst, "info") and inst.info:
                condition_id = inst.info.get("condition_id", "")
                if condition_id:
                    token_id = inst.info.get("token_id", "")
                    if token_id:
                        condition_pairs[condition_id].append({"token_id": token_id, "instrument": inst})

        client = strategy._balance_clob_client
        for condition_id, tokens in condition_pairs.items():
            if len(tokens) < 2:
                continue
            balances = []
            for token in tokens:
                try:
                    params = BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=token["token_id"],
                        signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
                    )
                    result = client.get_balance_allowance(params)
                    balance_raw = int(result.get("balance", "0")) if result else 0
                    balances.append(balance_raw)
                except Exception:
                    balances.append(0)
            min_balance = min(balances)
            if min_balance < 100000:
                continue
            merge_amount_usdc = min_balance / 1_000_000
            logger_info_fn(
                f"Merge opportunity detected! condition={condition_id[:16]}... "
                f"overlap={merge_amount_usdc:.4f} USDC — executing merge"
            )
            success = execute_merge_on_chain(
                pk=pk,
                condition_id=condition_id,
                amount=min_balance,
                rpc_url=rpc_url,
                chain_id=chain_id,
                logger_info_fn=logger_info_fn,
                logger_warning_fn=logger_warning_fn,
            )
            if success:
                deduct_qty = Decimal(str(merge_amount_usdc))
                old_delta, strategy.inventory_delta_shares = adjust_inventory_after_merge_fn(
                    tokens=tokens,
                    deduct_qty=deduct_qty,
                    live_inventory_cost=strategy.live_inventory_cost,
                    inventory_delta_shares=strategy.inventory_delta_shares,
                    instrument_key_fn=strategy._instrument_key,
                )
                logger_info_fn(
                    f"Deducted {float(deduct_qty):.6f} from live_inventory_cost "
                    f"and inventory_delta after merge. "
                    f"delta {float(old_delta):.6f} -> {float(strategy.inventory_delta_shares):.6f}"
                )
    except Exception as e:
        logger_debug_fn(f"Merge check failed: {e}")
