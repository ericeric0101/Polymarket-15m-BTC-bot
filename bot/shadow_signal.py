from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _norm_tanh(value: Optional[float], scale: float) -> float:
    if value is None or scale <= 0:
        return 0.0
    return math.tanh(value / scale)


@dataclass(frozen=True)
class ShadowSignalConfig:
    min_edge: Decimal = Decimal("0.04")
    min_prob_band: Decimal = Decimal("0.08")
    max_prob_band: Decimal = Decimal("0.92")
    min_entry_sec: float = 90.0
    strike_z_weight: float = 0.55
    ret_10_weight: float = 0.20
    ret_30_weight: float = 0.15
    persistence_weight: float = 0.10
    strike_z_scale: float = 1.5
    ret_10_bps_scale: float = 18.0
    ret_30_bps_scale: float = 35.0
    shadow_score_min_abs: float = 0.15
    breakout_window_sec: float = 60.0
    ret_10_lookback_sec: float = 10.0
    ret_30_lookback_sec: float = 30.0


DEFAULT_SHADOW_SIGNAL_CONFIG = ShadowSignalConfig()


def _spot_at_or_before(
    history: Sequence[Tuple[float, Decimal]],
    cutoff_ts: float,
) -> Optional[Decimal]:
    for ts, price in reversed(history):
        if ts <= cutoff_ts:
            return price
    return None


def _return_bps(
    *,
    history: Sequence[Tuple[float, Decimal]],
    now_ts: float,
    spot: Decimal,
    lookback_sec: float,
) -> Optional[float]:
    if lookback_sec <= 0:
        return None
    hist_spot = _spot_at_or_before(history, now_ts - lookback_sec)
    if hist_spot is None or hist_spot <= 0:
        return None
    try:
        return (float(spot / hist_spot) - 1.0) * 10000.0
    except Exception:
        return None


def _breakout_persistence(
    *,
    history: Sequence[Tuple[float, Decimal]],
    now_ts: float,
    spot: Decimal,
    strike: Decimal,
    window_sec: float,
) -> Optional[float]:
    if window_sec <= 0:
        return None
    cutoff_ts = now_ts - window_sec
    window = [(ts, px) for ts, px in history if ts >= cutoff_ts]
    if not window:
        return None
    current_sign = 1 if spot > strike else -1
    aligned = 0
    for _, px in window:
        px_sign = 1 if px > strike else -1
        if px_sign == current_sign:
            aligned += 1
    return aligned / len(window)


def _strike_z(spot: Decimal, strike: Decimal, sigma: Decimal, time_left_sec: float) -> float:
    if strike <= 0 or sigma <= 0 or time_left_sec <= 0:
        return 0.0
    t_years = time_left_sec / (365.0 * 24.0 * 3600.0)
    if t_years <= 0:
        return 0.0
    sigma_f = float(sigma)
    denom = sigma_f * math.sqrt(t_years)
    if denom <= 0:
        return 0.0
    try:
        numer = math.log(float(spot / strike)) - 0.5 * (sigma_f ** 2) * t_years
        return numer / denom
    except Exception:
        return 0.0


def compute_shadow_signal(
    *,
    history: Sequence[Tuple[float, Decimal]],
    now_ts: float,
    spot: Decimal,
    strike: Optional[Decimal],
    sigma: Decimal,
    time_left_sec: float,
    cfg: ShadowSignalConfig = DEFAULT_SHADOW_SIGNAL_CONFIG,
) -> Dict[str, Optional[float]]:
    if strike is None or strike <= 0:
        return {
            "spot_minus_strike": None,
            "spot_minus_strike_bps": None,
            "ret_10_bps": None,
            "ret_30_bps": None,
            "breakout_persistence_60s": None,
            "strike_z": None,
            "shadow_score": 0.0,
            "shadow_prob_up": 0.5,
            "shadow_prob_down": 0.5,
        }

    spot_minus_strike = float(spot - strike)
    spot_minus_strike_bps = (float(spot / strike) - 1.0) * 10000.0
    ret_10_bps = _return_bps(
        history=history,
        now_ts=now_ts,
        spot=spot,
        lookback_sec=cfg.ret_10_lookback_sec,
    )
    ret_30_bps = _return_bps(
        history=history,
        now_ts=now_ts,
        spot=spot,
        lookback_sec=cfg.ret_30_lookback_sec,
    )
    breakout_persistence = _breakout_persistence(
        history=history,
        now_ts=now_ts,
        spot=spot,
        strike=strike,
        window_sec=cfg.breakout_window_sec,
    )
    strike_z = _strike_z(spot=spot, strike=strike, sigma=sigma, time_left_sec=time_left_sec)

    strike_component = _norm_tanh(strike_z, cfg.strike_z_scale)
    ret10_component = _norm_tanh(ret_10_bps, cfg.ret_10_bps_scale)
    ret30_component = _norm_tanh(ret_30_bps, cfg.ret_30_bps_scale)
    persistence_component = 0.0 if breakout_persistence is None else ((2.0 * breakout_persistence) - 1.0)

    shadow_score = (
        (cfg.strike_z_weight * strike_component)
        + (cfg.ret_10_weight * ret10_component)
        + (cfg.ret_30_weight * ret30_component)
        + (cfg.persistence_weight * persistence_component)
    )
    shadow_score = _clamp(shadow_score, -1.0, 1.0)
    shadow_prob_up = _clamp(0.5 + (0.49 * shadow_score), 0.01, 0.99)
    shadow_prob_down = 1.0 - shadow_prob_up

    return {
        "spot_minus_strike": spot_minus_strike,
        "spot_minus_strike_bps": spot_minus_strike_bps,
        "ret_10_bps": ret_10_bps,
        "ret_30_bps": ret_30_bps,
        "breakout_persistence_60s": breakout_persistence,
        "strike_z": strike_z,
        "shadow_score": shadow_score,
        "shadow_prob_up": shadow_prob_up,
        "shadow_prob_down": shadow_prob_down,
    }


def select_shadow_candidate(
    *,
    shadow_prob_up: float,
    shadow_prob_down: float,
    ask_up: Optional[Decimal],
    ask_down: Optional[Decimal],
    time_left_sec: float,
    shadow_score: float,
    cfg: ShadowSignalConfig = DEFAULT_SHADOW_SIGNAL_CONFIG,
) -> Tuple[Optional[str], Optional[Decimal]]:
    if time_left_sec < cfg.min_entry_sec:
        return None, None
    if abs(shadow_score) < cfg.shadow_score_min_abs:
        return None, None

    bias_side: Optional[str] = None
    if shadow_score > 0 and shadow_prob_up > shadow_prob_down:
        bias_side = "UP"
    elif shadow_score < 0 and shadow_prob_down > shadow_prob_up:
        bias_side = "DOWN"
    if bias_side is None:
        return None, None

    if bias_side == "UP":
        if not (float(cfg.min_prob_band) <= shadow_prob_up <= float(cfg.max_prob_band)):
            return None, None
        if ask_up is None or ask_up <= 0:
            return None, None
        edge_up = Decimal(str(shadow_prob_up)) - ask_up
        if edge_up >= cfg.min_edge:
            return "BUY_UP", edge_up
        return None, None

    if not (float(cfg.min_prob_band) <= shadow_prob_down <= float(cfg.max_prob_band)):
        return None, None
    if ask_down is None or ask_down <= 0:
        return None, None
    edge_down = Decimal(str(shadow_prob_down)) - ask_down
    if edge_down >= cfg.min_edge:
        return "BUY_DOWN", edge_down

    return None, None


def build_live_signal_compare_payload(
    *,
    slug: str,
    spot: Decimal,
    strike: Optional[Decimal],
    sigma: Decimal,
    time_left_sec: float,
    history: Sequence[Tuple[float, Decimal]],
    now_ts: float,
    active_side_value: str,
    active_side_locked: bool,
    side_score: Decimal,
    side_reason: str,
    ask_up: Optional[Decimal],
    ask_down: Optional[Decimal],
    bid_up: Optional[Decimal],
    bid_down: Optional[Decimal],
    cfg: ShadowSignalConfig = DEFAULT_SHADOW_SIGNAL_CONFIG,
) -> Dict[str, Any]:
    signal = compute_shadow_signal(
        history=history,
        now_ts=now_ts,
        spot=spot,
        strike=strike,
        sigma=sigma,
        time_left_sec=time_left_sec,
        cfg=cfg,
    )
    candidate_side, candidate_edge = select_shadow_candidate(
        shadow_prob_up=float(signal["shadow_prob_up"] or 0.5),
        shadow_prob_down=float(signal["shadow_prob_down"] or 0.5),
        ask_up=ask_up,
        ask_down=ask_down,
        time_left_sec=time_left_sec,
        shadow_score=float(signal["shadow_score"] or 0.0),
        cfg=cfg,
    )
    main_candidate_side = None
    if active_side_value == "UP":
        main_candidate_side = "BUY_UP"
    elif active_side_value == "DOWN":
        main_candidate_side = "BUY_DOWN"
    shadow_prob_up = float(signal["shadow_prob_up"] or 0.5)
    shadow_prob_down = float(signal["shadow_prob_down"] or 0.5)
    shadow_bias_side = None
    if shadow_prob_up > shadow_prob_down:
        shadow_bias_side = "UP"
    elif shadow_prob_down > shadow_prob_up:
        shadow_bias_side = "DOWN"

    return {
        "slug": slug,
        "probe_kind": "live_shadow_compare",
        "spot": float(spot),
        "strike": float(strike) if strike is not None else None,
        "sigma": float(sigma),
        "time_left_sec": float(time_left_sec),
        "bid_up": float(bid_up) if bid_up is not None else None,
        "ask_up": float(ask_up) if ask_up is not None else None,
        "bid_down": float(bid_down) if bid_down is not None else None,
        "ask_down": float(ask_down) if ask_down is not None else None,
        "main_active_side": active_side_value,
        "main_side_locked": bool(active_side_locked),
        "main_score": float(side_score),
        "main_reason": side_reason,
        "main_candidate_side": main_candidate_side,
        "spot_minus_strike": signal["spot_minus_strike"],
        "spot_minus_strike_bps": signal["spot_minus_strike_bps"],
        "ret_10_bps": signal["ret_10_bps"],
        "ret_30_bps": signal["ret_30_bps"],
        "breakout_persistence_60s": signal["breakout_persistence_60s"],
        "strike_z": signal["strike_z"],
        "shadow_score": signal["shadow_score"],
        "shadow_prob_up": signal["shadow_prob_up"],
        "shadow_prob_down": signal["shadow_prob_down"],
        "shadow_bias_side": shadow_bias_side,
        "shadow_candidate_side": candidate_side,
        "shadow_candidate_edge": float(candidate_edge) if candidate_edge is not None else None,
        "shadow_min_score_abs": cfg.shadow_score_min_abs,
        "shadow_min_edge": float(cfg.min_edge),
    }
