from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Dict, Optional

from loguru import logger


class StrategyDBRuntimeMixin:
    def _db_strategy_event(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        if not self.trade_db:
            return
        payload_out: Dict[str, Any] = dict(payload or {})
        if self.current_market_slug and "slug" not in payload_out:
            payload_out["slug"] = self.current_market_slug
        if self.current_market_slug and "market_slug" not in payload_out:
            payload_out["market_slug"] = self.current_market_slug
        if self.instrument_id and "instrument_id" not in payload_out:
            payload_out["instrument_id"] = str(self.instrument_id)
        self.trade_db.log_strategy_event(
            run_id=self.run_id,
            event_type=event_type,
            payload=payload_out,
        )

    def _db_order_event(
        self,
        event_type: str,
        client_order_id: Optional[str] = None,
        venue_order_id: Optional[str] = None,
        side: Optional[str] = None,
        price: Optional[float] = None,
        qty: Optional[float] = None,
        status: Optional[str] = None,
        reason: Optional[str] = None,
        commission_usdc: Optional[float] = None,
        expected_net_usdc: Optional[float] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.trade_db:
            return
        payload_out: Dict[str, Any] = dict(payload or {})
        if self.current_market_slug and "slug" not in payload_out:
            payload_out["slug"] = self.current_market_slug
        if self.current_market_slug and "market_slug" not in payload_out:
            payload_out["market_slug"] = self.current_market_slug
        if self.instrument_id and "instrument_id" not in payload_out:
            payload_out["instrument_id"] = str(self.instrument_id)

        side_out = side
        if side_out:
            side_norm = self._normalize_side_text(side_out)
            if side_norm:
                side_out = side_norm.upper()
        self.trade_db.log_order_event(
            run_id=self.run_id,
            event_type=event_type,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            side=side_out,
            price=price,
            qty=qty,
            status=status,
            reason=reason,
            instrument_id=str(self.instrument_id) if self.instrument_id else None,
            token_id=self.current_token_id,
            fee_rate_bps=self.last_observed_fee_rate_bps,
            expected_net_usdc=expected_net_usdc,
            commission_usdc=commission_usdc,
            payload=payload_out,
        )

    def _db_buy_path_diagnostic(
        self,
        *,
        event_type: str,
        reason: str,
        status: str,
        side: str,
        payload: Optional[Dict[str, Any]] = None,
        price: Optional[float] = None,
        expected_net_usdc: Optional[float] = None,
    ) -> None:
        payload_out: Dict[str, Any] = dict(payload or {})
        self._db_order_event(
            event_type=event_type,
            side=side,
            status=status,
            reason=reason,
            price=price,
            expected_net_usdc=expected_net_usdc,
            payload=payload_out,
        )
        self._db_strategy_event(
            "BUY_PATH_DIAGNOSTIC",
            {
                "order_event_type": event_type,
                "side": side,
                "status": status,
                "reason": reason,
                "price": price,
                "expected_net_usdc": expected_net_usdc,
                **payload_out,
            },
        )

    def _recover_market_strike_from_trade_db_on_startup(self) -> None:
        if not self.trade_db or not self.current_market_slug:
            return
        slug = str(self.current_market_slug or "")
        cached = self.market_strike_cache_by_slug.get(slug)
        if isinstance(cached, Decimal) and cached > 0:
            return
        recovered = self.trade_db.load_latest_locked_strike(slug)
        if not recovered:
            return
        strike = recovered.get("strike")
        if not isinstance(strike, Decimal) or strike <= 0:
            return
        strike_source = str(recovered.get("strike_source") or "trade_db_recovered")
        self.market_strike_cache_by_slug[slug] = strike
        self.market_strike_source_by_slug[slug] = strike_source
        self.market_strike_provisional_by_slug.pop(slug, None)
        self.market_strike_provisional_source_by_slug.pop(slug, None)
        if self.current_market_open_spot is None or Decimal(str(self.current_market_open_spot or "0")) <= 0:
            self.current_market_open_spot = strike
        sample_dt_sec = recovered.get("sample_dt_sec")
        logger.info(
            "Recovered authoritative market strike from trade journal: "
            f"slug={slug} strike=${float(strike):.2f} source={strike_source} "
            f"logged_at={recovered.get('ts')}"
        )
        self._db_strategy_event(
            "MARKET_STRIKE_RECOVERED",
            {
                "slug": slug,
                "strike": float(strike),
                "strike_source": strike_source,
                "authoritative": bool(recovered.get("authoritative", False)),
                "recovered_from_ts": recovered.get("ts"),
                "sample_dt_sec": sample_dt_sec,
            },
        )

    def _emit_buy_observe_diagnostic(
        self,
        *,
        inst_id: Any,
        desired_entry: Dict[str, Any],
        quote_intent_state: Any,
        locked_side_runtime: Any,
        current_inst_inventory_qty: Decimal,
        market_buy_count: int,
        time_left_sec: float | None,
    ) -> None:
        if not self.trade_db:
            return
        reason = str(desired_entry.get("diag_reason", "") or "")
        if not reason and str(getattr(quote_intent_state, "quote_mode", "") or "") != "QuoteMode.OBSERVE":
            return
        if not reason:
            if locked_side_runtime.entry_blocked:
                reason = str(locked_side_runtime.entry_block_reason or "locked_side_entry_blocked")
            else:
                reason = "quote_mode_observe"
        slug = str(self.current_market_slug or "")
        inst_txt = str(inst_id)
        throttle_key = f"{slug}|{inst_txt}|{reason}"
        now_ts = time.time()
        last_ts = float(self._last_buy_observe_diag_ts_by_key.get(throttle_key, 0.0))
        if now_ts - last_ts < float(self.buy_observe_diag_interval_sec):
            return
        self._last_buy_observe_diag_ts_by_key[throttle_key] = now_ts
        payload = {
            "slug": slug,
            "instrument_id": inst_txt,
            "active_side": self.active_side.value,
            "side_score": float(self.side_decision_score),
            "diag_reason": reason,
            "quote_mode": str(getattr(quote_intent_state, "quote_mode", "") or ""),
            "should_quote": bool(desired_entry.get("should_quote", False)),
            "entry_mode": str(desired_entry.get("entry_mode", "") or ""),
            "price": float(desired_entry.get("price")) if desired_entry.get("price") is not None else None,
            "p_fair": float(desired_entry.get("p_fair")) if desired_entry.get("p_fair") is not None else None,
            "robust_net_usdc": float(desired_entry.get("robust_net")) if desired_entry.get("robust_net") is not None else None,
            "directional_edge_ps": float(desired_entry.get("directional_edge_ps")) if desired_entry.get("directional_edge_ps") is not None else None,
            "market_buy_count": int(market_buy_count),
            "inventory_qty": float(current_inst_inventory_qty),
            "locked_side_entry_blocked": bool(locked_side_runtime.entry_blocked),
            "locked_side_entry_block_reason": str(locked_side_runtime.entry_block_reason or ""),
            "time_left_sec": float(time_left_sec) if time_left_sec is not None else None,
        }
        self._db_buy_path_diagnostic(
            event_type="ORDER_OBSERVE_BUY_BLOCKED",
            side="BUY",
            status="OBSERVE",
            reason=reason,
            price=float(desired_entry.get("price")) if desired_entry.get("price") is not None else None,
            expected_net_usdc=float(desired_entry.get("min_expected_net_usdc")) if desired_entry.get("min_expected_net_usdc") is not None else None,
            payload=payload,
        )
