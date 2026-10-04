"""Typed read-only projection of already-persisted prediction snapshots."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class MarketEvidence:
    market_slug: str
    timestamp: float
    run_id: str
    time_left_sec: float | None
    market_start_ts: float | None
    market_start_taipei: str | None
    session_regime: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze(self.payload))

    @classmethod
    def from_snapshot(cls, payload: dict[str, Any], *, run_id: str,
                      market_start_taipei: str | None, session_regime: str) -> "MarketEvidence":
        value = dict(payload)
        return cls(
            market_slug=str(value.get("market_slug") or ""),
            timestamp=float(value.get("snapshot_ts") or 0.0),
            run_id=str(run_id),
            time_left_sec=_number(value.get("time_left_sec")),
            market_start_ts=_number(value.get("market_start_ts")),
            market_start_taipei=market_start_taipei,
            session_regime=session_regime,
            payload=value,
        )

    def get(self, name: str) -> Any:
        """Expose canonical captured values without recalculation."""
        return self.payload.get(name)

    def display_fields(self) -> dict[str, Any]:
        """Safe read-only dashboard/diagnostic serialization."""
        names = ("best_bid_up", "best_ask_up", "up_mid", "spread_up", "official_twap",
                 "settlement_state_side", "required_move_sigma", "required_move_bps",
                 "p_up_ex_market", "btc_return_5s_bps", "btc_return_10s_bps",
                 "btc_return_30s_bps", "market_quote_fresh", "btc_fresh",
                 "twap_fresh", "p_ex_fresh", "joint_fresh")
        provenance = {name: value for name, value in self.payload.items()
                      if name.endswith(("_fresh", "_schema_version")) or name in
                      {"run_id", "snapshot_ts", "config_hash", "git_commit"}}
        return {"run_id": self.run_id, "snapshot_ts": self.timestamp,
                **provenance, "market_slug": self.market_slug, "time_left_sec": self.time_left_sec,
                "session_regime": self.session_regime, **{name: self.payload.get(name) for name in names}}


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
