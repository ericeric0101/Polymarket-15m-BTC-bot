"""Observation-only entry-decision trace for the staged decision-chain refactor."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def classify_entry_decision_layer(*, reason: str, event_type: str, shadow_only: bool) -> str:
    """Map the current scattered reasons into the proposed five-layer vocabulary."""
    text = f"{event_type} {reason}".lower()
    if shadow_only:
        return "model_consistency"
    if any(token in text for token in ("twap", "external_entry_confirmation", "no_valid_quote", "quote_")):
        return "hard_safety"
    if any(token in text for token in ("directional", "locked_side", "first_entry", "market_reset")):
        return "direction"
    if any(token in text for token in ("fair_edge", "spot_strike", "down_high_price", "reduce_only")):
        return "model_consistency"
    if any(token in text for token in ("econ_gate", "expected_net", "directional_edge", "robust_net")):
        return "economics"
    return "execution"


@dataclass(frozen=True)
class EntryDecision:
    """A single, serializable observation of the existing BUY decision path.

    This class deliberately does not evaluate a gate. It records the outcome
    of the current path so Phase 1 can be deployed without behavioral change.
    """

    slug: str
    instrument_id: str
    side: str
    state: str
    layer: str
    final_reason: str
    source_event_type: str
    shadow_only: bool
    entry_mode: str
    time_left_sec: float | None
    side_score: float | None
    fair: float | None
    entry_price: float | None
    fair_minus_entry: float | None
    robust_net_usdc: float | None

    @classmethod
    def observe(
        cls,
        *,
        slug: str,
        instrument_id: Any,
        side: str,
        should_quote: bool,
        reason: str = "",
        source_event_type: str = "",
        shadow_only: bool = False,
        entry_mode: str = "",
        time_left_sec: float | None = None,
        side_score: float | None = None,
        fair: float | None = None,
        entry_price: float | None = None,
        robust_net_usdc: float | None = None,
    ) -> "EntryDecision":
        if shadow_only:
            state = "SHADOW"
        elif should_quote:
            state = "ALLOW"
        else:
            state = "REJECT"
        final_reason = str(reason or ("eligible" if state == "ALLOW" else "unspecified"))
        return cls(
            slug=str(slug or ""),
            instrument_id=str(instrument_id or ""),
            side=str(side or "").upper(),
            state=state,
            layer=classify_entry_decision_layer(
                reason=final_reason,
                event_type=source_event_type,
                shadow_only=shadow_only,
            ),
            final_reason=final_reason,
            source_event_type=str(source_event_type or ""),
            shadow_only=bool(shadow_only),
            entry_mode=str(entry_mode or ""),
            time_left_sec=time_left_sec,
            side_score=side_score,
            fair=fair,
            entry_price=entry_price,
            fair_minus_entry=(fair - entry_price if fair is not None and entry_price is not None else None),
            robust_net_usdc=robust_net_usdc,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "instrument_id": self.instrument_id,
            "side": self.side,
            "state": self.state,
            "layer": self.layer,
            "final_reason": self.final_reason,
            "source_event_type": self.source_event_type,
            "shadow_only": self.shadow_only,
            "entry_mode": self.entry_mode,
            "time_left_sec": self.time_left_sec,
            "side_score": self.side_score,
            "fair": self.fair,
            "entry_price": self.entry_price,
            "fair_minus_entry": self.fair_minus_entry,
            "robust_net_usdc": self.robust_net_usdc,
        }
