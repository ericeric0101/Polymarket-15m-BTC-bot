"""Entry V3 candidate-level decision telemetry (research only; no order authority).

SHADOW ONLY. This module never imports execution, order submission/cancel modules
or Nautilus order types.  It reads cached strategy state by value at the moment a
BUY decision event is emitted (``_db_order_event``), writes immutable snapshots to a
separate JSONL store (never the trade journal), and samples cached BBO on its own
daemon thread for first-quote path statistics and held-side markouts.

Why a new snapshot (see reports/.../gap_root_cause.md): the existing
``decision_trace`` is attached at journal-write time by an as-of selector that
rejects a whole prediction snapshot when any upstream *source* clock is ahead of
the local clock (Polymarket/Binance lead the host by 0-0.63 s), which blanked
p_ex/TTE/sigma/bid/ask on ~52% of first submits.  Here every field is copied from
memory in the same synchronous call as the decision; freshness uses LOCAL receipt
clocks only; upstream source-clock lead is recorded as a diagnostic, never as a
rejection rule.

Flags (read once at startup, default OFF):
CANDIDATE_DECISION_TELEMETRY_ENABLED, FIRST_QUOTE_ONLY_SHADOW_ENABLED,
HELD_SIDE_MARKOUT_TELEMETRY_ENABLED.  FQO and markouts require the candidate flag.

DRY-RUN plumbing (``bot.dry_run_plumbing.PLUMBING_REVISION``): the paper maker lifecycle is
recorded on the same candidate -- ``SHADOW_SIM_ENTRY_FILLED`` becomes LIFECYCLE
``SIMULATED_FILL`` (joined by the paper order's own ``dry_run_client_order_id``, never by time),
fill-anchored markouts use ``anchor=SIMULATED_FILL``, and every accepted DRY-RUN candidate whose
market closes gets exactly one terminal LIFECYCLE record.  All of it carries
``execution_mode=DRY_RUN`` / ``evidence_tier=SIMULATED_ENV``.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from bot.dry_run_plumbing import (EXECUTION_MODE_DRY_RUN, PLUMBING_REVISION, evidence_tier, execution_mode)

SCHEMA_VERSION = "candidate_decision_v2"
# v2 (2026-10-13): atomic decision-time feature vector X_t for future P(win) research --
# BTC reference by source (local receipt <= cutoff), short returns, TWAP state, other-side
# BBO, top-of-book imbalance, explicit feature cutoff.  No future / outcome fields.
ACCEPT_EVENTS = frozenset({"ORDER_SUBMIT", "ORDER_DRY_RUN_SUBMITTED"})
REJECT_PREFIXES = ("ORDER_SKIP_", "ORDER_OBSERVE_BUY_BLOCKED")
LIFECYCLE_EVENTS = frozenset({"ORDER_FILLED", "ORDER_CANCELED", "ORDER_REJECTED", "ORDER_DRY_RUN_CANCELLED",
                              "ORDER_DRY_RUN_FILLED", "ORDER_EXPIRED"})
MARKOUT_HORIZONS_SEC = (1, 3, 5, 10, 15, 30)
# DRY-RUN paper-order lifecycle (bot.shadow_simulation); LIVE semantics are untouched.
DRY_RUN_SUBMIT_EVENT = "ORDER_DRY_RUN_SUBMITTED"
SIM_FILL_EVENT = "SHADOW_SIM_ENTRY_FILLED"
SIM_EXPIRE_EVENT = "SHADOW_SIM_ENTRY_EXPIRED"
# Journaled by the paper-order code in the same submit call, immediately before ORDER_DRY_RUN_SUBMITTED;
# its payload carries the decision's p_fair ("fair") and the quote time it came from (planned_quote_ts).
SIM_ENTRY_EVENTS = frozenset({"SHADOW_SIM_ENTRY_CANDIDATE", "SHADOW_SIM_ENTRY_REQUOTED"})
SIMULATED_FILL_EVENT_TYPE = "ORDER_DRY_RUN_FILLED"   # already read by the offline evaluator as a fill
DRY_RUN_TERMINAL_EVENT_TYPE = "DRY_RUN_TERMINAL"
TERMINAL_LIFECYCLE_EVENTS = ("SIMULATED_FILL", "SIMULATED_CANCEL", "SIMULATED_EXPIRE", "REJECTED")
STABLE_ROLES = ("FIRST_ACCEPTED", "FIRST_EVALUATION")
# Offline canonical rule: FIRST_ACCEPTED if the market has one, else FIRST_EVALUATION.
PEX_MAX_AGE_SEC = 8.0
BBO_MAX_AGE_SEC = 3.0
TICK = 0.01
RETURN_HORIZONS_SEC = (5, 15, 30, 60)
REFERENCE_MAX_AGE_SEC = 10.0
TWAP_STATE_MAX_AGE_SEC = 5.0
# Never part of a decision snapshot (post-hoc label / linker tables only).
FORBIDDEN_SNAPSHOT_FIELD_TOKENS = ("markout", "outcome", "final_strike", "fill_price", "filled", "requote",
                                   "exit_type", "payoff", "hold_y", "pnl", "settle")
DEFAULT_QUEUE_MAX = 4096
DEFAULT_SAMPLE_INTERVAL_SEC = 0.25
MAX_TRACKED_MARKETS = 8
PRUNE_AFTER_END_SEC = 120.0
BOT_DISK_GUARD_BYTES = 10 * 1024 ** 3
DISK_RESERVE_BYTES = 3 * 1024 ** 3


def _num(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        out = float(value)
        return out if out == out else None
    except (TypeError, ValueError):
        return None


def candidate_id(market_slug: str, role: str, side: str = "", run_id: str = "", seq: int = 0,
                 schema_version: str = SCHEMA_VERSION) -> str:
    """Deterministic id.  FIRST_ACCEPTED / FIRST_EVALUATION ids depend only on the market,
    so a restart reproduces (and offline dedup collapses) them; TRANSITION ids include
    run_id + sequence so they never collide across restarts."""
    if role in STABLE_ROLES:
        key = "|".join((market_slug, role, schema_version))
    else:
        key = "|".join((market_slug, role, side or "", run_id or "", str(int(seq)), schema_version))
    return hashlib.sha256(key.encode()).hexdigest()[:20]


def exit_policy_fingerprint(strategy: Any) -> dict[str, Any]:
    names = ("absolute_max_loss_enabled", "absolute_max_loss_usdc", "absolute_max_loss_twap_authority_gate_enabled",
             "catastrophic_stop_loss_enabled", "catastrophic_stop_loss_usdc", "tail_protect_tp_enabled",
             "tail_protect_tp_price", "hold_to_redeem_enabled", "taker_exit_enabled")
    out = {n: (str(getattr(strategy, n)) if getattr(strategy, n, None) is not None else None) for n in names}
    from bot.exit_policy_state import effective_exit_policy
    eff = effective_exit_policy(strategy)
    out["conditional_breaker_enabled"] = eff["conditional_breaker_enabled"]
    out["breaker_v2_effective_for_conditional_exit"] = eff["breaker_v2_effective_for_conditional_exit"]
    out["catastrophic_breaker_enabled"] = eff["catastrophic_breaker_enabled"]
    for key in ("stop_loss_enabled", "endgame_twap_exit_enabled", "invalidation_recovery_enabled",
                "recovery_exit_ladder_enabled", "maker_urgent_exit_enabled"):
        out[key] = eff[key]
    return out


def strike_module_version() -> Optional[str]:
    try:
        p = Path(__file__).resolve().parent / "strike_finality.py"
        return hashlib.sha256(p.read_bytes()).hexdigest()[:12]
    except Exception:
        return None


def build_run_header(strategy: Any, flags: dict[str, bool]) -> dict[str, Any]:
    body = {"git_head": str(getattr(strategy, "runtime_git_revision", "") or "") or None,
            "schema_version": SCHEMA_VERSION, "flags": dict(sorted(flags.items())),
            "exit_policy": exit_policy_fingerprint(strategy), "strike_module_version": strike_module_version(),
            # Deterministic research policy fingerprint (bot.research_startup_guard); None outside the profile.
            "policy_fingerprint": getattr(strategy, "policy_fingerprint", None),
            # Population identity: PRE_PLUMBING data has none of these keys and never pools with this.
            "plumbing_revision": PLUMBING_REVISION, "execution_mode": execution_mode(strategy),
            "evidence_tier": evidence_tier(execution_mode(strategy)),
            "run_label": getattr(strategy, "run_label", None)}
    fp = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return {"record_type": "RUN_HEADER", "run_id": str(getattr(strategy, "run_id", "") or ""), **body,
            "run_fingerprint": fp}


def strike_status_for(strategy: Any, slug: str) -> tuple[str, Optional[str]]:
    """PROVISIONAL / FINAL from bot.strike_finality; UNKNOWN only when no tracker is wired."""
    try:
        from bot.strike_finality import STATUS_UNTRACKED, strike_finality_provenance
        status = strike_finality_provenance(getattr(strategy, "strike_finality", None), slug)["status"]
        if status == STATUS_UNTRACKED:
            return "UNKNOWN", "STRIKE_TRACKER_ABSENT"
        return status, None
    except Exception:
        return "UNKNOWN", "STRIKE_TRACKER_ERROR"


@dataclass(frozen=True)
class CandidateDecisionSnapshot:
    candidate_id: str
    candidate_role: str
    market_slug: str
    token_id: Optional[str]
    instrument_id: Optional[str]
    candidate_side: Optional[str]
    market_start_ts: Optional[float]
    market_end_ts: Optional[float]
    decision_ts: float
    decision_ts_source: str
    decision_wall_ts: float
    decision_monotonic_ns: int
    run_id: str
    run_fingerprint: str
    schema_version: str
    best_bid: Optional[float]
    best_ask: Optional[float]
    mid: Optional[float]
    spread: Optional[float]
    top_bid_size: Optional[float]
    top_ask_size: Optional[float]
    executable_ask: Optional[float]
    executable_bid: Optional[float]
    bbo_receipt_ts: Optional[float]
    bbo_age_sec: Optional[float]
    raw_p_ex: Optional[float]
    p_up_ex_market: Optional[float]
    p_down_ex_market: Optional[float]
    pex_snapshot_ts: Optional[float]
    pex_age_sec: Optional[float]
    pex_reason: Optional[str]
    side_score: Optional[float]
    side_score_abs: Optional[float]
    required_move_sigma: Optional[float]
    signed_strike_distance_usd: Optional[float]
    runtime_strike: Optional[float]
    strike_status: str
    strike_status_reason: Optional[str]
    source_clock_lead_sec: Optional[float]
    tte_sec: Optional[float]
    elapsed_since_open_sec: Optional[float]
    production_p_fair: Optional[float]
    proposed_maker_price: Optional[float]
    proposed_qty: Optional[float]
    eligibility: str
    eligibility_event_type: str
    reject_reason: Optional[str]
    client_order_id: Optional[str]
    alignment_status: str
    missing_reasons: tuple = field(default_factory=tuple)
    # ---- v2 decision-time features (all from local receipts <= feature_cutoff_ts) ----
    feature_cutoff_ts: Optional[float] = None
    feature_cutoff_basis: Optional[str] = None
    top_imbalance: Optional[float] = None
    other_side_bid: Optional[float] = None
    other_side_ask: Optional[float] = None
    other_side_bbo_receipt_ts: Optional[float] = None
    other_side_bbo_age_sec: Optional[float] = None
    btc_chainlink_price: Optional[float] = None
    btc_chainlink_receipt_ts: Optional[float] = None
    btc_chainlink_age_sec: Optional[float] = None
    btc_chainlink_source_ts: Optional[float] = None
    btc_binance_price: Optional[float] = None
    btc_binance_receipt_ts: Optional[float] = None
    btc_binance_age_sec: Optional[float] = None
    btc_binance_source_ts: Optional[float] = None
    btc_twap_price: Optional[float] = None
    btc_twap_receipt_ts: Optional[float] = None
    btc_twap_age_sec: Optional[float] = None
    btc_twap_source_ts: Optional[float] = None
    reference_spot: Optional[float] = None
    reference_spot_source: Optional[str] = None
    btc_ret_5s: Optional[float] = None
    btc_ret_15s: Optional[float] = None
    btc_ret_30s: Optional[float] = None
    btc_ret_60s: Optional[float] = None
    btc_ret_source: Optional[str] = None
    twap_state_current: Optional[float] = None
    twap_state_projected_side: Optional[str] = None
    twap_state_observed_ts: Optional[float] = None
    twap_state_age_sec: Optional[float] = None
    twap_minus_strike_usd: Optional[float] = None
    reference_minus_strike_usd: Optional[float] = None
    p_ex_receipt_ts: Optional[float] = None
    # production_p_fair provenance: DECISION_EVENT_PAYLOAD = the value this decision used.
    production_p_fair_source: Optional[str] = None
    production_p_fair_asof_ts: Optional[float] = None

    def as_payload(self) -> dict[str, Any]:
        out = asdict(self)
        out["missing_reasons"] = dict(self.missing_reasons)
        return {"record_type": "CANDIDATE", **out}


def _side_of(strategy: Any, inst: Any, payload: dict) -> Optional[str]:
    fn = getattr(strategy, "_side_for_instrument_id", None)
    norm = getattr(strategy, "_normalize_instrument_id", None)
    if callable(fn) and inst:
        try:
            raw = fn(norm(inst) if callable(norm) else inst)
            v = str(getattr(raw, "value", raw)).upper()
            if v in ("UP", "DOWN"):
                return v
        except Exception:
            pass
    for key in ("active_side",):
        v = str(payload.get(key) or "").upper()
        if v in ("UP", "DOWN"):
            return v
    conf = payload.get("external_entry_confirmation") or {}
    v = str(conf.get("active_side") or "").upper()
    return v if v in ("UP", "DOWN") else None


def _inst_key(strategy: Any, inst: Any) -> str:
    fn = getattr(strategy, "_instrument_key", None)
    try:
        return str(fn(inst)) if callable(fn) else str(inst)
    except Exception:
        return str(inst)


def read_bbo(strategy: Any, inst_key: str) -> dict[str, Optional[float]]:
    """Value copy of cached BBO (no network, never raises)."""
    try:
        q = (getattr(strategy, "latest_quote_by_inst", None) or {}).get(inst_key)
        rts = (getattr(strategy, "last_quote_update_ts_by_inst", None) or {}).get(inst_key)
        depth = (getattr(strategy, "latest_quote_depth_by_inst", None) or {}).get(inst_key)
        bid, ask = (q if q else (None, None))
        return {"bid": _num(bid), "ask": _num(ask), "receipt_ts": _num(rts),
                "bid_size": _num(depth[0]) if depth else None, "ask_size": _num(depth[1]) if depth else None}
    except Exception:
        return {"bid": None, "ask": None, "receipt_ts": None, "bid_size": None, "ask_size": None}


def _receipt_value(price: Any, receipt: Any, source_ts: Any, cutoff: float, max_age: float,
                   missing: dict, key: str) -> tuple:
    """(price, receipt_ts, age, source_ts) only if received locally at or before the cutoff."""
    p, r, src = _num(price), _num(receipt), _num(source_ts)
    if p is None or r is None or r <= 0:
        missing[key] = "REFERENCE_NOT_RECEIVED"
        return None, None, None, None
    if r > cutoff:
        missing[key] = "RECEIPT_AFTER_DECISION"
        return None, None, None, None
    age = cutoff - r
    if age > max_age:
        missing[key] = "REFERENCE_STALE"
    return p, r, age, src


def _returns_from_history(history: Any, cutoff: float, missing: dict) -> dict[str, Optional[float]]:
    """Binance aggTrade history (source_ts, price, receipt_ts); ordered by LOCAL receipt, <= cutoff."""
    out: dict[str, Optional[float]] = {f"btc_ret_{h}s": None for h in RETURN_HORIZONS_SEC}
    try:
        pts = sorted(((float(p[2]), float(p[1])) for p in list(history or ())
                      if len(p) >= 3 and p[2] is not None and float(p[2]) <= cutoff), key=lambda x: x[0])
    except Exception:
        pts = []
    if not pts:
        missing["btc_returns"] = "RETURN_HISTORY_EMPTY"
        return out
    now_r, now_p = pts[-1]
    if cutoff - now_r > REFERENCE_MAX_AGE_SEC:
        missing["btc_returns"] = "RETURN_HISTORY_STALE"
        return out
    short = []
    for h in RETURN_HORIZONS_SEC:
        target = now_r - h
        then = None
        for r, p in reversed(pts):
            if r <= target:
                then = p
                break
        if then is None or then <= 0:
            short.append(h)
            continue
        out[f"btc_ret_{h}s"] = now_p / then - 1.0
    if short:
        missing["btc_returns"] = "RETURN_HISTORY_INSUFFICIENT:" + ",".join(str(h) for h in short)
    return out


def decision_features(strategy: Any, *, slug: str, side: Optional[str], inst: Any, strike: Optional[float],
                      bid_size: Optional[float], ask_size: Optional[float], cutoff: float,
                      missing: dict) -> dict[str, Any]:
    """v2 feature groups, copied by value from existing in-memory state.  No I/O, never raises."""
    f: dict[str, Any] = {"feature_cutoff_ts": cutoff, "feature_cutoff_basis": "DECISION_WALL_TS"}
    try:
        if bid_size is not None and ask_size is not None and (bid_size + ask_size) > 0:
            f["top_imbalance"] = (bid_size - ask_size) / (bid_size + ask_size)
        else:
            missing["top_imbalance"] = "DEPTH_MISSING"
        other_key = None
        if side in ("UP", "DOWN"):
            fn = getattr(strategy, "_instrument_for_side", None)
            try:
                from bot.enums import ActiveSide
                other = fn(ActiveSide.DOWN if side == "UP" else ActiveSide.UP) if callable(fn) else None
                other_key = _inst_key(strategy, other) if other is not None else None
            except Exception:
                other_key = None
        if other_key:
            ob = read_bbo(strategy, other_key)
            if ob["receipt_ts"] is not None and ob["receipt_ts"] <= cutoff and ob["bid"] is not None:
                f.update(other_side_bid=ob["bid"], other_side_ask=ob["ask"], other_side_bbo_receipt_ts=ob["receipt_ts"],
                         other_side_bbo_age_sec=cutoff - ob["receipt_ts"])
            else:
                missing["other_side_bbo"] = "OTHER_SIDE_BBO_UNAVAILABLE"
        else:
            missing["other_side_bbo"] = "OTHER_SIDE_INSTRUMENT_UNKNOWN"
        for prefix, price_attr, receipt_attr, source_attr, scale in (
                ("btc_chainlink", "_polymarket_chainlink_price", "_polymarket_chainlink_price_ts",
                 "_polymarket_chainlink_event_ts_ms", 1000.0),
                ("btc_binance", "_binance_ws_price", "_binance_ws_price_ts", "_binance_ws_price_source_ts", 1.0),
                ("btc_twap", "_polymarket_chainlink_twap_price", "_polymarket_chainlink_twap_price_ts",
                 "_polymarket_chainlink_twap_observation_ts", 1.0)):
            src_raw = _num(getattr(strategy, source_attr, None))
            p, r, age, src = _receipt_value(getattr(strategy, price_attr, None), getattr(strategy, receipt_attr, None),
                                            (src_raw / scale) if src_raw is not None else None, cutoff,
                                            REFERENCE_MAX_AGE_SEC, missing, prefix)
            f.update({f"{prefix}_price": p, f"{prefix}_receipt_ts": r, f"{prefix}_age_sec": age,
                      f"{prefix}_source_ts": src})
        ref = _num(getattr(strategy, "latest_external_spot", None))
        f["reference_spot"] = ref
        f["reference_spot_source"] = str(getattr(strategy, "latest_external_spot_source", "") or "") or None
        f.update(_returns_from_history(getattr(strategy, "_prediction_btc_research_history", None), cutoff, missing))
        f["btc_ret_source"] = "binance_ws_aggtrade_local_receipt"
        twap_shadow = getattr(strategy, "twap_forward_shadow", None)
        latest = twap_shadow.latest(slug) if twap_shadow is not None and hasattr(twap_shadow, "latest") else None
        if latest:
            observed = _num(latest.get("observed_ts"))
            if observed is not None and observed <= cutoff:
                f.update(twap_state_projected_side=latest.get("projected_settlement_side_trend"),
                         twap_state_observed_ts=observed, twap_state_age_sec=cutoff - observed)
                if cutoff - observed > TWAP_STATE_MAX_AGE_SEC:
                    missing["twap_state"] = "TWAP_STATE_STALE"
            else:
                missing["twap_state"] = "TWAP_STATE_AFTER_DECISION"
        else:
            missing["twap_state"] = "TWAP_STATE_ABSENT"
        if ref is not None and strike is not None:
            f["reference_minus_strike_usd"] = ref - strike
        twap_now = f.get("btc_twap_price")
        f["twap_state_current"] = twap_now
        if twap_now is not None and strike is not None:
            f["twap_minus_strike_usd"] = twap_now - strike
    except Exception:
        missing["features_v2"] = "FEATURE_CAPTURE_ERROR"
    return f


def build_snapshot(strategy: Any, *, event_type: str, role: str, side_hint: Optional[str], price: Any, qty: Any,
                   client_order_id: Optional[str], reason: Optional[str], payload: dict, run_fingerprint: str,
                   seq: int, wall_ts: float, mono_ns: int) -> CandidateDecisionSnapshot:
    """Copy decision-time state by value.  Every missing field gets a reason code."""
    payload = dict(payload or {})
    missing: dict[str, str] = {}
    slug = str(payload.get("market_slug") or payload.get("slug") or getattr(strategy, "current_market_slug", "") or "")
    inst = (payload.get("submitted_instrument_id") or payload.get("instrument_id") or getattr(strategy, "instrument_id", None))
    side = side_hint or _side_of(strategy, inst, payload)
    key = _inst_key(strategy, inst) if inst else ""
    bbo = read_bbo(strategy, key) if key else read_bbo(strategy, "")
    bid, ask, rts = bbo["bid"], bbo["ask"], bbo["receipt_ts"]
    if rts is not None:
        decision_ts, ts_src = rts, "BBO_RECEIPT_TS"
    else:
        decision_ts, ts_src = wall_ts, "WALL_FALLBACK"
    bbo_age = (wall_ts - rts) if rts is not None else None
    if bid is None or ask is None:
        missing["bbo"] = "BBO_MISSING"
    elif bbo_age is not None and bbo_age > BBO_MAX_AGE_SEC:
        missing["bbo"] = "BBO_STALE"
    q = _num(qty)
    exec_ask = ask if (ask is not None and bbo["ask_size"] is not None and q is not None and bbo["ask_size"] >= q) else None
    exec_bid = bid if (bid is not None and bbo["bid_size"] is not None and q is not None and bbo["bid_size"] >= q) else None
    if exec_ask is None:
        missing["executable_ask"] = "DEPTH_MISSING" if bbo["ask_size"] is None else "DEPTH_INSUFFICIENT"
    # p_ex: latest in-memory prediction snapshot (no as-of rejection by upstream clocks).
    pup = pdn = sigma = psnap_ts = page = lead = None
    pex_reason = None
    snapper = getattr(strategy, "prediction_research_snapshotter", None)
    snap = getattr(snapper, "_last_payload_by_slug", {}).get(slug) if snapper is not None else None
    if snapper is None:
        pex_reason = "PEX_SNAPSHOTTER_ABSENT"
    elif not snap:
        pex_reason = "PEX_NOT_COMPUTED_AT_DECISION"
    else:
        snap = dict(snap)
        psnap_ts = _num(snap.get("snapshot_ts"))
        page = (wall_ts - psnap_ts) if psnap_ts is not None else None
        pup, pdn = _num(snap.get("p_up_ex_market")), _num(snap.get("p_down_ex_market"))
        sigma = _num(snap.get("required_move_sigma"))
        leads = [(_num(snap.get(k)) or 0) - psnap_ts for k in snap
                 if k.endswith("_source_ts") and _num(snap.get(k)) is not None and psnap_ts is not None]
        lead = max(leads) if leads else None
        if pup is None and pdn is None:
            pex_reason = "PEX_NOT_COMPUTED_AT_DECISION"
        elif page is not None and page < 0:
            pex_reason = "PEX_AFTER_DECISION"
            pup = pdn = sigma = None
        elif page is None or page > PEX_MAX_AGE_SEC:
            pex_reason = "PEX_STALE"
            pup = pdn = None
    raw = (pup if side == "UP" else pdn if side == "DOWN" else None)
    if raw is None and pex_reason is None:
        pex_reason = "PEX_SIDE_UNKNOWN"
    if pex_reason:
        missing["raw_p_ex"] = pex_reason
    if sigma is None:
        missing["required_move_sigma"] = "SIGMA_MISSING"
    score = _num(getattr(strategy, "side_decision_score", None))
    if score is None:
        missing["side_score"] = "SCORE_MISSING"
    strike = _num((getattr(strategy, "market_strike_cache_by_slug", None) or {}).get(slug))
    status, status_reason = strike_status_for(strategy, slug)
    spot = _num(getattr(strategy, "latest_external_spot", None))
    dist = None
    if strike is None:
        missing["runtime_strike"] = "STRIKE_MISSING"
    elif spot is not None and side in ("UP", "DOWN"):
        dist = (spot - strike) if side == "UP" else (strike - spot)
    try:
        start = float(slug.rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        start = None
    end = _num(getattr(strategy, "current_market_end_timestamp", None)) or ((start + 900) if start else None)
    tte = (end - decision_ts) if end is not None else None
    if tte is None:
        missing["tte_sec"] = "TTE_UNKNOWN"
    token = None
    try:
        tok_fn = getattr(strategy, "_extract_token_id_from_instrument", None)
        token = str(tok_fn(str(inst))) if callable(tok_fn) and inst else None
    except Exception:
        token = None
    accepted = event_type in ACCEPT_EVENTS
    cid = candidate_id(slug, role, side or "", str(getattr(strategy, "run_id", "") or ""), seq)
    core_ok = all(k not in missing for k in ("bbo", "tte_sec", "side_score")) and side is not None
    feats = decision_features(strategy, slug=slug, side=side, inst=inst, strike=strike, bid_size=bbo["bid_size"],
                              ask_size=bbo["ask_size"], cutoff=wall_ts, missing=missing)
    feats["p_ex_receipt_ts"] = psnap_ts
    # production_p_fair: only the value carried by this decision event (captured atomically);
    # absent or only available after the cutoff -> null with a reason, never recomputed or backfilled.
    p_fair = _num(payload.get("p_fair"))
    p_fair_asof = _num(payload.get("p_fair_asof_ts"))
    p_fair_source = None
    if p_fair is None:
        missing["production_p_fair"] = "PRODUCTION_P_FAIR_NOT_IN_DECISION_EVENT"
    elif p_fair_asof is not None and p_fair_asof > wall_ts:
        p_fair, p_fair_asof = None, None
        missing["production_p_fair"] = "PRODUCTION_P_FAIR_AFTER_DECISION"
    else:
        p_fair_source = str(payload.get("p_fair_source") or "DECISION_EVENT_PAYLOAD")
    feats["production_p_fair_source"] = p_fair_source
    feats["production_p_fair_asof_ts"] = p_fair_asof
    return CandidateDecisionSnapshot(
        candidate_id=cid, candidate_role=role, market_slug=slug, token_id=token, instrument_id=str(inst) if inst else None,
        candidate_side=side, market_start_ts=start, market_end_ts=end, decision_ts=decision_ts,
        decision_ts_source=ts_src, decision_wall_ts=wall_ts, decision_monotonic_ns=mono_ns,
        run_id=str(getattr(strategy, "run_id", "") or ""), run_fingerprint=run_fingerprint, schema_version=SCHEMA_VERSION,
        best_bid=bid, best_ask=ask, mid=((bid + ask) / 2) if (bid is not None and ask is not None) else None,
        spread=(ask - bid) if (bid is not None and ask is not None) else None,
        top_bid_size=bbo["bid_size"], top_ask_size=bbo["ask_size"], executable_ask=exec_ask, executable_bid=exec_bid,
        bbo_receipt_ts=rts, bbo_age_sec=bbo_age, raw_p_ex=raw, p_up_ex_market=pup, p_down_ex_market=pdn,
        pex_snapshot_ts=psnap_ts, pex_age_sec=page, pex_reason=pex_reason, side_score=score,
        side_score_abs=abs(score) if score is not None else None, required_move_sigma=sigma,
        signed_strike_distance_usd=dist, runtime_strike=strike, strike_status=status, strike_status_reason=status_reason,
        source_clock_lead_sec=lead, tte_sec=tte, elapsed_since_open_sec=(decision_ts - start) if start else None,
        production_p_fair=p_fair, proposed_maker_price=_num(price), proposed_qty=q,
        eligibility="ACCEPTED" if accepted else "REJECTED", eligibility_event_type=event_type,
        reject_reason=None if accepted else (reason or event_type), client_order_id=client_order_id,
        alignment_status="ALIGNED" if core_ok else "PARTIAL", missing_reasons=tuple(sorted(missing.items())),
        **feats)


class JsonlSink:
    """Bounded, non-blocking JSONL writer with a disk guard.  Never raises to callers."""

    def __init__(self, directory: Path, *, queue_max: int = DEFAULT_QUEUE_MAX, threaded: bool = True,
                 guard_bytes: int = BOT_DISK_GUARD_BYTES + DISK_RESERVE_BYTES,
                 disk_free_fn: Callable[[str], int] = lambda p: shutil.disk_usage(p).free,
                 warn: Callable[[str], None] = lambda m: None) -> None:
        self.directory = Path(directory)
        self.queue: queue.Queue = queue.Queue(maxsize=queue_max)
        self.counters = {"enqueued": 0, "dropped": 0, "written": 0, "errors": 0, "disk_guard_tripped": 0}
        self.enabled = True
        self._guard_bytes = guard_bytes
        self._disk_free = disk_free_fn
        self._warn = warn
        self._stop = threading.Event()
        self._thread = None
        if threaded:
            self._thread = threading.Thread(target=self._run, name="candidate-telemetry-writer", daemon=True)
            self._thread.start()

    def put(self, record: dict) -> bool:
        if not self.enabled:
            self.counters["dropped"] += 1
            return False
        try:
            self.queue.put_nowait(record)
            self.counters["enqueued"] += 1
            return True
        except queue.Full:
            self.counters["dropped"] += 1
            return False
        except Exception:
            self.counters["errors"] += 1
            return False

    def _check_disk(self) -> bool:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if self._disk_free(str(self.directory)) < self._guard_bytes:
                if self.enabled:
                    self.enabled = False
                    self.counters["disk_guard_tripped"] += 1
                    self._warn("candidate telemetry disabled: free disk below guard + reserve")
                return False
            return True
        except Exception:
            self.counters["errors"] += 1
            return False

    def flush(self, max_items: int = 100000) -> int:
        batch = []
        while len(batch) < max_items:
            try:
                batch.append(self.queue.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return 0
        if not self._check_disk():
            self.counters["dropped"] += len(batch)
            return 0
        try:
            day = datetime.now(timezone.utc).strftime("%Y%m%d")
            path = self.directory / f"candidate_v1_{day}.jsonl"
            with open(path, "a", encoding="utf-8") as fh:
                for rec in batch:
                    fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
            self.counters["written"] += len(batch)
            return len(batch)
        except Exception:
            self.counters["errors"] += 1
            self.counters["dropped"] += len(batch)
            return 0

    def _run(self) -> None:
        while not self._stop.wait(0.5):
            self.flush()
        self.flush()

    def close(self) -> None:
        self._stop.set()


@dataclass
class _MarketState:
    has_accepted: bool = False
    canonical_id: Optional[str] = None
    canonical_coid: Optional[str] = None
    last_eligibility: Optional[tuple] = None
    seq: int = 0
    inst_key: Optional[str] = None
    side: Optional[str] = None
    first_quote: Optional[float] = None
    first_submit_ts: Optional[float] = None
    requotes: list = field(default_factory=list)
    t_touch: Optional[float] = None
    t_through: Optional[float] = None
    withdrawn_ts: Optional[float] = None
    t_touch_after_withdraw: Optional[float] = None
    t_through_after_withdraw: Optional[float] = None
    canonical_fill_ts: Optional[float] = None
    min_ask: Optional[float] = None
    min_ask_ts: Optional[float] = None
    samples: int = 0
    end_ts: Optional[float] = None
    closed: bool = False
    # DRY-RUN paper order chain: {coid, submit_ts, price, qty, state OPEN|CANCELLED|EXPIRED|FILLED|REJECTED, end_reason}
    dry_orders: list = field(default_factory=list)
    terminal: Optional[str] = None
    fill_keys: set = field(default_factory=set)
    fill_cum_qty: float = 0.0


class CandidateTelemetryRecorder:
    """Observer for BUY decision / lifecycle events.  Read-only; no order authority."""

    def __init__(self, *, sink: Any, run_header: dict, fqo_enabled: bool = False, markout_enabled: bool = False,
                 clock: Callable[[], float] = time.time, mono: Callable[[], int] = time.monotonic_ns,
                 sample_interval_sec: float = DEFAULT_SAMPLE_INTERVAL_SEC) -> None:
        self.sink = sink
        self.run_header = dict(run_header)
        self.fingerprint = str(run_header.get("run_fingerprint") or "")
        self.fqo_enabled = bool(fqo_enabled)
        self.markout_enabled = bool(markout_enabled)
        self.clock, self.mono = clock, mono
        self.sample_interval_sec = float(sample_interval_sec)
        self._lock = threading.Lock()
        self._markets: dict[str, _MarketState] = {}
        self._markouts: list[tuple[float, dict]] = []
        self.counters = {"observed": 0, "candidates": 0, "lifecycle": 0, "exceptions": 0, "markouts": 0, "paths": 0,
                         "sim_fills": 0, "sim_fill_unmapped": 0, "sim_fill_duplicates": 0, "sim_fill_late": 0,
                         "terminals": 0}
        self.execution_mode = run_header.get("execution_mode")
        self._pending_paper_entry: dict[str, dict] = {}
        self.tags = {"execution_mode": self.execution_mode, "evidence_tier": run_header.get("evidence_tier"),
                     "plumbing_revision": run_header.get("plumbing_revision")}
        self._strategy_ref = None
        self._stop = threading.Event()
        self._sampler = None
        self._emit(self.run_header)

    # ------------------------------------------------------------ helpers
    def _emit(self, rec: dict) -> None:
        try:
            self.sink.put(rec)
        except Exception:
            self.counters["exceptions"] += 1

    def start_sampler(self, strategy: Any) -> None:
        self._strategy_ref = strategy
        if not (self.fqo_enabled or self.markout_enabled) or self._sampler is not None:
            return
        self._sampler = threading.Thread(target=self._sample_loop, name="candidate-telemetry-sampler", daemon=True)
        self._sampler.start()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ decision events
    def observe_order_event(self, strategy: Any, *, event_type: str, side: Optional[str], price: Any, qty: Any,
                            client_order_id: Optional[str], reason: Optional[str], payload: Optional[dict]) -> None:
        """Called synchronously from ``_db_order_event``.  Never raises."""
        try:
            self.counters["observed"] += 1
            self._strategy_ref = strategy
            et = str(event_type or "")
            s = str(side or "").upper()
            is_buy_decision = (et in ACCEPT_EVENTS and s == "BUY") or (et.startswith(REJECT_PREFIXES) and s in ("BUY", ""))
            if is_buy_decision:
                self._on_decision(strategy, et, price, qty, client_order_id, reason, payload or {})
            elif et in LIFECYCLE_EVENTS:
                self._on_lifecycle(strategy, et, s, price, qty, client_order_id, reason, payload or {})
            elif et == SIM_FILL_EVENT and self.execution_mode == EXECUTION_MODE_DRY_RUN:
                self._on_sim_fill(price, qty, client_order_id, payload or {})
            elif et == SIM_EXPIRE_EVENT and self.execution_mode == EXECUTION_MODE_DRY_RUN:
                self._on_sim_expire(payload or {})
            elif et in SIM_ENTRY_EVENTS and self.execution_mode == EXECUTION_MODE_DRY_RUN:
                p = payload or {}
                slug = str(p.get("slug") or p.get("market_slug") or getattr(strategy, "current_market_slug", "") or "")
                self._pending_paper_entry[slug] = {"order_key": p.get("order_key"), "fair": p.get("fair"),
                                                   "planned_quote_ts": p.get("planned_quote_ts")}
        except Exception:
            self.counters["exceptions"] += 1

    def _on_decision(self, strategy, et, price, qty, coid, reason, payload) -> None:
        wall, mono = self.clock(), self.mono()
        slug = str((payload or {}).get("market_slug") or (payload or {}).get("slug") or
                   getattr(strategy, "current_market_slug", "") or "")
        if not slug:
            return
        accepted = et in ACCEPT_EVENTS
        elig = ("ACCEPTED" if accepted else "REJECTED", None if accepted else (reason or et))
        if et == DRY_RUN_SUBMIT_EVENT and (payload or {}).get("p_fair") is None:
            # The DRY-RUN submit event does not carry p_fair; the paper order journaled in the same submit
            # call does (same directional snapshot).  Joined by order_key, consumed once, never reused.
            paper = self._pending_paper_entry.pop(slug, None)
            if paper and paper.get("fair") is not None and paper.get("order_key") == (payload or {}).get("order_key"):
                payload = {**payload, "p_fair": paper["fair"], "p_fair_asof_ts": paper.get("planned_quote_ts"),
                           "p_fair_source": "DRY_RUN_PAPER_ORDER_EVENT"}
        with self._lock:
            st = self._markets.setdefault(slug, _MarketState())
            first_eval = st.seq == 0
            if accepted and not st.has_accepted:
                role = "FIRST_ACCEPTED"
            elif first_eval:
                role = "FIRST_EVALUATION"
            elif elig != st.last_eligibility:
                role = "TRANSITION"
            else:
                role = None
            if accepted and st.has_accepted and coid and coid != st.canonical_coid:
                st.requotes.append({"ts": wall, "price": _num(price), "client_order_id": coid})
                if st.withdrawn_ts is None:
                    st.withdrawn_ts = wall
            requoted = self._dry_chain_submit(st, et, coid, price, qty, wall) if accepted else None
            st.last_eligibility = elig
            st.seq += 1
            seq = st.seq
        if requoted is not None:
            self._emit(requoted)
        if role is None:
            return
        snap = build_snapshot(strategy, event_type=et, role=role, side_hint=None, price=price, qty=qty,
                              client_order_id=coid, reason=reason, payload=payload, run_fingerprint=self.fingerprint,
                              seq=seq, wall_ts=wall, mono_ns=mono)
        with self._lock:
            st = self._markets[slug]
            if role == "FIRST_ACCEPTED":
                st.has_accepted = True
                st.canonical_id = snap.candidate_id
                st.canonical_coid = coid
                st.first_quote = _num(price)
                st.first_submit_ts = wall
                st.side = snap.candidate_side
                st.inst_key = _inst_key(strategy, snap.instrument_id) if snap.instrument_id else None
                st.end_ts = snap.market_end_ts
                if self.markout_enabled:
                    self._schedule_markouts(slug, "FIRST_SUBMIT", wall, snap.candidate_id, coid)
            elif role == "FIRST_EVALUATION" and st.canonical_id is None:
                st.canonical_id = snap.candidate_id
        self.counters["candidates"] += 1
        payload_out = snap.as_payload()
        payload_out["is_first_evaluation"] = bool(first_eval)
        self._emit(payload_out)

    def _on_lifecycle(self, strategy, et, side, price, qty, coid, reason, payload) -> None:
        wall, mono = self.clock(), self.mono()
        slug = str(payload.get("market_slug") or payload.get("slug") or getattr(strategy, "current_market_slug", "") or "")
        with self._lock:
            st = self._markets.get(slug)
            cid = st.canonical_id if st else None
            is_canonical_fill = bool(st and et in ("ORDER_FILLED", "ORDER_DRY_RUN_FILLED") and side == "BUY"
                                     and st.has_accepted)
            if st and st.has_accepted and coid and coid == st.canonical_coid:
                if et in ("ORDER_FILLED", "ORDER_DRY_RUN_FILLED") and st.canonical_fill_ts is None:
                    st.canonical_fill_ts = wall
                elif et in ("ORDER_CANCELED", "ORDER_DRY_RUN_CANCELLED", "ORDER_EXPIRED", "ORDER_REJECTED") \
                        and st.withdrawn_ts is None:
                    st.withdrawn_ts = wall
            order = self._dry_order(st, coid) if st else None
            if order is not None and order["state"] == "OPEN" and st.terminal is None:
                if et == "ORDER_DRY_RUN_CANCELLED":
                    order.update(state="CANCELLED", end_reason=reason)
                elif et in ("ORDER_REJECTED", "ORDER_DRY_RUN_REJECTED"):
                    order.update(state="REJECTED", end_reason=reason)
        if st is None:
            return
        self.counters["lifecycle"] += 1
        rec = {"record_type": "LIFECYCLE", "schema_version": SCHEMA_VERSION, "candidate_id": cid,
               "market_slug": slug, "event_type": et, "side": side or None, "price": _num(price), "qty": _num(qty),
               "client_order_id": coid, "reason": reason, "wall_ts": wall, "monotonic_ns": mono,
               "run_fingerprint": self.fingerprint, **self.tags}
        if order is not None:
            # Order-level DRY-RUN event; the candidate's single terminal state is a separate record.
            rec.update(lifecycle_event="ORDER_CANCEL" if et == "ORDER_DRY_RUN_CANCELLED" else "ORDER_REJECT",
                       terminal=False)
        self._emit(rec)
        if is_canonical_fill and self.markout_enabled:
            self._schedule_markouts(slug, "FILL", wall, cid, coid)

    # ------------------------------------------------------------ path stats + markouts
    # ------------------------------------------------------------ DRY-RUN paper order chain
    @staticmethod
    def _dry_order(st: _MarketState, coid: Optional[str]) -> Optional[dict]:
        if not coid:
            return None
        return next((o for o in st.dry_orders if o["coid"] == coid), None)

    def _lifecycle_base(self, st: _MarketState, slug: str, coid: Optional[str], wall: float) -> dict:
        return {"record_type": "LIFECYCLE", "schema_version": SCHEMA_VERSION, "candidate_id": st.canonical_id,
                "run_id": self.run_header.get("run_id"), "market_slug": slug, "client_order_id": coid,
                "canonical_client_order_id": st.canonical_coid, "candidate_side": st.side, "wall_ts": wall,
                "monotonic_ns": self.mono(), "run_fingerprint": self.fingerprint, "simulation": True, **self.tags}

    def _dry_chain_submit(self, st: _MarketState, et: str, coid: Optional[str], price: Any, qty: Any,
                          wall: float) -> Optional[dict]:
        """Caller holds the lock.  Returns a REQUOTED record when a later order replaces an earlier one."""
        if et != DRY_RUN_SUBMIT_EVENT or not coid or self._dry_order(st, coid) is not None or st.terminal is not None:
            return None
        prev = st.dry_orders[-1] if st.dry_orders else None
        st.dry_orders.append({"coid": coid, "submit_ts": wall, "price": _num(price), "qty": _num(qty),
                              "state": "OPEN", "end_reason": None})
        if prev is None:
            return None
        end = {"CANCELLED": "REQUOTE_CANCEL", "EXPIRED": "EXPIRED_THEN_REQUOTED", "REJECTED": "REJECTED_THEN_REQUOTED",
               "OPEN": "REPLACED_WITHOUT_CANCEL"}.get(prev["state"], prev["state"])
        if prev["state"] == "OPEN":
            prev.update(state="CANCELLED", end_reason="REPLACED_WITHOUT_CANCEL")
        slug = next((s for s, m in self._markets.items() if m is st), "")
        return {**self._lifecycle_base(st, slug, coid, wall), "event_type": DRY_RUN_SUBMIT_EVENT,
                "lifecycle_event": "REQUOTED", "terminal": False, "replaces_client_order_id": prev["coid"],
                "replaced_order_end": end, "replaced_order_end_reason": prev.get("end_reason"),
                "price": _num(price), "qty": _num(qty), "side": "BUY"}

    def _on_sim_fill(self, price: Any, qty: Any, sim_id: Optional[str], payload: dict) -> None:
        """SHADOW_SIM_ENTRY_FILLED -> LIFECYCLE SIMULATED_FILL on the existing candidate (never a new identity)."""
        wall = self.clock()
        slug = str(payload.get("slug") or payload.get("market_slug") or "")
        coid = payload.get("dry_run_client_order_id")
        fill_seq = int(payload.get("fill_seq") or 1)
        q = _num(qty) if _num(qty) is not None else _num(payload.get("qty"))
        schedule = None
        with self._lock:
            st = self._markets.get(slug)
            order = self._dry_order(st, coid) if st is not None else None
            if order is None:
                # No stable key, or a key outside this candidate's order chain: report, never guess.
                self.counters["sim_fill_unmapped"] += 1
                rec = {"record_type": "LIFECYCLE", "schema_version": SCHEMA_VERSION, "candidate_id": None,
                       "market_slug": slug, "event_type": SIM_FILL_EVENT, "lifecycle_event": "SIMULATED_FILL_UNMAPPED",
                       "terminal": False, "client_order_id": coid, "simulation_id": sim_id, "price": _num(price),
                       "qty": q, "wall_ts": wall, "run_fingerprint": self.fingerprint, "simulation": True,
                       "source_event": SIM_FILL_EVENT, **self.tags}
            elif (coid, fill_seq) in st.fill_keys:
                self.counters["sim_fill_duplicates"] += 1
                return
            elif st.terminal is not None and st.terminal != "SIMULATED_FILL":
                self.counters["sim_fill_late"] += 1
                return
            else:
                st.fill_keys.add((coid, fill_seq))
                first = st.terminal is None
                st.fill_cum_qty += float(q or 0.0)
                if first:
                    st.terminal = "SIMULATED_FILL"
                    order.update(state="FILLED", end_reason="SIMULATED_FILL")
                    if coid == st.canonical_coid and st.canonical_fill_ts is None:
                        st.canonical_fill_ts = wall
                    self.counters["terminals"] += 1
                    if self.markout_enabled:
                        schedule = (slug, wall, st.canonical_id, coid, st.end_ts)
                self.counters["sim_fills"] += 1
                fill_ts = _num(payload.get("filled_ts"))
                rec = {**self._lifecycle_base(st, slug, coid, wall), "event_type": SIMULATED_FILL_EVENT_TYPE,
                       "lifecycle_event": "SIMULATED_FILL", "terminal": first, "side": "BUY",
                       "instrument_id": payload.get("instrument_id"), "submit_ts": order["submit_ts"],
                       # Event time (simulator quote receipt) and local receipt time; ordering uses wall_ts.
                       "fill_event_ts": fill_ts, "price": _num(price), "qty": q, "fill_seq": fill_seq,
                       "cumulative_qty": st.fill_cum_qty, "is_canonical_order": coid == st.canonical_coid,
                       "simulation_id": sim_id, "source_event": SIM_FILL_EVENT, "reason": payload.get("status")}
        self._emit(rec)
        if schedule is not None:
            slug, t0, cid, coid, end_ts = schedule
            self._schedule_markouts(slug, "SIMULATED_FILL", t0, cid, coid, market_end_ts=end_ts)

    def _on_sim_expire(self, payload: dict) -> None:
        wall = self.clock()
        slug = str(payload.get("slug") or payload.get("market_slug") or "")
        coid = payload.get("dry_run_client_order_id")
        with self._lock:
            st = self._markets.get(slug)
            order = self._dry_order(st, coid) if st is not None else None
            if order is None or order["state"] != "OPEN" or st.terminal is not None:
                return
            order.update(state="EXPIRED", end_reason="SIM_ORDER_TTL")
            rec = {**self._lifecycle_base(st, slug, coid, wall), "event_type": SIM_EXPIRE_EVENT,
                   "lifecycle_event": "ORDER_EXPIRE", "terminal": False, "source_event": SIM_EXPIRE_EVENT}
        self._emit(rec)

    def _dry_terminal_on_close(self, slug: str, st: _MarketState, now: float) -> Optional[dict]:
        """Caller holds the lock.  Exactly one terminal state per accepted DRY-RUN candidate."""
        if not st.dry_orders or st.terminal is not None:
            return None
        last = st.dry_orders[-1]
        reason = last.get("end_reason")
        if last["state"] == "OPEN":
            kind, why = "SIMULATED_EXPIRE", "MARKET_ENDED"
        elif last["state"] == "EXPIRED":
            kind, why = "SIMULATED_EXPIRE", "SIM_ORDER_TTL"
        elif last["state"] == "REJECTED":
            kind, why = "REJECTED", reason
        elif str(reason or "").endswith(":ttl"):
            kind, why = "SIMULATED_EXPIRE", "ORDER_TTL"
        else:
            kind, why = "SIMULATED_CANCEL", reason
        st.terminal = kind
        self.counters["terminals"] += 1
        return {**self._lifecycle_base(st, slug, last["coid"], now), "event_type": DRY_RUN_TERMINAL_EVENT_TYPE,
                "lifecycle_event": kind, "terminal": True, "terminal_reason": why, "side": "BUY",
                "price": last.get("price"), "qty": last.get("qty"), "orders_in_chain": len(st.dry_orders),
                "market_end_ts": st.end_ts}

    def _schedule_markouts(self, slug, anchor, t0, cid, coid, market_end_ts=None) -> None:
        for h in MARKOUT_HORIZONS_SEC:
            m = {"slug": slug, "anchor": anchor, "anchor_type": anchor, "horizon_sec": h, "anchor_ts": t0,
                 "candidate_id": cid, "client_order_id": coid}
            if market_end_ts is not None:
                m["market_end_ts"] = market_end_ts
            self._markouts.append((t0 + h, m))

    def sample(self, strategy: Any = None, now: Optional[float] = None) -> None:
        """One sampling pass (called by the sampler thread; callable directly in tests)."""
        try:
            strategy = strategy if strategy is not None else self._strategy_ref
            if strategy is None:
                return
            now = self.clock() if now is None else float(now)
            due, keep = [], []
            with self._lock:
                for t, m in self._markouts:
                    (due if t <= now else keep).append((t, m))
                self._markouts = keep
                states = list(self._markets.items())
            for _, m in due:
                if m.get("market_end_ts") is not None and m["anchor_ts"] + m["horizon_sec"] > m["market_end_ts"]:
                    self.counters["markouts"] += 1
                    self._emit({"record_type": "MARKOUT", "schema_version": SCHEMA_VERSION, **m, "sample_ts": now,
                                "best_bid": None, "best_ask": None, "mid": None, "bbo_age_sec": None,
                                "reason": "MARKET_ENDED", "run_fingerprint": self.fingerprint, **self.tags})
                    continue
                st = dict(states).get(m["slug"])
                key = st.inst_key if st else None
                bbo = read_bbo(strategy, key) if key else {"bid": None, "ask": None, "receipt_ts": None}
                age = (now - bbo["receipt_ts"]) if bbo.get("receipt_ts") is not None else None
                stale = age is None or age > BBO_MAX_AGE_SEC or bbo.get("bid") is None or bbo.get("ask") is None
                self.counters["markouts"] += 1
                self._emit({"record_type": "MARKOUT", "schema_version": SCHEMA_VERSION, **m, "sample_ts": now,
                            "best_bid": None if stale else bbo["bid"], "best_ask": None if stale else bbo["ask"],
                            "mid": None if stale else (bbo["bid"] + bbo["ask"]) / 2, "bbo_age_sec": age,
                            "reason": ("BBO_MISSING" if bbo.get("receipt_ts") is None else "BBO_STALE") if stale else None,
                            "run_fingerprint": self.fingerprint, **self.tags})
            if self.fqo_enabled:
                for slug, st in states:
                    self._sample_path(strategy, slug, st, now)
            self._prune(now)
        except Exception:
            self.counters["exceptions"] += 1

    def _prune(self, now: float) -> None:
        """Bound memory: drop finished markets (closed or 120 s past end) beyond the newest few."""
        for slug, st in list(self._markets.items()):
            if not st.closed and st.first_quote is not None and st.end_ts is not None and now >= st.end_ts:
                self.close_market(slug, now)
        with self._lock:
            done = [s for s, st in self._markets.items()
                    if st.closed or (st.end_ts is not None and now > st.end_ts + PRUNE_AFTER_END_SEC)]
            if len(self._markets) > MAX_TRACKED_MARKETS:
                for s in done[: len(self._markets) - MAX_TRACKED_MARKETS]:
                    self._markets.pop(s, None)

    def _sample_path(self, strategy, slug, st: _MarketState, now: float) -> None:
        if st.closed or st.first_quote is None or st.inst_key is None:
            return
        if st.end_ts is not None and now >= st.end_ts:
            self.close_market(slug, now)
            return
        bbo = read_bbo(strategy, st.inst_key)
        ask, rts = bbo.get("ask"), bbo.get("receipt_ts")
        if ask is None or rts is None or (now - rts) > BBO_MAX_AGE_SEC:
            return
        with self._lock:
            st.samples += 1
            if st.min_ask is None or ask < st.min_ask:
                st.min_ask, st.min_ask_ts = ask, rts
            if st.t_touch is None and ask <= st.first_quote + 1e-9:
                st.t_touch = rts
            if st.t_through is None and ask <= st.first_quote - TICK + 1e-9:
                st.t_through = rts
            if st.withdrawn_ts is not None and rts >= st.withdrawn_ts:
                if st.t_touch_after_withdraw is None and ask <= st.first_quote + 1e-9:
                    st.t_touch_after_withdraw = rts
                if st.t_through_after_withdraw is None and ask <= st.first_quote - TICK + 1e-9:
                    st.t_through_after_withdraw = rts

    def close_market(self, slug: str, now: Optional[float] = None) -> None:
        with self._lock:
            st = self._markets.get(slug)
            if st is None or st.closed or st.first_quote is None:
                return
            st.closed = True
            rec = {"record_type": "PATH_STATS", "schema_version": SCHEMA_VERSION, "candidate_id": st.canonical_id,
                   "market_slug": slug, "client_order_id": st.canonical_coid, "first_quote_price": st.first_quote,
                   "first_submit_ts": st.first_submit_ts, "requotes": list(st.requotes), "t_touch": st.t_touch,
                   "t_through": st.t_through, "min_ask_after_submit": st.min_ask, "min_ask_ts": st.min_ask_ts,
                   "first_quote_withdrawn_ts": st.withdrawn_ts, "canonical_fill_ts": st.canonical_fill_ts,
                   "t_touch_after_withdraw": st.t_touch_after_withdraw,
                   "t_through_after_withdraw": st.t_through_after_withdraw,
                   "path_sample_interval_sec": self.sample_interval_sec, "path_samples": st.samples,
                   "path_end_ts": st.end_ts if st.end_ts is not None else now, "fqo_enabled": self.fqo_enabled,
                   "run_fingerprint": self.fingerprint, **self.tags}
            terminal = self._dry_terminal_on_close(slug, st, self.clock() if now is None else float(now))
        self.counters["paths"] += 1
        self._emit(rec)
        if terminal is not None:
            self._emit(terminal)

    def _sample_loop(self) -> None:
        while not self._stop.wait(self.sample_interval_sec):
            self.sample()


def flags_from_env(env: Optional[dict] = None) -> dict[str, bool]:
    env = os.environ if env is None else env
    def b(name):
        return str(env.get(name, "0")).strip().lower() in ("1", "true", "yes", "on")
    return {"CANDIDATE_DECISION_TELEMETRY_ENABLED": b("CANDIDATE_DECISION_TELEMETRY_ENABLED"),
            "FIRST_QUOTE_ONLY_SHADOW_ENABLED": b("FIRST_QUOTE_ONLY_SHADOW_ENABLED"),
            "HELD_SIDE_MARKOUT_TELEMETRY_ENABLED": b("HELD_SIDE_MARKOUT_TELEMETRY_ENABLED")}


def candidate_telemetry_dir(project_root: Path, env: Optional[dict] = None) -> Path:
    """CANDIDATE_TELEMETRY_DIR (repo-root anchored) or the default data/research/candidate_telemetry."""
    raw = str((os.environ if env is None else env).get("CANDIDATE_TELEMETRY_DIR") or "").strip()
    if not raw:
        return Path(project_root) / "data/research/candidate_telemetry"
    p = Path(raw).expanduser()
    return p if p.is_absolute() else Path(project_root) / p


def build_candidate_telemetry(strategy: Any, *, candidate: bool, fqo: bool, markout: bool,
                              directory: Optional[Path] = None, warn: Callable[[str], None] = lambda m: None
                              ) -> Optional[CandidateTelemetryRecorder]:
    """Constructed only when CANDIDATE_DECISION_TELEMETRY_ENABLED is on (FQO/markout need it)."""
    if not candidate:
        if fqo or markout:
            warn("FIRST_QUOTE_ONLY_SHADOW_ENABLED / HELD_SIDE_MARKOUT_TELEMETRY_ENABLED ignored: "
                 "CANDIDATE_DECISION_TELEMETRY_ENABLED is off")
        return None
    try:
        root = Path(directory) if directory else candidate_telemetry_dir(Path(getattr(strategy, "project_root", ".")))
        sink = JsonlSink(root, warn=warn)
        flags = {"CANDIDATE_DECISION_TELEMETRY_ENABLED": True, "FIRST_QUOTE_ONLY_SHADOW_ENABLED": bool(fqo),
                 "HELD_SIDE_MARKOUT_TELEMETRY_ENABLED": bool(markout),
                 "EARLY_CANCEL_SHADOW_ENABLED": bool(getattr(getattr(strategy, "entry_shadow", None), "c_enabled", False)),
                 "MAKER_CHASE_SHADOW_ENABLED": bool(getattr(getattr(strategy, "entry_shadow", None), "b_enabled", False)),
                 "PRICE_ADJUSTED_EDGE_SHADOW_ENABLED": bool(getattr(getattr(strategy, "entry_shadow", None), "d_enabled", False))}
        rec = CandidateTelemetryRecorder(sink=sink, run_header=build_run_header(strategy, flags),
                                         fqo_enabled=fqo, markout_enabled=markout)
        rec.start_sampler(strategy)
        return rec
    except Exception as exc:  # never let research telemetry break startup
        warn(f"candidate telemetry disabled: {type(exc).__name__}: {exc}")
        return None


def candidate_telemetry_summary(flags: dict[str, bool]) -> str:
    return "Entry V3 candidate telemetry (research only, no order authority): " + " ".join(
        f"{k}={int(v)}" for k, v in sorted(flags.items()))


def reconcile_dry_run_fills(records: Iterable[dict], journal_rows: Iterable[dict]) -> dict[str, Any]:
    """Offline 1:1 reconciliation of journal paper fills vs candidate telemetry (pure; no I/O).

    ``journal_rows``: order_events rows with ``event_type`` / ``price`` / ``qty`` / ``payload``.
    A journal fill joins a SIMULATED_FILL only by the paper order's ``dry_run_client_order_id``.
    """
    records = list(records)
    jfill = {}
    jfill_without_key = 0
    for r in journal_rows:
        if r.get("event_type") != SIM_FILL_EVENT:
            continue
        payload = r.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload or "{}")
        key = payload.get("dry_run_client_order_id")
        if not key:
            jfill_without_key += 1
            continue
        jfill[key] = {"price": _num(r.get("price")), "qty": _num(r.get("qty")), "filled_ts": _num(payload.get("filled_ts"))}
    tfill = {r.get("client_order_id"): r for r in records if r.get("record_type") == "LIFECYCLE"
             and r.get("lifecycle_event") == "SIMULATED_FILL" and int(r.get("fill_seq") or 1) == 1}
    matched = set(jfill) & set(tfill)
    mismatches = sum(1 for k in matched if (jfill[k]["price"], jfill[k]["qty"], jfill[k]["filled_ts"]) !=
                     (_num(tfill[k].get("price")), _num(tfill[k].get("qty")), _num(tfill[k].get("fill_event_ts"))))
    accepted = {r.get("market_slug"): r.get("candidate_id") for r in records
                if r.get("record_type") == "CANDIDATE" and r.get("candidate_role") == "FIRST_ACCEPTED"}
    closed = {r.get("market_slug") for r in records if r.get("record_type") == "PATH_STATS"}
    terminals: dict[str, list] = {}
    for r in records:
        if r.get("record_type") == "LIFECYCLE" and r.get("terminal") is True:
            terminals.setdefault(r.get("market_slug"), []).append(r.get("lifecycle_event"))
    accepted_closed = [s for s in accepted if s in closed]
    return {
        "journal_fills": len(jfill) + jfill_without_key, "journal_fills_without_key": jfill_without_key,
        "telemetry_simulated_fills": len(tfill), "matched": len(matched),
        "join_completeness": (len(matched) / (len(jfill) + jfill_without_key)) if (jfill or jfill_without_key) else None,
        "unmatched_journal_fills": len(set(jfill) - set(tfill)) + jfill_without_key,
        "unmatched_telemetry_fills": len(set(tfill) - set(jfill)), "field_mismatches": mismatches,
        "accepted_candidates": len(accepted), "accepted_closed": len(accepted_closed),
        "accepted_closed_with_one_terminal": sum(1 for s in accepted_closed if len(terminals.get(s, [])) == 1),
        "accepted_without_terminal_closed": sum(1 for s in accepted_closed if not terminals.get(s)),
        "accepted_with_multiple_terminals": sum(1 for s in accepted if len(terminals.get(s, [])) > 1),
        "accepted_still_open": sum(1 for s in accepted if s not in closed and not terminals.get(s)),
        "terminal_kinds": dict(sorted({k: sum(1 for v in terminals.values() for x in v if x == k)
                                       for k in TERMINAL_LIFECYCLE_EVENTS}.items())),
    }
