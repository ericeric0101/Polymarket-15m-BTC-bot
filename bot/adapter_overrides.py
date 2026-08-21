from __future__ import annotations

import asyncio
import os
import time
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
import py_clob_client_v2.http_helpers.helpers as pyclob_helpers
from loguru import logger
from py_clob_client_v2.exceptions import PolyApiException


_QUOTE_PROVENANCE_TTL_SEC = 600.0
_quote_provenance_by_tick_key: dict[tuple[object, ...], dict[str, object]] = {}


def _quote_provenance_key(quote: object) -> tuple[object, ...]:
    """Build a key that survives Nautilus message-bus object copies."""
    instrument_id = getattr(quote, "instrument_id", None)
    ts_event = getattr(quote, "ts_event", None)
    ts_init = getattr(quote, "ts_init", None)
    if instrument_id is not None and ts_event is not None and ts_init is not None:
        return (str(instrument_id), int(ts_event), int(ts_init))
    # Simple test doubles do not always expose Nautilus timestamps.
    return ("object_id", id(quote))


def record_quote_provenance(
    quote: object,
    *,
    source: str,
    raw_ws_received_ts: float | None = None,
) -> None:
    """Attach adapter-side provenance without mutating Nautilus QuoteTick objects."""
    now_ts = time.time()
    if len(_quote_provenance_by_tick_key) > 4096:
        cutoff = now_ts - _QUOTE_PROVENANCE_TTL_SEC
        stale_keys = [
            key
            for key, metadata in _quote_provenance_by_tick_key.items()
            if float(metadata.get("recorded_ts", 0.0)) < cutoff
        ]
        for key in stale_keys:
            _quote_provenance_by_tick_key.pop(key, None)
    _quote_provenance_by_tick_key[_quote_provenance_key(quote)] = {
        "source": str(source),
        "recorded_ts": now_ts,
        "raw_ws_received_ts": (
            float(raw_ws_received_ts) if raw_ws_received_ts is not None else None
        ),
    }


def quote_provenance_for_tick(tick: object) -> dict[str, object]:
    """Return best-effort provenance for the QuoteTick delivered to a strategy."""
    return dict(_quote_provenance_by_tick_key.get(_quote_provenance_key(tick), {}))


def record_quote_data_engine_queue_depth(quote: object, queue_depth: int) -> None:
    """Attach DataEngine queue depth observed immediately before enqueue."""
    metadata = _quote_provenance_by_tick_key.get(_quote_provenance_key(quote))
    if metadata is not None:
        metadata["data_engine_queue_depth"] = int(queue_depth)


def coalesce_price_changes_by_asset(price_changes: object) -> list[list[object]]:
    """Group a raw CLOB message so each asset publishes only its final quote."""
    grouped: dict[str, list[object]] = {}
    for change in price_changes:
        grouped.setdefault(str(change.asset_id), []).append(change)
    return list(grouped.values())


def retain_latest_quote(pending_quotes: dict[str, object], quote: object) -> None:
    """Keep only the newest undelivered QuoteTick for each instrument."""
    pending_quotes[str(quote.instrument_id)] = quote


def position_fetch_retry_delay_sec(error: Exception, attempt: int) -> float | None:
    """Return bounded retry delay only for temporary Data API rate limits."""
    if "HTTP 429:" not in str(error):
        return None
    return min(8.0, float(2 ** max(0, int(attempt))))


def should_publish_order_book_deltas(*, has_delta_subscription: bool) -> bool:
    """Avoid filling the DataEngine with depth updates no consumer requested."""
    return bool(has_delta_subscription)


def should_emit_quote_heartbeat(
    *,
    quote_unchanged: bool,
    now_ns: int,
    last_emit_ns: int,
    heartbeat_sec: float,
) -> bool:
    """Keep a subscribed top-of-book fresh when only deeper book levels change."""
    if not quote_unchanged:
        return True
    return now_ns - last_emit_ns >= int(max(0.1, heartbeat_sec) * 1_000_000_000)


def should_emit_transport_heartbeat(*, is_connected: bool, has_quote: bool) -> bool:
    """Return whether a cached quote should carry a transport-liveness heartbeat."""
    return is_connected and has_quote


def build_transport_heartbeat_quote(data_mod, quote: object, ts_init: int):
    """Clone a cached quote so heartbeat provenance cannot overwrite live data."""
    return data_mod.QuoteTick(
        instrument_id=quote.instrument_id,
        bid_price=quote.bid_price,
        ask_price=quote.ask_price,
        bid_size=quote.bid_size,
        ask_size=quote.ask_size,
        ts_event=quote.ts_event,
        ts_init=ts_init,
    )


def install_runtime_compatibility_overrides() -> None:
    _install_polymarket_data_overrides()
    _install_live_data_engine_observability_override()
    _install_polymarket_execution_overrides()
    _install_polymarket_user_trade_overrides()
    _install_pyclob_http_overrides()


def verify_runtime_compatibility_targets(project_root: Path) -> list[str]:
    targets: list[str] = []
    targets.append(f"project_root={project_root}")

    from nautilus_trader.adapters.polymarket.data import PolymarketDataClient
    from nautilus_trader.adapters.polymarket.execution import PolymarketExecutionClient

    targets.append(f"PolymarketDataClient._handle_quote={hasattr(PolymarketDataClient, '_handle_quote')}")
    targets.append(f"PolymarketExecutionClient._handle_ws_order_msg={hasattr(PolymarketExecutionClient, '_handle_ws_order_msg')}")
    try:
        from nautilus_trader.adapters.polymarket.schemas.user import PolymarketUserTrade
        targets.append(f"PolymarketUserTrade.last_qty={hasattr(PolymarketUserTrade, 'last_qty')}")
    except Exception as exc:
        targets.append(f"PolymarketUserTrade.import_error={exc}")
    targets.append(f"pyclob.request={hasattr(pyclob_helpers, 'request')}")
    return targets


def _install_polymarket_data_overrides() -> None:
    import nautilus_trader.adapters.polymarket.data as data_mod

    cls = data_mod.PolymarketDataClient
    if getattr(cls, "_btc15m_runtime_compat_patched", False):
        return

    original_init = cls.__init__
    original_connect = cls._connect
    original_disconnect = cls._disconnect
    original_handle_raw_ws_message = cls._handle_raw_ws_message

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        if not hasattr(self, "_drop_quote_warn_last_ts"):
            self._drop_quote_warn_last_ts = {}
        if not hasattr(self, "_drop_quote_warn_throttle_sec"):
            self._drop_quote_warn_throttle_sec = 30.0
        if not hasattr(self, "_tick_size_warn_last_ts"):
            self._tick_size_warn_last_ts = {}
        if not hasattr(self, "_tick_size_warn_throttle_sec"):
            self._tick_size_warn_throttle_sec = 60.0
        if not hasattr(self, "_quote_heartbeat_emit_ns"):
            self._quote_heartbeat_emit_ns = {}
        if not hasattr(self, "_quote_heartbeat_sec"):
            self._quote_heartbeat_sec = 5.0
        if not hasattr(self, "_quote_transport_heartbeat_task"):
            self._quote_transport_heartbeat_task = None
        if not hasattr(self, "_quote_delivery_pending"):
            self._quote_delivery_pending = {}
        if not hasattr(self, "_quote_delivery_task"):
            self._quote_delivery_task = None
        if not hasattr(self, "_quote_delivery_coalesce_sec"):
            self._quote_delivery_coalesce_sec = max(
                0.05,
                float(os.getenv("POLYMARKET_QUOTE_COALESCE_SEC", "0.25")),
            )

    async def flush_latest_quotes(self) -> None:
        """Deliver a bounded, latest-only QuoteTick stream to the strategy loop."""
        try:
            while True:
                await asyncio.sleep(float(self._quote_delivery_coalesce_sec))
                pending = self._quote_delivery_pending
                self._quote_delivery_pending = {}
                for quote in pending.values():
                    self._handle_data(quote)
                if not self._quote_delivery_pending:
                    return
        finally:
            self._quote_delivery_task = None

    def queue_latest_quote(self, quote) -> None:
        """Replace intermediate quotes that have not yet reached the main loop."""
        retain_latest_quote(self._quote_delivery_pending, quote)
        task = getattr(self, "_quote_delivery_task", None)
        if task is None or task.done():
            self._quote_delivery_task = self.create_task(flush_latest_quotes(self))

    async def quote_transport_heartbeat(self) -> None:
        """Publish cached quotes while the protocol-level WebSocket remains connected.

        The cached quote retains its original exchange timestamp. Strategy code can
        therefore observe transport liveness without treating this as fresh pricing.
        """
        while True:
            await asyncio.sleep(float(getattr(self, "_quote_heartbeat_sec", 5.0)))
            is_connected = bool(self._ws_client.is_connected())
            for instrument_id, quote in tuple(getattr(self, "_last_quotes", {}).items()):
                if instrument_id not in self.subscribed_quote_ticks():
                    continue
                if should_emit_transport_heartbeat(
                    is_connected=is_connected,
                    has_quote=quote is not None,
                ):
                    heartbeat = build_transport_heartbeat_quote(
                        data_mod,
                        quote,
                        self._clock.timestamp_ns(),
                    )
                    record_quote_provenance(heartbeat, source="transport_heartbeat")
                    self._handle_data(heartbeat)

    async def patched_connect(self) -> None:
        await original_connect(self)
        task = getattr(self, "_quote_transport_heartbeat_task", None)
        if task is None or task.done():
            self._quote_transport_heartbeat_task = self.create_task(quote_transport_heartbeat(self))

    async def patched_disconnect(self) -> None:
        task = getattr(self, "_quote_transport_heartbeat_task", None)
        if task is not None:
            task.cancel()
            self._quote_transport_heartbeat_task = None
        delivery_task = getattr(self, "_quote_delivery_task", None)
        if delivery_task is not None:
            delivery_task.cancel()
            self._quote_delivery_task = None
        self._quote_delivery_pending = {}
        await original_disconnect(self)

    def patched_handle_raw_ws_message(self, raw: bytes) -> None:
        """Capture ingress time before Nautilus decodes and routes a WS payload."""
        self._btc15m_raw_ws_received_ts = time.time()
        original_handle_raw_ws_message(self, raw)

    def patched_log_drop_quote_warning_throttled(self, instrument_id, reason: str) -> None:
        key = f"{instrument_id}:{reason}"
        now_ts = time.time()
        last_ts = getattr(self, "_drop_quote_warn_last_ts", {}).get(key, 0.0)
        if now_ts - last_ts < getattr(self, "_drop_quote_warn_throttle_sec", 30.0):
            return
        self._drop_quote_warn_last_ts[key] = now_ts
        self._log.warning(f"Dropping QuoteTick for {instrument_id}: {reason}")

    def patched_log_tick_size_warning_throttled(self, instrument, change) -> None:
        key = str(instrument.id)
        now_ts = time.time()
        last_ts = getattr(self, "_tick_size_warn_last_ts", {}).get(key, 0.0)
        if now_ts - last_ts < getattr(self, "_tick_size_warn_throttle_sec", 60.0):
            return
        self._tick_size_warn_last_ts[key] = now_ts
        ws_tick = getattr(change, "tick_size", None)
        if ws_tick is None:
            ws_tick = getattr(change, "min_tick_size", None)
        self._log.warning(
            f"Instrument tick size changed: id={instrument.id} price_increment={instrument.price_increment} ws_tick={ws_tick}",
        )

    def patched_apply_quote_change(self, instrument, ws_message, price_change) -> bool:
        """Apply an incremental CLOB update to the local book without publishing a quote."""
        now_ns = self._clock.timestamp_ns()
        order = data_mod.BookOrder(
            side=data_mod.OrderSide.BUY if price_change.side == data_mod.PolymarketOrderSide.BUY else data_mod.OrderSide.SELL,
            price=instrument.make_price(float(price_change.price)),
            size=instrument.make_qty(float(price_change.size)),
            order_id=0,
        )
        delta = data_mod.OrderBookDelta(
            instrument_id=instrument.id,
            action=data_mod.BookAction.UPDATE if order.size > 0 else data_mod.BookAction.DELETE,
            order=order,
            flags=data_mod.RecordFlag.F_LAST,
            sequence=0,
            ts_event=data_mod.millis_to_nanos(float(ws_message.timestamp)),
            ts_init=now_ns,
        )
        deltas = data_mod.OrderBookDeltas(instrument.id, [delta])

        if instrument.id not in self._local_books:
            if (
                instrument.id not in self.subscribed_quote_ticks()
                and instrument.id not in self.subscribed_order_book_deltas()
            ):
                return False
            self._create_local_book(instrument.id)

        local_book = self._local_books[instrument.id]
        local_book.apply(deltas)
        if should_publish_order_book_deltas(
            has_delta_subscription=instrument.id in self.subscribed_order_book_deltas(),
        ):
            self._handle_data(deltas)
        return True

    def patched_publish_quote(self, instrument, ws_message) -> None:
        """Publish the final top-of-book after all message updates are applied."""
        if instrument.id in self.subscribed_quote_ticks():
            now_ns = self._clock.timestamp_ns()
            local_book = self._local_books[instrument.id]
            bid_price = local_book.best_bid_price()
            ask_price = local_book.best_ask_price()
            bid_size = local_book.best_bid_size()
            ask_size = local_book.best_ask_size()

            if bid_price is None or ask_price is None:
                if self._config.drop_quotes_missing_side:
                    self._log_drop_quote_warning_throttled(
                        instrument.id,
                        f"bid_price={bid_price}, ask_price={ask_price}",
                    )
                    return
                if bid_price is None:
                    bid_price = instrument.make_price(data_mod.POLYMARKET_MIN_PRICE)
                    bid_size = instrument.make_qty(0.0)
                if ask_price is None:
                    ask_price = instrument.make_price(data_mod.POLYMARKET_MAX_PRICE)
                    ask_size = instrument.make_qty(0.0)

            quote = data_mod.QuoteTick(
                instrument_id=instrument.id,
                bid_price=bid_price,
                ask_price=ask_price,
                bid_size=bid_size,
                ask_size=ask_size,
                ts_event=data_mod.millis_to_nanos(float(ws_message.timestamp)),
                ts_init=self._clock.timestamp_ns(),
            )

            last_quote = self._last_quotes.get(instrument.id)
            quote_unchanged = last_quote is not None and (
                quote.bid_price == last_quote.bid_price
                and quote.ask_price == last_quote.ask_price
                and quote.bid_size == last_quote.bid_size
                and quote.ask_size == last_quote.ask_size
            )
            last_emit_ns = int(getattr(self, "_quote_heartbeat_emit_ns", {}).get(instrument.id, 0) or 0)
            if not should_emit_quote_heartbeat(
                quote_unchanged=quote_unchanged,
                now_ns=now_ns,
                last_emit_ns=last_emit_ns,
                heartbeat_sec=float(getattr(self, "_quote_heartbeat_sec", 5.0)),
            ):
                return

            self._last_quotes[instrument.id] = quote
            self._quote_heartbeat_emit_ns[instrument.id] = now_ns
            record_quote_provenance(
                quote,
                source="ws_price_change",
                raw_ws_received_ts=getattr(self, "_btc15m_raw_ws_received_ts", None),
            )
            self._queue_latest_quote(quote)

    def patched_handle_quote(self, instrument, ws_message, price_change) -> None:
        if self._apply_quote_change(instrument, ws_message, price_change):
            self._publish_quote(instrument, ws_message)

    def patched_handle_quotes(self, ws_message) -> None:
        """Coalesce quote fan-out while retaining every local-book delta in order."""
        for changes in coalesce_price_changes_by_asset(ws_message.price_changes):
            first_change = changes[0]
            instrument_id = data_mod.get_polymarket_instrument_id(ws_message.market, first_change.asset_id)
            instrument = self._cache.instrument(instrument_id)
            if instrument is None:
                self._log.error(f"Cannot find instrument for {instrument_id}")
                continue
            applied = False
            for price_change in changes:
                applied = self._apply_quote_change(instrument, ws_message, price_change) or applied
            if applied:
                self._publish_quote(instrument, ws_message)

    def patched_handle_book_snapshot(self, instrument, ws_message) -> None:
        now_ns = self._clock.timestamp_ns()
        deltas = ws_message.parse_to_snapshot(instrument=instrument, ts_init=now_ns)
        if deltas is None:
            return
        if should_publish_order_book_deltas(
            has_delta_subscription=instrument.id in self.subscribed_order_book_deltas(),
        ):
            self._handle_deltas(instrument, deltas)
        else:
            # Retain a complete local book for quote generation while avoiding
            # a DataEngine event for depth nobody subscribed to consume.
            local_book = data_mod.OrderBook(instrument.id, book_type=data_mod.BookType.L2_MBP)
            local_book.apply_deltas(deltas)
            self._local_books[instrument.id] = local_book
        if instrument.id in self.subscribed_quote_ticks():
            quote = ws_message.parse_to_quote(
                instrument=instrument,
                ts_init=now_ns,
                drop_quotes_missing_side=self._config.drop_quotes_missing_side,
            )
            if quote is None:
                self._log_drop_quote_warning_throttled(
                    instrument.id,
                    "missing bid or ask prices in snapshot",
                )
                return
            self._last_quotes[instrument.id] = quote
            self._quote_heartbeat_emit_ns[instrument.id] = now_ns
            record_quote_provenance(
                quote,
                source="ws_snapshot",
                raw_ws_received_ts=getattr(self, "_btc15m_raw_ws_received_ts", None),
            )
            self._queue_latest_quote(quote)

    cls.__init__ = patched_init
    cls._connect = patched_connect
    cls._disconnect = patched_disconnect
    cls._handle_raw_ws_message = patched_handle_raw_ws_message
    cls._log_drop_quote_warning_throttled = patched_log_drop_quote_warning_throttled
    cls._log_tick_size_warning_throttled = patched_log_tick_size_warning_throttled
    cls._apply_quote_change = patched_apply_quote_change
    cls._publish_quote = patched_publish_quote
    cls._queue_latest_quote = queue_latest_quote
    cls._handle_quote = patched_handle_quote
    cls._handle_quotes = patched_handle_quotes
    cls._handle_book_snapshot = patched_handle_book_snapshot
    cls._btc15m_runtime_compat_patched = True


def _install_live_data_engine_observability_override() -> None:
    """Record queue depth for QuoteTicks without changing DataEngine behavior."""
    from nautilus_trader.live.data_engine import LiveDataEngine

    if getattr(LiveDataEngine, "_btc15m_queue_telemetry_patched", False):
        return

    original_process = LiveDataEngine.process

    def patched_process(self, data) -> None:
        if type(data).__name__ == "QuoteTick":
            try:
                record_quote_data_engine_queue_depth(data, self.data_qsize())
            except Exception:
                # Queue observability must never interfere with market data.
                pass
        original_process(self, data)

    LiveDataEngine.process = patched_process
    LiveDataEngine._btc15m_queue_telemetry_patched = True


def _install_polymarket_execution_overrides() -> None:
    import nautilus_trader.adapters.polymarket.execution as exec_mod

    cls = exec_mod.PolymarketExecutionClient
    if getattr(cls, "_btc15m_runtime_compat_patched", False):
        return

    original_fetch_user_positions = cls._fetch_user_positions

    async def patched_fetch_user_positions(self, *, limit: int = 100, size_threshold: int = 0):
        """Retry temporary Gamma/Data API rate limits during startup reconciliation."""
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                return await original_fetch_user_positions(
                    self,
                    limit=limit,
                    size_threshold=size_threshold,
                )
            except RuntimeError as exc:
                delay_sec = position_fetch_retry_delay_sec(exc, attempt)
                if delay_sec is None or attempt == max_attempts - 1:
                    raise
                self._log.warning(
                    "Polymarket position reconciliation rate limited; "
                    f"retrying attempt={attempt + 2}/{max_attempts} in {delay_sec:.0f}s",
                )
                await asyncio.sleep(delay_sec)

    async def patched_cancel_order(self, command) -> None:
        await self._maintain_active_market(command.instrument_id)
        order = self._cache.order(command.client_order_id)
        if order is None:
            self._log.error(f"Cannot cancel order: {command.client_order_id!r} not found in cache")
            return
        if order.is_closed:
            self._log.warning(
                f"`CancelOrder` command for {command.client_order_id!r} when order already {order.status_string()} "
                "(will not send to exchange)",
            )
            return
        if order.venue_order_id is None:
            client_id_str = str(order.client_order_id.value)
            warned = getattr(self, "_warned_no_venue_id", None)
            if warned is None:
                warned = set()
                self._warned_no_venue_id = warned
            if client_id_str not in warned:
                warned.add(client_id_str)
                self._log.warning(f"Cannot cancel on Polymarket: no VenueOrderId for {client_id_str}")
            return

        retry_manager = await self._retry_manager_pool.acquire()
        try:
            response = await retry_manager.run(
                "cancel_order",
                [order.client_order_id, order.venue_order_id],
                asyncio.to_thread,
                self._http_client.cancel,
                order_id=order.venue_order_id.value,
            )
            if not response or not retry_manager.result:
                reason = retry_manager.message
            else:
                reason = response.get("not_canceled")
            if reason:
                self.generate_order_cancel_rejected(
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    venue_order_id=order.venue_order_id,
                    reason=str(reason),
                    ts_event=self._clock.timestamp_ns(),
                )
        finally:
            await self._retry_manager_pool.release(retry_manager)

    def patched_handle_ws_order_msg(self, msg, wait_for_ack: bool):
        self._log.debug(f"Handling order message, {wait_for_ack=}")
        venue_order_id = msg.venue_order_id()
        instrument_id = exec_mod.get_polymarket_instrument_id(msg.market, msg.asset_id)
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            self._log.warning(
                f"Received order message for unknown instrument {instrument_id} "
                f"(market={msg.market}, asset_id={msg.asset_id}). "
                f"This may indicate the instrument is not subscribed or cached, skipping order processing",
            )
            return
        if wait_for_ack:
            self.create_task(self._wait_for_ack_order(msg, venue_order_id))
            return
        client_order_id = self._cache.client_order_id(venue_order_id)
        self._log.debug(f"Processing order update for {client_order_id!r}")
        strategy_id = self._cache.strategy_id_for_order(client_order_id) if client_order_id else None
        if strategy_id is None:
            report = msg.parse_to_order_status_report(
                account_id=self.account_id,
                instrument=instrument,
                client_order_id=client_order_id,
                ts_init=self._clock.timestamp_ns(),
            )
            self._send_order_status_report(report)
            return
        self._log.debug(f"Order {msg.type.value}: {client_order_id!r}", exec_mod.LogColor.MAGENTA)
        match msg.type:
            case exec_mod.PolymarketEventType.PLACEMENT:
                order = self._cache.order(client_order_id) if client_order_id else None
                if order is None or not order.is_open:
                    self.generate_order_accepted(
                        strategy_id=strategy_id,
                        instrument_id=instrument_id,
                        client_order_id=client_order_id,
                        venue_order_id=venue_order_id,
                        ts_event=self._clock.timestamp_ns(),
                    )
                else:
                    self._log.debug(
                        f"Order {client_order_id!r} already accepted - skipping duplicate placement event",
                    )
            case exec_mod.PolymarketEventType.CANCELLATION:
                order_obj = self._cache.order(client_order_id) if client_order_id else None
                if order_obj is None or not order_obj.is_canceled:
                    self.generate_order_canceled(
                        strategy_id=strategy_id,
                        instrument_id=instrument_id,
                        client_order_id=client_order_id,
                        venue_order_id=venue_order_id,
                        ts_event=exec_mod.millis_to_nanos(int(msg.timestamp)),
                    )
                else:
                    self._log.debug(f"Order {client_order_id!r} already canceled - skipping duplicate cancellation event")
            case exec_mod.PolymarketEventType.UPDATE | exec_mod.PolymarketEventType.TRADE:
                self._log.debug(f"Skipping order update: {msg}")
            case _:
                raise RuntimeError(f"Unknown `PolymarketEventType`, was '{msg.type.value}'")

    def patched_handle_ws_trade_msg(self, msg, wait_for_ack: bool):
        self._log.debug(f"Handling trade message, {wait_for_ack=}")
        trade_id = exec_mod.TradeId(msg.id)
        trade_str = f"Trade {trade_id}"
        log_msg = (
            f"{trade_str} {msg.status.value}: market={msg.market} asset={msg.asset_id} "
            f"side={msg.side.value} liq={msg.trader_side.value} price={msg.price} size={msg.size}"
        )
        match msg.status:
            case exec_mod.PolymarketTradeStatus.RETRYING:
                self._log.warning(log_msg)
                return
            case exec_mod.PolymarketTradeStatus.FAILED:
                self._log.error(log_msg)
                return
            case _:
                self._log.info(log_msg, exec_mod.LogColor.BLUE)

        if trade_id in self._finalized_trades:
            self._log.debug(f"Trade {trade_id} already finalized - skipping duplicate")
            return

        previous_status = self._processed_trades.get(trade_id)
        if previous_status is not None:
            if (
                msg.status in exec_mod.POLYMARKET_FINALIZED_TRADE_STATUSES
                and previous_status not in exec_mod.POLYMARKET_FINALIZED_TRADE_STATUSES
            ):
                self._record_processed_trade(trade_id, msg.status)
                self._log.debug(
                    f"Trade {trade_id} transitioned from {previous_status.value} "
                    f"to {msg.status.value} - refreshing account state",
                )
                self.create_task(self._update_account_state())
            else:
                self._log.debug(
                    f"Trade {trade_id} already processed with status {previous_status.value} - skipping",
                )
            return

        filled_user_order_ids = msg.get_filled_user_order_ids(self._wallet_address, self._api_key)
        for order_id in filled_user_order_ids:
            self._handle_user_trade_in_ws_trade_msg(msg, trade_id, wait_for_ack, order_id)

    cls._cancel_order = patched_cancel_order
    cls._handle_ws_order_msg = patched_handle_ws_order_msg
    cls._handle_ws_trade_msg = patched_handle_ws_trade_msg
    cls._btc15m_runtime_compat_patched = True
    cls._fetch_user_positions = patched_fetch_user_positions


def _safe_decimal(value, fallback=None) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        if fallback is None:
            raise
    return Decimal(str(fallback))


def _install_polymarket_user_trade_overrides() -> None:
    from nautilus_trader.adapters.polymarket.schemas.user import PolymarketUserTrade
    from nautilus_trader.model.enums import LiquiditySide

    cls = PolymarketUserTrade
    if getattr(cls, "_btc15m_decimal_fallback_patched", False):
        return

    def patched_last_px(self, filled_user_order_id: str) -> Decimal:
        if self.liquidity_side() == LiquiditySide.TAKER:
            return _safe_decimal(self.price)
        order = self.get_maker_order(filled_user_order_id)
        return _safe_decimal(order.price, self.price)

    def patched_last_qty(self, filled_user_order_id: str) -> Decimal:
        if self.liquidity_side() == LiquiditySide.TAKER:
            return _safe_decimal(self.size)
        order = self.get_maker_order(filled_user_order_id)
        return _safe_decimal(order.matched_amount, self.size)

    def patched_get_fee_rate_bps(self, filled_user_order_id: str) -> Decimal:
        if self.liquidity_side() == LiquiditySide.TAKER:
            return _safe_decimal(self.fee_rate_bps, 0)
        order = self.get_maker_order(filled_user_order_id)
        return _safe_decimal(order.fee_rate_bps, self.fee_rate_bps or 0)

    cls.last_px = patched_last_px
    cls.last_qty = patched_last_qty
    cls.get_fee_rate_bps = patched_get_fee_rate_bps
    cls._btc15m_decimal_fallback_patched = True


def _install_pyclob_http_overrides() -> None:
    if getattr(pyclob_helpers, "_btc15m_runtime_compat_patched", False):
        return

    if not hasattr(pyclob_helpers, "_http_client_local"):
        pyclob_helpers._http_client_local = threading.local()

    def _get_thread_local_client(*, http2: bool) -> httpx.Client:
        local = pyclob_helpers._http_client_local
        attr = "http2_client" if http2 else "http1_client"
        client = getattr(local, attr, None)
        if client is None:
            client = httpx.Client(http2=http2)
            setattr(local, attr, client)
        return client

    def _overload_headers(method: str, headers: dict | None) -> dict:
        overload = getattr(pyclob_helpers, "_overload_headers", None)
        if overload is None:
            overload = getattr(pyclob_helpers, "overloadHeaders", None)
        if overload is None:
            return dict(headers or {})
        return dict(overload(method, headers))

    def _request_with_client(client: httpx.Client, endpoint: str, method: str, headers: dict, data, params=None):
        if isinstance(data, str):
            return client.request(
                method=method,
                url=endpoint,
                headers=headers,
                content=data.encode("utf-8"),
                params=params,
            )
        return client.request(
            method=method,
            url=endpoint,
            headers=headers,
            json=data,
            params=params,
        )

    def request(endpoint: str, method: str, headers=None, data=None, params=None):
        try:
            headers = _overload_headers(method, headers)
            resp = _request_with_client(
                _get_thread_local_client(http2=True),
                endpoint,
                method,
                headers,
                data,
                params,
            )
            if resp.status_code != 200:
                raise PolyApiException(resp)
            try:
                return resp.json()
            except ValueError:
                return resp.text
        except (httpx.RequestError, RuntimeError):
            try:
                fallback_headers = _overload_headers(method, headers)
                fallback_headers["Connection"] = "close"
                resp = _request_with_client(
                    _get_thread_local_client(http2=False),
                    endpoint,
                    method,
                    fallback_headers,
                    data,
                    params,
                )
                if resp.status_code != 200:
                    raise PolyApiException(resp)
                try:
                    return resp.json()
                except ValueError:
                    return resp.text
            except (httpx.RequestError, RuntimeError):
                raise PolyApiException(error_msg="Request exception!")

    pyclob_helpers._request_with_client = _request_with_client
    pyclob_helpers.request = request
    pyclob_helpers._btc15m_runtime_compat_patched = True
