"""Strike finality gate for the opening Price To Beat.

Polymarket's frontend ``crypto-price`` endpoint (the runtime's authoritative
strike source) returns ``openPrice`` while the 15-minute window is still
running, flagged ``completed=false, incomplete=true``.  The flags describe the
whole window, not the open price, so an in-window openPrice is PROVISIONAL: on
btc-updown-15m-1791711900 the value locked ~2 s after open (82956.853422…)
differed from the finalized price-to-beat (82956.368497…) and the runtime
labeled an official UP as DOWN.  A strike is FINAL only from a response with
``completed is True and incomplete is False``; it is then immutable.

Tie semantics (market rules text): "Up" if the Chainlink TWAP at the end of the
range is greater than or equal to the price at the beginning -> ``>=``.

Runtime trading consumers keep reading the provisional value exactly as before
(entry, Breaker V2, endgame exit); this module only adds finality state,
journaled revisions and a FINAL_ONLY settlement gate.  The poller runs on its
own daemon thread with bounded, backed-off requests and never blocks trading.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Optional

from loguru import logger

STATUS_PROVISIONAL = "PROVISIONAL"
STATUS_FINAL = "FINAL"
STATUS_UNTRACKED = "UNTRACKED"

EVENT_PROVISIONAL_REVISED = "STRIKE_PROVISIONAL_REVISED"
EVENT_FINALIZED = "STRIKE_FINALIZED"
EVENT_FINAL_CONFLICT = "STRIKE_FINAL_CONFLICT"
EVENT_FINALIZATION_EXPIRED = "STRIKE_FINALIZATION_EXPIRED"
EVENT_TYPES = (EVENT_PROVISIONAL_REVISED, EVENT_FINALIZED, EVENT_FINAL_CONFLICT, EVENT_FINALIZATION_EXPIRED)

FINAL_SOURCE = "polymarket_crypto_price_completed"

# In-window revalidation: backed off, capped at one request per minute.
IN_WINDOW_POLL_SEC = (15.0, 30.0, 60.0)
# After window end.  Observed 2026-10-12 (btc-updown-15m-1791809100): the
# endpoint reported completed=true ~70 s after end; responses are cached for
# ~11 s, so polling faster than every 10 s gains nothing.
POST_END_POLL_SEC = (2.0, 3.0, 5.0, 10.0)
# Give up (stay PROVISIONAL; FINAL_ONLY consumers stay fail-closed) after this.
# Kept below the 300 s before the next market's first possible entry
# (FIRST_ENTRY_MAX_TIME_LEFT_SEC=600) so finalized settlement PnL reaches the
# session/regime guards before that entry.
FINALIZATION_MAX_WAIT_SEC = 240.0
_MAX_TRACKED_SLUGS = 64


@dataclass(frozen=True)
class CryptoPriceSnapshot:
    open_price: Optional[Decimal]
    close_price: Optional[Decimal]
    completed: Optional[bool]
    incomplete: Optional[bool]
    fetched_ts: float


def _positive_decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() and out > 0 else None


def _strict_bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def parse_crypto_price_payload(obj: Any, *, fetched_ts: float) -> Optional[CryptoPriceSnapshot]:
    """Parse a ``crypto-price`` JSON body; only real JSON booleans count as flags."""
    if not isinstance(obj, dict):
        return None
    return CryptoPriceSnapshot(
        open_price=_positive_decimal(obj.get("openPrice")),
        close_price=_positive_decimal(obj.get("closePrice")),
        completed=_strict_bool(obj.get("completed")),
        incomplete=_strict_bool(obj.get("incomplete")),
        fetched_ts=float(fetched_ts),
    )


def is_final_snapshot(snapshot: Optional[CryptoPriceSnapshot]) -> bool:
    return bool(
        snapshot is not None
        and snapshot.completed is True
        and snapshot.incomplete is False
        and snapshot.open_price is not None
    )


def settle_side(close: Decimal, strike: Decimal) -> str:
    """Official side from a final close and final strike (Decimal only; tie -> UP)."""
    if not isinstance(close, Decimal) or not isinstance(strike, Decimal):
        raise TypeError("settle_side requires Decimal close and strike")
    return "UP" if close >= strike else "DOWN"


def _dec_str(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value)


class StrikeFinalityTracker:
    """Per-slug finality state machine (pure; thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, Any]] = {}

    def register_provisional(self, slug: str, strike: Decimal, *, source: str, start_ts: float,
                             end_ts: float, now: float) -> None:
        if not slug or not isinstance(strike, Decimal):
            return
        with self._lock:
            state = self._state.get(slug)
            if state is not None:
                return  # first lock wins; FINAL is never downgraded
            if len(self._state) >= _MAX_TRACKED_SLUGS:
                self._state.pop(next(iter(self._state)))
            self._state[slug] = {
                "status": STATUS_PROVISIONAL, "locked": strike, "last_provisional": strike,
                "source": source, "start_ts": float(start_ts), "end_ts": float(end_ts),
                "locked_ts": float(now), "final": None, "finalized_ts": None, "polls": 0,
                "expired": False,
            }

    def restore_final(self, slug: str, final_strike: Decimal, *, finalized_ts: Any,
                      locked: Optional[Decimal] = None) -> None:
        with self._lock:
            state = self._state.setdefault(slug, {
                "status": STATUS_PROVISIONAL, "locked": locked, "last_provisional": locked, "source": "restored",
                "start_ts": None, "end_ts": None, "locked_ts": None, "final": None, "finalized_ts": None,
                "polls": 0, "expired": False,
            })
            state.update(status=STATUS_FINAL, final=final_strike, finalized_ts=finalized_ts)

    def is_tracked(self, slug: str) -> bool:
        with self._lock:
            return slug in self._state

    def status(self, slug: str) -> Optional[str]:
        with self._lock:
            state = self._state.get(slug)
            return None if state is None else state["status"]

    def final_strike(self, slug: str) -> Optional[Decimal]:
        with self._lock:
            state = self._state.get(slug)
            return None if state is None else state["final"]

    def locked_provisional(self, slug: str) -> Optional[Decimal]:
        with self._lock:
            state = self._state.get(slug)
            return None if state is None else state["locked"]

    def observe(self, slug: str, snapshot: Optional[CryptoPriceSnapshot], *, now: float) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        if snapshot is None:
            return events
        with self._lock:
            state = self._state.get(slug)
            if state is None:
                return events
            state["polls"] += 1
            if state["status"] == STATUS_FINAL:
                if is_final_snapshot(snapshot) and snapshot.open_price != state["final"]:
                    events.append((EVENT_FINAL_CONFLICT, {
                        "slug": slug, "kept_final_strike": _dec_str(state["final"]),
                        "conflicting_final_strike": _dec_str(snapshot.open_price), "observed_ts": float(now),
                    }))
                return events
            if is_final_snapshot(snapshot):
                final = snapshot.open_price
                locked = state["locked"]
                state.update(status=STATUS_FINAL, final=final, finalized_ts=float(now))
                delta = (final - locked) if locked is not None else None
                events.append((EVENT_FINALIZED, {
                    "slug": slug, "strike_finality": STATUS_FINAL,
                    "finalized_strike": _dec_str(final), "finalized_strike_float": float(final),
                    "finalized_ts": float(now), "source": FINAL_SOURCE,
                    "completed": snapshot.completed, "incomplete": snapshot.incomplete,
                    "official_close": _dec_str(snapshot.close_price),
                    "locked_provisional_strike": _dec_str(locked),
                    "last_provisional_strike": _dec_str(state["last_provisional"]),
                    "delta_vs_locked_provisional": _dec_str(delta),
                    "differs_from_locked_provisional": bool(delta is not None and delta != 0),
                    "locked_ts": state["locked_ts"], "market_end_ts": state["end_ts"],
                    "polls": state["polls"],
                }))
                return events
            new = snapshot.open_price
            old = state["last_provisional"]
            if new is not None and new != old:
                state["last_provisional"] = new
                events.append((EVENT_PROVISIONAL_REVISED, {
                    "slug": slug, "strike_finality": STATUS_PROVISIONAL,
                    "old_strike": _dec_str(old), "new_strike": _dec_str(new),
                    "delta": _dec_str(None if old is None else new - old),
                    "observed_ts": float(now), "completed": snapshot.completed,
                    "incomplete": snapshot.incomplete, "source": "polymarket_crypto_price_open_in_window",
                    "applied_to_runtime_decisions": False,
                }))
        return events

    def expire(self, slug: str, *, now: float) -> list[tuple[str, dict]]:
        with self._lock:
            state = self._state.get(slug)
            if state is None or state["status"] == STATUS_FINAL or state["expired"]:
                return []
            state["expired"] = True
            return [(EVENT_FINALIZATION_EXPIRED, {
                "slug": slug, "strike_finality": STATUS_PROVISIONAL, "expired_ts": float(now),
                "locked_provisional_strike": _dec_str(state["locked"]),
                "last_provisional_strike": _dec_str(state["last_provisional"]),
                "market_end_ts": state["end_ts"], "polls": state["polls"],
                "max_wait_after_end_sec": FINALIZATION_MAX_WAIT_SEC,
            })]


def strike_finality_provenance(tracker: Optional[StrikeFinalityTracker], slug: str) -> dict[str, Any]:
    """Settlement / breaker view of one slug's strike finality.

    ``status`` is UNTRACKED only for hosts that never wired a tracker (legacy
    harnesses).  A wired tracker that does not know the slug is PROVISIONAL:
    unknown finality is treated as not final (FINAL_ONLY fail-closed).
    """
    if tracker is None or not slug:
        return {"status": STATUS_UNTRACKED, "final_strike": None,
                "provenance": {"strike_finality": STATUS_UNTRACKED}}
    status = tracker.status(slug) or STATUS_PROVISIONAL
    final = tracker.final_strike(slug) if status == STATUS_FINAL else None
    locked = tracker.locked_provisional(slug)
    delta = (final - locked) if (final is not None and locked is not None) else None
    return {
        "status": status,
        "final_strike": final,
        "provenance": {
            "strike_finality": status,
            "strike_locked_provisional": _dec_str(locked),
            "strike_final": _dec_str(final),
            "strike_final_delta_vs_locked": _dec_str(delta),
        },
    }


def restore_finality_from_events(tracker: StrikeFinalityTracker, slug: str,
                                 rows: Iterable[tuple[str, dict]]) -> Optional[str]:
    """Rebuild one slug's finality from journal rows ``(event_type, payload)`` (restart)."""
    locked = final = finalized_ts = None
    for event_type, payload in rows:
        if not isinstance(payload, dict) or str(payload.get("slug") or "") != slug:
            continue
        if event_type == "MARKET_STRIKE_LOCKED" and locked is None:
            locked = _positive_decimal(payload.get("strike"))
        elif event_type == EVENT_FINALIZED and final is None:
            final = _positive_decimal(payload.get("finalized_strike"))
            finalized_ts = payload.get("finalized_ts")
    if final is not None:
        tracker.restore_final(slug, final, finalized_ts=finalized_ts, locked=locked)
        return STATUS_FINAL
    if locked is not None:
        tracker.register_provisional(slug, locked, source="restored", start_ts=0.0, end_ts=0.0, now=time.time())
        return STATUS_PROVISIONAL
    return None


class StrikeFinalityPoller:
    """Bounded background finalization polling (one daemon thread per slug)."""

    def __init__(self, tracker: StrikeFinalityTracker,
                 fetch: Callable[[int, int], Optional[CryptoPriceSnapshot]],
                 on_events: Callable[[str, list], None], *, clock: Callable[[], float] = time.time,
                 sleep: Optional[Callable[[float], bool]] = None) -> None:
        self._tracker = tracker
        self._fetch = fetch
        self._on_events = on_events
        self._clock = clock
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

    def active_slugs(self) -> set[str]:
        with self._lock:
            return {slug for slug, t in self._threads.items() if t.is_alive()}

    def start(self, slug: str, start_ts: int, end_ts: int) -> None:
        with self._lock:
            existing = self._threads.get(slug)
            if existing is not None and existing.is_alive():
                return
            thread = threading.Thread(target=self.run_blocking, args=(slug, int(start_ts), int(end_ts)),
                                      name=f"strike-finality-{slug}", daemon=True)
            self._threads[slug] = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _emit(self, slug: str, events: list) -> None:
        if not events:
            return
        try:
            self._on_events(slug, events)
        except Exception as exc:  # journal/host failure must never kill the poller
            logger.warning(f"[STRIKE] finality event handler failed for {slug}: {type(exc).__name__}: {exc}")

    def _delay(self, attempt: int, now: float, end_ts: float) -> float:
        if now < end_ts:
            step = IN_WINDOW_POLL_SEC[min(attempt, len(IN_WINDOW_POLL_SEC) - 1)]
            return max(0.5, min(step, end_ts - now + 1.0))
        return POST_END_POLL_SEC[min(attempt, len(POST_END_POLL_SEC) - 1)]

    def run_blocking(self, slug: str, start_ts: int, end_ts: int) -> None:
        deadline = float(end_ts) + FINALIZATION_MAX_WAIT_SEC
        in_window_attempt = post_end_attempt = 0
        while not self._stop.is_set():
            if self._tracker.status(slug) == STATUS_FINAL:
                return
            now = self._clock()
            if now > deadline:
                self._emit(slug, self._tracker.expire(slug, now=now))
                return
            try:
                snapshot = self._fetch(int(start_ts), int(end_ts))
            except Exception as exc:  # timeout / network: stay PROVISIONAL, retry with backoff
                logger.debug(f"[STRIKE] finality poll failed for {slug}: {type(exc).__name__}: {exc}")
                snapshot = None
            self._emit(slug, self._tracker.observe(slug, snapshot, now=self._clock()))
            if self._tracker.status(slug) == STATUS_FINAL:
                return
            now = self._clock()
            if now < end_ts:
                delay = self._delay(in_window_attempt, now, end_ts)
                in_window_attempt += 1
            else:
                delay = self._delay(post_end_attempt, now, end_ts)
                post_end_attempt += 1
            if self._sleep(delay):
                return
