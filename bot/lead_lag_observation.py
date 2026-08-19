"""Passive external-price lead/lag observations for forecast research."""
from __future__ import annotations

from typing import Any, Optional


LEAD_LAG_HORIZONS_SEC = (5, 15, 30, 60)
LEAD_LAG_SNAPSHOT_INTERVAL_SEC = 15.0
LEAD_LAG_STATE_TTL_SEC = 120.0


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
    """Persist fixed-horizon market-mid outcomes without affecting execution."""

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
        }

    def _lead_lag_observation_on_quote(self, now_ts: float) -> None:
        if getattr(self, "trade_db", None) is None:
            return
        slug = str(getattr(self, "current_market_slug", "") or "")
        if not slug:
            return
        up_mid = self._lead_lag_up_mid()
        if up_mid is None:
            return

        pending = getattr(self, "_lead_lag_pending", None)
        if not isinstance(pending, dict):
            pending = {}
            self._lead_lag_pending = pending
        for observation_id, state in list(pending.items()):
            created_ts = float(state.get("observed_ts", now_ts))
            elapsed_sec = max(0.0, now_ts - created_ts)
            if elapsed_sec > LEAD_LAG_STATE_TTL_SEC:
                pending.pop(observation_id, None)
                continue
            if state.get("slug") != slug:
                continue
            completed = set(state.get("completed_horizons", ()))
            for horizon_sec in LEAD_LAG_HORIZONS_SEC:
                if horizon_sec in completed or elapsed_sec < horizon_sec:
                    continue
                future = self._lead_lag_snapshot_payload(slug=slug, now_ts=now_ts, up_mid=up_mid)
                entry_mid = float(state["up_mid"])
                binance_start = _number(state.get("binance_price"))
                twap_start = _number(state.get("twap_price"))
                binance_end = _number(future.get("binance_price"))
                twap_end = _number(future.get("twap_price"))
                payload = {
                    **state,
                    "horizon_target_sec": horizon_sec,
                    "elapsed_sec": elapsed_sec,
                    "future_observed_ts": now_ts,
                    "future_up_mid": up_mid,
                    "up_mid_change_ps": up_mid - entry_mid,
                    "up_mid_return_bps": ((up_mid / entry_mid) - 1.0) * 10_000.0,
                    "future_binance_price": binance_end,
                    "future_binance_age_sec": future["binance_age_sec"],
                    "future_twap_price": twap_end,
                    "future_twap_age_sec": future["twap_age_sec"],
                    "future_reference_price": future["reference_price"],
                    "future_reference_source": future["reference_source"],
                    "binance_return_bps": (
                        ((binance_end / binance_start) - 1.0) * 10_000.0
                        if binance_start and binance_end
                        else None
                    ),
                    "twap_return_bps": (
                        ((twap_end / twap_start) - 1.0) * 10_000.0
                        if twap_start and twap_end
                        else None
                    ),
                }
                self._db_strategy_event("EXTERNAL_LEAD_LAG_OUTCOME", payload)
                completed.add(horizon_sec)
            state["completed_horizons"] = sorted(completed)
            if len(completed) == len(LEAD_LAG_HORIZONS_SEC):
                pending.pop(observation_id, None)

        last_by_slug = getattr(self, "_lead_lag_last_snapshot_ts_by_slug", None)
        if not isinstance(last_by_slug, dict):
            last_by_slug = {}
            self._lead_lag_last_snapshot_ts_by_slug = last_by_slug
        if now_ts - float(last_by_slug.get(slug, 0.0)) < LEAD_LAG_SNAPSHOT_INTERVAL_SEC:
            return

        state = self._lead_lag_snapshot_payload(slug=slug, now_ts=now_ts, up_mid=up_mid)
        observation_id = f"lead-lag:{slug}:{int(now_ts * 1000)}"
        state["observation_id"] = observation_id
        state["completed_horizons"] = []
        pending[observation_id] = state
        last_by_slug[slug] = now_ts
        self._db_strategy_event(
            "EXTERNAL_LEAD_LAG_SNAPSHOT",
            {**state, "completed_horizons": []},
        )
