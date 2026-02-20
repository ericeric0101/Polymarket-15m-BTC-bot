"""
Daily maker economics reporter with JSON + CSV exports.
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class RebateDailyMetrics:
    date_utc: str
    quotes_submitted: int = 0
    quotes_cancelled_ttl: int = 0
    quotes_cancelled_requote: int = 0
    quotes_cancelled_risk: int = 0
    fills: int = 0
    denied: int = 0
    fill_notional_usdc: float = 0.0
    # Quote-time expected economics
    quote_estimated_fee_equivalent_usdc: float = 0.0
    quote_estimated_rebate_usdc: float = 0.0
    quote_expected_spread_capture_usdc: float = 0.0
    quote_expected_net_usdc: float = 0.0
    # Fill-attributed expected economics
    fill_estimated_fee_equivalent_usdc: float = 0.0
    fill_estimated_rebate_usdc: float = 0.0
    fill_expected_spread_capture_usdc: float = 0.0
    fill_expected_net_usdc: float = 0.0
    # Realized fields (placeholder until full position accounting is added)
    realized_spread_pnl_usdc: float = 0.0
    taker_fee_paid_usdc: float = 0.0
    net_after_fees_usdc: float = 0.0
    api_fee_rate_success_rate: float = 1.0
    api_fee_rate_last_latency_ms: float = 0.0


class RebateReporter:
    def __init__(self, output_dir: str = "./logs/rebate") -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._daily: Dict[str, RebateDailyMetrics] = {}
        self._last_api_health: Dict[str, float] = {}

    def _key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _get_metrics(self) -> RebateDailyMetrics:
        key = self._key()
        if key not in self._daily:
            self._daily[key] = RebateDailyMetrics(date_utc=key)
        return self._daily[key]

    def get_current_metrics(self) -> Dict[str, Any]:
        return asdict(self._get_metrics())

    def record_quote(self, fee_equivalent: float, rebate: float, spread_capture: float, expected_net: float) -> None:
        m = self._get_metrics()
        m.quotes_submitted += 1
        m.quote_estimated_fee_equivalent_usdc += fee_equivalent
        m.quote_estimated_rebate_usdc += rebate
        m.quote_expected_spread_capture_usdc += spread_capture
        m.quote_expected_net_usdc += expected_net

    def record_fill(self, econ: Optional[Any], fill_qty: float, fill_price: float) -> None:
        m = self._get_metrics()
        m.fills += 1
        m.fill_notional_usdc += max(0.0, fill_qty * fill_price)
        if econ is not None:
            m.fill_estimated_fee_equivalent_usdc += float(getattr(econ, "fee_equivalent_usdc", 0.0))
            m.fill_estimated_rebate_usdc += float(getattr(econ, "expected_rebate_usdc", 0.0))
            m.fill_expected_spread_capture_usdc += float(getattr(econ, "expected_spread_capture_usdc", 0.0))
            m.fill_expected_net_usdc += float(getattr(econ, "expected_net_usdc", 0.0))

    def record_denied(self) -> None:
        m = self._get_metrics()
        m.denied += 1

    def record_cancel(self, reason: str) -> None:
        m = self._get_metrics()
        if reason == "ttl":
            m.quotes_cancelled_ttl += 1
        elif reason == "requote":
            m.quotes_cancelled_requote += 1
        else:
            m.quotes_cancelled_risk += 1

    def record_api_health(self, fee_rate_health: Dict[str, float]) -> None:
        self._last_api_health = fee_rate_health or {}
        m = self._get_metrics()
        m.api_fee_rate_success_rate = float(self._last_api_health.get("success_rate", 1.0))
        m.api_fee_rate_last_latency_ms = float(self._last_api_health.get("last_latency_ms", 0.0))

    def _compute_derived(self, m: RebateDailyMetrics) -> None:
        # Conservative net: realized spread pnl (if any) + fill-attributed rebate - taker fees.
        m.net_after_fees_usdc = m.realized_spread_pnl_usdc + m.fill_estimated_rebate_usdc - m.taker_fee_paid_usdc

    def flush_daily_report(self) -> None:
        for key, metrics in self._daily.items():
            self._compute_derived(metrics)
            payload = asdict(metrics)
            json_path = self.output_dir / f"rebate_report_{key}.json"
            csv_path = self.output_dir / f"rebate_report_{key}.csv"

            json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            with csv_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(payload.keys()))
                writer.writeheader()
                writer.writerow(payload)
