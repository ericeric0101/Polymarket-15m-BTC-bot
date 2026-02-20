"""
Fee rate fetcher for Polymarket CLOB fee-enabled markets.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Optional

import httpx


@dataclass
class FeeRateRecord:
    token_id: str
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
        self._cache: Dict[str, FeeRateRecord] = {}
        self._stats = {
            "requests_total": 0,
            "requests_success": 0,
            "requests_failed": 0,
            "cache_hits": 0,
            "last_latency_ms": 0.0,
        }

    async def get_fee_rate_bps(self, token_id: str) -> Optional[int]:
        now = time.time()
        cached = self._cache.get(token_id)
        if cached and (now - cached.fetched_at) < self.ttl_sec:
            self._stats["cache_hits"] += 1
            return cached.fee_rate_bps

        try:
            started = time.time()
            self._stats["requests_total"] += 1
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                resp = await client.get(f"{self.base_url}/fee-rate", params={"token_id": token_id})
                resp.raise_for_status()
                data = resp.json()
                bps = int(data.get("fee_rate_bps", 0))
                self._stats["requests_success"] += 1
                self._stats["last_latency_ms"] = (time.time() - started) * 1000.0
                self._cache[token_id] = FeeRateRecord(
                    token_id=token_id,
                    fee_rate_bps=bps,
                    fetched_at=now,
                )
                return bps
        except Exception:
            self._stats["requests_failed"] += 1
            return cached.fee_rate_bps if cached else None

    def get_health_snapshot(self) -> Dict[str, float]:
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
        }
