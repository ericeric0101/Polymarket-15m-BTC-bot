"""
Fee rate fetcher for Polymarket CLOB fee-enabled markets.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class FeeRateRecord:
    token_id: str
    fee_rate_decimal: Decimal
    fee_rate_bps: int
    fetched_at: float


class FeeRateClient:
    def __init__(
        self,
        base_url: str = "https://clob.polymarket.com",
        timeout_sec: float = 4.0,
        ttl_sec: int = 300,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self.ttl_sec = ttl_sec
        self.debug_enabled = os.getenv("FEE_RATE_DEBUG", "1").strip().lower() not in ("0", "false", "no")
        self.debug_interval_sec = int(os.getenv("FEE_RATE_DEBUG_INTERVAL_SEC", "30"))
        self._cache: Dict[str, FeeRateRecord] = {}
        self._last_debug_ts = 0.0
        self._stats = {
            "requests_total": 0,
            "requests_success": 0,
            "requests_failed": 0,
            "cache_hits": 0,
            "last_latency_ms": 0.0,
            "last_status_code": 0,
            "last_error_reason": "",
            "last_response_excerpt": "",
        }

    def _log_debug_limited(self, msg: str) -> None:
        if not self.debug_enabled:
            return
        now = time.time()
        if now - self._last_debug_ts < self.debug_interval_sec:
            return
        self._last_debug_ts = now
        logger.warning(msg)

    @staticmethod
    def _parse_decimal(value: object) -> Optional[Decimal]:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None

    @classmethod
    def _extract_fee_rate_decimal(cls, data: dict) -> Optional[Decimal]:
        # Nested payloads used by some API gateways.
        for nested_key in ("data", "result", "fee", "payload"):
            nested = data.get(nested_key)
            if isinstance(nested, dict):
                nested_value = cls._extract_fee_rate_decimal(nested)
                if nested_value is not None:
                    return nested_value

        # Decimal-form fields (preferred)
        for key in ("fee_rate", "taker_fee_rate", "feeRate", "takerFeeRate", "rate"):
            value = cls._parse_decimal(data.get(key))
            if value is not None and value >= 0:
                return value

        # Basis-point style fields
        for key in (
            "fee_rate_bps",
            "feeRateBps",
            "bps",
            "taker_fee_bps",
            "takerFeeBps",
            "base_fee",
            "baseFee",
            "maker_base_fee",
            "makerBaseFee",
            "taker_base_fee",
            "takerBaseFee",
        ):
            value = cls._parse_decimal(data.get(key))
            if value is not None and value >= 0:
                return value / Decimal("10000")

        return None

    async def get_fee_rate_decimal(self, token_id: str) -> Optional[Decimal]:
        now = time.time()
        cached = self._cache.get(token_id)
        if cached and (now - cached.fetched_at) < self.ttl_sec:
            self._stats["cache_hits"] += 1
            return cached.fee_rate_decimal

        try:
            started = time.time()
            self._stats["requests_total"] += 1
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                resp = await client.get(f"{self.base_url}/fee-rate", params={"token_id": token_id})
                self._stats["last_status_code"] = int(resp.status_code)
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    self._stats["last_error_reason"] = "non_dict_json"
                    self._stats["last_response_excerpt"] = str(data)[:220]
                    self._log_debug_limited(
                        f"/fee-rate non-dict payload token={token_id} status={resp.status_code} "
                        f"payload={self._stats['last_response_excerpt']}"
                    )
                    return cached.fee_rate_decimal if cached else None
                fee_rate_decimal = self._extract_fee_rate_decimal(data)
                if fee_rate_decimal is None:
                    self._stats["last_error_reason"] = "missing_fee_fields"
                    self._stats["last_response_excerpt"] = str(data)[:220]
                    self._log_debug_limited(
                        f"/fee-rate missing fee fields token={token_id} status={resp.status_code} "
                        f"payload={self._stats['last_response_excerpt']}"
                    )
                    return cached.fee_rate_decimal if cached else None
                bps = int((fee_rate_decimal * Decimal("10000")).quantize(Decimal("1")))
                self._stats["requests_success"] += 1
                self._stats["last_latency_ms"] = (time.time() - started) * 1000.0
                self._stats["last_error_reason"] = ""
                self._stats["last_response_excerpt"] = ""
                self._cache[token_id] = FeeRateRecord(
                    token_id=token_id,
                    fee_rate_decimal=fee_rate_decimal,
                    fee_rate_bps=bps,
                    fetched_at=now,
                )
                return fee_rate_decimal
        except httpx.HTTPStatusError as e:
            self._stats["requests_failed"] += 1
            status = int(e.response.status_code) if e.response is not None else 0
            body = ""
            if e.response is not None:
                try:
                    body = e.response.text[:220]
                except Exception:
                    body = ""
            self._stats["last_status_code"] = status
            self._stats["last_error_reason"] = "http_status_error"
            self._stats["last_response_excerpt"] = body
            self._log_debug_limited(
                f"/fee-rate http error token={token_id} status={status} body={body}"
            )
            return cached.fee_rate_decimal if cached else None
        except Exception as e:
            self._stats["requests_failed"] += 1
            self._stats["last_error_reason"] = f"exception:{type(e).__name__}"
            self._stats["last_response_excerpt"] = str(e)[:220]
            self._log_debug_limited(
                f"/fee-rate exception token={token_id} err={type(e).__name__}: {e}"
            )
            return cached.fee_rate_decimal if cached else None

    async def get_fee_rate_bps(self, token_id: str) -> Optional[int]:
        fee_rate_decimal = await self.get_fee_rate_decimal(token_id)
        if fee_rate_decimal is None:
            return None
        return int((fee_rate_decimal * Decimal("10000")).quantize(Decimal("1")))

    def get_health_snapshot(self) -> Dict[str, Any]:
        total = float(self._stats["requests_total"])
        success = float(self._stats["requests_success"])
        fail = float(self._stats["requests_failed"])
        return {
            "requests_total": total,
            "requests_success": success,
            "requests_failed": fail,
            "success_rate": (success / total) if total > 0 else 1.0,
            "cache_hits": float(self._stats["cache_hits"]),
            "last_latency_ms": float(self._stats["last_latency_ms"]),
            "last_status_code": int(self._stats["last_status_code"]),
            "last_error_reason": str(self._stats["last_error_reason"]),
            "last_response_excerpt": str(self._stats["last_response_excerpt"]),
        }
