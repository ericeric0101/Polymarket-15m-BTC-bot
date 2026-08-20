from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


MARKOUT_HORIZONS_SEC = (1, 3, 5, 10, 30)


@dataclass
class FillObservation:
    fill_id: str
    instrument_key: str
    side: str
    fill_price: Decimal
    qty: Decimal
    filled_ts: float
    reference_mid: Decimal | None = None
    model_probability: Decimal | None = None
    edge_ps: Decimal | None = None
    liquidity_class: str | None = None
    entry_context: dict[str, Any] = field(default_factory=dict)
    completed_horizons: set[int] = field(default_factory=set)


class TradeTelemetry:
    def __init__(self) -> None:
        self.pending: dict[str, FillObservation] = {}

    def record_fill(self, **kwargs: Any) -> None:
        obs = FillObservation(**kwargs)
        self.pending[obs.fill_id] = obs

    def observe(self, instrument_key: str, mid: Decimal, now_ts: float) -> list[dict[str, Any]]:
        completed: list[dict[str, Any]] = []
        for fill_id, obs in list(self.pending.items()):
            if obs.instrument_key != instrument_key:
                continue
            elapsed = max(0.0, float(now_ts) - obs.filled_ts)
            for horizon in MARKOUT_HORIZONS_SEC:
                if horizon in obs.completed_horizons or elapsed < horizon:
                    continue
                signed_markout = (
                    mid - obs.fill_price if obs.side.lower() == "buy" else obs.fill_price - mid
                )
                obs.completed_horizons.add(horizon)
                payload = {
                    "fill_id": fill_id,
                    "instrument_key": instrument_key,
                    "side": obs.side,
                    "horizon_sec": horizon,
                    "elapsed_sec": elapsed,
                    "fill_price": float(obs.fill_price),
                    "markout_mid": float(mid),
                    "signed_markout_ps": float(signed_markout),
                    "model_probability": float(obs.model_probability) if obs.model_probability is not None else None,
                    "edge_ps": float(obs.edge_ps) if obs.edge_ps is not None else None,
                    "liquidity_class": obs.liquidity_class,
                }
                # Freeze the decision context captured at fill time.  Markout
                # is a post-fill measurement, so recomputing these values from
                # a later quote would create look-ahead bias in calibration.
                payload.update(obs.entry_context)
                completed.append(payload)
            if len(obs.completed_horizons) == len(MARKOUT_HORIZONS_SEC):
                self.pending.pop(fill_id, None)
        return completed
