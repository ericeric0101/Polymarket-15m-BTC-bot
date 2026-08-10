from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        dec = Decimal(str(value))
    except Exception:
        return None
    if dec.is_nan():
        return None
    return dec


def _to_float(value: Any) -> Optional[float]:
    dec = _to_decimal(value)
    if dec is None:
        return None
    return float(dec)


@dataclass(frozen=True)
class EntryConfirmationConfig:
    enabled: bool = False
    shadow_enabled: bool = True
    book_mid_threshold_ps: Decimal = Decimal("0.02")
    conflict_size_multiplier: Decimal = Decimal("0.5")
    skip_strong_conflict: bool = False
    weak_pfair_lower: Decimal = Decimal("0.47")
    weak_pfair_upper: Decimal = Decimal("0.53")


@dataclass(frozen=True)
class EntryConfirmationSignal:
    state: str
    action: str
    active_side: str
    confidence: Decimal
    reason: str
    shadow_only: bool
    features: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        payload = {
            "state": self.state,
            "action": self.action,
            "active_side": self.active_side,
            "confidence": float(self.confidence),
            "reason": self.reason,
            "shadow_only": bool(self.shadow_only),
        }
        payload.update(self.features)
        return payload


class EntryConfirmationEngine:
    def __init__(self, config: EntryConfirmationConfig) -> None:
        self.config = config

    def evaluate(
        self,
        *,
        active_side: str,
        p_fair: Any,
        fair: Any,
        best_bid: Any,
        best_ask: Any,
        ref_spot: Any,
        ref_spot_source: str,
        ref_spot_age_sec: Optional[float],
        strike: Any,
        binance_spot: Any,
        binance_age_sec: Optional[float],
    ) -> EntryConfirmationSignal:
        side = str(active_side or "").upper()
        p_fair_dec = _to_decimal(p_fair)
        fair_dec = _to_decimal(fair)
        bid_dec = _to_decimal(best_bid)
        ask_dec = _to_decimal(best_ask)
        spot_dec = _to_decimal(ref_spot)
        strike_dec = _to_decimal(strike)
        binance_dec = _to_decimal(binance_spot)

        book_mid = None
        book_vs_fair = None
        if bid_dec is not None and ask_dec is not None and bid_dec > 0 and ask_dec > 0:
            book_mid = (bid_dec + ask_dec) / Decimal("2")
            if fair_dec is not None:
                book_vs_fair = book_mid - fair_dec

        spot_minus_strike = None
        spot_minus_strike_bps = None
        if spot_dec is not None and strike_dec is not None and strike_dec > 0:
            spot_minus_strike = spot_dec - strike_dec
            spot_minus_strike_bps = ((spot_dec / strike_dec) - Decimal("1")) * Decimal("10000")

        binance_basis_bps = None
        if binance_dec is not None and spot_dec is not None and spot_dec > 0:
            binance_basis_bps = ((binance_dec / spot_dec) - Decimal("1")) * Decimal("10000")

        features = {
            "p_fair": _to_float(p_fair_dec),
            "fair": _to_float(fair_dec),
            "best_bid": _to_float(bid_dec),
            "best_ask": _to_float(ask_dec),
            "book_mid": _to_float(book_mid),
            "book_vs_fair_ps": _to_float(book_vs_fair),
            "ref_spot": _to_float(spot_dec),
            "ref_spot_source": str(ref_spot_source or ""),
            "ref_spot_age_sec": ref_spot_age_sec,
            "strike": _to_float(strike_dec),
            "spot_minus_strike": _to_float(spot_minus_strike),
            "spot_minus_strike_bps": _to_float(spot_minus_strike_bps),
            "binance_spot": _to_float(binance_dec),
            "binance_age_sec": binance_age_sec,
            "binance_basis_bps": _to_float(binance_basis_bps),
        }

        if side not in {"UP", "DOWN"} or book_mid is None:
            return EntryConfirmationSignal(
                state="unavailable",
                action="observe",
                active_side=side or "NONE",
                confidence=Decimal("0"),
                reason="missing_side_or_book",
                shadow_only=not self.config.enabled,
                features=features,
            )

        threshold = max(Decimal("0"), self.config.book_mid_threshold_ps)
        book_supports_active = book_mid >= (Decimal("0.5") + threshold)
        book_conflicts_active = book_mid <= (Decimal("0.5") - threshold)

        spot_supports_active = None
        if spot_minus_strike is not None:
            spot_supports_active = (
                (side == "UP" and spot_minus_strike > 0)
                or (side == "DOWN" and spot_minus_strike < 0)
            )

        state = "neutral"
        reason = "mixed_or_near_threshold"
        confidence = Decimal("0")
        if book_supports_active and spot_supports_active is True:
            state = "support"
            reason = "book_and_spot_support_active_side"
            confidence = min(Decimal("1"), abs(book_mid - Decimal("0.5")) / Decimal("0.25"))
        elif book_conflicts_active and spot_supports_active is False:
            state = "conflict"
            reason = "book_and_spot_conflict_active_side"
            confidence = min(Decimal("1"), abs(book_mid - Decimal("0.5")) / Decimal("0.25"))
        elif book_supports_active:
            state = "book_support"
            reason = "book_supports_active_side"
            confidence = min(Decimal("1"), abs(book_mid - Decimal("0.5")) / Decimal("0.30"))
        elif book_conflicts_active:
            state = "book_conflict"
            reason = "book_conflicts_active_side"
            confidence = min(Decimal("1"), abs(book_mid - Decimal("0.5")) / Decimal("0.30"))

        action = "observe"
        if self.config.enabled:
            if state in {"conflict", "book_conflict"}:
                is_weak = (
                    p_fair_dec is not None
                    and self.config.weak_pfair_lower <= p_fair_dec <= self.config.weak_pfair_upper
                )
                # A deeply one-sided book against the selected token is a
                # stronger contradiction than a near-50/50 model reading.
                # Do not reduce into that contradiction in live mode.
                strong_conflict = (
                    state == "conflict"
                    or confidence >= Decimal("0.80")
                )
                if self.config.skip_strong_conflict and (strong_conflict or is_weak):
                    action = "skip"
                else:
                    action = "reduce_size"

        return EntryConfirmationSignal(
            state=state,
            action=action,
            active_side=side,
            confidence=confidence,
            reason=reason,
            shadow_only=not self.config.enabled,
            features=features,
        )


def apply_entry_confirmation_adjustment(
    *,
    desired_entry: dict[str, Any],
    side: str,
    signal: EntryConfirmationSignal,
    config: EntryConfirmationConfig,
) -> dict[str, Any]:
    if side != "buy":
        return desired_entry
    desired_entry["external_entry_confirmation"] = signal.as_payload()
    if not config.enabled or not desired_entry.get("should_quote", False):
        return desired_entry

    if signal.action == "skip":
        desired_entry["should_quote"] = False
        desired_entry["diag_reason"] = "external_entry_confirmation_skip"
        return desired_entry

    if signal.action == "reduce_size":
        prior_multiplier = Decimal(str(desired_entry.get("size_multiplier", Decimal("1")) or "1"))
        adjusted_multiplier = max(Decimal("0"), prior_multiplier * config.conflict_size_multiplier)
        desired_entry["size_multiplier"] = adjusted_multiplier
        desired_entry["external_entry_confirmation_size_adjustment"] = {
            "prior_size_multiplier": prior_multiplier,
            "adjusted_size_multiplier": adjusted_multiplier,
            "multiplier": config.conflict_size_multiplier,
            "state": signal.state,
        }
        diag_reason = str(desired_entry.get("diag_reason", "") or "")
        adjustment_reason = (
            "external_entry_confirmation_reduce "
            f"state={signal.state} mult={float(prior_multiplier):.3f}->{float(adjusted_multiplier):.3f}"
        )
        desired_entry["diag_reason"] = (
            f"{diag_reason}; {adjustment_reason}" if diag_reason else adjustment_reason
        )
    return desired_entry
