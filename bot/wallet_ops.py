from __future__ import annotations

from decimal import Decimal
import os
import time
from typing import Any, Callable


_CONDITIONAL_BALANCE_FAILURE_BACKOFF_MAX_SEC = 120.0


def ensure_balance_clob_client(
    current_client: Any,
    logger_info_fn: Callable[[str], None],
    logger_warning_fn: Callable[[str], None],
) -> Any:
    if current_client is not None:
        return current_client

    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds, AssetType, BalanceAllowanceParams

    clob_base = os.getenv("POLYMARKET_CLOB_BASE_URL", "https://clob.polymarket.com").rstrip("/")
    pk = os.getenv("POLYMARKET_PK")
    if not pk:
        return None

    client = ClobClient(
        clob_base,
        int(os.getenv("POLYMARKET_CHAIN_ID", "137")),
        key=pk,
        signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
        funder=os.getenv("POLYMARKET_FUNDER") or None,
    )
    api_key = os.getenv("POLYMARKET_API_KEY")
    api_secret = os.getenv("POLYMARKET_API_SECRET")
    passphrase = os.getenv("POLYMARKET_PASSPHRASE")

    probe_params = BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL,
        signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
    )

    if api_key and api_secret and passphrase:
        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=passphrase,
        )
        client.set_api_creds(creds)
        try:
            client.get_balance_allowance(probe_params)
            return client
        except Exception as exc:
            logger_warning_fn(f"Balance cache: configured L2 creds rejected, retrying derive/create: {exc}")

    try:
        try:
            derived = client.create_api_key()
            logger_info_fn("Balance cache: derived API creds from create_api_key()")
        except Exception:
            derived = client.derive_api_key()
            logger_info_fn("Balance cache: derived API creds from derive_api_key()")
        client.set_api_creds(derived)
        client.get_balance_allowance(probe_params)
    except Exception as exc:
        logger_warning_fn(f"Balance cache: failed to derive valid API creds: {exc}")
        return None
    return client


def refresh_collateral_balance(
    current_client: Any,
    cached_balance: Decimal | None,
    logger_info_fn: Callable[[str], None],
    logger_warning_fn: Callable[[str], None],
    logger_debug_fn: Callable[[str], None],
) -> tuple[Any, Decimal | None]:
    client = ensure_balance_clob_client(
        current_client=current_client,
        logger_info_fn=logger_info_fn,
        logger_warning_fn=logger_warning_fn,
    )
    if client is None:
        return current_client, cached_balance

    try:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
        )
        result = client.get_balance_allowance(params)
        if result and isinstance(result, dict):
            balance_val = result.get("balance")
            if balance_val is not None:
                new_balance = Decimal(str(balance_val)) / Decimal("1000000")
                if cached_balance is None:
                    logger_info_fn(f"Balance cache updated: {float(new_balance):.4f} USDC")
                elif new_balance != cached_balance:
                    logger_info_fn(
                        "Balance cache updated: "
                        f"{float(cached_balance):.4f} -> {float(new_balance):.4f} USDC"
                    )
                cached_balance = new_balance
    except Exception as exc:
        logger_debug_fn(f"Balance cache refresh failed: {exc}")
    return client, cached_balance


def fetch_conditional_balance(
    token: str,
    current_client: Any,
    cached_entry: dict[str, Any] | None,
    conditional_balance_check_interval_sec: float,
    force_refresh: bool,
    logger_debug_fn: Callable[[str], None],
) -> tuple[Any, Decimal | None, dict[str, Any] | None]:
    now_ts = time.time()
    retry_after_ts = float(cached_entry.get("retry_after_ts", 0.0)) if cached_entry else 0.0
    # A forced refresh is normally used after a rejected sell.  It must not
    # bypass a known upstream outage and hammer Polymarket's balance endpoint.
    if cached_entry is not None and now_ts < retry_after_ts:
        bal = cached_entry.get("balance")
        return current_client, (Decimal(str(bal)) if bal is not None else None), cached_entry
    if (
        not force_refresh
        and cached_entry is not None
        and (now_ts - float(cached_entry.get("ts", 0.0))) < conditional_balance_check_interval_sec
    ):
        bal = cached_entry.get("balance")
        return current_client, (Decimal(str(bal)) if bal is not None else None), cached_entry

    client = current_client
    if client is None:
        return current_client, (Decimal(str(cached_entry.get("balance"))) if cached_entry and cached_entry.get("balance") is not None else None), cached_entry

    try:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=token,
            signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
        )
        result = client.get_balance_allowance(params)
        if result and isinstance(result, dict):
            raw = result.get("balance")
            if raw is not None:
                balance_shares = Decimal(str(raw)) / Decimal("1000000")
                cached_entry = {
                    "ts": now_ts,
                    "balance": str(balance_shares),
                }
                return client, balance_shares, cached_entry
    except Exception as exc:
        prior_failures = int(cached_entry.get("failure_count", 0)) if cached_entry else 0
        failure_count = prior_failures + 1
        retry_delay_sec = min(
            _CONDITIONAL_BALANCE_FAILURE_BACKOFF_MAX_SEC,
            max(float(conditional_balance_check_interval_sec), 1.0) * (2 ** (failure_count - 1)),
        )
        cached_entry = {
            **(cached_entry or {}),
            "failure_count": failure_count,
            "retry_after_ts": now_ts + retry_delay_sec,
            "last_failure": str(exc),
        }
        logger_debug_fn(
            "Conditional balance fetch failed for token="
            f"{token}: {exc}; retry suppressed for {retry_delay_sec:.0f}s"
        )

    if cached_entry and cached_entry.get("balance") is not None:
        return client, Decimal(str(cached_entry.get("balance"))), cached_entry
    return client, None, cached_entry
