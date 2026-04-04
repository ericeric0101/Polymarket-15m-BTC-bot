from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import py_clob_client.http_helpers.helpers as pyclob_helpers
from loguru import logger
from py_clob_client.exceptions import PolyApiException


def install_runtime_compatibility_overrides() -> None:
    _install_polymarket_data_overrides()
    _install_polymarket_execution_overrides()
    _install_pyclob_http_overrides()


def verify_runtime_compatibility_targets(project_root: Path) -> list[str]:
    targets: list[str] = []
    targets.append(f"project_root={project_root}")

    from nautilus_trader.adapters.polymarket.data import PolymarketDataClient
    from nautilus_trader.adapters.polymarket.execution import PolymarketExecutionClient

    targets.append(f"PolymarketDataClient._handle_quote={hasattr(PolymarketDataClient, '_handle_quote')}")
    targets.append(f"PolymarketExecutionClient._handle_ws_order_msg={hasattr(PolymarketExecutionClient, '_handle_ws_order_msg')}")
    targets.append(f"pyclob.request={hasattr(pyclob_helpers, 'request')}")
    return targets


def _install_polymarket_data_overrides() -> None:
    import nautilus_trader.adapters.polymarket.data as data_mod

    cls = data_mod.PolymarketDataClient
    if getattr(cls, "_btc15m_runtime_compat_patched", False):
        return

    original_init = cls.__init__

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

    def patched_handle_quote(self, instrument, ws_message, price_change) -> None:
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
                return
            self._create_local_book(instrument.id)

        local_book = self._local_books[instrument.id]
        local_book.apply(deltas)
        self._handle_data(deltas)

        if instrument.id in self.subscribed_quote_ticks():
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
            if last_quote is not None and (
                quote.bid_price == last_quote.bid_price
                and quote.ask_price == last_quote.ask_price
                and quote.bid_size == last_quote.bid_size
                and quote.ask_size == last_quote.ask_size
            ):
                return

            self._last_quotes[instrument.id] = quote
            self._handle_data(quote)

    def patched_handle_book_snapshot(self, instrument, ws_message) -> None:
        now_ns = self._clock.timestamp_ns()
        deltas = ws_message.parse_to_snapshot(instrument=instrument, ts_init=now_ns)
        if deltas is None:
            return
        self._handle_deltas(instrument, deltas)
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
            self._handle_data(quote)

    cls.__init__ = patched_init
    cls._log_drop_quote_warning_throttled = patched_log_drop_quote_warning_throttled
    cls._log_tick_size_warning_throttled = patched_log_tick_size_warning_throttled
    cls._handle_quote = patched_handle_quote
    cls._handle_book_snapshot = patched_handle_book_snapshot
    cls._btc15m_runtime_compat_patched = True


def _install_polymarket_execution_overrides() -> None:
    import nautilus_trader.adapters.polymarket.execution as exec_mod

    cls = exec_mod.PolymarketExecutionClient
    if getattr(cls, "_btc15m_runtime_compat_patched", False):
        return

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


def _install_pyclob_http_overrides() -> None:
    if getattr(pyclob_helpers, "_btc15m_runtime_compat_patched", False):
        return

    if not hasattr(pyclob_helpers, "_http_client_http1"):
        pyclob_helpers._http_client_http1 = httpx.Client(http2=False)

    def _request_with_client(client: httpx.Client, endpoint: str, method: str, headers: dict, data):
        if isinstance(data, str):
            return client.request(
                method=method,
                url=endpoint,
                headers=headers,
                content=data.encode("utf-8"),
            )
        return client.request(
            method=method,
            url=endpoint,
            headers=headers,
            json=data,
        )

    def request(endpoint: str, method: str, headers=None, data=None):
        try:
            headers = pyclob_helpers.overloadHeaders(method, headers)
            resp = _request_with_client(pyclob_helpers._http_client, endpoint, method, headers, data)
            if resp.status_code != 200:
                raise PolyApiException(resp)
            try:
                return resp.json()
            except ValueError:
                return resp.text
        except httpx.RequestError:
            try:
                fallback_headers = dict(pyclob_helpers.overloadHeaders(method, headers))
                fallback_headers["Connection"] = "close"
                resp = _request_with_client(
                    pyclob_helpers._http_client_http1,
                    endpoint,
                    method,
                    fallback_headers,
                    data,
                )
                if resp.status_code != 200:
                    raise PolyApiException(resp)
                try:
                    return resp.json()
                except ValueError:
                    return resp.text
            except httpx.RequestError:
                raise PolyApiException(error_msg="Request exception!")

    pyclob_helpers._request_with_client = _request_with_client
    pyclob_helpers.request = request
    pyclob_helpers._btc15m_runtime_compat_patched = True

