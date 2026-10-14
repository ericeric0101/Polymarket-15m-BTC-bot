"""Rolling Binance-minus-Chainlink basis for the degraded reference-spot fallback.

Binance BTCUSDT trades at a drifting premium to the Chainlink BTC/USD stream that
settles these markets (1.6-10 bps across 2026-09-30..10-12; see
reports/fast_spot_provenance_backfill_20261015). When the Chainlink reference is
stale and the bot falls back to Binance, comparing the raw Binance level with a
Chainlink-denominated strike shifts every spot-vs-strike reading toward UP.

The estimator only uses pairs observed *before* the query (no look-ahead) and
returns ``None`` until it has enough recent pairs; callers then keep the legacy
raw value and record that no correction was applied.
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal
from statistics import median
from typing import Any, Optional

BINANCE_SOURCE = "binance_ws"
BASIS_ADJUSTED_SOURCE = "binance_ws_basis_adjusted"


class RollingBasisEstimator:
    def __init__(
        self,
        *,
        window_sec: float = 3600.0,
        min_pairs: int = 30,
        max_pair_skew_sec: float = 1.0,
        max_age_sec: float = 2.0,
        max_pairs: int = 4000,
    ) -> None:
        self.window_sec = float(window_sec)
        self.min_pairs = int(min_pairs)
        self.max_pair_skew_sec = float(max_pair_skew_sec)
        self.max_age_sec = float(max_age_sec)
        self._pairs: deque[tuple[float, float]] = deque(maxlen=int(max_pairs))
        self._last_chainlink_ts: Optional[float] = None

    def observe(
        self,
        *,
        binance_price: Any,
        binance_ts: float,
        chainlink_price: Any,
        chainlink_ts: float,
        now_ts: float,
    ) -> bool:
        """Record one pair per Chainlink update when both feeds are fresh and close in time."""
        try:
            b = float(binance_price)
            c = float(chainlink_price)
            bt = float(binance_ts or 0.0)
            ct = float(chainlink_ts or 0.0)
        except (TypeError, ValueError):
            return False
        if b <= 0 or c <= 0 or bt <= 0 or ct <= 0:
            return False
        if ct == self._last_chainlink_ts:
            return False
        if now_ts - bt > self.max_age_sec or now_ts - ct > self.max_age_sec:
            return False
        if abs(bt - ct) > self.max_pair_skew_sec:
            return False
        self._last_chainlink_ts = ct
        self._pairs.append((max(bt, ct), (b - c) / c * 1e4))
        return True

    def estimate(self, now_ts: float) -> tuple[Optional[float], int]:
        """Median basis in bps over the trailing window, and the pair count used."""
        lo = float(now_ts) - self.window_sec
        values = [bps for ts, bps in self._pairs if lo <= ts <= now_ts]
        if len(values) < self.min_pairs:
            return None, len(values)
        return float(median(values)), len(values)


def adjust_binance_price(price: Decimal, basis_bps: float) -> Decimal:
    """Map a Binance price onto the Chainlink level: price * (1 - basis/1e4)."""
    return price * (Decimal("1") - Decimal(str(basis_bps)) / Decimal("10000"))


def observe_reference_basis(host: Any, now_ts: float) -> None:
    """Feed the host's estimator from its latest Binance and raw Chainlink spots."""
    estimator = getattr(host, "_reference_basis_estimator", None)
    if estimator is None:
        return
    try:
        estimator.observe(
            binance_price=getattr(host, "_binance_ws_price", None),
            binance_ts=float(getattr(host, "_binance_ws_price_ts", 0.0) or 0.0),
            chainlink_price=getattr(host, "_polymarket_chainlink_price", None),
            chainlink_ts=float(getattr(host, "_polymarket_chainlink_price_ts", 0.0) or 0.0),
            now_ts=float(now_ts),
        )
    except Exception:
        return


def apply_reference_basis(
    host: Any, candidate: tuple[Optional[Decimal], str, Optional[float]], now_ts: float,
) -> tuple[Optional[Decimal], str, Optional[float]]:
    """Basis-adjust a Binance reference candidate; any other source passes through.

    Without an estimate the raw Binance value is kept (legacy behaviour) and the
    host's ``_reference_spot_basis_state`` records that no correction was applied.
    """
    price, source, age = candidate
    estimator = getattr(host, "_reference_basis_estimator", None)
    if source != BINANCE_SOURCE or price is None or estimator is None:
        if estimator is not None:
            host._reference_spot_basis_state = {"reference_basis_applied": False, "reference_basis_bps": None,
                                                "reference_basis_pairs": None}
        return candidate
    basis, pairs = estimator.estimate(float(now_ts))
    if basis is None:
        host._reference_spot_basis_state = {"reference_basis_applied": False, "reference_basis_bps": None,
                                            "reference_basis_pairs": pairs, "reference_basis_unavailable": True}
        return candidate
    host._reference_spot_basis_state = {"reference_basis_applied": True, "reference_basis_bps": basis,
                                        "reference_basis_pairs": pairs, "reference_spot_raw_price": float(price)}
    return (adjust_binance_price(price, basis), BASIS_ADJUSTED_SOURCE, age)


_DEGRADED_REFERENCE_PREFIXES = ("binance", "coinbase")


def is_degraded_reference_source(source: Any) -> bool:
    """True for non-Chainlink reference spots (raw or basis-adjusted exchange prices).

    An unknown/empty source is not classified (legacy hosts without provenance).
    """
    return str(source or "").startswith(_DEGRADED_REFERENCE_PREFIXES)


def reference_spot_provenance(host: Any, now_ts: Optional[float] = None) -> dict[str, Any]:
    """Reference-spot source/age/basis fields for exit and stop event payloads."""
    detail = getattr(host, "_capture_market_open_spot_detail", None)
    if not callable(detail):
        return {}
    try:
        price, source, age = detail(now_ts=now_ts) if now_ts is not None else detail()
    except Exception:
        return {}
    fields: dict[str, Any] = {
        "reference_spot_source": source,
        "reference_spot_price": float(price) if price is not None else None,
        "reference_spot_age_sec": float(age) if age is not None else None,
        "reference_spot_degraded": is_degraded_reference_source(source),
    }
    state = getattr(host, "_reference_spot_basis_state", None)
    if isinstance(state, dict):
        fields.update(state)
    return fields
