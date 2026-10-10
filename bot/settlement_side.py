"""Canonical settlement-side state of the official 60 s Chainlink TWAP.

Neutral, pure helpers shared by the exit path (Breaker V2 authority gate) and
research telemetry (STOP_TIMING), so the two can never drift apart.

Semantics (verified against ``bot.lifecycle_runtime.canonical_settlement_outcome``):

* Tie settles UP: TWAP == strike is FAVORABLE for a held UP token and ADVERSE
  for a held DOWN token.  No epsilon is applied.
* Freshness uses the Chainlink *source* observation timestamp (RTDS
  ``payload.timestamp``), never local receipt time, with the same 5 s bound the
  conditional absolute breaker's TWAP vote and STOP_TIMING use.
* A small negative age (source clock slightly ahead of the host clock) within
  ``FUTURE_SKEW_TOLERANCE_SEC`` is a clock artefact and reads as age 0.0 — the
  same 1 s tolerance the STOP_TIMING L2 age correction uses.  Anything further in
  the future is ``FUTURE`` (never clamped to fresh).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

REFERENCE_MAX_AGE_SEC = 5.0
FUTURE_SKEW_TOLERANCE_SEC = 1.0

TWAP_ADVERSE = "ADVERSE"
TWAP_FAVORABLE = "FAVORABLE"
# Non-directional states: every one of these is "not fresh-and-known".
TWAP_STALE = "STALE"
TWAP_FUTURE = "FUTURE"
TWAP_UNAVAILABLE = "UNAVAILABLE"
TWAP_STRIKE_INVALID = "STRIKE_INVALID"
TWAP_SLUG_MISMATCH = "SLUG_MISMATCH"
TWAP_UNKNOWN = "UNKNOWN"
DIRECTIONAL_STATES = frozenset({TWAP_ADVERSE, TWAP_FAVORABLE})


def _dec(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return out if out.is_finite() else None


def signed_strike_distance(held_side: str, reference: Optional[Decimal],
                           strike: Optional[Decimal]) -> tuple[Optional[float], Optional[float], str]:
    """Distance in the held side's favour (USD, bps) and ADVERSE/FAVORABLE/UNKNOWN.

    Settlement tie semantics: TWAP == strike settles UP, so a tie is favourable
    for UP and adverse for DOWN.
    """
    side = str(held_side).upper()
    reference, strike = _dec(reference), _dec(strike)
    if reference is None or strike is None or strike <= 0 or side not in {"UP", "DOWN"}:
        return None, None, TWAP_UNKNOWN
    raw = reference - strike
    distance = raw if side == "UP" else -raw
    adverse = raw < 0 if side == "UP" else raw >= 0
    return float(distance), float(distance / strike * Decimal("10000")), (TWAP_ADVERSE if adverse else TWAP_FAVORABLE)


def source_age(now_ts: float, source_ts: Any, *,
               future_tolerance_sec: float = FUTURE_SKEW_TOLERANCE_SEC) -> tuple[Optional[float], bool]:
    """(age_sec, is_future).  Small negative ages within tolerance read as 0.0."""
    ts = _dec(source_ts)
    if ts is None or ts <= 0:
        return None, False
    age = float(now_ts) - float(ts)
    if age < 0:
        if age >= -abs(float(future_tolerance_sec)):
            return 0.0, False
        return age, True
    return age, False


def canonical_twap_side_state(
    *, held_side: str, twap_features: Optional[dict[str, Any]], strike: Any, strike_verified: bool,
    expected_slug: str, now_ts: float, max_age_sec: float = REFERENCE_MAX_AGE_SEC,
    future_tolerance_sec: float = FUTURE_SKEW_TOLERANCE_SEC,
) -> dict[str, Any]:
    """Settlement-side state of the latest official TWAP for one held position.

    ``twap_features`` is ``TwapForwardShadow.latest(slug)``: ``official_current_twap``
    plus ``source_ts`` (Chainlink observation time) and ``market_slug``.
    Returns a dict whose ``state`` is ADVERSE / FAVORABLE only when the TWAP is
    present, fresh, not from the future, for the expected market, and compared
    against a verified strike; otherwise a non-directional state.  Pure; never raises.
    """
    out: dict[str, Any] = {
        "state": TWAP_UNKNOWN, "twap": None, "strike": None, "strike_verified": bool(strike_verified),
        "signed_distance_usd": None, "signed_distance_bps": None, "age_sec": None, "source_ts": None,
        "features_slug": None, "max_age_sec": float(max_age_sec),
    }
    try:
        feats = twap_features or {}
        twap = _dec(feats.get("official_current_twap"))
        strike_d = _dec(strike)
        out["twap"] = float(twap) if twap is not None else None
        out["strike"] = float(strike_d) if strike_d is not None else None
        out["source_ts"] = float(_dec(feats.get("source_ts"))) if _dec(feats.get("source_ts")) is not None else None
        features_slug = feats.get("market_slug")
        out["features_slug"] = str(features_slug) if features_slug is not None else None
        if twap is None or twap <= 0:
            out["state"] = TWAP_UNAVAILABLE
            return out
        if features_slug is not None and str(features_slug) != str(expected_slug or ""):
            out["state"] = TWAP_SLUG_MISMATCH
            return out
        age, future = source_age(now_ts, feats.get("source_ts"), future_tolerance_sec=future_tolerance_sec)
        out["age_sec"] = age
        if age is None:
            out["state"] = TWAP_UNAVAILABLE
            return out
        if future:
            out["state"] = TWAP_FUTURE
            return out
        if age > float(max_age_sec):
            out["state"] = TWAP_STALE
            return out
        if not strike_verified or strike_d is None or strike_d <= 0:
            out["state"] = TWAP_STRIKE_INVALID
            return out
        distance_usd, distance_bps, side_state = signed_strike_distance(held_side, twap, strike_d)
        out["signed_distance_usd"], out["signed_distance_bps"] = distance_usd, distance_bps
        out["state"] = side_state
        return out
    except Exception:
        out["state"] = TWAP_UNKNOWN
        return out
