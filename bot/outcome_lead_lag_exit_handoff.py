"""Strategy-owned live Outcome-to-Chainlink fast-follow entry handoff."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from math import gcd
from zoneinfo import ZoneInfo

from loguru import logger
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.objects import Price, Quantity

from bot.depth_risk import ExecutionEstimate, estimate_taker_execution


TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class FastFollowLiveConfig:
    signal_ttl_ms: int = 6_000
    max_entry_price: Decimal = Decimal("0.90")
    max_slippage_ticks: int = 1
    max_entries_per_night: int = 15
    max_loss_usdc_per_night: Decimal = Decimal("5")
    l2_depth_buffer: Decimal = Decimal("1.20")
    l2_max_age_sec: float = 1.0
    failed_entry_cooldown_sec: float = 10.0


def _night_key(now_ts: float) -> str | None:
    local = datetime.fromtimestamp(now_ts, tz=TAIPEI)
    if local.hour >= 19:
        return local.date().isoformat() if local.weekday() < 5 else None
    if local.hour < 7:
        prior = local - timedelta(days=1)
        return prior.date().isoformat() if prior.weekday() < 5 else None
    return None


def _instrument_tick(instrument) -> Decimal:
    raw = getattr(instrument, "price_increment", None)
    if raw is not None:
        try:
            return Decimal(str(raw.as_decimal() if hasattr(raw, "as_decimal") else raw))
        except Exception:
            pass
    return Decimal(str(10 ** (-int(getattr(instrument, "price_precision", 2)))))


def _venue_compatible_fast_follow_quantity(quantity: Decimal, limit_price: Decimal) -> tuple[Decimal, Decimal]:
    """Round down to CLOB's maker/taker amount-accuracy grid.

    Polymarket market BUYs require maker amount (price × shares) to have at
    most two decimals and taker amount to have at most four.  Instrument size
    precision can be six, so blindly using it creates invalid amounts such as
    0.75 × 5.5 = 4.125.  Return (safe_qty, grid_step) without increasing risk.
    """
    quantity = max(Decimal("0"), Decimal(str(quantity)))
    limit_price = max(Decimal("0"), Decimal(str(limit_price)))
    if quantity <= 0 or limit_price <= 0:
        return Decimal("0"), Decimal("0")
    taker_scale = 4
    maker_scale = 2
    price_scale = max(0, -limit_price.as_tuple().exponent)
    price_integer = int((limit_price * (Decimal(10) ** price_scale)).to_integral_value())
    denominator = 10 ** max(0, price_scale + taker_scale - maker_scale)
    unit_step = denominator // gcd(abs(price_integer), denominator)
    grid_step = Decimal(unit_step).scaleb(-taker_scale)
    safe_qty = (quantity / grid_step).to_integral_value(rounding=ROUND_FLOOR) * grid_step
    return safe_qty.quantize(Decimal("0.0001"), rounding=ROUND_FLOOR), grid_step


def _book_asks(book) -> list[tuple[Decimal, Decimal]]:
    """Convert the cached Nautilus L2 book to immutable price/size levels."""
    levels: list[tuple[Decimal, Decimal]] = []
    if book is None:
        return levels
    try:
        raw_levels = book.asks()
    except Exception:
        return levels
    for level in raw_levels:
        try:
            raw_price = level.price
            price = raw_price.as_decimal() if hasattr(raw_price, "as_decimal") else Decimal(str(raw_price))
            raw_size = level.size()
            size = raw_size.as_decimal() if hasattr(raw_size, "as_decimal") else Decimal(str(raw_size))
        except Exception:
            continue
        if price > 0 and size > 0:
            levels.append((price, size))
    return levels


def fast_follow_l2_precheck(
    *,
    asks: list[tuple[Decimal, Decimal]],
    quantity: Decimal,
    limit_price: Decimal,
    depth_buffer: Decimal,
) -> tuple[bool, ExecutionEstimate]:
    """Require full visible FOK capacity plus a conservative depth buffer.

    This is deliberately only an admission check: the live book can change
    after the snapshot, so the FOK order remains the venue-side final guard.
    """
    requested = max(Decimal("0"), Decimal(str(quantity)))
    estimate = estimate_taker_execution(
        side="buy", requested_quantity=requested,
        levels=asks, price_boundary=Decimal(str(limit_price)),
    )
    required_visible = requested * max(Decimal("1"), Decimal(str(depth_buffer)))
    return (
        requested > 0
        and estimate.filled_quantity >= requested
        and estimate.visible_depth >= required_visible,
        estimate,
    )


class OutcomeFastFollowLive:
    """Queue a confirmed signal off-thread; submit only on a quote callback."""

    def __init__(self, strategy, config: FastFollowLiveConfig) -> None:
        self.strategy = strategy
        self.config = config
        self._lock = threading.Lock()
        self._pending = None
        self._attempted_slugs: set[str] = set()
        self._failed_slug_cooldown_until: dict[str, float] = {}
        self._pending_order_ids: dict[str, dict] = {}
        self._position_instruments: set[str] = set()
        # A venue submission is not a trade. Keep completed entry fills
        # separate from short-lived FOK reservations so a reject/cancel cannot
        # exhaust the nightly risk budget.
        self._night_filled_entries: dict[str, int] = {}
        self._night_pending_entry_ids: dict[str, set[str]] = {}
        self._night_realized_pnl: dict[str, Decimal] = {}
        self._position_night_by_instrument: dict[str, str] = {}
        self._loaded_nights: set[str] = set()
        self._blocked_candidate_reasons: set[tuple[int, str]] = set()

    def _ensure_night_loaded(self, night: str) -> bool:
        if not self._runtime_journal_ready():
            return False
        if night in self._loaded_nights:
            return True
        if not bool(getattr(self.strategy, "trade_db_buy_ready", True)):
            return False
        loader = getattr(getattr(self.strategy, "trade_db", None), "load_fast_follow_night_risk", None)
        recovered = loader(night) if loader is not None else {}
        if recovered is None:
            self.strategy.trade_db_buy_ready = False
            self.strategy.trade_db_health_reason = "night_risk_query_failed"
            logger.error("Trade journal night-risk lookup failed; blocking fast-follow BUYs")
            return False
        # Old journals counted submissions, not fills. They cannot reliably
        # distinguish rejected FOKs, so use them conservatively only while
        # bridging an already-started legacy night.
        recovered_filled = recovered.get("filled_entries")
        if recovered_filled is None:
            recovered_filled = recovered.get("legacy_attempted_entries", recovered.get("attempted_entries", 0))
        self._night_filled_entries[night] = max(
            self._night_filled_entries.get(night, 0),
            int(recovered_filled or 0),
        )
        self._night_pending_entry_ids.setdefault(night, set())
        restored_positions = recovered.get("open_position_instruments") or []
        if isinstance(restored_positions, (list, tuple, set)):
            for instrument_id in restored_positions:
                instrument_key = str(instrument_id or "")
                if instrument_key:
                    self._position_instruments.add(instrument_key)
                    self._position_night_by_instrument[instrument_key] = night
        self._night_realized_pnl[night] = min(
            self._night_realized_pnl.get(night, Decimal("0")),
            Decimal(str(recovered.get("realized_pnl_usdc") or "0")),
        )
        self._loaded_nights.add(night)
        return True

    def _runtime_journal_ready(self) -> bool:
        """Fail closed if journal health cannot be read or is no longer ready."""
        try:
            journal = getattr(self.strategy, "trade_db", None)
            runtime_health = getattr(journal, "runtime_health", None)
            if not callable(runtime_health):
                raise RuntimeError("trade journal runtime health is unavailable")
            health = runtime_health()
            if not isinstance(health, dict) or health.get("ready") is not True:
                raise RuntimeError("trade journal runtime health is malformed or not ready")
            ready = True
        except Exception as e:
            ready = False
            health = {"reason": "runtime_health_read_failed", "error": str(e)}
        if not ready:
            self.strategy.trade_db_buy_ready = False
            self.strategy.trade_db_health_reason = str(health.get("reason", "trade_journal_runtime_failure"))
        return ready

    def _persist_night(self, night: str) -> bool:
        """Persist reservations before an entry; a failure must block the FOK."""
        if not self._runtime_journal_ready():
            return False
        try:
            persisted = self.strategy._db_strategy_event("FAST_FOLLOW_RISK_STATE", {
                "night_key": night,
                # Compatibility alias: it now represents completed fills.
                "filled_entries": self._night_filled_entries.get(night, 0),
                "pending_entries": len(self._night_pending_entry_ids.get(night, set())),
                "attempted_entries": self._night_filled_entries.get(night, 0),
                "open_position_instruments": sorted(
                    instrument_id
                    for instrument_id, position_night in self._position_night_by_instrument.items()
                    if position_night == night
                ),
                "realized_pnl_usdc": float(self._night_realized_pnl.get(night, Decimal("0"))),
            })
            if persisted is False:
                return False
        except Exception as e:
            self.strategy.trade_db_buy_ready = False
            self.strategy.trade_db_health_reason = "risk_state_persist_failed"
            logger.error(f"Fast-follow risk-state persistence failed; blocking BUY: {e}")
            return False
        return self._runtime_journal_ready()

    def _rollback_unsubmitted_entry_reservation(self, *, slug: str, night: str, client_order_id: str) -> None:
        self._pending_order_ids.pop(client_order_id, None)
        self._night_pending_entry_ids.setdefault(night, set()).discard(client_order_id)
        self._attempted_slugs.discard(slug)

    def night_risk_snapshot(self, now_ts: float | None = None) -> dict[str, int | str | float | None]:
        """Return the live quota state for status output and diagnostics."""
        now = time.time() if now_ts is None else float(now_ts)
        night = _night_key(now)
        if night is None:
            return {
                "night_key": None,
                "filled_entries": 0,
                "pending_entries": 0,
                "max_entries": self.config.max_entries_per_night,
                "realized_pnl_usdc": 0.0,
            }
        if not self._ensure_night_loaded(night):
            return {
                "night_key": night, "filled_entries": 0, "pending_entries": 0,
                "max_entries": self.config.max_entries_per_night, "realized_pnl_usdc": 0.0,
            }
        return {
            "night_key": night,
            "filled_entries": self._night_filled_entries.get(night, 0),
            "pending_entries": len(self._night_pending_entry_ids.get(night, set())),
            "max_entries": self.config.max_entries_per_night,
            "realized_pnl_usdc": float(self._night_realized_pnl.get(night, Decimal("0"))),
        }

    def _record_blocked(self, candidate, reason: str, **payload) -> None:
        """Record one decision diagnostic without quote-path event spam."""
        key = (int(candidate.created_epoch_ns), reason)
        if key in self._blocked_candidate_reasons:
            return
        self._blocked_candidate_reasons.add(key)
        self.strategy._db_strategy_event("FAST_FOLLOW_ENTRY_BLOCKED", {
            "slug": candidate.slug,
            "reason": reason,
            "direction": candidate.decision.direction,
            **payload,
        })

    def record_candidate(self, candidate) -> None:
        if candidate.decision.state != "follower_confirmed":
            return
        with self._lock:
            self._pending = candidate
        self.strategy._db_strategy_event("FAST_FOLLOW_CONFIRMED", {
            "slug": candidate.slug,
            "market_id": candidate.market_id,
            "direction": candidate.decision.direction,
            "signal_created_epoch_ns": candidate.created_epoch_ns,
            "decision": candidate.decision.__dict__,
        })

    def order_metadata(self, client_order_id: str) -> dict | None:
        return self._pending_order_ids.get(client_order_id)

    def on_order_terminal(self, client_order_id: str) -> None:
        metadata = self._pending_order_ids.pop(client_order_id, None)
        if metadata is None:
            return
        night = metadata.get("night_key")
        if night:
            self._night_pending_entry_ids.setdefault(night, set()).discard(client_order_id)
            self._persist_night(night)
        slug = str(metadata.get("slug") or "")
        if slug:
            self._attempted_slugs.discard(slug)
            self._failed_slug_cooldown_until[slug] = time.time() + self.config.failed_entry_cooldown_sec

    def blocks_normal_buy(self, slug: str) -> bool:
        with self._lock:
            pending_slug = getattr(self._pending, "slug", None)
        return (
            slug in self._attempted_slugs
            or pending_slug == slug
            or time.time() < self._failed_slug_cooldown_until.get(slug, 0.0)
        )

    def on_fill(self, *, client_order_id: str, side: str, instrument_id: str, realized_net_usdc=None) -> None:
        metadata = self._pending_order_ids.pop(client_order_id, None)
        if metadata is not None and side == "buy":
            self._position_instruments.add(instrument_id)
            night = metadata.get("night_key")
            if night:
                self._ensure_night_loaded(night)
                self._position_night_by_instrument[instrument_id] = night
                self._night_pending_entry_ids.setdefault(night, set()).discard(client_order_id)
                self._night_filled_entries[night] = self._night_filled_entries.get(night, 0) + 1
                self._persist_night(night)
        if side == "sell" and instrument_id in self._position_instruments and realized_net_usdc is not None:
            key = self._position_night_by_instrument.get(instrument_id) or _night_key(time.time())
            if key is not None:
                self._ensure_night_loaded(key)
                self._night_realized_pnl[key] = self._night_realized_pnl.get(key, Decimal("0")) + Decimal(str(realized_net_usdc))
                self._persist_night(key)
            state = getattr(self.strategy, "live_inventory_cost", {}).get(instrument_id, {})
            exchange_min = Decimal(str(getattr(self.strategy, "maker_exchange_min_shares", "5")))
            if Decimal(str(state.get("qty", "0"))) < exchange_min:
                self._position_instruments.discard(instrument_id)
                self._position_night_by_instrument.pop(instrument_id, None)
                if key is not None:
                    self._persist_night(key)

    def on_quote(self, *, instrument_id, best_bid: Decimal, best_ask: Decimal, ask_size: Decimal | None, now_ts: float) -> bool:
        with self._lock:
            candidate = self._pending
        if candidate is None or candidate.slug != str(getattr(self.strategy, "current_market_slug", "") or ""):
            return False
        age_ms = (time.time_ns() - candidate.created_epoch_ns) / 1_000_000
        if age_ms < 0 or age_ms > self.config.signal_ttl_ms:
            with self._lock:
                if self._pending is candidate:
                    self._pending = None
            self.strategy._db_strategy_event("FAST_FOLLOW_EXPIRED", {
                "slug": candidate.slug, "direction": candidate.decision.direction, "signal_age_ms": age_ms,
            })
            return False
        slug = candidate.slug
        side = getattr(self.strategy._side_for_instrument_id(instrument_id), "value", "NONE")
        wanted_side = "UP" if candidate.decision.direction > 0 else "DOWN"
        inst_key = self.strategy._instrument_key(instrument_id)
        # Entry-only authority: an opposite-side signal never sells, cancels
        # TP, or otherwise alters an existing position.  The normal strategy
        # remains the sole owner of every exit path.
        if bool(getattr(self.strategy, "maker_kill_switch", False)):
            self._record_blocked(candidate, "maker_kill_switch_on")
            return False
        if not self._runtime_journal_ready():
            self._record_blocked(candidate, "trade_journal_unhealthy",
                                 reason_detail=str(getattr(self.strategy, "trade_db_health_reason", "unknown")))
            return False
        if not bool(getattr(self.strategy, "trade_db_buy_ready", True)):
            self._record_blocked(candidate, "trade_journal_unhealthy",
                                 reason_detail=str(getattr(self.strategy, "trade_db_health_reason", "unknown")))
            return False
        if slug in self._attempted_slugs or int(getattr(self.strategy, "market_buy_count_total_by_slug", {}).get(slug, 0)) > 0:
            self._record_blocked(candidate, "market_already_owned")
            return False
        if time.time() < self._failed_slug_cooldown_until.get(slug, 0.0):
            self._record_blocked(candidate, "failed_fast_follow_cooldown")
            return False
        night = _night_key(now_ts)
        if night is None:
            self._record_blocked(candidate, "outside_taipei_weeknight_session")
            return False
        if not self._ensure_night_loaded(night):
            self._record_blocked(candidate, "trade_journal_unhealthy", reason_detail="night_risk_query_failed")
            return False
        filled_entries = self._night_filled_entries.get(night, 0)
        pending_entries = len(self._night_pending_entry_ids.get(night, set()))
        if filled_entries + pending_entries >= self.config.max_entries_per_night:
            self._record_blocked(
                candidate, "nightly_filled_entry_limit", filled_entries=filled_entries,
                pending_entries=pending_entries, max_entries=self.config.max_entries_per_night,
            )
            return False
        if self._night_realized_pnl.get(night, Decimal("0")) <= -self.config.max_loss_usdc_per_night:
            self._record_blocked(candidate, "nightly_realized_loss_limit")
            return False
        if side != wanted_side or best_ask <= 0 or best_ask > self.config.max_entry_price:
            self._record_blocked(candidate, "side_or_price_ineligible", observed_side=side, best_ask=float(best_ask))
            return False
        if ask_size is not None and ask_size <= 0:
            self._record_blocked(candidate, "no_ask_depth")
            return False
        if bool(getattr(self.strategy, "_twap_reference_degraded", True)):
            self._record_blocked(candidate, "twap_reference_degraded")
            return False
        strike_eligible = getattr(self.strategy, "_market_strike_is_entry_eligible", None)
        if strike_eligible is None or not strike_eligible(slug):
            self._record_blocked(candidate, "strike_not_verified")
            return False
        end_ts = getattr(self.strategy, "current_market_end_timestamp", None)
        min_time_left = max(
            float(getattr(self.strategy, "maker_min_minutes_to_close", 0.0) or 0.0) * 60.0,
            float(getattr(self.strategy, "bi_side_min_time_left_sec", 0.0) or 0.0),
        )
        time_left = end_ts - now_ts if end_ts is not None else None
        if time_left is None or time_left < min_time_left:
            self._record_blocked(candidate, "too_close_to_market_end", time_left=time_left)
            return False
        max_time_left = float(getattr(self.strategy, "first_entry_max_time_left_sec", 0.0) or 0.0)
        if max_time_left > 0 and time_left > max_time_left:
            self._record_blocked(candidate, "too_early_in_market", time_left=time_left)
            return False
        active = getattr(getattr(self.strategy, "active_side", None), "value", "NONE")
        if bool(getattr(self.strategy, "active_side_locked", False)) and active not in {"NONE", wanted_side}:
            self._record_blocked(candidate, "locked_side_invalidated", active_side=active)
            return False
        held = Decimal(str(getattr(self.strategy, "live_inventory_cost", {}).get(inst_key, {}).get("qty", "0")))
        if held > 0:
            self._record_blocked(candidate, "existing_inventory", held=float(held))
            return False

        instrument = self.strategy.cache.instrument(instrument_id)
        if instrument is None:
            self._record_blocked(candidate, "instrument_missing")
            return False
        for state in getattr(self.strategy, "active_maker_orders", {}).values():
            state_side = str(state.get("side", "") or "").lower()
            state_inst_key = self.strategy._instrument_key(state.get("instrument_id"))
            if (
                state_side == "sell"
                and state_inst_key != inst_key
                and bool(state.get("pending_cancel", False))
            ):
                self._record_blocked(
                    candidate, "unresolved_other_market_sell",
                    instrument_id=state_inst_key,
                )
                return False
            if self.strategy._instrument_key(state.get("instrument_id")) != inst_key:
                continue
            self._record_blocked(
                candidate, "existing_order_owner", instrument_id=inst_key,
                order_side=str(state.get("side", "")).lower(),
            )
            return False
        base_shares = Decimal(str(getattr(self.strategy, "maker_fixed_shares", "10")))
        high_threshold = Decimal(str(getattr(self.strategy, "maker_high_entry_price_size_adjust_threshold", "0.70")))
        multiplier = Decimal(str(getattr(self.strategy, "maker_high_entry_price_size_adjust_multiplier", "0.55")))
        quantity = base_shares * multiplier if best_ask > high_threshold else base_shares
        exchange_min = Decimal(str(getattr(self.strategy, "maker_exchange_min_shares", "5")))
        if quantity < exchange_min:
            self._record_blocked(candidate, "quantity_below_exchange_min", quantity=float(quantity))
            return False
        tick = max(Decimal("0.001"), _instrument_tick(instrument))
        limit_price = self.strategy._align_price_to_tick(
            min(self.config.max_entry_price, best_ask + tick * self.config.max_slippage_ticks),
            "buy", instrument,
        )
        requested_quantity = quantity
        quantity, venue_quantity_step = _venue_compatible_fast_follow_quantity(quantity, limit_price)
        if quantity < exchange_min:
            self._record_blocked(
                candidate, "venue_amount_grid_below_exchange_min",
                requested_quantity=float(requested_quantity),
                venue_quantity_step=float(venue_quantity_step),
            )
            return False
        precision = min(4, int(getattr(instrument, "size_precision", 6)))
        balance = getattr(self.strategy, "_cached_usdc_balance", None)
        if balance is None or Decimal(str(balance)) < limit_price * quantity:
            self._record_blocked(candidate, "insufficient_usdc_balance")
            return False

        l2_update_ts = float(
            getattr(self.strategy, "fast_follow_l2_update_ts_by_inst", {}).get(inst_key, 0.0) or 0.0
        )
        l2_age_sec = max(0.0, now_ts - l2_update_ts) if l2_update_ts > 0 else None
        book = None
        try:
            book = self.strategy.cache.order_book(instrument_id)
        except Exception:
            book = None
        asks = _book_asks(book)
        if l2_age_sec is None or l2_age_sec > self.config.l2_max_age_sec or not asks:
            self._record_blocked(
                candidate,
                "l2_book_unavailable_or_stale",
                l2_age_sec=l2_age_sec,
                l2_max_age_sec=self.config.l2_max_age_sec,
                l2_level_count=len(asks),
            )
            return False
        l2_ok, l2_estimate = fast_follow_l2_precheck(
            asks=asks,
            quantity=quantity,
            limit_price=limit_price,
            depth_buffer=self.config.l2_depth_buffer,
        )
        l2_payload = {
            **l2_estimate.as_payload(),
            "l2_age_sec": l2_age_sec,
            "l2_level_count": len(asks),
            "l2_depth_buffer": float(self.config.l2_depth_buffer),
            "required_visible_depth": float(quantity * self.config.l2_depth_buffer),
        }
        if not l2_ok:
            self._record_blocked(candidate, "l2_fok_depth_insufficient", **l2_payload)
            return False

        execution_penalty_bypassed = bool(
            getattr(self.strategy, "outcome_bypass_execution_penalty", False)
        )
        if not execution_penalty_bypassed:
            check = getattr(self.strategy, "fast_follow_execution_penalty_allows", None)
            allowed = False
            if callable(check):
                try:
                    allowed = bool(check(
                        candidate=candidate, instrument_id=instrument_id,
                        limit_price=limit_price, quantity=quantity,
                    ))
                except Exception as e:
                    logger.error(f"Outcome execution-penalty check failed; blocking BUY: {e}")
            if not allowed:
                economics = getattr(self.strategy, "_last_fast_follow_economics_context", {})
                self._record_blocked(
                    candidate,
                    "execution_penalty_check_failed",
                    **(economics if isinstance(economics, dict) else {}),
                )
                return False

        coid = ClientOrderId(f"BTC-15M-FAST-FOLLOW-BUY-{int(now_ts * 1000)}")
        order = self.strategy.order_factory.limit(
            instrument_id=instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity(float(quantity), precision=precision),
            price=Price(float(limit_price), precision=int(getattr(instrument, "price_precision", 2))),
            client_order_id=coid,
            quote_quantity=False,
            time_in_force=TimeInForce.FOK,
        )
        metadata = {
            "slug": slug, "direction": candidate.decision.direction,
            "wanted_side": wanted_side, "signal_age_ms": age_ms,
            "best_bid": float(best_bid), "best_ask": float(best_ask),
            "limit_price": float(limit_price), "quantity": float(quantity),
            "requested_quantity": float(requested_quantity),
            "venue_quantity_step": float(venue_quantity_step),
            "l2_precheck": l2_payload,
            "requested_tif": "FOK", "execution_penalty_bypassed": execution_penalty_bypassed, "night_key": night,
            "instrument_id": str(instrument_id),
            "submit_epoch_ns": time.time_ns(), "submit_monotonic_ns": time.perf_counter_ns(),
        }
        self._attempted_slugs.add(slug)
        self._pending_order_ids[str(coid)] = metadata
        self._night_pending_entry_ids.setdefault(night, set()).add(str(coid))
        if not self._persist_night(night):
            self._rollback_unsubmitted_entry_reservation(slug=slug, night=night, client_order_id=str(coid))
            self._record_blocked(candidate, "risk_state_persist_failed")
            return False
        # This is an intent, not venue acceptance.  It closes the crash window
        # between a durable risk reservation and the FOK reaching the venue.
        try:
            persisted = self.strategy._db_order_event(
                event_type="ORDER_FAST_FOLLOW_INTENT", client_order_id=str(coid), side="BUY",
                price=float(limit_price), qty=float(quantity), status="INTENT",
                reason="outcome_then_twap_confirmed", instrument_id=str(instrument_id), payload=metadata,
            )
            if persisted is False:
                self.strategy.trade_db_buy_ready = False
                self.strategy.trade_db_health_reason = "fast_follow_intent_persist_failed"
        except Exception as e:
            self.strategy.trade_db_buy_ready = False
            self.strategy.trade_db_health_reason = "fast_follow_intent_persist_failed"
            logger.error(f"Fast-follow intent persistence failed; blocking BUY: {e}")
        if not self._runtime_journal_ready():
            self._rollback_unsubmitted_entry_reservation(slug=slug, night=night, client_order_id=str(coid))
            self._record_blocked(candidate, "fast_follow_intent_persist_failed")
            return False
        # Keep local recent-submit state before handing the FOK to the venue.
        # The durable intent above is the crash-recovery evidence if the venue
        # fills but its callback and later submission event are both lost.
        inst_key = self.strategy._instrument_key(instrument_id)
        recent_submits = getattr(self.strategy, "recent_buy_submit_by_inst", None)
        if not isinstance(recent_submits, dict):
            recent_submits = {}
            self.strategy.recent_buy_submit_by_inst = recent_submits
        recent_submits[inst_key] = {
            "price": limit_price, "quantity": quantity,
            "created_ts": time.time(), "client_order_id": str(coid),
        }
        with self._lock:
            if self._pending is candidate:
                self._pending = None
        if self.strategy._is_dry_run_mode():
            event_type = "ORDER_DRY_RUN_SUBMITTED"
        else:
            self.strategy.submit_order(order)
            event_type = "ORDER_FAST_FOLLOW_SUBMIT"
        self.strategy._db_order_event(
            event_type=event_type, client_order_id=str(coid), side="BUY",
            price=float(limit_price), qty=float(quantity), status="SUBMITTED",
            reason="outcome_then_twap_confirmed", payload=metadata,
        )
        logger.warning(
            f"FAST FOLLOW BUY submit: slug={slug} side={wanted_side} qty={quantity} "
            f"limit={limit_price} signal_age_ms={age_ms:.1f}"
        )
        return True


def handoff_confirmed_candidate(*args, **kwargs) -> bool:
    owner = kwargs.pop("owner", None)
    return bool(owner and owner.on_quote(*args, **kwargs))
