"""Breaker V2: canonical-TWAP SELL authority for the conditional absolute breaker.

Behind ``ABSOLUTE_MAX_LOSS_TWAP_AUTHORITY_GATE_ENABLED`` (default off).  At the
FIRST moment the existing conditional absolute-loss breaker becomes eligible for
a position epoch, the canonical official-TWAP side state
(``bot.settlement_side.canonical_twap_side_state``) decides once:

* fresh TWAP ADVERSE to the held side   -> ``ALLOW``  (legacy breaker proceeds)
* fresh TWAP FAVORABLE to the held side -> ``SUPPRESS`` (this breaker may not SELL)
* anything else (stale / future / unavailable / strike or slug invalid /
  unknown)                              -> ``FALLBACK_LEGACY`` (legacy behaviour)

The decision is locked for the whole position epoch (``slug|instrument|opened_ts``)
and is never re-decided from a later TWAP state.  SUPPRESS affects only the
conditional absolute breaker; catastrophic, TP and every other exit stay active,
and BUY exposure on the market pair may not be increased.  Weekday/weekend is
recorded as an evaluation stratum only and never enters the decision.

Pure helpers + a bounded in-memory book; persistence and rehydration are done by
``bot.taker_exit`` through the trade journal.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from bot.settlement_side import TWAP_ADVERSE, TWAP_FAVORABLE

ALLOW = "ALLOW"
SUPPRESS = "SUPPRESS"
FALLBACK_LEGACY = "FALLBACK_LEGACY"
DECISIONS = frozenset({ALLOW, SUPPRESS, FALLBACK_LEGACY})

DECISION_EVENT = "BREAKER_V2_AUTHORITY_DECISION"
REHYDRATED_EVENT = "BREAKER_V2_AUTHORITY_REHYDRATED"
SCHEMA_VERSION = "breaker_v2_authority_v1"
FLAG_ENV = "ABSOLUTE_MAX_LOSS_TWAP_AUTHORITY_GATE_ENABLED"
# Residual-qty tolerance when deciding whether journal SELL fills closed the
# position a persisted decision belonged to.
CLOSE_QTY_EPSILON = 0.000001

_TAIPEI = ZoneInfo("Asia/Taipei")
MAX_BOOK_ENTRIES = 256


def breaker_v2_authority_summary(enabled: bool) -> str:
    """Startup log line with the effective flag value."""
    value = "1" if enabled else "0"
    effect = ("conditional absolute breaker SELL authority gated by canonical TWAP at first eligibility"
              if enabled else "legacy conditional absolute breaker (no V2 gate)")
    return f"Breaker V2 authority gate: {FLAG_ENV}={value} ({effect}; catastrophic/TP unaffected)"


def decide_authority(twap_state: str) -> tuple[str, str]:
    """(decision, reason) from a ``canonical_twap_side_state`` state.  Pure."""
    if twap_state == TWAP_ADVERSE:
        return ALLOW, "fresh_twap_adverse"
    if twap_state == TWAP_FAVORABLE:
        return SUPPRESS, "fresh_twap_favorable"
    return FALLBACK_LEGACY, f"twap_{str(twap_state or 'UNKNOWN').lower()}"


def position_epoch_key(slug: str, instrument_id: str, opened_ts: float) -> str:
    """Same key as the STOP_TIMING position epoch."""
    return f"{slug}|{instrument_id}|{float(opened_ts or 0.0):.6f}"


def market_day_type(slug: str, fallback_ts: Optional[float] = None) -> str:
    """WEEKDAY / WEEKEND by the market's Taipei calendar date.  Evaluation only."""
    ts: Optional[float] = None
    try:
        ts = float(int(str(slug).rsplit("-", 1)[-1]))
    except (TypeError, ValueError):
        ts = float(fallback_ts) if fallback_ts else None
    if ts is None or ts <= 0:
        return "UNKNOWN"
    return "WEEKEND" if datetime.fromtimestamp(ts, timezone.utc).astimezone(_TAIPEI).weekday() >= 5 else "WEEKDAY"


def rehydrated_decision(lookup: Optional[dict[str, Any]]) -> tuple[Optional[str], str]:
    """Decision to restore from a journal lookup, or (None, reason) to decide fresh.

    ``lookup`` is ``TradeJournalDB.load_breaker_v2_authority_decision`` output:
    ``None`` means the lookup itself failed.  Fail-safe: anything that cannot be
    reconstructed reliably restores ``FALLBACK_LEGACY`` (legacy behaviour), never
    a fresh decision from a newer TWAP state.
    """
    if lookup is None:
        return FALLBACK_LEGACY, "rehydration_lookup_failed"
    record = lookup.get("decision_record")
    if record:
        decision = str(record.get("decision") or "")
        try:
            closed = float(lookup.get("sell_qty_after_decision") or 0.0) + CLOSE_QTY_EPSILON >= float(
                record.get("qty") or 0.0)
        except (TypeError, ValueError):
            return FALLBACK_LEGACY, "rehydration_record_unreadable"
        if closed and float(record.get("qty") or 0.0) > 0:
            # That position was fully sold after its decision; this is a new one.
            return None, "prior_decision_position_closed"
        if decision not in DECISIONS:
            return FALLBACK_LEGACY, "rehydration_record_unreadable"
        return decision, "rehydrated_from_journal"
    if lookup.get("prior_absolute_submit"):
        # The legacy breaker already acted on this instrument in this market
        # without a recorded decision (e.g. a pre-flag run): keep legacy.
        return FALLBACK_LEGACY, "prior_absolute_submit_without_decision"
    return None, "no_prior_decision"


class BreakerV2AuthorityBook:
    """Bounded in-memory decisions keyed by position epoch, plus BUY blocks."""

    def __init__(self, max_entries: int = MAX_BOOK_ENTRIES) -> None:
        self.max_entries = max(8, int(max_entries))
        self._decisions: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self.buy_blocked_instruments: set[str] = set()

    def get(self, epoch_key: str) -> Optional[str]:
        entry = self._decisions.get(epoch_key)
        return entry["decision"] if entry else None

    def entry(self, epoch_key: str) -> Optional[dict[str, Any]]:
        entry = self._decisions.get(epoch_key)
        return dict(entry) if entry else None

    def has_other_epoch(self, slug: str, instrument_id: str, epoch_key: str) -> bool:
        """A decision for an earlier position on this instrument in this process."""
        return any(e["slug"] == slug and e["instrument_id"] == instrument_id and key != epoch_key
                   for key, e in self._decisions.items())

    def lock(self, epoch_key: str, *, slug: str, instrument_id: str, decision: str, reason: str) -> str:
        """Record a decision once.  An existing decision always wins (locked)."""
        existing = self._decisions.get(epoch_key)
        if existing:
            return existing["decision"]
        if decision not in DECISIONS:
            decision, reason = FALLBACK_LEGACY, "invalid_decision"
        self._decisions[epoch_key] = {"decision": decision, "reason": reason, "slug": slug,
                                      "instrument_id": instrument_id}
        while len(self._decisions) > self.max_entries:
            self._decisions.popitem(last=False)
        return decision

    def block_buys(self, instruments: Any) -> None:
        for inst in instruments or ():
            if inst:
                self.buy_blocked_instruments.add(str(inst))
        while len(self.buy_blocked_instruments) > self.max_entries:
            self.buy_blocked_instruments.pop()


def breaker_v2_buy_block_reason(strategy: Any, order: Any) -> str:
    """'' = allowed.  A BUY on an instrument whose market pair has a SUPPRESS
    decision is refused: SUPPRESS never allows increasing that exposure."""
    book = getattr(strategy, "breaker_v2_authority_book", None)
    if not isinstance(book, BreakerV2AuthorityBook) or not book.buy_blocked_instruments:
        return ""
    from bot.kill_switch import order_side_name
    if order_side_name(order) != "BUY":
        return ""
    try:
        inst = getattr(order, "instrument_id", None)
        key_fn = getattr(strategy, "_instrument_key", None)
        inst_key = str(key_fn(inst)) if callable(key_fn) else str(inst)
    except Exception:
        # A BUY whose instrument cannot be identified while a SUPPRESS block is
        # active fails closed (BUY is never required for safety).
        return "breaker_v2_suppress_buy_instrument_unidentified"
    if inst_key in book.buy_blocked_instruments:
        return "breaker_v2_suppress_blocks_buy_exposure"
    return ""
