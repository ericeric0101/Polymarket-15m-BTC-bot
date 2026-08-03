from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


def _clamp(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    if value < lower:
        return lower
    if value > upper:
        return upper
    return value


def _to_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        if value is None:
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


@dataclass(frozen=True)
class EntryQualityAdjustment:
    size_multiplier: Decimal = Decimal("1")
    suggested_size_multiplier: Decimal = Decimal("1")
    min_expected_net_uplift_usdc: Decimal = Decimal("0")
    chase_risk: Decimal = Decimal("0")
    post_entry_decay_risk: Decimal = Decimal("0")
    quote_placement_mode: str = "default"
    label: str = "neutral"
    reasons: tuple[str, ...] = ()
    metrics: dict[str, float | None] | None = None

    def as_payload(self) -> dict[str, Any]:
        payload = dict(self.metrics or {})
        payload.update(
            {
                "entry_quality_label": self.label,
                "entry_quality_risk": float(self.chase_risk),
                "entry_quality_size_multiplier": float(self.size_multiplier),
                "entry_quality_suggested_size_multiplier": float(
                    self.suggested_size_multiplier
                ),
                "entry_quality_min_expected_uplift_usdc": float(
                    self.min_expected_net_uplift_usdc
                ),
                "entry_quality_post_entry_decay_risk": float(
                    self.post_entry_decay_risk
                ),
                "entry_quality_quote_placement_mode": self.quote_placement_mode,
                "entry_quality_reasons": list(self.reasons),
            }
        )
        return payload


def evaluate_entry_quality_adjustment(
    *,
    candidate_entry_price: Decimal | None,
    side_score: Decimal,
    fair: Decimal | None,
    robust_net_usdc: Decimal | None,
    spot_minus_strike_avg: Decimal | None,
    active_side_value: str,
    shadow_payload: dict[str, Any] | None = None,
    allow_size_down: bool = True,
) -> EntryQualityAdjustment:
    if candidate_entry_price is None or candidate_entry_price <= 0:
        return EntryQualityAdjustment()

    price = Decimal(str(candidate_entry_price))
    score_abs = abs(Decimal(str(side_score)))
    fair_edge_ps = (
        Decimal(str(fair)) - price
        if fair is not None and Decimal(str(fair)) > 0
        else Decimal("0")
    )
    robust_net = _to_decimal(robust_net_usdc)
    breakout_persistence = _clamp(
        _to_decimal((shadow_payload or {}).get("breakout_persistence_60s")),
        Decimal("0"),
        Decimal("1"),
    )
    ret_30_bps = abs(_to_decimal((shadow_payload or {}).get("ret_30_bps")))
    signed_spot_minus_strike = _to_decimal((shadow_payload or {}).get("spot_minus_strike"))
    if signed_spot_minus_strike == 0 and spot_minus_strike_avg is not None:
        signed_spot_minus_strike = Decimal(str(spot_minus_strike_avg))
    if str(active_side_value or "NONE").upper() == "DOWN":
        signed_spot_minus_strike = -signed_spot_minus_strike

    price_pressure = _clamp((price - Decimal("0.72")) / Decimal("0.18"), Decimal("0"), Decimal("1"))
    score_extension = _clamp((score_abs - Decimal("0.55")) / Decimal("0.25"), Decimal("0"), Decimal("1"))
    fair_edge_deficit = _clamp((Decimal("0.05") - fair_edge_ps) / Decimal("0.05"), Decimal("0"), Decimal("1"))
    robust_deficit = _clamp((Decimal("0.10") - robust_net) / Decimal("0.10"), Decimal("0"), Decimal("1"))
    ret_heat = _clamp((ret_30_bps - Decimal("4")) / Decimal("4"), Decimal("0"), Decimal("1"))

    breakout_support = breakout_persistence
    spot_support = _clamp(signed_spot_minus_strike / Decimal("40"), Decimal("0"), Decimal("1"))
    fair_support = _clamp(fair_edge_ps / Decimal("0.08"), Decimal("0"), Decimal("1"))
    robust_support = _clamp(robust_net / Decimal("0.12"), Decimal("0"), Decimal("1"))

    raw_risk = (
        (Decimal("0.42") * price_pressure)
        + (Decimal("0.23") * score_extension)
        + (Decimal("0.20") * fair_edge_deficit)
        + (Decimal("0.12") * robust_deficit)
        + (Decimal("0.08") * ret_heat)
        - (Decimal("0.17") * breakout_support)
        - (Decimal("0.14") * spot_support)
        - (Decimal("0.08") * fair_support)
        - (Decimal("0.06") * robust_support)
    )
    chase_risk = _clamp(raw_risk, Decimal("0"), Decimal("1"))
    raw_decay_risk = (
        (Decimal("0.34") * price_pressure)
        + (Decimal("0.22") * score_extension)
        + (Decimal("0.24") * fair_edge_deficit)
        + (Decimal("0.12") * robust_deficit)
        + (Decimal("0.08") * ret_heat)
        - (Decimal("0.20") * breakout_support)
        - (Decimal("0.13") * spot_support)
        - (Decimal("0.07") * robust_support)
    )
    post_entry_decay_risk = _clamp(raw_decay_risk, Decimal("0"), Decimal("1"))
    if price < Decimal("0.72") or chase_risk <= Decimal("0.15"):
        return EntryQualityAdjustment(
            chase_risk=chase_risk,
            post_entry_decay_risk=post_entry_decay_risk,
            metrics={
                "candidate_entry_price": float(price),
                "score_abs": float(score_abs),
                "fair_edge_ps": float(fair_edge_ps),
                "robust_net_usdc": float(robust_net),
                "signed_spot_minus_strike": float(signed_spot_minus_strike),
                "breakout_persistence_60s": float(breakout_persistence),
                "ret_30_bps_abs": float(ret_30_bps),
            },
        )

    suggested_size_multiplier = _clamp(
        Decimal("1") - (Decimal("0.45") * chase_risk),
        Decimal("0.55"),
        Decimal("1"),
    )
    size_multiplier = suggested_size_multiplier if allow_size_down else Decimal("1")
    min_expected_uplift = _clamp(
        Decimal("0.012") * chase_risk,
        Decimal("0"),
        Decimal("0.012"),
    )
    quote_placement_mode = "default"
    if post_entry_decay_risk >= Decimal("0.55"):
        quote_placement_mode = "join_bid"
    elif chase_risk >= Decimal("0.35"):
        quote_placement_mode = "one_tick_above_bid"

    reasons: list[str] = []
    if price_pressure >= Decimal("0.40"):
        reasons.append("high_price")
    if score_extension >= Decimal("0.35"):
        reasons.append("extended_score")
    if fair_edge_deficit >= Decimal("0.35"):
        reasons.append("thin_fair_buffer")
    if robust_deficit >= Decimal("0.35"):
        reasons.append("thin_robust_net")
    if ret_heat >= Decimal("0.35"):
        reasons.append("hot_ret30")
    if breakout_support >= Decimal("0.75"):
        reasons.append("supported_breakout")
    if spot_support >= Decimal("0.60"):
        reasons.append("supported_spot")

    label = "high_chase_risk" if chase_risk >= Decimal("0.45") else "moderate_chase_risk"
    return EntryQualityAdjustment(
        size_multiplier=size_multiplier,
        suggested_size_multiplier=suggested_size_multiplier,
        min_expected_net_uplift_usdc=min_expected_uplift,
        chase_risk=chase_risk,
        post_entry_decay_risk=post_entry_decay_risk,
        quote_placement_mode=quote_placement_mode,
        label=label,
        reasons=tuple(reasons),
        metrics={
            "candidate_entry_price": float(price),
            "score_abs": float(score_abs),
            "fair_edge_ps": float(fair_edge_ps),
            "robust_net_usdc": float(robust_net),
            "signed_spot_minus_strike": float(signed_spot_minus_strike),
            "breakout_persistence_60s": float(breakout_persistence),
            "ret_30_bps_abs": float(ret_30_bps),
            "price_pressure": float(price_pressure),
            "score_extension": float(score_extension),
            "fair_edge_deficit": float(fair_edge_deficit),
            "robust_deficit": float(robust_deficit),
            "post_entry_decay_risk": float(post_entry_decay_risk),
        },
    )
