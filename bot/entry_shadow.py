"""E5B entry shadow telemetry: D (price-adjusted edge), C (early cancel), B (maker chase).

SHADOW ONLY. This module has no access to orders: it never imports the strategy,
the execution client or Nautilus order types, and its only output is a telemetry
sink callable.  Decisions are labels (WOULD_CANCEL / WOULD_CHASE / ...) for later
research; nothing here can submit, cancel, replace, buy or sell.

Frozen definitions (docs/research/e5b_d_prereg.md and
reports/entry_research_e5_maker_selection_20261011/e5_prereg.md, sha 8cb7cc33...):

* Canonical attempt = the first maker BUY order submitted in a market that is not
  rejected (post-only rejections are skipped).  Later BUY orders in the same
  market are linked sub-orders, never new attempts.
* Placement baseline a0 = the side's best ask observed at or before placement
  (receipt age <= 3 s).
* Observation at horizon h = the latest quote of the side received at or before
  place+h with age <= 1.5 s (h <= 5) or 3 s (h > 5); otherwise CENSORED -> UNKNOWN.
* C = EC1@15s: canonical order not filled by place+15 and ask_15 - a0 <= -0.01.
* B = CH_B@10s: canonical order not filled by place+10, ask_10 - a0 >= +0.05,
  signed side_score_10 >= 0 and spread_10 (ask - bid of the side) <= 0.03.
  Execution economics use the ask at decision + L (L = 2.4 s; sensitivity 3.6 s),
  observed later and never fed back into the decision.
* D = edge_cal_v1: p_cal = ask + shrunk (win - ask) residual of the ask x TTE cell; RAW / NET edges.
"""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

SCHEMA_VERSION = "entry_shadow_v1"
FORWARD_CUTOFF_TS = 1791691110
D_HORIZONS_SEC = (0, 3, 5, 10, 15, 30)
C_HORIZON_SEC = 15
B_HORIZON_SEC = 10
SHORT_HORIZON_MAX_SEC = 5
TOL_SHORT_SEC = 1.5
TOL_LONG_SEC = 3.0
PLACEMENT_TOL_SEC = 3.0
LATENCY_SEC = 2.4
LATENCY_SENSITIVITY_SEC = 3.6
EC1_ASK_DROP = Decimal("-0.01")
CHB_ASK_RISE = Decimal("0.05")
CHB_MAX_SPREAD = Decimal("0.03")
CHASE_MAX_PRICE = Decimal("0.97")
PENALTY_NONE = 0.0
PENALTY_REFERENCE = 0.018
EDGE_CAL_ASK_EDGES = (0.0, 0.50, 0.65, 0.75, 0.85, 0.97)
EDGE_CAL_TTE_SPLIT_SEC = 480.0
EDGE_CAL_SHRINK_K = 20
EDGE_CAL_MIN_MARKETS = 20
EDGE_CAL_MIN_DAYS = 3
DEFAULT_CALIBRATION_PATH = Path(__file__).resolve().parents[1] / "config/research/entry_edge_calibration_v1.json"
MAX_ATTEMPTS = 16
MAX_SAMPLES_PER_ATTEMPT = 128
ATTEMPT_TTL_SEC = 30 + TOL_LONG_SEC + LATENCY_SENSITIVITY_SEC + 5.0

UNKNOWN = "UNKNOWN"
NOT_APPLICABLE = "NOT_APPLICABLE"
_TAIPEI = ZoneInfo("Asia/Taipei")


def _d(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value))
    except Exception:
        return None
    return out if out.is_finite() else None


def _f(value: Any) -> Optional[float]:
    d = _d(value)
    return float(d) if d is not None else None


def market_day_type(slug: str) -> str:
    """Taipei WEEKDAY/WEEKEND of the market start. Evaluation stratum only."""
    try:
        ts = float(int(str(slug).rsplit("-", 1)[-1]))
    except (TypeError, ValueError):
        return UNKNOWN
    return "WEEKEND" if datetime.fromtimestamp(ts, timezone.utc).astimezone(_TAIPEI).weekday() >= 5 else "WEEKDAY"


def tolerance_for(horizon_sec: float) -> float:
    return TOL_SHORT_SEC if horizon_sec <= SHORT_HORIZON_MAX_SEC else TOL_LONG_SEC


# ------------------------------------------------------------------ snapshot
@dataclass(frozen=True)
class EntryShadowSnapshot:
    """One immutable observation consumed identically by D, C and B."""

    schema_version: str
    timestamp: float
    attempt_id: str
    market: str
    instrument: str
    side: str
    horizon_sec: float
    tte_sec: Optional[float]
    side_score: Optional[float]
    side_score_signed: Optional[float]
    entry_side_score_signed: Optional[float]
    strike: Optional[float]
    strike_verified: Optional[bool]
    quote_receipt_ts: Optional[float]
    quote_age_at_horizon_sec: Optional[float]
    observation_state: str  # OBSERVED | CENSORED
    best_bid: Optional[float]
    best_ask: Optional[float]
    spread: Optional[float]
    top_ask_size: Optional[float]
    top_bid_size: Optional[float]
    l2_age_sec: Optional[float]
    binance_spot: Optional[float]
    binance_move_bps_signed: Optional[float]
    chainlink_spot: Optional[float]
    chainlink_age_sec: Optional[float]
    chainlink_move_bps_signed: Optional[float]
    twap_value: Optional[float]
    twap_state: Optional[str]
    signed_distance_usd: Optional[float]
    placement_ask: Optional[float]
    placement_bid: Optional[float]
    planned_maker_price: Optional[float]
    planned_qty: Optional[float]
    maker_age_sec: Optional[float]
    maker_filled_qty: Optional[float]
    maker_status: str
    canonical_filled_by_horizon: bool
    market_filled_by_horizon: bool
    production_action: str
    market_day_type: str

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QuoteSample:
    """Raw read-only market state captured from the runtime at one tick."""

    sample_ts: float
    quote_receipt_ts: Optional[float]
    best_bid: Optional[Decimal]
    best_ask: Optional[Decimal]
    top_bid_size: Optional[Decimal] = None
    top_ask_size: Optional[Decimal] = None
    side_score: Optional[float] = None
    tte_sec: Optional[float] = None
    strike: Optional[float] = None
    strike_verified: Optional[bool] = None
    binance_spot: Optional[float] = None
    chainlink_spot: Optional[float] = None
    chainlink_age_sec: Optional[float] = None
    twap_value: Optional[float] = None
    twap_state: Optional[str] = None
    signed_distance_usd: Optional[float] = None
    l2_age_sec: Optional[float] = None
    ask_levels: Optional[tuple] = None  # ((price, size), ...) best first, if fresh


def asof_sample(samples: Any, target_ts: float, tolerance_sec: float) -> Optional[QuoteSample]:
    """Latest sample whose quote was received at or before target_ts, within tolerance."""
    best = None
    for s in samples:
        rts = s.quote_receipt_ts
        if rts is None or rts > target_ts:
            continue
        if best is None or rts > best.quote_receipt_ts:
            best = s
    if best is None or target_ts - best.quote_receipt_ts > tolerance_sec:
        return None
    if best.best_ask is None or best.best_ask <= 0:
        return None
    return best


# ------------------------------------------------------------------ D: edge
def edge_cell(ask: float, tte_sec: float) -> Optional[str]:
    if ask is None or tte_sec is None or not (EDGE_CAL_ASK_EDGES[0] < ask < EDGE_CAL_ASK_EDGES[-1]):
        return None
    for lo, hi in zip(EDGE_CAL_ASK_EDGES, EDGE_CAL_ASK_EDGES[1:]):
        if lo <= ask < hi:
            tte = "le480" if tte_sec <= EDGE_CAL_TTE_SPLIT_SEC else "gt480"
            return f"ask_{lo:.2f}_{hi:.2f}|tte_{tte}"
    return None


class EdgeCalibration:
    """Preloaded, pure-arithmetic calibration table (no inference in the hot path)."""

    def __init__(self, doc: Optional[dict[str, Any]]) -> None:
        self.doc = doc or {}
        self.version = str(self.doc.get("version") or "edge_cal_missing")
        self.cells = dict(self.doc.get("cells") or {})

    @classmethod
    def load(cls, path: Path = DEFAULT_CALIBRATION_PATH) -> "EdgeCalibration":
        try:
            return cls(json.loads(Path(path).read_text()))
        except Exception:
            return cls(None)

    def predict(self, ask: Optional[float], tte_sec: Optional[float]) -> tuple[Optional[float], Optional[str], str]:
        cell = edge_cell(ask, tte_sec) if ask is not None and tte_sec is not None else None
        if cell is None:
            return None, None, "OUT_OF_RANGE_OR_MISSING"
        entry = self.cells.get(cell)
        if not entry:
            return None, cell, "CELL_MISSING"
        if not entry.get("usable"):
            return None, cell, "CELL_INSUFFICIENT"
        residual = entry.get("residual_shrunk")
        if residual is None:
            return None, cell, "CELL_MISSING"
        p = min(0.999, max(0.001, float(ask) + float(residual)))
        return p, cell, "OK"


def taker_fee_frac(ask: float) -> Optional[float]:
    """Official crypto taker fee as fee shares / shares at this price."""
    try:
        from execution.rebate_model import estimate_taker_buy_fee_shares
        shares = Decimal("100")
        fee_shares = estimate_taker_buy_fee_shares(shares=shares, probability=Decimal(str(ask)))
        return float(Decimal(str(fee_shares)) / shares)
    except Exception:
        return None


def evaluate_edge(snapshot: EntryShadowSnapshot, calibration: EdgeCalibration) -> dict[str, Any]:
    """D: residual edge at the executable ask. Pure; never raises."""
    out: dict[str, Any] = {
        "model_version": calibration.version, "edge_state": UNKNOWN, "calibration_cell": None,
        "calibration_status": None, "calibrated_probability": None, "executable_ask": snapshot.best_ask,
        "fee_frac": None, "fee_equivalent": None, "raw_edge": None, "net_edge_before_selection": None,
        "penalty_none": PENALTY_NONE, "net_edge_after_selection_penalty_none": None,
        "penalty_reference": PENALTY_REFERENCE, "net_edge_after_selection_penalty_reference": None,
        "maker_raw_edge": None,
    }
    try:
        if snapshot.observation_state != "OBSERVED" or snapshot.best_ask is None:
            out["calibration_status"] = "NO_FRESH_QUOTE"
            return out
        p, cell, status = calibration.predict(snapshot.best_ask, snapshot.tte_sec)
        out["calibration_cell"], out["calibration_status"] = cell, status
        if p is None:
            return out
        frac = taker_fee_frac(snapshot.best_ask)
        if frac is None:
            out["calibration_status"] = "FEE_MODEL_UNAVAILABLE"
            return out
        raw = p - snapshot.best_ask
        fee_eq = p * frac
        net = raw - fee_eq
        out.update({
            "edge_state": "VALUE", "calibrated_probability": round(p, 6), "fee_frac": round(frac, 6),
            "fee_equivalent": round(fee_eq, 6), "raw_edge": round(raw, 6),
            "net_edge_before_selection": round(net, 6),
            "net_edge_after_selection_penalty_none": round(net - PENALTY_NONE, 6),
            "net_edge_after_selection_penalty_reference": round(net - PENALTY_REFERENCE, 6),
            "maker_raw_edge": (round(p - snapshot.planned_maker_price, 6)
                               if snapshot.planned_maker_price is not None else None),
        })
        return out
    except Exception:
        return {**out, "edge_state": UNKNOWN, "calibration_status": "ERROR"}


# ------------------------------------------------------------------ C / B rules
def evaluate_ec1(snapshot: EntryShadowSnapshot) -> dict[str, Any]:
    """C: EC1@15s. WOULD_CANCEL | WOULD_KEEP | UNKNOWN | NOT_APPLICABLE (already filled)."""
    if snapshot.canonical_filled_by_horizon:
        return {"decision": NOT_APPLICABLE, "reason": "canonical_order_filled_before_horizon", "ask_change": None}
    a, a0 = _d(snapshot.best_ask), _d(snapshot.placement_ask)
    if snapshot.observation_state != "OBSERVED" or a is None or a0 is None:
        return {"decision": UNKNOWN, "reason": "censored_or_missing_quote", "ask_change": None}
    change = a - a0
    decision = "WOULD_CANCEL" if change <= EC1_ASK_DROP else "WOULD_KEEP"
    return {"decision": decision, "reason": "ec1_ask_toward_maker" if decision == "WOULD_CANCEL" else "ec1_not_met",
            "ask_change": float(change)}


def evaluate_chb(snapshot: EntryShadowSnapshot) -> dict[str, Any]:
    """B: CH_B@10s. WOULD_CHASE | WOULD_NOT_CHASE | UNKNOWN | NOT_APPLICABLE (already filled)."""
    if snapshot.canonical_filled_by_horizon:
        return {"decision": NOT_APPLICABLE, "reason": "canonical_order_filled_before_horizon",
                "ask_change": None, "score_signed": None, "spread": None}
    a, a0, spread = _d(snapshot.best_ask), _d(snapshot.placement_ask), _d(snapshot.spread)
    score = snapshot.side_score_signed
    if snapshot.observation_state != "OBSERVED" or a is None or a0 is None or spread is None or score is None:
        return {"decision": UNKNOWN, "reason": "censored_or_missing_input",
                "ask_change": float(a - a0) if a is not None and a0 is not None else None,
                "score_signed": score, "spread": float(spread) if spread is not None else None}
    change = a - a0
    met = change >= CHB_ASK_RISE and score >= 0 and spread <= CHB_MAX_SPREAD
    return {"decision": "WOULD_CHASE" if met else "WOULD_NOT_CHASE",
            "reason": "chb_favorable_escape_signal_ok" if met else "chb_not_met",
            "ask_change": float(change), "score_signed": float(score), "spread": float(spread)}


def depth_state(levels: Optional[tuple], top_ask_size: Optional[float], qty: Optional[float],
                l2_age_sec: Optional[float]) -> dict[str, Any]:
    """Executable depth for the planned qty. UNKNOWN is never treated as executable."""
    out = {"target_qty": qty, "top_ask_size": top_ask_size, "depth_vwap": None,
           "supported_qty": None, "depth_freshness_sec": l2_age_sec, "depth_state": "DEPTH_UNKNOWN"}
    try:
        need = float(qty) if qty is not None else None
        if need is None or need <= 0:
            return out
        if levels:
            remaining, notional, got = need, 0.0, 0.0
            for price, size in levels:
                p, s = float(price), float(size)
                if p <= 0 or s <= 0:
                    continue
                take = min(s, remaining)
                notional += take * p
                got += take
                remaining -= take
                if remaining <= 1e-12:
                    break
            out["supported_qty"] = got
            if remaining <= 1e-12:
                out.update(depth_state="DEPTH_SUPPORTED", depth_vwap=round(notional / need, 6))
            elif got > 0:
                out["depth_state"] = "DEPTH_PARTIAL"
            return out
        if top_ask_size is not None:
            out["supported_qty"] = min(float(top_ask_size), need)
            out["depth_state"] = "DEPTH_SUPPORTED" if float(top_ask_size) >= need else "DEPTH_PARTIAL"
        return out
    except Exception:
        return out


# ------------------------------------------------------------------ tracker
@dataclass
class Attempt:
    attempt_id: str
    market: str
    instrument: str
    side: str
    canonical_coid: str
    place_ts: float
    maker_price: Optional[float]
    qty: Optional[float]
    a0: Optional[QuoteSample] = None
    entry_score_signed: Optional[float] = None
    samples: deque = field(default_factory=lambda: deque(maxlen=MAX_SAMPLES_PER_ATTEMPT))
    canonical_fill_ts: Optional[float] = None
    canonical_filled_qty: float = 0.0
    market_fill_ts: Optional[float] = None
    canonical_cancel_ack_ts: Optional[float] = None
    linked_coids: list = field(default_factory=list)
    done: set = field(default_factory=set)
    pending_c: Optional[dict] = None
    pending_b: Optional[dict] = None


class EntryShadowTracker:
    """Fixed-size attempt state; emits one event per attempt x strategy x horizon."""

    def __init__(self, *, emit: Optional[Callable[[str, dict], Any]], calibration: Optional[EdgeCalibration] = None,
                 d_enabled: bool = False, c_enabled: bool = False, b_enabled: bool = False,
                 clock: Callable[[], float] = time.time) -> None:
        self._emit_sink = emit
        self.calibration = calibration or EdgeCalibration.load()
        self.d_enabled, self.c_enabled, self.b_enabled = bool(d_enabled), bool(c_enabled), bool(b_enabled)
        self.attempts: dict[str, Attempt] = {}
        self._by_coid: dict[str, str] = {}
        self.counters = {"events": 0, "sink_rejected": 0, "exceptions": 0, "attempts": 0, "ticks": 0}
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self.d_enabled or self.c_enabled or self.b_enabled

    # ---------------------------------------------------------------- emit
    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._emit_sink is None:
            return
        try:
            accepted = self._emit_sink(event_type, {"shadow_schema": SCHEMA_VERSION, **payload})
            self.counters["events" if accepted is not False else "sink_rejected"] += 1
        except Exception:
            self.counters["exceptions"] += 1

    # ---------------------------------------------------------------- lifecycle
    def on_buy_submit(self, *, coid: str, market: str, instrument: str, side: str, maker_price: Any, qty: Any,
                      place_ts: float, sample: Optional[QuoteSample]) -> None:
        if not self.enabled or not coid or not market:
            return
        existing = self.attempts.get(market)
        if existing is not None:
            existing.linked_coids.append(coid)
            self._by_coid[coid] = market
            return
        if len(self.attempts) >= MAX_ATTEMPTS:
            oldest = min(self.attempts.values(), key=lambda a: a.place_ts)
            self._drop(oldest.market)
        side_u = str(side).upper()
        a0 = asof_sample([sample] if sample else [], place_ts, PLACEMENT_TOL_SEC)
        score = sample.side_score if sample else None
        sign = 1.0 if side_u == "UP" else -1.0 if side_u == "DOWN" else None
        attempt = Attempt(
            attempt_id=f"{market}|{coid}", market=market, instrument=instrument, side=side_u, canonical_coid=coid,
            place_ts=float(place_ts), maker_price=_f(maker_price), qty=_f(qty), a0=a0,
            entry_score_signed=(score * sign) if score is not None and sign is not None else None,
        )
        if sample is not None:
            attempt.samples.append(sample)
        self.attempts[market] = attempt
        self._by_coid[coid] = market
        self.counters["attempts"] += 1

    def on_reject(self, *, coid: str) -> None:
        market = self._by_coid.pop(coid, None)
        attempt = self.attempts.get(market) if market else None
        if attempt is None:
            return
        if attempt.canonical_coid == coid and attempt.canonical_fill_ts is None and not attempt.done:
            # Rejected first order (e.g. post-only cross): not an attempt; the next
            # accepted BUY in this market becomes canonical.
            self._drop(market)
            self.counters["attempts"] -= 1
            self.counters["rejected_first_orders"] = self.counters.get("rejected_first_orders", 0) + 1
        elif coid in attempt.linked_coids:
            attempt.linked_coids.remove(coid)

    def on_fill(self, *, coid: str, fill_ts: float, qty: Any, cancel_pending: bool = False,
                cancel_request_age_sec: Optional[float] = None) -> None:
        market = self._by_coid.get(coid)
        attempt = self.attempts.get(market) if market else None
        if attempt is None:
            return
        if attempt.market_fill_ts is None:
            attempt.market_fill_ts = float(fill_ts)
        if coid == attempt.canonical_coid:
            attempt.canonical_filled_qty += float(_f(qty) or 0.0)
            if attempt.canonical_fill_ts is None:
                attempt.canonical_fill_ts = float(fill_ts)
        if cancel_pending:
            self._emit("ENTRY_CANCEL_RACE_TELEMETRY", {
                "attempt_id": attempt.attempt_id, "market": attempt.market, "client_order_id": coid,
                "canonical": coid == attempt.canonical_coid, "event": "FILL_DURING_PENDING_CANCEL",
                "fill_ts": float(fill_ts), "fill_qty": _f(qty), "cancel_request_age_sec": cancel_request_age_sec,
                "market_day_type": market_day_type(attempt.market),
            })

    def on_cancel_ack(self, *, coid: str, ack_ts: float, request_to_ack_sec: Optional[float],
                      reason: Optional[str], remaining_qty: Any = None) -> None:
        market = self._by_coid.get(coid)
        attempt = self.attempts.get(market) if market else None
        if attempt is None:
            return
        if coid == attempt.canonical_coid:
            attempt.canonical_cancel_ack_ts = float(ack_ts)
        self._emit("ENTRY_CANCEL_RACE_TELEMETRY", {
            "attempt_id": attempt.attempt_id, "market": attempt.market, "client_order_id": coid,
            "canonical": coid == attempt.canonical_coid, "event": "CANCEL_ACK", "ack_ts": float(ack_ts),
            "request_to_ack_sec": request_to_ack_sec, "cancel_reason": reason,
            "filled_qty_before_ack": attempt.canonical_filled_qty if coid == attempt.canonical_coid else None,
            "remaining_qty": _f(remaining_qty), "market_day_type": market_day_type(attempt.market),
        })

    def _drop(self, market: str) -> None:
        attempt = self.attempts.pop(market, None)
        if attempt is None:
            return
        for coid in [attempt.canonical_coid, *attempt.linked_coids]:
            self._by_coid.pop(coid, None)

    # ---------------------------------------------------------------- ticks
    def instruments(self) -> list[str]:
        return [a.instrument for a in self.attempts.values()]

    def on_tick(self, now: float, sample_for: Callable[[str], Optional[QuoteSample]]) -> None:
        """Called from the quote cycle. sample_for(instrument) is a read-only market reader."""
        if not self.enabled or not self.attempts:
            return
        self.counters["ticks"] += 1
        for market in list(self.attempts):
            attempt = self.attempts.get(market)
            if attempt is None:
                continue
            try:
                sample = sample_for(attempt.instrument)
                if sample is not None and (not attempt.samples
                                           or sample.quote_receipt_ts != attempt.samples[-1].quote_receipt_ts):
                    attempt.samples.append(sample)
                self._process(attempt, now)
            except Exception:
                self.counters["exceptions"] += 1
            if now - attempt.place_ts > ATTEMPT_TTL_SEC:
                self._finish(attempt, now)
                self._drop(market)

    def _snapshot(self, attempt: Attempt, horizon: float, now: float) -> EntryShadowSnapshot:
        target = attempt.place_ts + horizon
        tol = tolerance_for(horizon) if horizon > 0 else PLACEMENT_TOL_SEC
        obs = asof_sample(attempt.samples, target, tol)
        a0 = attempt.a0
        sign = 1.0 if attempt.side == "UP" else -1.0 if attempt.side == "DOWN" else None
        ref = obs or (attempt.samples[-1] if attempt.samples else None)
        bid = float(obs.best_bid) if obs and obs.best_bid is not None else None
        ask = float(obs.best_ask) if obs and obs.best_ask is not None else None

        def move(cur, base):
            if cur is None or base is None or not base or sign is None:
                return None
            return round(sign * (cur - base) / base * 1e4, 4)

        canonical_filled = attempt.canonical_fill_ts is not None and attempt.canonical_fill_ts <= target
        market_filled = attempt.market_fill_ts is not None and attempt.market_fill_ts <= target
        if canonical_filled:
            status = "FILLED"
        elif attempt.canonical_cancel_ack_ts is not None and attempt.canonical_cancel_ack_ts <= target:
            status = "CANCELLED"
        else:
            status = "RESTING"
        return EntryShadowSnapshot(
            schema_version=SCHEMA_VERSION, timestamp=float(now), attempt_id=attempt.attempt_id,
            market=attempt.market, instrument=attempt.instrument, side=attempt.side, horizon_sec=float(horizon),
            tte_sec=ref.tte_sec if ref else None, side_score=ref.side_score if ref else None,
            side_score_signed=(ref.side_score * sign) if ref and ref.side_score is not None and sign else None,
            entry_side_score_signed=attempt.entry_score_signed,
            strike=ref.strike if ref else None, strike_verified=ref.strike_verified if ref else None,
            quote_receipt_ts=obs.quote_receipt_ts if obs else None,
            quote_age_at_horizon_sec=round(target - obs.quote_receipt_ts, 4) if obs else None,
            observation_state="OBSERVED" if obs else "CENSORED", best_bid=bid, best_ask=ask,
            spread=round(ask - bid, 6) if ask is not None and bid is not None else None,
            top_ask_size=_f(obs.top_ask_size) if obs else None, top_bid_size=_f(obs.top_bid_size) if obs else None,
            l2_age_sec=obs.l2_age_sec if obs else None,
            binance_spot=ref.binance_spot if ref else None,
            binance_move_bps_signed=move(ref.binance_spot if ref else None, a0.binance_spot if a0 else None),
            chainlink_spot=ref.chainlink_spot if ref else None, chainlink_age_sec=ref.chainlink_age_sec if ref else None,
            chainlink_move_bps_signed=move(ref.chainlink_spot if ref else None, a0.chainlink_spot if a0 else None),
            twap_value=ref.twap_value if ref else None, twap_state=ref.twap_state if ref else None,
            signed_distance_usd=ref.signed_distance_usd if ref else None,
            placement_ask=float(a0.best_ask) if a0 and a0.best_ask is not None else None,
            placement_bid=float(a0.best_bid) if a0 and a0.best_bid is not None else None,
            planned_maker_price=attempt.maker_price, planned_qty=attempt.qty,
            maker_age_sec=round(target - attempt.place_ts, 4), maker_filled_qty=attempt.canonical_filled_qty,
            maker_status=status, canonical_filled_by_horizon=canonical_filled, market_filled_by_horizon=market_filled,
            production_action=f"MAKER_{status}", market_day_type=market_day_type(attempt.market),
        )

    def _process(self, attempt: Attempt, now: float) -> None:
        for h in D_HORIZONS_SEC:
            key = ("D", h)
            if key in attempt.done or now < attempt.place_ts + h:
                continue
            attempt.done.add(key)
            if self.d_enabled:
                snap = self._snapshot(attempt, h, now)
                self._emit("ENTRY_EDGE_SHADOW", {**snap.as_payload(), **evaluate_edge(snap, self.calibration)})
        if self.c_enabled and ("C", C_HORIZON_SEC) not in attempt.done and now >= attempt.place_ts + C_HORIZON_SEC:
            attempt.done.add(("C", C_HORIZON_SEC))
            snap = self._snapshot(attempt, C_HORIZON_SEC, now)
            # Decision is frozen now; race fields are filled in later.
            attempt.pending_c = {"snapshot": snap, "rule": evaluate_ec1(snap),
                                 "d_edge": evaluate_edge(snap, self.calibration)}
        if self.b_enabled and ("B", B_HORIZON_SEC) not in attempt.done and now >= attempt.place_ts + B_HORIZON_SEC:
            attempt.done.add(("B", B_HORIZON_SEC))
            snap = self._snapshot(attempt, B_HORIZON_SEC, now)
            attempt.pending_b = {"snapshot": snap, "rule": evaluate_chb(snap),
                                 "d_edge": evaluate_edge(snap, self.calibration)}
        self._flush_pending(attempt, now, final=False)

    def _flush_pending(self, attempt: Attempt, now: float, *, final: bool) -> None:
        if attempt.pending_c is not None:
            t = attempt.place_ts + C_HORIZON_SEC
            if final or now >= t + LATENCY_SENSITIVITY_SEC:
                p = attempt.pending_c
                attempt.pending_c = None
                fill = attempt.canonical_fill_ts
                self._emit("ENTRY_EARLY_CANCEL_SHADOW", {
                    **p["snapshot"].as_payload(), "rule_version": "EC1@15s_v1", "c_rule_decision": p["rule"]["decision"],
                    "c_rule_reason": p["rule"]["reason"], "ec1_ask_change": p["rule"]["ask_change"],
                    "ec1_threshold": float(EC1_ASK_DROP), "d_edge_state": p["d_edge"]["edge_state"],
                    "d_net_edge_before_selection": p["d_edge"]["net_edge_before_selection"],
                    # Telemetry only (observed after the decision): would the cancel have raced a fill?
                    "race_latency_assumed_sec": LATENCY_SEC,
                    "race_canonical_fill_within_latency": bool(fill is not None and t < fill <= t + LATENCY_SEC),
                    "race_canonical_fill_within_latency_x1_5": bool(fill is not None and t < fill <= t + LATENCY_SENSITIVITY_SEC),
                    "emitted_partial": bool(final and now < t + LATENCY_SENSITIVITY_SEC),
                })
        if attempt.pending_b is not None:
            t = attempt.place_ts + B_HORIZON_SEC
            due = t + LATENCY_SENSITIVITY_SEC
            if final or now >= due:
                p = attempt.pending_b
                attempt.pending_b = None
                snap = p["snapshot"]
                exec_l = asof_sample(attempt.samples, t + LATENCY_SEC, TOL_LONG_SEC)
                exec_l15 = asof_sample(attempt.samples, t + LATENCY_SENSITIVITY_SEC, TOL_LONG_SEC)
                ask_l = float(exec_l.best_ask) if exec_l else None
                ask_l15 = float(exec_l15.best_ask) if exec_l15 else None
                depth_src = exec_l or (asof_sample(attempt.samples, t, TOL_LONG_SEC))
                depth = depth_state(depth_src.ask_levels if depth_src else None,
                                    _f(depth_src.top_ask_size) if depth_src else None, attempt.qty,
                                    depth_src.l2_age_sec if depth_src else None)
                fill = attempt.canonical_fill_ts
                race = bool(fill is not None and t < fill <= t + LATENCY_SEC)
                if p["rule"]["decision"] != "WOULD_CHASE":
                    econ_state = NOT_APPLICABLE
                elif race:
                    econ_state = "CANCEL_RACE_FILLED_AT_MAKER"
                elif ask_l is None:
                    econ_state = "EXEC_PRICE_UNKNOWN"
                elif Decimal(str(ask_l)) > CHASE_MAX_PRICE:
                    econ_state = "SKIPPED_ABOVE_0_97"
                elif depth["depth_state"] != "DEPTH_SUPPORTED":
                    econ_state = "NOT_EXECUTABLE_DEPTH" if depth["depth_state"] == "DEPTH_PARTIAL" else "DEPTH_UNKNOWN"
                else:
                    econ_state = "EXECUTABLE"
                frac = taker_fee_frac(ask_l) if ask_l is not None else None
                self._emit("ENTRY_CHASE_SHADOW", {
                    **snap.as_payload(), "rule_version": "CH_B@10s_v1", "b_rule_decision": p["rule"]["decision"],
                    "b_rule_reason": p["rule"]["reason"], "favorable_move": p["rule"]["ask_change"],
                    "chb_score_signed": p["rule"]["score_signed"], "chb_spread": p["rule"]["spread"],
                    "chb_thresholds": {"ask_rise": float(CHB_ASK_RISE), "max_spread": float(CHB_MAX_SPREAD), "min_score_signed": 0.0},
                    # Simulated execution economics (observed after the decision; never an input to it).
                    "latency_sec": LATENCY_SEC, "latency_sensitivity_sec": LATENCY_SENSITIVITY_SEC,
                    "exec_ask_at_latency": ask_l, "exec_ask_at_latency_x1_5": ask_l15,
                    "exec_fee_frac_at_latency": frac, "chase_economic_state": econ_state, **depth,
                    "d_edge_state": p["d_edge"]["edge_state"],
                    "d_net_edge_before_selection": p["d_edge"]["net_edge_before_selection"],
                    "emitted_partial": bool(final and now < due),
                })

    def _finish(self, attempt: Attempt, now: float) -> None:
        self._process(attempt, now)
        self._flush_pending(attempt, now, final=True)
