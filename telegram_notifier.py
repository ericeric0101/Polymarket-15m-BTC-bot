from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from telegram import Bot
except Exception:  # pragma: no cover - keeps local formatter tests dependency-free.
    Bot = None  # type: ignore[assignment]


class TelegramNotifier:
    """Small synchronous wrapper for Telegram push messages."""

    def __init__(self, token: Optional[str] = None, owner_chat_id: Optional[int | str] = None) -> None:
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        raw_chat_id = owner_chat_id if owner_chat_id is not None else os.getenv("TELEGRAM_OWNER_CHAT_ID", "")
        self.owner_chat_id = int(raw_chat_id) if str(raw_chat_id).strip() else None
        self._bot = Bot(self.token) if Bot is not None and self.token else None

    def send_message(self, text: str, parse_mode: str = "MarkdownV2") -> None:
        if self._bot is None or self.owner_chat_id is None:
            logger.warning("Telegram notifier is not configured; message was not sent")
            return

        async def _send() -> None:
            await self._bot.send_message(
                chat_id=self.owner_chat_id,
                text=text,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(_send())
            except Exception:
                logger.exception("Failed to send Telegram message")
            return

        def _runner() -> None:
            try:
                asyncio.run(_send())
            except Exception:
                logger.exception("Failed to send Telegram message")

        threading.Thread(target=_runner, daemon=True).start()

    @staticmethod
    def escape_md(text: str) -> str:
        return "".join(f"\\{ch}" if ch in r"_*[]()~`>#+-=|{}.!" else ch for ch in str(text))
