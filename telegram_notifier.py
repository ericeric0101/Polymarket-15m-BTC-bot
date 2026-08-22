from __future__ import annotations

import asyncio
import logging
import os
from queue import Full, Queue
import threading
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from telegram import Bot
    from telegram.error import NetworkError, TimedOut
    from telegram.request import HTTPXRequest
except Exception:  # pragma: no cover - keeps local formatter tests dependency-free.
    Bot = None  # type: ignore[assignment]
    NetworkError = TimedOut = Exception  # type: ignore[assignment,misc]
    HTTPXRequest = None  # type: ignore[assignment]


class TelegramNotifier:
    """Non-blocking, serialized Telegram push delivery.

    The notifier is called from the strategy's synchronous quote loop.  A
    single worker owns the Telegram HTTP client so concurrent alerts cannot
    exhaust python-telegram-bot's connection pool.
    """

    _QUEUE_CAPACITY = 100
    _RETRY_ATTEMPTS = 2
    _RETRY_DELAY_SEC = 1.0

    def __init__(self, token: Optional[str] = None, owner_chat_id: Optional[int | str] = None) -> None:
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        raw_chat_id = owner_chat_id if owner_chat_id is not None else os.getenv("TELEGRAM_OWNER_CHAT_ID", "")
        self.owner_chat_id = int(raw_chat_id) if str(raw_chat_id).strip() else None
        self._messages: Queue[tuple[str, str] | None] = Queue(maxsize=self._QUEUE_CAPACITY)
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._closed = False

    def send_message(self, text: str, parse_mode: str = "MarkdownV2") -> None:
        if Bot is None or not self.token or self.owner_chat_id is None:
            logger.warning("Telegram notifier is not configured; message was not sent")
            return
        with self._worker_lock:
            if self._closed:
                logger.warning("Telegram notifier is closed; message was not sent")
                return
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._run_worker,
                    daemon=True,
                    name="telegram-notifier",
                )
                self._worker.start()
        try:
            self._messages.put_nowait((str(text), str(parse_mode)))
        except Full:
            # Notification delivery must never stall the trading loop.  This
            # can only occur while Telegram is persistently unavailable.
            logger.warning("Telegram notification queue is full; dropping newest message")

    def close(self, timeout_sec: float = 2.0) -> None:
        """Stop the notification worker; primarily useful for controlled shutdown/tests."""
        with self._worker_lock:
            self._closed = True
            worker = self._worker
        if worker is None or not worker.is_alive():
            return
        try:
            self._messages.put_nowait(None)
        except Full:
            logger.warning("Telegram notification worker did not receive shutdown signal: queue full")
            return
        worker.join(timeout=max(0.0, float(timeout_sec)))

    def _run_worker(self) -> None:
        asyncio.run(self._deliver_messages())

    async def _deliver_messages(self) -> None:
        if Bot is None or HTTPXRequest is None or not self.token or self.owner_chat_id is None:
            return
        request = HTTPXRequest(
            connection_pool_size=1,
            pool_timeout=10.0,
            connect_timeout=5.0,
            read_timeout=10.0,
            write_timeout=10.0,
        )
        bot = Bot(self.token, request=request)
        try:
            await bot.initialize()
            while True:
                message = await asyncio.to_thread(self._messages.get)
                if message is None:
                    return
                text, parse_mode = message
                await self._send_with_retry(bot, text=text, parse_mode=parse_mode)
        except Exception:
            logger.exception("Telegram notification worker stopped unexpectedly")
        finally:
            try:
                await bot.shutdown()
            except Exception:
                logger.exception("Telegram notification worker shutdown failed")

    async def _send_with_retry(self, bot: Bot, *, text: str, parse_mode: str) -> None:
        for attempt in range(1, self._RETRY_ATTEMPTS + 1):
            try:
                await bot.send_message(
                    chat_id=self.owner_chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    disable_web_page_preview=True,
                )
                return
            except (TimedOut, NetworkError) as exc:
                if attempt == self._RETRY_ATTEMPTS:
                    logger.warning(
                        "Telegram message delivery failed after %s attempts: %s",
                        attempt,
                        exc,
                    )
                    return
                logger.warning(
                    "Telegram delivery attempt %s/%s failed (%s); retrying",
                    attempt,
                    self._RETRY_ATTEMPTS,
                    exc,
                )
                await asyncio.sleep(self._RETRY_DELAY_SEC)
            except Exception:
                logger.exception("Telegram message delivery failed without retry")
                return

    @staticmethod
    def escape_md(text: str) -> str:
        return "".join(f"\\{ch}" if ch in r"_*[]()~`>#+-=|{}.!" else ch for ch in str(text))
