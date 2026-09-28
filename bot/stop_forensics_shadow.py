"""Research-only adverse-episode and shadow-stop recorder.

The recorder only produces structured observations.  It does not return a
trading decision and therefore cannot alter live stop-loss authority.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any


class StopForensicsShadow:
    CHECKPOINTS = (5, 10, 15, 20, 30)

    def __init__(self, *, db: Any = None, run_id: str = "") -> None:
        self.events: list[dict[str, Any]] = []
        self.db, self.run_id = db, str(run_id)
        self._episodes: dict[tuple[str, str], dict[str, Any]] = {}

    @staticmethod
    def _number(value: Decimal | float | None) -> float | None:
        return None if value is None else float(value)

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append({"event_type": event_type, "payload": payload})
        if self.db is not None:
            try:
                self.db.enqueue_decision(
                    run_id=self.run_id, slug=str(payload.get("slug") or ""), market_id=None,
                    decision_epoch_ns=int(float(payload.get("first_adverse_ts") or payload.get("cleared_ts") or 0) * 1_000_000_000),
                    payload={"event_type": event_type, **payload},
                )
            except Exception:
                # Research persistence cannot affect live strategy authority.
                pass

    @staticmethod
    def _levels(levels: Any) -> list[tuple[Decimal, Decimal]]:
        result = []
        for row in levels or ():
            try:
                price = Decimal(str(getattr(row, "price", row[0] if isinstance(row, (tuple, list)) else 0)))
                raw_size = getattr(row, "size", row[1] if isinstance(row, (tuple, list)) and len(row) > 1 else 0)
                size = Decimal(str(raw_size() if callable(raw_size) else raw_size))
                if price > 0 and size > 0: result.append((price, size))
            except Exception:
                continue
        return sorted(result, reverse=True)

    @staticmethod
    def _depth_metrics(levels: list[tuple[Decimal, Decimal]], best_bid: Decimal | None, qty: Decimal) -> dict[str, Any]:
        if best_bid is None or qty <= 0 or not levels:
            return {key: None for key in ("bid_depth_1c", "bid_depth_2c", "bid_depth_5c", "coverage_top", "coverage_1c", "coverage_2c", "coverage_5c", "depth_weighted_exit_price", "depth_weighted_exit_pnl")}
        def depth(cents: Decimal) -> Decimal:
            return sum(size for price, size in levels if price >= best_bid - cents)
        top = sum(size for price, size in levels if price == best_bid)
        result = {"bid_depth_1c": float(depth(Decimal("0.01"))), "bid_depth_2c": float(depth(Decimal("0.02"))), "bid_depth_5c": float(depth(Decimal("0.05"))),
                  "coverage_top": float(min(Decimal("1"), top / qty)), "coverage_1c": float(min(Decimal("1"), depth(Decimal("0.01")) / qty)),
                  "coverage_2c": float(min(Decimal("1"), depth(Decimal("0.02")) / qty)), "coverage_5c": float(min(Decimal("1"), depth(Decimal("0.05")) / qty))}
        remaining, total, filled = qty, Decimal("0"), Decimal("0")
        for price, size in levels:
            take = min(remaining, size); total += take * price; filled += take; remaining -= take
            if remaining <= 0: break
        result["depth_weighted_exit_price"] = float(total / filled) if filled > 0 else None
        result["execution_feasible"] = bool(filled >= qty)
        return result

    def observe(
        self, *, raw_adverse: bool, now_ts: float, slug: str, instrument_id: str,
        position_side: str, entry_price: Decimal, qty: Decimal, signal_side: str,
        signal_score: Decimal, official_strike: Decimal, spot: Decimal,
        fair_probability: Decimal | None, fair_at_entry: Decimal | None, leader_side: str,
        best_bid: Decimal | None, best_bid_size: Decimal | None, time_left_sec: float,
        twap_features: dict[str, Any] | None = None, bid_levels: Any = None,
    ) -> None:
        key = (str(slug), str(instrument_id))
        episode = self._episodes.get(key)
        if not raw_adverse:
            if episode is not None:
                self._emit_missing_checkpoints(episode, now_ts)
                self._emit("STOP_SHADOW_ADVERSE_EPISODE_CLEARED", {
                    "episode_id": episode["id"], "cleared_ts": now_ts,
                    "duration_from_first_adverse_sec": max(0.0, now_ts - episode["first_ts"]),
                })
                self._episodes.pop(key, None)
            return
        if episode is None:
            episode = {
                "id": f"{slug}:{instrument_id}:{now_ts:.6f}", "first_ts": now_ts,
                "emitted_checkpoints": set(), "emitted_candidates": set(),
                "fair_at_entry": fair_at_entry,
            }
            self._episodes[key] = episode
            self._emit("STOP_SHADOW_ADVERSE_EPISODE_STARTED", {
                "episode_id": episode["id"], "slug": slug, "instrument_id": instrument_id,
                "first_adverse_ts": now_ts, "position_side": position_side,
            })
        elapsed = max(0.0, now_ts - episode["first_ts"])
        reversed_signal = str(signal_side).upper() not in {"", str(position_side).upper()}
        projected_side = str((twap_features or {}).get("projected_settlement_side_trend") or "UNKNOWN").upper()
        twap_adverse = projected_side not in {"UNKNOWN", "TIE", str(position_side).upper()}
        # Older/replayed telemetry has no TWAP feature; retain leader only as
        # backwards-compatible context fallback, never as an additional vote.
        adverse_leader = str(leader_side).upper() not in {"", str(position_side).upper()}
        fair_available = fair_at_entry is not None and fair_probability is not None
        fair_delta = fair_at_entry - fair_probability if fair_available else None
        fair_deteriorated = fair_delta >= Decimal("0.05") if fair_delta is not None else None
        components = {"signal": True, "twap": twap_features is not None, "fair": fair_available}
        weakening = int(reversed_signal) + int(twap_adverse if twap_features is not None else adverse_leader) + int(fair_deteriorated is True)
        depth = self._depth_metrics(self._levels(bid_levels), best_bid, qty)
        base = {
            "episode_id": episode["id"], "slug": slug, "instrument_id": instrument_id,
            "first_adverse_ts": episode["first_ts"], "elapsed_sec": elapsed,
            "position_side": position_side, "entry_price": self._number(entry_price),
            "qty": self._number(qty), "best_bid": self._number(best_bid),
            "best_bid_size": self._number(best_bid_size), "signal_side": signal_side,
            "signal_score": self._number(signal_score), "official_strike": self._number(official_strike),
            "spot": self._number(spot), "leader_side": leader_side,
            "fair_at_entry": self._number(fair_at_entry), "fair_probability": self._number(fair_probability),
            "fair_delta_ppt": self._number(fair_delta * Decimal("100")) if fair_delta is not None else None, "time_left_sec": float(time_left_sec),
            "thesis_weakening_count": weakening,
            "thesis_weakening_available_count": sum(components.values()), "thesis_component_availability": components,
            "twap_trajectory_adverse": twap_adverse if twap_features is not None else None,
        }
        if twap_features:
            base.update({key: twap_features.get(key) for key in (
                "official_current_twap", "twap_minus_strike_bps", "spot_minus_twap_bps",
                "twap_slope_5s_bps_per_sec", "twap_slope_10s_bps_per_sec",
                "projected_settlement_twap_flat_bps_vs_strike", "projected_settlement_twap_trend_bps_vs_strike",
                "projected_crossing_eta_sec", "crossing_eta_confidence", "projected_settlement_side_trend",
            )})
        base.update(depth)
        for checkpoint in self.CHECKPOINTS:
            if elapsed >= checkpoint and checkpoint not in episode["emitted_checkpoints"]:
                episode["emitted_checkpoints"].add(checkpoint)
                self._emit("STOP_SHADOW_CHECKPOINT", {
                    **base, "checkpoint_sec": checkpoint,
                    "checkpoint_quote_available": best_bid is not None,
                    "quote_age_sec": 0.0 if best_bid is not None else None,
                })
        if weakening < 2:
            return
        adaptive = 15 if abs((spot - official_strike) / official_strike * Decimal("10000")) < 1 else 10
        if time_left_sec > 300:
            adaptive += 5
        candidates = {"STOP_SHADOW_P5": 5, "STOP_SHADOW_P10": 10, "STOP_SHADOW_P15": 15, "STOP_SHADOW_ADAPTIVE": adaptive}
        for name, required in candidates.items():
            if elapsed >= required and name not in episode["emitted_candidates"]:
                episode["emitted_candidates"].add(name)
                self._emit("STOP_SHADOW_CANDIDATE", {**base, "candidate": name, "required_persistence_sec": required,
                                                       "decision_triggered": True,
                                                       "execution_feasible": depth.get("execution_feasible", "unknown")})

    def _emit_missing_checkpoints(self, episode: dict[str, Any], now_ts: float) -> None:
        elapsed = max(0.0, now_ts - episode["first_ts"])
        for checkpoint in self.CHECKPOINTS:
            if elapsed >= checkpoint and checkpoint not in episode["emitted_checkpoints"]:
                self._emit("STOP_SHADOW_CHECKPOINT", {
                    "episode_id": episode["id"], "checkpoint_sec": checkpoint,
                    "checkpoint_quote_available": False, "quote_age_sec": None,
                    "best_bid": None,
                })
