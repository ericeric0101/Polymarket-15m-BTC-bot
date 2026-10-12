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
from typing import Any, Callable, Optional

SCHEMA_VERSION = "candidate_decision_v1"
ACCEPT_EVENTS = frozenset({"ORDER_SUBMIT", "ORDER_DRY_RUN_SUBMITTED"})
REJECT_PREFIXES = ("ORDER_SKIP_", "ORDER_OBSERVE_BUY_BLOCKED")
LIFECYCLE_EVENTS = frozenset({"ORDER_FILLED", "ORDER_CANCELED", "ORDER_REJECTED", "ORDER_DRY_RUN_CANCELLED",
                              "ORDER_DRY_RUN_FILLED", "ORDER_EXPIRED"})
MARKOUT_HORIZONS_SEC = (1, 3, 5, 10, 15, 30)
STABLE_ROLES = ("FIRST_ACCEPTED", "FIRST_EVALUATION")
# Offline canonical rule: FIRST_ACCEPTED if the market has one, else FIRST_EVALUATION.
PEX_MAX_AGE_SEC = 8.0
BBO_MAX_AGE_SEC = 3.0
TICK = 0.01
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
            "exit_policy": exit_policy_fingerprint(strategy), "strike_module_version": strike_module_version()}
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
        production_p_fair=_num(payload.get("p_fair")), proposed_maker_price=_num(price), proposed_qty=q,
        eligibility="ACCEPTED" if accepted else "REJECTED", eligibility_event_type=event_type,
        reject_reason=None if accepted else (reason or event_type), client_order_id=client_order_id,
        alignment_status="ALIGNED" if core_ok else "PARTIAL", missing_reasons=tuple(sorted(missing.items())))


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
        self.counters = {"observed": 0, "candidates": 0, "lifecycle": 0, "exceptions": 0, "markouts": 0, "paths": 0}
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
            st.last_eligibility = elig
            st.seq += 1
            seq = st.seq
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
        if st is None:
            return
        self.counters["lifecycle"] += 1
        self._emit({"record_type": "LIFECYCLE", "schema_version": SCHEMA_VERSION, "candidate_id": cid,
                    "market_slug": slug, "event_type": et, "side": side or None, "price": _num(price), "qty": _num(qty),
                    "client_order_id": coid, "reason": reason, "wall_ts": wall, "monotonic_ns": mono,
                    "run_fingerprint": self.fingerprint})
        if is_canonical_fill and self.markout_enabled:
            self._schedule_markouts(slug, "FILL", wall, cid, coid)

    # ------------------------------------------------------------ path stats + markouts
    def _schedule_markouts(self, slug, anchor, t0, cid, coid) -> None:
        for h in MARKOUT_HORIZONS_SEC:
            self._markouts.append((t0 + h, {"slug": slug, "anchor": anchor, "horizon_sec": h, "anchor_ts": t0,
                                            "candidate_id": cid, "client_order_id": coid}))

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
                            "run_fingerprint": self.fingerprint})
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
                   "run_fingerprint": self.fingerprint}
        self.counters["paths"] += 1
        self._emit(rec)

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
        root = Path(directory) if directory else Path(getattr(strategy, "project_root", ".")) / "data/research/candidate_telemetry"
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
