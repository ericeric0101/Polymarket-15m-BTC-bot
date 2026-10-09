from __future__ import annotations

import copy
import threading
import time
import math
from collections import deque
from decimal import Decimal
from typing import Any, Dict, Protocol

from loguru import logger

from bot.enums import ActiveSide, MarketPhase
from bot.lifecycle import determine_lifecycle_timer_action, select_next_market_window
from bot.ops import handle_waiting_phase_search
from bot.post_trade import compute_settlement_summary


# Near-tie guard: a 60s TWAP tick stamped `offset` seconds away from market end
# misses (or adds) `offset` of the 60 one-second samples the official label
# averages, so the official value can still differ from it by roughly
# offset/60 * (recent - dropped price spread).  Both observed official/TWAP
# conflicts were near-ties labeled from ticks 2-4 s before end (margins 0.02
# and 0.60 bps); see docs/near_tie_twap_settlement_guard.md.
NEAR_TIE_BPS_PER_OFFSET_SEC = 0.5

# Deferred relabel of a near-tie UNKNOWN settlement: RTDS emits a 60s TWAP
# tick stamped exactly at the 15-minute boundary in 210/221 observed markets,
# received 1.34 s (median) / 2.52 s (max) after it.  Wait at most this long
# after market end for it; otherwise the UNKNOWN stays for startup Gamma
# reconciliation.  The wait never blocks: the tick is captured by the RTDS
# thread and finalized by the lifecycle timer thread.
SETTLEMENT_RELABEL_MAX_WAIT_SEC = 5.0
SETTLEMENT_RELABEL_POLL_SEC = 0.25
# A tick counts as end-stamped when its source clock is within this of end
# (RTDS source timestamps are whole seconds).
SETTLEMENT_RELABEL_END_TOLERANCE_SEC = 0.5
_RECENT_TWAP_TICKS_MAXLEN = 16
_SETTLEMENT_RELABEL_LOCK = threading.Lock()


def canonical_settlement_outcome(label: dict[str, Any]) -> str:
    """UP/DOWN only from a canonical official-TWAP label; otherwise UNKNOWN."""
    side = label.get("side") if label.get("canonical") else None
    return side if side in ("UP", "DOWN") else "UNKNOWN"


def _canonical_twap_shadow_label(
    *, twap_price: Any, source_ts: Any, window_sec: Any, strike: Any,
    settlement_ts: float, freshness_sec: float = 10.0, market_end_ts: Any = None,
    near_tie_bps_per_offset_sec: float = NEAR_TIE_BPS_PER_OFFSET_SEC,
) -> dict[str, Any]:
    """Describe whether the latest direct official-TWAP tick is a valid label.

    This is the runtime settlement authority (see canonical_settlement_outcome).
    Tie semantics: TWAP == strike settles UP (``>=``), compared as floats.
    Near-tie guard: when |TWAP - strike| (bps) is below
    ``near_tie_bps_per_offset_sec * |source_ts - market_end_ts|`` the tick is
    not the end-of-market value closely enough to decide the side, so the
    label is not canonical (-> UNKNOWN; startup Gamma reconciliation resolves
    it).  A tick stamped exactly at market end always decides.  Unknown
    market end falls back to the tick age as the offset.
    """
    try:
        price = float(twap_price)
        observed = float(source_ts)
        window = int(window_sec)
        strike_value = float(strike)
        settled = float(settlement_ts)
        age = settled - observed
        values_valid = all(map(math.isfinite, (price, observed, strike_value, settled, age)))
    except (TypeError, ValueError, OverflowError):
        price = observed = strike_value = 0.0
        window, age, values_valid = 0, None, False
    source = f"polymarket_chainlink_twap_{window}s_ws" if values_valid and price > 0 and window > 0 else "unavailable"
    fresh = bool(
        values_valid and price > 0 and strike_value > 0 and observed > 0
        and window == 60 and age is not None and 0.0 <= age <= float(freshness_sec)
    )
    margin_bps = end_offset = near_tie_band = None
    near_tie_unresolved = False
    if fresh:
        margin_bps = (price - strike_value) / strike_value * 1e4
        try:
            end_offset = abs(observed - float(market_end_ts))
        except (TypeError, ValueError, OverflowError):
            end_offset = None
        if end_offset is None or not math.isfinite(end_offset):
            end_offset = age
        near_tie_band = float(near_tie_bps_per_offset_sec) * end_offset
        near_tie_unresolved = abs(margin_bps) < near_tie_band
    canonical = fresh and not near_tie_unresolved
    return {
        "source": source,
        "age_sec": age,
        "canonical": canonical,
        "side": ("UP" if price >= strike_value else "DOWN") if canonical else None,
        "margin_bps": margin_bps,
        "end_offset_sec": end_offset,
        "near_tie_band_bps": near_tie_band,
        "near_tie_unresolved": near_tie_unresolved,
    }


class StrategyLifecycleHost(Protocol):
    """
    Minimum runtime contract for StrategyLifecycleMixin.

    The mixin still relies on many strategy-level methods; this protocol lists
    the critical ones so lifecycle refactors do not break silently.
    """

    market_phase: MarketPhase
    current_market_slug: str
    current_market_end_timestamp: Any
    inventory_delta_shares: Decimal
    market_cycle_realized_net_usdc: Decimal
    active_side: ActiveSide
    active_maker_orders: Dict[str, Dict[str, Any]]

    def _db_strategy_event(self, event_type: str, payload: Dict[str, Any]) -> None: ...
    def _cancel_active_maker_orders(self) -> None: ...
    def _cancel_maker_order_side(self, order_key: str, reason: str = "") -> None: ...
    def _record_market_settlement(self) -> None: ...
    def _update_terminal_dashboard_snapshot(self) -> None: ...
    def _append_cycle_and_maybe_trigger_regime_guard(
        self,
        *,
        cycle_combined_pnl: float,
        slug: str,
        source: str,
    ) -> None: ...


class StrategyLifecycleMixin:
    """
    Runtime lifecycle orchestration for the live BTC 15-minute bot.

    This keeps market phase transition, settlement bookkeeping, and proactive
    next-market search out of `run_bot.py` while preserving existing behavior.
    """

    def _transition_market_phase(self, new_phase: MarketPhase, now_ts: float) -> None:
        old_phase = self.market_phase
        self.market_phase = new_phase

        end_ts = getattr(self, "current_market_end_timestamp", None)
        time_left = (end_ts - now_ts) if end_ts else None

        logger.warning(
            f"MARKET PHASE: {old_phase.value} → {new_phase.value} "
            f"slug={self.current_market_slug or '-'} "
            f"time_left={time_left / 60:.1f}m" if time_left is not None else
            f"MARKET PHASE: {old_phase.value} → {new_phase.value} "
            f"slug={self.current_market_slug or '-'} "
            f"time_left=N/A"
        )
        self._db_strategy_event(
            "MARKET_PHASE_CHANGE",
            {
                "from": old_phase.value,
                "to": new_phase.value,
                "slug": self.current_market_slug,
                "time_left_sec": time_left,
            },
        )

        if new_phase == MarketPhase.SETTLING:
            self._cancel_active_maker_orders()
            self._record_market_settlement()
            logger.info("Settling: all maker orders cancelled. Waiting for grace period.")
        elif new_phase == MarketPhase.WAITING:
            self._cancel_active_maker_orders()
            self.current_market_end_timestamp = None
            logger.info("Waiting: proactively searching for next market.")
        elif new_phase == MarketPhase.REDUCE_ONLY:
            for order_key, state in list(self.active_maker_orders.items()):
                if str(state.get("side", "")) == "buy":
                    self._cancel_maker_order_side(order_key, reason="reduce_only")
        self._update_terminal_dashboard_snapshot()

    def _record_market_settlement(self) -> None:
        try:
            raw_inv = float(self.inventory_delta_shares)
            ledger_inv = 0.0
            inventory_side = None
            has_inventory_ledger = bool(self.live_inventory_cost)
            for inv_key, inv_state in self.live_inventory_cost.items():
                inv_qty = float(inv_state.get("qty", 0))
                if inv_qty > 0.001:
                    detected = self._side_for_instrument_id(inv_key)
                    if detected != ActiveSide.NONE:
                        ledger_inv += inv_qty
                        if inventory_side is None:
                            inventory_side = detected.value

            inv = ledger_inv if has_inventory_ledger else raw_inv
            if has_inventory_ledger and abs(raw_inv - ledger_inv) > 0.001:
                logger.warning(
                    f"Settlement inventory reconciled from ledger: "
                    f"raw_inv={raw_inv:.6f} ledger_inv={ledger_inv:.6f} "
                    f"slug={self.current_market_slug or ''}"
                )
                self._db_strategy_event(
                    "MARKET_SETTLEMENT_INVENTORY_RECONCILED",
                    {
                        "slug": self.current_market_slug or "",
                        "raw_inventory_delta_shares": raw_inv,
                        "ledger_inventory_shares": ledger_inv,
                        "reason": "settlement_uses_live_inventory_cost_ledger",
                    },
                )
                self.inventory_delta_shares = Decimal(str(ledger_inv))

            # Diagnostic only: the general-purpose spot cache can be stale or
            # move after the settlement window, so it never decides the outcome.
            spot = 0.0
            if self.latest_external_spot is not None and self.latest_external_spot > 0:
                spot = float(self.latest_external_spot)
            elif self.last_external_spot is not None and self.last_external_spot > 0:
                spot = float(self.last_external_spot)

            slug = self.current_market_slug or ""
            strike = 0.0
            if slug and slug in self.market_strike_cache_by_slug:
                strike = float(self.market_strike_cache_by_slug[slug])

            # Single runtime settlement authority: the canonical official-TWAP
            # label. Missing/degraded TWAP -> UNKNOWN; never a spot fallback.
            # Official Polymarket resolution is applied retrospectively only
            # (startup Gamma reconciliation / research), never here.
            shadow_settlement_ts = time.time()
            shadow_label = _canonical_twap_shadow_label(
                twap_price=getattr(self, "_polymarket_chainlink_twap_price", None),
                source_ts=getattr(self, "_polymarket_chainlink_twap_observation_ts", None),
                window_sec=getattr(self, "_polymarket_chainlink_twap_window_sec", None),
                strike=strike,
                settlement_ts=shadow_settlement_ts,
                freshness_sec=float(getattr(self, "_RAW_SPOT_FRESHNESS_SEC", 10.0)),
                market_end_ts=getattr(self, "current_market_end_timestamp", None),
            )
            settlement_outcome = canonical_settlement_outcome(shadow_label)
            shadow_settlement_side = settlement_outcome
            settlement_provenance = {
                "outcome_source": "canonical_twap" if settlement_outcome != "UNKNOWN" else "unavailable",
                "settlement_reference_source": shadow_label["source"],
                "settlement_reference_is_canonical": shadow_label["canonical"],
                "settlement_reference_age_sec": shadow_label["age_sec"],
                "settlement_reference_margin_bps": shadow_label["margin_bps"],
                "settlement_reference_end_offset_sec": shadow_label["end_offset_sec"],
                "settlement_near_tie_band_bps": shadow_label["near_tie_band_bps"],
                "settlement_near_tie_unresolved": shadow_label["near_tie_unresolved"],
                "latest_spot_diagnostic": spot,
            }
            relabel_deadline = None
            market_end_ts = getattr(self, "current_market_end_timestamp", None)
            if shadow_label["near_tie_unresolved"] and slug:
                try:
                    end_value = float(market_end_ts)
                    if math.isfinite(end_value) and end_value > 0:
                        relabel_deadline = end_value + SETTLEMENT_RELABEL_MAX_WAIT_SEC
                except (TypeError, ValueError):
                    relabel_deadline = None
            if relabel_deadline is not None:
                settlement_provenance["settlement_relabel_pending"] = True
                settlement_provenance["settlement_relabel_deadline_ts"] = relabel_deadline
            relabel_base = {
                "slug": slug, "spot": spot, "strike": strike,
                "market_end_ts": market_end_ts, "deadline_ts": relabel_deadline,
                "settled_ts": shadow_settlement_ts, "active_side": self.active_side.value,
                "initial_provenance": dict(settlement_provenance),
            }
            if settlement_outcome == "UNKNOWN":
                reason = (
                    "near-tie TWAP tick not at market end"
                    if shadow_label["near_tie_unresolved"] else "canonical TWAP unavailable or stale"
                )
                logger.warning(
                    f"Settlement outcome UNKNOWN: {reason} "
                    f"(source={shadow_label['source']} age={shadow_label['age_sec']} "
                    f"margin_bps={shadow_label['margin_bps']} end_offset={shadow_label['end_offset_sec']}) "
                    f"slug={slug} inv={inv}"
                )
            if hasattr(self, "_settle_shadow_simulation"):
                self._settle_shadow_simulation(slug=slug, outcome=settlement_outcome, spot=spot, strike=strike)

            stop_shadow = getattr(self, "stop_forensics_shadow", None)
            if stop_shadow is not None and settlement_outcome != "UNKNOWN":
                try:
                    stop_shadow.record_settlement(
                        slug=slug,
                        settlement_side=shadow_settlement_side,
                        settlement_ts=shadow_settlement_ts,
                        settlement_reference_source=shadow_label["source"],
                        settlement_reference_is_canonical=shadow_label["canonical"],
                        settlement_reference_age_sec=shadow_label["age_sec"],
                    )
                except Exception as shadow_error:
                    logger.warning(
                        "Stop continuation shadow settlement capture failed: "
                        f"{type(shadow_error).__name__}: {shadow_error}"
                    )

            trend_shadow = getattr(self, "trend_entry_shadow", None)
            if trend_shadow is not None:
                try:
                    trend_shadow.on_settlement(
                        slug=slug,
                        outcome=settlement_outcome,
                        settlement_ts=time.time(),
                    )
                except Exception as shadow_error:
                    logger.warning(
                        "Trend-entry shadow settlement capture failed: "
                        f"{type(shadow_error).__name__}: {shadow_error}"
                    )
            forward_shadow = getattr(self, "forward_shadow_experiment", None)
            if forward_shadow is not None:
                try:
                    forward_shadow.on_settlement(
                        slug=slug,
                        outcome=settlement_outcome,
                        settlement_ts=shadow_settlement_ts,
                        settlement_source=shadow_label["source"],
                    )
                except Exception as shadow_error:
                    logger.warning(
                        "Forward-shadow settlement capture failed: "
                        f"{type(shadow_error).__name__}: {shadow_error}"
                    )
            twap_shadow = getattr(self, "twap_forward_shadow", None)
            if twap_shadow is not None:
                try:
                    twap_shadow.finalize_market(
                        slug,
                        settlement_side=shadow_settlement_side,
                        settlement_ts=shadow_settlement_ts,
                        settlement_reference_source=shadow_label["source"],
                        settlement_reference_is_canonical=shadow_label["canonical"],
                        settlement_reference_age_sec=shadow_label["age_sec"],
                    )
                except Exception as shadow_error:
                    logger.warning(
                        "TWAP forward-shadow settlement capture failed: "
                        f"{type(shadow_error).__name__}: {shadow_error}"
                    )

            if inv < 0.001:
                logger.info("Settlement: no inventory to settle.")
                # Persist the market label even with no trade. This is required
                # for deterministic replay against every observed market.
                self._db_strategy_event(
                    "MARKET_SETTLEMENT",
                    {
                        "slug": slug,
                        "spot": spot,
                        "strike": strike,
                        "outcome": settlement_outcome,
                        "outcome_only": True,
                        "reference_source": str(getattr(self, "latest_external_spot_source", "") or ""),
                        **settlement_provenance,
                        "active_side": self.active_side.value,
                        "inventory_side": None,
                        "inventory_shares": 0.0,
                        "redeem_per_share": 0.0,
                        "redeem_value_usdc": 0.0,
                        "inventory_cost_usdc": 0.0,
                        "settlement_pnl_usdc": 0.0,
                    },
                )
                cycle_fill_realized = float(self.market_cycle_realized_net_usdc)
                self._append_cycle_and_maybe_trigger_regime_guard(
                    cycle_combined_pnl=cycle_fill_realized,
                    slug=self.current_market_slug or "",
                    source="settlement_no_inventory",
                )
                self._db_strategy_event(
                    "MARKET_CYCLE_PNL",
                    {
                        "slug": self.current_market_slug or "",
                        "active_side": self.active_side.value,
                        "cycle_fill_realized_usdc": cycle_fill_realized,
                        "cycle_settlement_pnl_usdc": 0.0,
                        "cycle_combined_pnl_usdc": cycle_fill_realized,
                        "recent_window_size": len(self.recent_market_combined_pnls),
                    },
                )
                # `cycle_fill_realized` has already been applied on each SELL
                # fill.  The no-inventory branch therefore has no additional
                # finalized settlement PnL to add to the session guard.
                self._cycle_total_trades += 1
                if cycle_fill_realized > 0:
                    self._cycle_total_wins += 1
                if self.terminal_dashboard:
                    self.terminal_dashboard.record_cycle(
                        slug=self.current_market_slug or "",
                        pnl_usdc=cycle_fill_realized,
                    )
                self.market_cycle_realized_net_usdc = Decimal("0")
                if relabel_deadline is not None:
                    self._schedule_settlement_relabel({
                        **relabel_base, "cycle_pnl_written": True, "inv": 0.0,
                        "inventory_side": None, "live_inventory_cost": {},
                        "market_cycle_realized_net_usdc": Decimal("0"),
                    })
                return

            if settlement_outcome == "UNKNOWN":
                # Settlement PnL cannot be known without the canonical label.
                # No MARKET_CYCLE_PNL is written, so startup reconciliation
                # resolves this cycle from the official (Gamma) resolution.
                self._db_strategy_event("MARKET_SETTLEMENT", {
                    "slug": slug,
                    "spot": spot,
                    "strike": strike,
                    "outcome": "UNKNOWN",
                    "settlement_pending": True,
                    "active_side": self.active_side.value,
                    "inventory_side": inventory_side or self.active_side.value,
                    "inventory_shares": inv,
                    "settlement_pnl_usdc": None,
                    **settlement_provenance,
                })
                if relabel_deadline is not None:
                    # Snapshot every per-market input now: rollover resets
                    # slug, inventory and cycle realized PnL for the next market.
                    self._schedule_settlement_relabel({
                        **relabel_base, "cycle_pnl_written": False, "inv": inv,
                        "inventory_side": inventory_side,
                        "live_inventory_cost": copy.deepcopy(self.live_inventory_cost),
                        "market_cycle_realized_net_usdc": Decimal(str(self.market_cycle_realized_net_usdc)),
                    })
                self.market_cycle_realized_net_usdc = Decimal("0")
                return

            self._write_canonical_settlement(
                slug=slug,
                spot=spot,
                strike=strike,
                outcome=settlement_outcome,
                settlement_provenance=settlement_provenance,
                inv=inv,
                inventory_side=inventory_side,
                active_side=self.active_side.value,
                live_inventory_cost=self.live_inventory_cost,
                market_cycle_realized_net_usdc=self.market_cycle_realized_net_usdc,
            )
            self.market_cycle_realized_net_usdc = Decimal("0")
            self._update_terminal_dashboard_snapshot()
        except Exception as e:
            logger.warning(f"Settlement recording failed: {e}")

    def _write_canonical_settlement(
        self, *, slug: str, spot: float, strike: float, outcome: str,
        settlement_provenance: Dict[str, Any], inv: float, inventory_side: Any,
        active_side: str, live_inventory_cost: Dict[str, Dict[str, Any]],
        market_cycle_realized_net_usdc: Decimal, pnl_source: str = "settlement",
    ) -> None:
        """Write MARKET_SETTLEMENT + MARKET_CYCLE_PNL for a canonical UP/DOWN label.

        Every per-market input is passed explicitly so a deferred relabel can
        finalize from its settlement-time snapshot after the live per-market
        state (slug, inventory, cycle realized PnL) has rolled over.
        """
        settlement = compute_settlement_summary(
            outcome=outcome,
            inventory_shares=inv,
            live_inventory_cost=live_inventory_cost,
            market_cycle_realized_net_usdc=market_cycle_realized_net_usdc,
            active_side=active_side,
            inventory_side=inventory_side,
        )

        logger.info(
            f"SETTLEMENT: slug={slug} spot={spot:.2f} strike={strike:.2f} "
            f"outcome={settlement.outcome} active_side={settlement.active_side} "
            f"inventory_side={inventory_side or settlement.active_side} "
            f"inv={inv:.4f} redeem=${settlement.redeem_value:.4f} "
            f"cost=${settlement.inventory_cost:.4f} pnl={settlement.settlement_pnl:+.4f}"
        )

        self._db_strategy_event("MARKET_SETTLEMENT", {
            "slug": slug,
            "spot": spot,
            "strike": strike,
            "outcome": settlement.outcome,
            **settlement_provenance,
            "active_side": settlement.active_side,
            "inventory_side": inventory_side or settlement.active_side,
            "inventory_shares": inv,
            "redeem_per_share": settlement.redeem_per_share,
            "redeem_value_usdc": settlement.redeem_value,
            "inventory_cost_usdc": settlement.inventory_cost,
            "settlement_pnl_usdc": settlement.settlement_pnl,
        })
        self._append_cycle_and_maybe_trigger_regime_guard(
            cycle_combined_pnl=settlement.cycle_combined_pnl,
            slug=slug,
            source=pnl_source,
        )
        self._db_strategy_event(
            "MARKET_CYCLE_PNL",
            {
                "slug": slug,
                "active_side": settlement.active_side,
                "cycle_fill_realized_usdc": settlement.cycle_fill_realized,
                "cycle_settlement_pnl_usdc": settlement.settlement_pnl,
                "cycle_combined_pnl_usdc": settlement.cycle_combined_pnl,
                "recent_window_size": len(self.recent_market_combined_pnls),
            },
        )
        record_session_pnl = getattr(self, "_record_session_realized_pnl", None)
        if callable(record_session_pnl) and Decimal(str(settlement.settlement_pnl)) != 0:
            # Only the residual position's final payout belongs here;
            # prior SELL fills were accounted for at fill time.
            record_session_pnl(Decimal(str(settlement.settlement_pnl)), source=pnl_source)
        self._cycle_total_trades += 1
        if settlement.cycle_combined_pnl > 0:
            self._cycle_total_wins += 1
        if self.terminal_dashboard:
            self.terminal_dashboard.record_cycle(
                slug=slug,
                pnl_usdc=settlement.cycle_combined_pnl,
            )

    # ------------------------------------------------------------------
    # Deferred near-tie relabel
    # ------------------------------------------------------------------

    def _observe_settlement_relabel_twap_tick(self, price: Any, source_ts: Any, window_sec: Any) -> None:
        """RTDS thread hook: O(1), never blocks on I/O.

        Remembers recent TWAP ticks and captures the end-stamped tick for a
        pending relabel.  Finalization happens on the lifecycle timer thread.
        """
        try:
            tick = (float(price), float(source_ts), int(window_sec), time.time())
        except (TypeError, ValueError, OverflowError):
            return
        with _SETTLEMENT_RELABEL_LOCK:
            recent = getattr(self, "_recent_twap_ticks", None)
            if recent is None:
                recent = self._recent_twap_ticks = deque(maxlen=_RECENT_TWAP_TICKS_MAXLEN)
            recent.append(tick)
            pending = getattr(self, "_pending_settlement_relabel", None)
            if pending is not None and pending.get("end_tick") is None and self._is_relabel_end_tick(pending, tick):
                pending["end_tick"] = tick

    @staticmethod
    def _is_relabel_end_tick(pending: Dict[str, Any], tick: tuple) -> bool:
        price, source_ts, window, _received = tick
        try:
            end_ts = float(pending.get("market_end_ts"))
        except (TypeError, ValueError):
            return False
        return (
            price > 0 and window == 60 and math.isfinite(source_ts)
            and abs(source_ts - end_ts) < SETTLEMENT_RELABEL_END_TOLERANCE_SEC
        )

    def _schedule_settlement_relabel(self, pending: Dict[str, Any]) -> None:
        """Arm a deferred relabel from a settlement-time snapshot (non-blocking)."""
        with _SETTLEMENT_RELABEL_LOCK:
            superseded = getattr(self, "_pending_settlement_relabel", None)
            pending = dict(pending, end_tick=None)
            # The end tick may already have arrived (and been overwritten by
            # a later tick) before settlement ran.
            for tick in getattr(self, "_recent_twap_ticks", None) or ():
                if self._is_relabel_end_tick(pending, tick):
                    pending["end_tick"] = tick
            self._pending_settlement_relabel = pending
        if superseded is not None:
            if superseded.get("end_tick") is not None:
                try:
                    self._finalize_settlement_relabel(superseded, time.time())
                except Exception as exc:
                    logger.warning(f"Settlement relabel failed for slug={superseded.get('slug')}: {exc}")
            else:
                self._expire_settlement_relabel(superseded, reason="superseded_by_next_settlement")
        logger.info(
            f"Settlement relabel armed: slug={pending['slug']} "
            f"deadline_in={pending['deadline_ts'] - time.time():.2f}s "
            f"end_tick_already_seen={pending['end_tick'] is not None}"
        )

    def _process_pending_settlement_relabel(self, now_ts: float | None = None) -> bool:
        """Lifecycle-timer hook.  Returns True while a relabel is still waiting.

        Exactly-once: the pending snapshot is detached under the lock before
        anything is written, so concurrent callers cannot finalize it twice.
        """
        now = time.time() if now_ts is None else float(now_ts)
        with _SETTLEMENT_RELABEL_LOCK:
            pending = getattr(self, "_pending_settlement_relabel", None)
            if pending is None:
                return False
            if pending.get("end_tick") is None and now < float(pending["deadline_ts"]):
                return True
            self._pending_settlement_relabel = None
        if pending.get("end_tick") is None:
            self._expire_settlement_relabel(pending, reason="end_stamped_twap_tick_not_received")
            return False
        try:
            self._finalize_settlement_relabel(pending, now)
        except Exception as exc:
            logger.warning(f"Settlement relabel failed for slug={pending.get('slug')}: {exc}")
        return False

    def _expire_settlement_relabel(self, pending: Dict[str, Any], *, reason: str) -> None:
        logger.warning(
            f"Settlement relabel expired: slug={pending.get('slug')} reason={reason}; "
            "UNKNOWN stays for startup Gamma reconciliation"
        )
        self._db_strategy_event("MARKET_SETTLEMENT_RELABEL_EXPIRED", {
            "slug": pending.get("slug"),
            "reason": reason,
            "market_end_ts": pending.get("market_end_ts"),
            "deadline_ts": pending.get("deadline_ts"),
            "cycle_pnl_written": bool(pending.get("cycle_pnl_written")),
        })

    def _finalize_settlement_relabel(self, pending: Dict[str, Any], now_ts: float) -> None:
        price, source_ts, window, received_ts = pending["end_tick"]
        label = _canonical_twap_shadow_label(
            twap_price=price, source_ts=source_ts, window_sec=window,
            strike=pending["strike"], settlement_ts=received_ts,
            freshness_sec=float(getattr(self, "_RAW_SPOT_FRESHNESS_SEC", 10.0)),
            market_end_ts=pending["market_end_ts"],
        )
        outcome = canonical_settlement_outcome(label)
        if outcome == "UNKNOWN":
            self._expire_settlement_relabel(pending, reason="end_stamped_twap_tick_not_canonical")
            return
        slug = pending["slug"]
        initial = pending["initial_provenance"]
        provenance = {
            "outcome_source": "canonical_twap_deferred_relabel",
            "settlement_reference_source": label["source"],
            "settlement_reference_is_canonical": label["canonical"],
            "settlement_reference_age_sec": label["age_sec"],
            "settlement_reference_margin_bps": label["margin_bps"],
            "settlement_reference_end_offset_sec": label["end_offset_sec"],
            "settlement_near_tie_band_bps": label["near_tie_band_bps"],
            "settlement_near_tie_unresolved": label["near_tie_unresolved"],
            "latest_spot_diagnostic": pending["spot"],
            "settlement_relabel_of_pending": True,
            "settlement_relabel_twap": price,
            "settlement_relabel_tick_source_ts": source_ts,
            "settlement_relabel_tick_received_ts": received_ts,
            "settlement_relabel_delay_sec": now_ts - float(pending["settled_ts"]),
            "settlement_initial_reference_margin_bps": initial.get("settlement_reference_margin_bps"),
            "settlement_initial_reference_end_offset_sec": initial.get("settlement_reference_end_offset_sec"),
        }
        logger.warning(
            f"Settlement relabeled from end-stamped TWAP tick: slug={slug} outcome={outcome} "
            f"margin_bps={label['margin_bps']} initial_margin_bps={initial.get('settlement_reference_margin_bps')}"
        )
        if pending["cycle_pnl_written"]:
            # No inventory: MARKET_CYCLE_PNL (fill-only) was already written at
            # settlement; only the market label is upgraded.
            self._db_strategy_event("MARKET_SETTLEMENT", {
                "slug": slug, "spot": pending["spot"], "strike": pending["strike"],
                "outcome": outcome, "outcome_only": True,
                **provenance,
                "active_side": pending["active_side"], "inventory_side": None,
                "inventory_shares": 0.0, "redeem_per_share": 0.0, "redeem_value_usdc": 0.0,
                "inventory_cost_usdc": 0.0, "settlement_pnl_usdc": 0.0,
            })
            return
        self._write_canonical_settlement(
            slug=slug,
            spot=pending["spot"],
            strike=pending["strike"],
            outcome=outcome,
            settlement_provenance=provenance,
            inv=pending["inv"],
            inventory_side=pending["inventory_side"],
            active_side=pending["active_side"],
            live_inventory_cost=pending["live_inventory_cost"],
            market_cycle_realized_net_usdc=pending["market_cycle_realized_net_usdc"],
            pnl_source="settlement_deferred_relabel",
        )
        self._update_terminal_dashboard_snapshot()

    def _search_next_market(self) -> bool:
        try:
            btc_slugs = self._resolve_btc_15m_market_slugs()
            if not btc_slugs:
                logger.debug("Next market search: no slugs found")
                self.next_market_slug = None
                self.next_market_start_ts = None
                return False

            now_ts = time.time()
            best_slug, best_start_ts = select_next_market_window(
                btc_slugs=btc_slugs,
                now_ts=now_ts,
            )

            if best_slug:
                self.next_market_slug = best_slug
                self.next_market_start_ts = best_start_ts
                from bot.instrument_admission import request_instrument_admission
                request_instrument_admission(self, best_slug)
                time_until = best_start_ts - now_ts if best_start_ts else 0
                logger.info(
                    f"Next market found: {best_slug} "
                    f"(starts in {time_until / 60:.1f}m)"
                )

                previous_slug = self.current_market_slug
                if self._find_btc_instrument():
                    if self.current_market_slug != previous_slug:
                        logger.info(
                            f"Switched to new market: {previous_slug} → {self.current_market_slug}"
                        )
                        if self.auto_redeem_enabled and self.auto_redeem_on_rollover and previous_slug:
                            self._schedule_auto_redeem(
                                reason=f"lifecycle_rollover:{previous_slug}->{self.current_market_slug}"
                            )
                    return True
            else:
                self.next_market_slug = None
                self.next_market_start_ts = None
                logger.debug("Next market search: no future markets found")
        except Exception as e:
            logger.warning(f"Next market search failed: {e}")
        return False

    def _start_market_lifecycle_timer(self) -> None:
        while not self._lifecycle_stop_event.is_set():
            if self._stopping:
                return

            now_ts = time.time()
            phase = self._update_market_phase()
            relabel_waiting = self._process_pending_settlement_relabel()
            end_ts = getattr(self, "current_market_end_timestamp", None)
            action = determine_lifecycle_timer_action(
                phase_value=phase.value,
                now_ts=now_ts,
                end_ts=end_ts,
                min_minutes_to_close=float(self.maker_min_minutes_to_close),
                settling_grace_sec=float(self.market_settling_grace_sec),
                market_settling_since_ts=float(self._market_settling_since_ts),
            )

            if action.should_reload_instrument:
                self._lifecycle_stop_event.wait(action.wait_sec or 0.0)
                try:
                    if not self._find_btc_instrument():
                        logger.warning("Lifecycle timer: no BTC instrument found")
                except Exception as e:
                    logger.error(f"Lifecycle timer reload failed: {e}")
                continue

            if action.wait_sec is not None and not action.should_search_next:
                wait_sec = action.wait_sec
                if relabel_waiting:
                    wait_sec = min(wait_sec, SETTLEMENT_RELABEL_POLL_SEC)
                self._lifecycle_stop_event.wait(wait_sec)
                continue

            if relabel_waiting:
                # Short settling grace: keep polling the bounded relabel
                # before the (longer) next-market search waits.
                self._lifecycle_stop_event.wait(SETTLEMENT_RELABEL_POLL_SEC)
                continue

            if action.should_search_next:
                max_waiting_misses = 3

                def _request_rollover() -> None:
                    self._collection_stop_context = {
                        "stop_request_source": "strategy_requested",
                        "lifecycle_reason": "stale_instrument_lifecycle",
                        "waiting_miss_count": self._waiting_miss_count,
                        "next_market_slug": self.next_market_slug,
                    }
                    self._waiting_miss_count = 0
                    self._stopping = True
                    self._rollover_requested_flag = True
                    try:
                        stop_node = getattr(self, "_request_node_stop_callback", None)
                        if not callable(stop_node):
                            raise RuntimeError("launcher node-stop callback is not configured")
                        stop_node()
                    except Exception as exc:
                        self._stopping = False
                        self._rollover_requested_flag = False
                        logger.error(f"Lifecycle node rollover stop failed: {exc}")

                self._waiting_miss_count = handle_waiting_phase_search(
                    search_next_market_fn=self._search_next_market,
                    update_market_phase_fn=self._update_market_phase,
                    schedule_auto_redeem_fn=self._schedule_auto_redeem if self.auto_redeem_enabled else None,
                    next_market_slug=self.next_market_slug,
                    market_next_poll_sec=float(self.market_next_poll_sec),
                    waiting_miss_count=getattr(self, "_waiting_miss_count", 0),
                    max_waiting_misses=max_waiting_misses,
                    lifecycle_wait_fn=self._lifecycle_stop_event.wait,
                    logger_info_fn=logger.info,
                    logger_warning_fn=logger.warning,
                    request_rollover_fn=_request_rollover,
                )
                if self._stopping and self._rollover_requested_flag:
                    return
