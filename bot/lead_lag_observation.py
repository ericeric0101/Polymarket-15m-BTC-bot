"""Passive external-price lead/lag observations for forecast research."""
from __future__ import annotations

from typing import Any, Optional


LEAD_LAG_SNAPSHOT_INTERVAL_SEC = 5.0


def _number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _age(now_ts: float, source_ts: Any) -> Optional[float]:
    try:
        timestamp = float(source_ts or 0.0)
    except (TypeError, ValueError):
        return None
    return max(0.0, now_ts - timestamp) if timestamp > 0.0 else None


class LeadLagObservationMixin:
    """Enqueue raw cross-market snapshots without affecting execution."""

    def _lead_lag_up_mid(self) -> Optional[float]:
        instrument_id = getattr(self, "current_up_instrument_id", None)
        if instrument_id is None:
            return None
        quote = getattr(self, "latest_quote_by_inst", {}).get(str(instrument_id))
        if not isinstance(quote, tuple) or len(quote) < 2:
            return None
        bid, ask = _number(quote[0]), _number(quote[1])
        if bid is None or ask is None or bid <= 0.0 or ask <= 0.0:
            return None
        return (bid + ask) / 2.0

    def _lead_lag_snapshot_payload(self, *, slug: str, now_ts: float, up_mid: float) -> dict[str, Any]:
        twap_price = _number(getattr(self, "_polymarket_chainlink_twap_price", None))
        binance_price = _number(getattr(self, "_binance_ws_price", None))
        reference_price = _number(getattr(self, "latest_external_spot", None))
        outcome_observer = getattr(self, "hyperliquid_outcome_observer", None)
        outcome = outcome_observer.snapshot() if outcome_observer is not None else {"available": False}
        return {
            "slug": slug,
            "observed_ts": now_ts,
            "time_left_sec": max(
                0.0,
                float(getattr(self, "current_market_end_timestamp", now_ts) or now_ts) - now_ts,
            ),
            "up_mid": up_mid,
            "binance_price": binance_price,
            "binance_age_sec": _age(now_ts, getattr(self, "_binance_ws_price_ts", 0.0)),
            "twap_price": twap_price,
            "twap_age_sec": _age(now_ts, getattr(self, "_polymarket_chainlink_twap_price_ts", 0.0)),
            "reference_price": reference_price,
            "reference_source": str(getattr(self, "latest_external_spot_source", "") or ""),
            "reference_age_sec": _age(now_ts, getattr(self, "latest_external_spot_source_ts", 0.0)),
            "binance_minus_twap_usd": (
                binance_price - twap_price
                if binance_price is not None and twap_price is not None
                else None
            ),
            "hyperliquid_outcome_available": bool(outcome.get("available", False)),
            "hyperliquid_outcome_analysis_available": bool(outcome.get("analysis_available", False)),
            "hyperliquid_outcome_stream_connected": bool(outcome.get("stream_connected", False)),
            "hyperliquid_outcome_mids_age_sec": outcome.get("mids_age_sec"),
            "hyperliquid_outcome_side0_book_age_sec": outcome.get("side0_book_age_sec"),
            "hyperliquid_outcome_side1_book_age_sec": outcome.get("side1_book_age_sec"),
            "hyperliquid_outcome_source": outcome.get("source"),
            "hyperliquid_outcome_market_id": outcome.get("market_id"),
            "hyperliquid_outcome_side0_all_mid": outcome.get("side0_all_mid"),
            "hyperliquid_outcome_side1_all_mid": outcome.get("side1_all_mid"),
            "hyperliquid_outcome_side0_bbo_mid": outcome.get("side0_bbo_mid"),
            "hyperliquid_outcome_side1_bbo_mid": outcome.get("side1_bbo_mid"),
            "hyperliquid_outcome_side0_bid": outcome.get("side0_bid"),
            "hyperliquid_outcome_side0_ask": outcome.get("side0_ask"),
            "hyperliquid_outcome_side0_bid_depth": outcome.get("side0_bid_depth"),
            "hyperliquid_outcome_side0_ask_depth": outcome.get("side0_ask_depth"),
            "hyperliquid_outcome_side1_bid": outcome.get("side1_bid"),
            "hyperliquid_outcome_side1_ask": outcome.get("side1_ask"),
            "hyperliquid_outcome_side1_bid_depth": outcome.get("side1_bid_depth"),
            "hyperliquid_outcome_side1_ask_depth": outcome.get("side1_ask_depth"),
            "hyperliquid_outcome_btc_mark": outcome.get("btc_mark"),
        }

    def _lead_lag_observation_on_quote(self, now_ts: float) -> None:
        lead_lag_db = getattr(self, "lead_lag_db", None)
        if lead_lag_db is None:
            return
        slug = str(getattr(self, "current_market_slug", "") or "")
        if not slug:
            return
        up_mid = self._lead_lag_up_mid()
        if up_mid is None:
            return

        last_by_slug = getattr(self, "_lead_lag_last_snapshot_ts_by_slug", None)
        if not isinstance(last_by_slug, dict):
            last_by_slug = {}
            self._lead_lag_last_snapshot_ts_by_slug = last_by_slug
        if now_ts - float(last_by_slug.get(slug, 0.0)) < LEAD_LAG_SNAPSHOT_INTERVAL_SEC:
            return

        state = self._lead_lag_snapshot_payload(slug=slug, now_ts=now_ts, up_mid=up_mid)
        last_by_slug[slug] = now_ts
        lead_lag_db.enqueue_snapshot(
            run_id=str(getattr(self, "run_id", "")),
            polymarket_slug=slug,
            hyperliquid_market_id=(int(state["hyperliquid_outcome_market_id"]) if state.get("hyperliquid_outcome_market_id") is not None else None),
            observed_ts=now_ts,
            payload=state,
        )
