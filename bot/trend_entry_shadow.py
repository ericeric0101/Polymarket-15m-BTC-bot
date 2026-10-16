"""Research-only BTC trend entry candidates and executable BBO markouts.

This recorder has no order, cancel, sizing, ownership, or strategy-gate API.
It writes only to the asynchronous research database and never affects live
entry decisions.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

from loguru import logger


TREND_ENTRY_SHADOW_CONFIGS = ((60, 0), (120, 0), (180, 0), (120, 2), (180, 2), (180, 5))
TREND_ENTRY_SHADOW_MARKOUTS_MS = (1_000, 5_000, 10_000, 30_000)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except Exception:
        return None


def _levels(rows: Iterable[Any] | None) -> list[tuple[Decimal, Decimal]]:
    result = []
    for row in rows or ():
        try:
            price_raw = getattr(row, "price", row[0] if isinstance(row, (tuple, list)) else None)
            size_attr = getattr(row, "size", None)
            size_raw = size_attr() if callable(size_attr) else (size_attr if size_attr is not None else row[1])
            price, size = _decimal(price_raw), _decimal(size_raw)
            if price is not None and size is not None and price > 0 and size > 0:
                result.append((price, size))
        except Exception:
            continue
    return result


def _near_depth(levels: list[tuple[Decimal, Decimal]], best: Decimal, *, side: str, cents: int) -> float | None:
    if not levels:
        return None
    boundary = Decimal(cents) / Decimal("100")
    if side == "ask":
        quantity = sum((size for price, size in levels if price <= best + boundary), Decimal("0"))
    else:
        quantity = sum((size for price, size in levels if price >= best - boundary), Decimal("0"))
    return float(quantity)


class TrendEntryShadow:
    """Capture a fixed set of candidate schedules without granting order authority."""

    def __init__(
        self,
        *,
        db: Any,
        run_id: str,
        max_reference_age_sec: float = 10.0,
        max_quote_age_sec: float = 2.0,
        markout_lateness_sec: float = 3.0,
    ) -> None:
        self.db = db
        self.run_id = str(run_id)
        self.max_reference_age_sec = max(0.0, float(max_reference_age_sec))
        self.max_quote_age_sec = max(0.0, float(max_quote_age_sec))
        self.markout_lateness_sec = max(0.0, float(markout_lateness_sec))
        self._captured: set[tuple[str, int, int]] = set()
        self._unavailable_reported: set[tuple[str, int, int]] = set()
        self._candidates_by_slug: dict[str, list[dict[str, Any]]] = {}
        self._pending_markouts: dict[str, dict[str, Any]] = {}

    def _decision(self, slug: str, ts: float, payload: dict[str, Any]) -> None:
        try:
            self.db.enqueue_decision(
                run_id=self.run_id,
                slug=slug,
                market_id=None,
                decision_epoch_ns=int(float(ts) * 1_000_000_000),
                payload=payload,
            )
        except Exception:
            # Shadow persistence must never escape into the live quote path.
            return

    def _markout(self, candidate: dict[str, Any], horizon_ms: int, observed_ts: float, payload: dict[str, Any]) -> None:
        try:
            self.db.enqueue_markout(
                run_id=self.run_id,
                slug=candidate["slug"],
                market_id=None,
                candidate_epoch_ns=candidate["candidate_epoch_ns"],
                horizon_ms=int(horizon_ms),
                observed_epoch_ns=int(float(observed_ts) * 1_000_000_000),
                payload=payload,
            )
        except Exception:
            return

    def _record_missed_markout(self, candidate: dict[str, Any], horizon_ms: int, *, now_ts: float, status: str) -> None:
        self._markout(candidate, horizon_ms, now_ts, {
            "event_type": "TREND_ENTRY_SHADOW_MARKOUT",
            "candidate_id": candidate["candidate_id"],
            "slug": candidate["slug"],
            "signal_side": candidate["signal_side"],
            "status": status,
            "horizon_ms": int(horizon_ms),
            "elapsed_sec": max(0.0, float(now_ts) - candidate["candidate_ts"]),
            "markout_bid": None,
            "gross_markout_per_share": None,
        })
        candidate["markouts_seen"].add(int(horizon_ms))

    def _update_markouts(
        self,
        *,
        slug: str,
        instrument_id: str,
        instrument_side: str,
        now_ts: float,
        bid: Decimal,
        bid_size: Decimal | None,
        bid_levels: list[tuple[Decimal, Decimal]],
        quote_age_sec: float | None,
    ) -> None:
        for candidate_id, candidate in tuple(self._pending_markouts.items()):
            if candidate["slug"] != slug or candidate["instrument_id"] != instrument_id:
                continue
            elapsed = max(0.0, float(now_ts) - candidate["candidate_ts"])
            for horizon_ms in TREND_ENTRY_SHADOW_MARKOUTS_MS:
                if horizon_ms in candidate["markouts_seen"]:
                    continue
                target_sec = horizon_ms / 1000.0
                if elapsed < target_sec:
                    continue
                if elapsed > target_sec + self.markout_lateness_sec:
                    self._record_missed_markout(
                        candidate, horizon_ms, now_ts=now_ts, status="missed_quote_window",
                    )
                    continue
                entry_ask = Decimal(str(candidate["entry_ask"]))
                signed = bid - entry_ask
                self._markout(candidate, horizon_ms, now_ts, {
                    "event_type": "TREND_ENTRY_SHADOW_MARKOUT",
                    "candidate_id": candidate_id,
                    "slug": slug,
                    "signal_side": instrument_side,
                    "status": "observed",
                    "horizon_ms": int(horizon_ms),
                    "elapsed_sec": elapsed,
                    "entry_ask": float(entry_ask),
                    "markout_bid": float(bid),
                    "entry_ask_size": candidate.get("entry_ask_size"),
                    "markout_bid_size": float(bid_size) if bid_size is not None else None,
                    "entry_ask_depth_1c": candidate.get("ask_depth_within_1c"),
                    "markout_bid_depth_1c": _near_depth(bid_levels, bid, side="bid", cents=1),
                    "gross_markout_per_share": float(signed),
                    "gross_markout_pct_of_entry": float(signed / entry_ask) if entry_ask > 0 else None,
                    "quote_age_sec": quote_age_sec,
                    "fee_or_slippage_adjusted": False,
                })
                candidate["markouts_seen"].add(int(horizon_ms))
            if len(candidate["markouts_seen"]) == len(TREND_ENTRY_SHADOW_MARKOUTS_MS):
                self._pending_markouts.pop(candidate_id, None)

    def on_quote(
        self,
        *,
        slug: str,
        market_start_ts: float | None,
        market_end_ts: float | None,
        now_ts: float,
        instrument_id: Any,
        instrument_side: str,
        reference_spot: Any,
        reference_ts: float | None,
        reference_source: str,
        strike: Any,
        best_bid: Any,
        best_ask: Any,
        bid_size: Any = None,
        ask_size: Any = None,
        bid_levels: Iterable[Any] | None = None,
        ask_levels: Iterable[Any] | None = None,
        quote_source_ts: float | None = None,
    ) -> int:
        slug = str(slug or "")
        now_ts = float(now_ts)
        if not slug or market_start_ts is None:
            return 0
        bid, ask = _decimal(best_bid), _decimal(best_ask)
        spot, anchor = _decimal(reference_spot), _decimal(strike)
        bid_qty, ask_qty = _decimal(bid_size), _decimal(ask_size)
        bid_levels_norm, ask_levels_norm = _levels(bid_levels), _levels(ask_levels)
        quote_age = max(0.0, now_ts - float(quote_source_ts)) if quote_source_ts else None
        side = str(instrument_side or "").upper()

        if (
            bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid
            and side in {"UP", "DOWN"}
        ):
            self._update_markouts(
                slug=slug, instrument_id=str(instrument_id), instrument_side=side,
                now_ts=now_ts, bid=bid, bid_size=bid_qty, bid_levels=bid_levels_norm,
                quote_age_sec=quote_age,
            )

        market_age = max(0.0, now_ts - float(market_start_ts))
        if spot is None or spot <= 0 or anchor is None or anchor <= 0:
            return 0
        ref_age = max(0.0, now_ts - float(reference_ts)) if reference_ts else None
        if ref_age is None or ref_age > self.max_reference_age_sec:
            return 0
        return_bps = float((spot / anchor - Decimal("1")) * Decimal("10000"))
        signal_side = "UP" if return_bps > 0 else "DOWN" if return_bps < 0 else ""
        emitted = 0
        for config_index, (window_sec, threshold_bps) in enumerate(TREND_ENTRY_SHADOW_CONFIGS):
            key = (slug, window_sec, threshold_bps)
            if key in self._captured or market_age < window_sec:
                continue
            if not signal_side:
                # No directional observation exists at exactly zero return.
                continue
            if side != signal_side:
                continue
            if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
                continue
            if quote_age is not None and quote_age > self.max_quote_age_sec:
                continue
            qualified = abs(return_bps) >= float(threshold_bps)
            candidate_id = f"{slug}:t{window_sec}:b{threshold_bps}"
            candidate_ts = now_ts
            # The research schema's markout uniqueness key is candidate time
            # plus horizon. Multiple threshold variants intentionally share
            # one quote, so reserve a deterministic 1ns identity offset while
            # retaining the true observation time in candidate_ts/payload.
            candidate_epoch_ns = int(candidate_ts * 1_000_000_000) + config_index
            payload = {
                "event_type": "TREND_ENTRY_SHADOW_CANDIDATE",
                "candidate_id": candidate_id,
                "candidate_epoch_ns": candidate_epoch_ns,
                "candidate_observed_ts": candidate_ts,
                "slug": slug,
                "market_start_ts": float(market_start_ts),
                "market_age_sec": market_age,
                "scheduled_window_sec": int(window_sec),
                "schedule_lateness_sec": market_age - window_sec,
                "threshold_bps": int(threshold_bps),
                "btc_open_return_bps": return_bps,
                "signal_side": signal_side,
                "qualified": bool(qualified),
                "reference_spot": float(spot),
                "reference_ts": float(reference_ts),
                "reference_age_sec": ref_age,
                "reference_source": str(reference_source or ""),
                "strike": float(anchor),
                "instrument_id": str(instrument_id),
                "entry_bid": float(bid),
                "entry_ask": float(ask),
                "entry_mid": float((bid + ask) / Decimal("2")),
                "spread": float(ask - bid),
                "entry_bid_size": float(bid_qty) if bid_qty is not None else None,
                "entry_ask_size": float(ask_qty) if ask_qty is not None else None,
                "bid_depth_within_1c": _near_depth(bid_levels_norm, bid, side="bid", cents=1),
                "bid_depth_within_2c": _near_depth(bid_levels_norm, bid, side="bid", cents=2),
                "bid_depth_within_5c": _near_depth(bid_levels_norm, bid, side="bid", cents=5),
                "ask_depth_within_1c": _near_depth(ask_levels_norm, ask, side="ask", cents=1),
                "ask_depth_within_2c": _near_depth(ask_levels_norm, ask, side="ask", cents=2),
                "ask_depth_within_5c": _near_depth(ask_levels_norm, ask, side="ask", cents=5),
                "quote_source_ts": float(quote_source_ts) if quote_source_ts else None,
                "quote_age_sec": quote_age,
                "time_left_sec": max(0.0, float(market_end_ts) - now_ts) if market_end_ts else None,
                "markout_horizons_ms": list(TREND_ENTRY_SHADOW_MARKOUTS_MS),
                "authority": "research_only_no_order_or_ownership",
            }
            candidate = {
                **payload,
                "candidate_ts": candidate_ts,
                "markouts_seen": set(),
            }
            self._decision(slug, candidate_ts, payload)
            self._captured.add(key)
            self._candidates_by_slug.setdefault(slug, []).append(candidate)
            self._pending_markouts[candidate_id] = candidate
            emitted += 1
        return emitted

    def on_settlement(self, *, slug: str, outcome: str, settlement_ts: float) -> int:
        slug = str(slug or "")
        outcome = str(outcome or "").upper()
        if outcome not in {"UP", "DOWN"}:
            return 0
        candidates = self._candidates_by_slug.pop(slug, [])
        for candidate in candidates:
            for horizon_ms in TREND_ENTRY_SHADOW_MARKOUTS_MS:
                if horizon_ms not in candidate["markouts_seen"]:
                    self._record_missed_markout(
                        candidate, horizon_ms, now_ts=float(settlement_ts),
                        status="no_observation_before_settlement",
                    )
            self._decision(slug, float(settlement_ts), {
                "event_type": "TREND_ENTRY_SHADOW_SETTLEMENT",
                "candidate_id": candidate["candidate_id"],
                "candidate_epoch_ns": candidate["candidate_epoch_ns"],
                "slug": slug,
                "scheduled_window_sec": candidate["scheduled_window_sec"],
                "threshold_bps": candidate["threshold_bps"],
                "signal_side": candidate["signal_side"],
                "qualified": candidate["qualified"],
                "outcome": outcome,
                "outcome_source": "strategy_settlement_spot_vs_cached_strike",
                "direction_correct": candidate["signal_side"] == outcome,
                "would_win_if_entered": bool(candidate["qualified"] and candidate["signal_side"] == outcome),
                "settlement_ts": float(settlement_ts),
                "authority": "research_only_no_order_or_ownership",
            })
            self._pending_markouts.pop(candidate["candidate_id"], None)
        self._captured = {key for key in self._captured if key[0] != slug}
        self._unavailable_reported = {key for key in self._unavailable_reported if key[0] != slug}
        return len(candidates)


def record_strategy_quote(
    strategy: Any,
    *,
    instrument_id: Any,
    now_ts: float,
    bid: Any,
    ask: Any,
    bid_size: Any,
    ask_size: Any,
    quote_source_ts: float,
) -> int:
    """Bridge a fresh production quote into the no-authority research recorder."""
    recorder = getattr(strategy, "trend_entry_shadow", None)
    if recorder is None:
        return 0
    try:
        slug = str(getattr(strategy, "current_market_slug", "") or "")
        starts = getattr(strategy, "market_start_ts_by_slug", {}) or {}
        market_start_ts = starts.get(slug)
        if market_start_ts is None:
            market_start_ts = int(slug.rsplit("-", 1)[1])
        side_value = strategy._side_for_instrument_id(instrument_id)
        side = str(getattr(side_value, "value", side_value) or "").upper()
        reference = getattr(strategy, "latest_external_spot", None)
        reference_ts = float(getattr(strategy, "latest_external_spot_source_ts", 0.0) or 0.0)
        reference_source = str(getattr(strategy, "latest_external_spot_source", "") or "")
        strike_map = getattr(strategy, "market_strike_cache_by_slug", {}) or {}
        strike = strike_map.get(slug)
        cache = getattr(strategy, "cache", None)
        book = cache.order_book(instrument_id) if cache is not None else None
        bid_levels = book.bids() if book is not None else None
        ask_levels = book.asks() if book is not None else None
        return recorder.on_quote(
            slug=slug,
            market_start_ts=market_start_ts,
            market_end_ts=getattr(strategy, "current_market_end_timestamp", None),
            now_ts=now_ts,
            instrument_id=instrument_id,
            instrument_side=side,
            reference_spot=reference,
            reference_ts=reference_ts,
            reference_source=reference_source,
            strike=strike,
            best_bid=bid,
            best_ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            bid_levels=bid_levels,
            ask_levels=ask_levels,
            quote_source_ts=quote_source_ts,
        )
    except Exception as exc:
        # Optional research capture must not alter order handling. Report only
        # once per exception class to avoid turning a data issue into log spam.
        reported = getattr(strategy, "_trend_entry_shadow_errors_reported", None)
        if reported is None:
            reported = set()
            strategy._trend_entry_shadow_errors_reported = reported
        error_key = type(exc).__name__
        if error_key not in reported:
            reported.add(error_key)
            logger.warning(f"Trend-entry shadow capture unavailable: {error_key}: {exc}")
        return 0
