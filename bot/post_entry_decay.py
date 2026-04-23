from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import os
from typing import Any, Callable


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_horizons() -> tuple[int, ...]:
    raw = os.getenv("POST_ENTRY_DECAY_HORIZONS_SEC", "10,30,60")
    out: list[int] = []
    for token in raw.split(","):
        try:
            val = int(float(token.strip()))
        except Exception:
            continue
        if val > 0 and val not in out:
            out.append(val)
    return tuple(sorted(out)) or (10, 30, 60)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _signed_for_side(side: str, score: float) -> float:
    side_u = str(side or "").upper()
    if side_u == "UP":
        return score
    if side_u == "DOWN":
        return -score
    return 0.0


@dataclass
class PostEntrySignalSample:
    ts: float
    score: float
    signed_score: float
    proposed_side: str
    fair_side: float | None
    spot_minus_strike: float | None
    reason: str


@dataclass
class PostEntryDecayTracker:
    inst_key: str
    slug: str
    entry_side: str
    entry_ts: float
    entry_score: float
    entry_signed_score: float
    entry_price: float
    entry_qty: float
    entry_fair_side: float | None = None
    entry_spot_minus_strike: float | None = None
    samples: list[PostEntrySignalSample] = field(default_factory=list)
    logged_horizons: set[int] = field(default_factory=set)


def _side_fair(entry_side: str, inputs: dict[str, Any]) -> float | None:
    if str(entry_side).upper() == "UP":
        return _as_float(inputs.get("fair_up"))
    if str(entry_side).upper() == "DOWN":
        return _as_float(inputs.get("fair_down"))
    return None


def _spot_minus_strike(inputs: dict[str, Any]) -> float | None:
    spot = _as_float(inputs.get("spot"))
    strike = _as_float(inputs.get("strike"))
    if spot is None or strike is None:
        return None
    return spot - strike


def register_post_entry_buy(
    *,
    trackers: dict[str, PostEntryDecayTracker],
    inst_key: str,
    slug: str,
    entry_side: str,
    now_ts: float,
    score: Decimal | float,
    fill_price: Decimal | float,
    fill_qty: Decimal | float,
    inputs: dict[str, Any] | None,
    strategy_event_fn: Callable[[str, dict[str, Any]], None],
) -> None:
    if os.getenv("POST_ENTRY_DECAY_OBSERVATION_ENABLED", "1").strip().lower() in {"0", "false", "no"}:
        return
    side = str(entry_side or "NONE").upper()
    if not inst_key or not slug or side not in {"UP", "DOWN"}:
        return

    score_f = float(score)
    price_f = float(fill_price)
    qty_f = float(fill_qty)
    entry_signed = _signed_for_side(side, score_f)
    inputs_d = dict(inputs or {})
    existing = trackers.get(inst_key)
    update_existing = bool(
        existing
        and existing.slug == slug
        and existing.entry_side == side
        and (now_ts - existing.entry_ts) <= 5.0
    )
    if update_existing:
        old_qty = max(0.0, existing.entry_qty)
        new_qty = max(0.0, qty_f)
        total_qty = old_qty + new_qty
        if total_qty > 0:
            existing.entry_price = ((existing.entry_price * old_qty) + (price_f * new_qty)) / total_qty
            existing.entry_qty = total_qty
        tracker = existing
    else:
        tracker = PostEntryDecayTracker(
            inst_key=inst_key,
            slug=slug,
            entry_side=side,
            entry_ts=now_ts,
            entry_score=score_f,
            entry_signed_score=entry_signed,
            entry_price=price_f,
            entry_qty=qty_f,
            entry_fair_side=_side_fair(side, inputs_d),
            entry_spot_minus_strike=_spot_minus_strike(inputs_d),
        )
        trackers[inst_key] = tracker

    strategy_event_fn(
        "POST_ENTRY_DECAY_REGISTERED",
        {
            "instrument_id": inst_key,
            "slug": slug,
            "entry_side": side,
            "entry_ts": now_ts,
            "entry_score": score_f,
            "entry_signed_score": entry_signed,
            "entry_price": tracker.entry_price,
            "entry_qty": tracker.entry_qty,
            "entry_fair_side": tracker.entry_fair_side,
            "entry_spot_minus_strike": tracker.entry_spot_minus_strike,
            "updated_existing": update_existing,
        },
    )


def _sample_at_or_before(samples: list[PostEntrySignalSample], elapsed_sec: float) -> PostEntrySignalSample | None:
    eligible = [s for s in samples if s.ts <= elapsed_sec + 0.75]
    if not eligible:
        return None
    return max(eligible, key=lambda s: s.ts)


def _adverse_area(samples: list[PostEntrySignalSample], horizon_sec: int) -> tuple[float, float, float]:
    if not samples:
        return 0.0, 0.0, 0.0
    ordered = [s for s in samples if 0.0 <= s.ts <= horizon_sec]
    if not ordered:
        return 0.0, 0.0, 0.0
    area = 0.0
    adverse_sec = 0.0
    prev = ordered[0]
    min_signed = prev.signed_score
    max_signed = prev.signed_score
    for cur in ordered[1:]:
        dt = max(0.0, min(cur.ts, float(horizon_sec)) - min(prev.ts, float(horizon_sec)))
        adverse = max(0.0, -prev.signed_score)
        area += adverse * dt
        if adverse > 0:
            adverse_sec += dt
        min_signed = min(min_signed, cur.signed_score)
        max_signed = max(max_signed, cur.signed_score)
        prev = cur
    return area, adverse_sec, min_signed


def _classify(samples: list[PostEntrySignalSample], horizon_sec: int) -> tuple[str, dict[str, Any]]:
    adverse_threshold = _env_float("POST_ENTRY_DECAY_ADVERSE_SCORE_ABS", 0.15)
    recovery_threshold = _env_float("POST_ENTRY_DECAY_RECOVERY_SCORE_ABS", 0.0)
    area_threshold_30 = _env_float("POST_ENTRY_DECAY_MIN_ADVERSE_AREA_30", 3.0)

    current = _sample_at_or_before(samples, float(horizon_sec))
    s10 = _sample_at_or_before(samples, 10.0)
    s30 = _sample_at_or_before(samples, 30.0)
    area, adverse_sec, min_signed = _adverse_area(samples, horizon_sec)

    signed_now = current.signed_score if current else None
    signed_10 = s10.signed_score if s10 else None
    signed_30 = s30.signed_score if s30 else None

    adverse_10 = signed_10 is not None and signed_10 <= -adverse_threshold
    adverse_30 = signed_30 is not None and signed_30 <= -adverse_threshold
    adverse_now = signed_now is not None and signed_now <= -adverse_threshold
    recovered_now = signed_now is not None and signed_now >= recovery_threshold

    if horizon_sec >= 30 and adverse_10 and recovered_now:
        label = "noise_flip_recovered"
    elif horizon_sec >= 60 and adverse_10 and adverse_30 and adverse_now:
        label = "persistent_decay"
    elif horizon_sec >= 30 and adverse_now and area >= area_threshold_30:
        label = "confirmed_decay"
    elif adverse_now:
        label = "adverse_unconfirmed"
    elif min_signed <= -adverse_threshold:
        label = "brief_adverse_noise"
    else:
        label = "normal"

    return label, {
        "score_signed_now": signed_now,
        "score_signed_10s": signed_10,
        "score_signed_30s": signed_30,
        "adverse_10s": adverse_10,
        "adverse_30s": adverse_30,
        "adverse_now": adverse_now,
        "recovered_now": recovered_now,
        "min_signed_score": min_signed,
        "adverse_area": area,
        "adverse_sec": adverse_sec,
        "adverse_threshold": adverse_threshold,
        "recovery_threshold": recovery_threshold,
    }


def record_post_entry_signal_sample(
    *,
    trackers: dict[str, PostEntryDecayTracker],
    slug: str,
    now_ts: float,
    score: Decimal | float,
    proposed_side: str,
    reason: str,
    inputs: dict[str, Any] | None,
    side_invalidation_confirmed_by_slug: dict[str, bool] | None,
    strategy_event_fn: Callable[[str, dict[str, Any]], None],
) -> None:
    if os.getenv("POST_ENTRY_DECAY_OBSERVATION_ENABLED", "1").strip().lower() in {"0", "false", "no"}:
        return
    if not trackers or not slug:
        return

    max_horizon = max(_env_horizons())
    expiry_sec = max_horizon + _env_float("POST_ENTRY_DECAY_RETENTION_BUFFER_SEC", 30.0)
    score_f = float(score)
    inputs_d = dict(inputs or {})
    invalidated = bool((side_invalidation_confirmed_by_slug or {}).get(slug, False))

    expired: list[str] = []
    for inst_key, tracker in list(trackers.items()):
        age = now_ts - tracker.entry_ts
        if age > expiry_sec or tracker.slug != slug:
            if age > expiry_sec:
                expired.append(inst_key)
            continue
        sample = PostEntrySignalSample(
            ts=max(0.0, age),
            score=score_f,
            signed_score=_signed_for_side(tracker.entry_side, score_f),
            proposed_side=str(proposed_side or "NONE").upper(),
            fair_side=_side_fair(tracker.entry_side, inputs_d),
            spot_minus_strike=_spot_minus_strike(inputs_d),
            reason=reason,
        )
        tracker.samples.append(sample)
        tracker.samples = [s for s in tracker.samples if s.ts >= max(0.0, age - expiry_sec)]

        for horizon in _env_horizons():
            if horizon in tracker.logged_horizons or age < horizon:
                continue
            classification, metrics = _classify(tracker.samples, horizon)
            tracker.logged_horizons.add(horizon)
            strategy_event_fn(
                "POST_ENTRY_DECAY_OBSERVATION",
                {
                    "instrument_id": tracker.inst_key,
                    "slug": tracker.slug,
                    "entry_side": tracker.entry_side,
                    "entry_ts": tracker.entry_ts,
                    "horizon_sec": horizon,
                    "classification": classification,
                    "entry_score": tracker.entry_score,
                    "entry_signed_score": tracker.entry_signed_score,
                    "entry_price": tracker.entry_price,
                    "entry_qty": tracker.entry_qty,
                    "entry_fair_side": tracker.entry_fair_side,
                    "entry_spot_minus_strike": tracker.entry_spot_minus_strike,
                    "current_score": score_f,
                    "current_proposed_side": sample.proposed_side,
                    "current_fair_side": sample.fair_side,
                    "current_spot_minus_strike": sample.spot_minus_strike,
                    "side_invalidation_confirmed": invalidated,
                    **metrics,
                },
            )

    for inst_key in expired:
        trackers.pop(inst_key, None)
