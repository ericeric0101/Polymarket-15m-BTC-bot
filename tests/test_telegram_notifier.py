import asyncio
import threading

import telegram_notifier
from telegram.error import TimedOut


def test_notifier_serializes_delivery_and_stops_cleanly(monkeypatch) -> None:
    delivered: list[str] = []
    complete = threading.Event()

    class FakeRequest:
        def __init__(self, **_kwargs) -> None:
            pass

    class FakeBot:
        active = 0
        peak_active = 0

        def __init__(self, _token, request=None) -> None:
            self.request = request

        async def initialize(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

        async def send_message(self, *, text, **_kwargs) -> None:
            type(self).active += 1
            type(self).peak_active = max(type(self).peak_active, type(self).active)
            try:
                await asyncio.sleep(0.01)
                delivered.append(text)
                if len(delivered) == 2:
                    complete.set()
            finally:
                type(self).active -= 1

    monkeypatch.setattr(telegram_notifier, "Bot", FakeBot)
    monkeypatch.setattr(telegram_notifier, "HTTPXRequest", FakeRequest)
    notifier = telegram_notifier.TelegramNotifier(token="token", owner_chat_id=1)

    notifier.send_message("first")
    notifier.send_message("second")

    assert complete.wait(1.0)
    notifier.close()
    assert delivered == ["first", "second"]
    assert FakeBot.peak_active == 1


def test_notifier_retries_transient_timeout(monkeypatch) -> None:
    class FakeBot:
        attempts = 0

        async def send_message(self, **_kwargs) -> None:
            type(self).attempts += 1
            if type(self).attempts == 1:
                raise TimedOut("pool exhausted")

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(telegram_notifier.asyncio, "sleep", no_sleep)
    notifier = telegram_notifier.TelegramNotifier(token="token", owner_chat_id=1)
    asyncio.run(notifier._send_with_retry(FakeBot(), text="retry", parse_mode="MarkdownV2"))

    assert FakeBot.attempts == 2
