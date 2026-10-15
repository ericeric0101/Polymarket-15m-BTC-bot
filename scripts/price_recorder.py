"""Standalone 1-second recorder: Chainlink RTDS (raw spot + 60 s TWAP), Binance BTCUSDT, Coinbase BTC-USD.

Research only. Runs as its own process, independent of the trading bot:

    .venv/bin/python scripts/price_recorder.py

Output: data/price_sources_1s/YYYY-MM-DD/HH.parquet (one file per UTC hour),
retention 30 days, writes paused below 10 GiB free. See bot/research/price_recorder.py.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import signal
import sys
import threading
import time
from typing import Callable, Optional

from loguru import logger

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.price_streams import (  # noqa: E402
    BINANCE_AGGTRADE_WS_URL,
    POLYMARKET_LIVE_WS_URL,
    RTDS_APPLICATION_HEARTBEAT_TEXT,
    build_polymarket_chainlink_subscribe_payload,
    extract_binance_aggtrade_tick,
    extract_polymarket_chainlink_tick,
    rtds_application_heartbeat_due,
    safe_json_loads,
)
from bot.research.price_recorder import (  # noqa: E402
    HourlyParquetStore,
    SecondBucketer,
    parse_coinbase_ticker,
)

COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
COINBASE_SUBSCRIBE = {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]}
SILENCE_RECONNECT_SEC = 30.0
MAX_CONNECTION_SEC = 23 * 3600


class Feed(threading.Thread):
    """One reconnecting websocket; ``on_message`` returns how many ticks it recorded."""

    def __init__(self, name: str, url: str, stop: threading.Event,
                 on_open: Callable[[object], None], on_message: Callable[[str | bytes], int],
                 heartbeat: Optional[Callable[[object, float], None]] = None) -> None:
        super().__init__(name=f"feed-{name}", daemon=True)
        self.feed_name, self.url, self.stop_event = name, url, stop
        self.on_open, self.on_message, self.heartbeat = on_open, on_message, heartbeat
        self.connects = 0
        self.ticks = 0
        self.last_tick_monotonic = 0.0

    def run(self) -> None:
        import websockets.sync.client as ws_sync

        delay = 1.0
        while not self.stop_event.is_set():
            try:
                with ws_sync.connect(self.url, close_timeout=5, ping_interval=20, ping_timeout=20,
                                     max_size=2 ** 22) as ws:
                    self.connects += 1
                    delay = 1.0
                    opened = last_data = time.monotonic()
                    self.on_open(ws)
                    logger.info(f"{self.feed_name}: connected (#{self.connects})")
                    while not self.stop_event.is_set():
                        now = time.monotonic()
                        if self.heartbeat is not None:
                            self.heartbeat(ws, now)
                        if now - last_data > SILENCE_RECONNECT_SEC:
                            logger.warning(f"{self.feed_name}: silent {now - last_data:.0f}s; reconnecting")
                            break
                        if now - opened > MAX_CONNECTION_SEC:
                            logger.info(f"{self.feed_name}: rotating long-lived connection")
                            break
                        try:
                            raw = ws.recv(timeout=1.0)
                        except TimeoutError:
                            continue
                        n = self.on_message(raw)
                        if n:
                            self.ticks += n
                            last_data = self.last_tick_monotonic = time.monotonic()
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                logger.warning(f"{self.feed_name}: {type(exc).__name__}: {exc}; retry in {delay:.0f}s")
            self.stop_event.wait(delay)
            delay = min(delay * 2, 30.0)


def build_feeds(bucketer: SecondBucketer, stop: threading.Event) -> list[Feed]:
    def rtds_open(ws) -> None:
        ws.send(json.dumps(build_polymarket_chainlink_subscribe_payload(
            use_twap=True, window_seconds=60, symbol="btc/usd", include_spot=True)))

    last_ping = {"t": 0.0}

    def rtds_heartbeat(ws, now: float) -> None:
        if rtds_application_heartbeat_due(now_monotonic=now, last_sent_monotonic=last_ping["t"]):
            ws.send(RTDS_APPLICATION_HEARTBEAT_TEXT)  # RTDS requires this application frame
            last_ping["t"] = now

    def rtds_message(raw) -> int:
        payload = safe_json_loads(raw)
        if payload is None:
            return 0  # PONG and other non-JSON frames
        tick = extract_polymarket_chainlink_tick(payload)
        if tick is None:
            return 0
        if tick.source.startswith("polymarket_chainlink_twap_60"):
            source = "chainlink_twap60"
        elif tick.source == "polymarket_chainlink_ws":
            source = "chainlink_spot"
        else:
            return 0
        bucketer.observe(source, float(tick.price), tick.received_at_ts, tick.updated_at_ms)
        return 1

    def binance_message(raw) -> int:
        tick = extract_binance_aggtrade_tick(raw)
        if tick is None:
            return 0
        bucketer.observe("binance_btcusdt", float(tick.price), tick.received_at_ts,
                         tick.exchange_trade_ts_ms or tick.updated_at_ms)
        return 1

    def coinbase_open(ws) -> None:
        ws.send(json.dumps(COINBASE_SUBSCRIBE))

    def coinbase_message(raw) -> int:
        received = time.time()
        parsed = parse_coinbase_ticker(safe_json_loads(raw))
        if parsed is None:
            return 0
        price, source_ms = parsed
        bucketer.observe("coinbase_btcusd", price, received, source_ms)
        return 1

    return [
        Feed("chainlink_rtds", POLYMARKET_LIVE_WS_URL, stop, rtds_open, rtds_message, rtds_heartbeat),
        Feed("binance", BINANCE_AGGTRADE_WS_URL, stop, lambda ws: None, binance_message),
        Feed("coinbase", COINBASE_WS_URL, stop, coinbase_open, coinbase_message),
    ]


def write_status(path: pathlib.Path, feeds: list[Feed], store: HourlyParquetStore, started: float) -> None:
    now = time.time()
    status = {
        "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "uptime_sec": round(now - started),
        "feeds": {f.feed_name: {"connects": f.connects, "ticks": f.ticks,
                                "last_tick_age_sec": (round(time.monotonic() - f.last_tick_monotonic, 1)
                                                      if f.last_tick_monotonic else None)}
                  for f in feeds},
        "store": vars(store.stats),
        "disk_ok": store.disk_ok(),
    }
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(status, indent=1, default=str))
        tmp.replace(path)
    except OSError as exc:
        logger.warning(f"status write failed: {exc}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", default=str(REPO_ROOT / "data" / "price_sources_1s"))
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--min-free-disk-gb", type=float, default=10.0)
    parser.add_argument("--max-buffer-rows", type=int, default=60_000)
    parser.add_argument("--duration-sec", type=float, default=0.0, help="stop after N seconds (0 = run forever)")
    args = parser.parse_args(argv)

    store = HourlyParquetStore(args.data_dir, retention_days=args.retention_days,
                               min_free_disk_gb=args.min_free_disk_gb, max_buffer_rows=args.max_buffer_rows)
    bucketer = SecondBucketer(store.add)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    store.enforce_retention()
    feeds = build_feeds(bucketer, stop)
    for feed in feeds:
        feed.start()
    started = time.time()
    status_path = pathlib.Path(args.data_dir) / "_status.json"
    last_status = 0.0
    logger.info(f"price recorder started -> {args.data_dir}")
    try:
        while not stop.wait(1.0):
            now = time.time()
            bucketer.flush_older_than(now)
            for path in store.flush():  # completed UTC hours only
                logger.info(f"wrote {path}")
                store.enforce_retention()
            if now - last_status >= 60.0:
                last_status = now
                write_status(status_path, feeds, store, started)
            if args.duration_sec and now - started >= args.duration_sec:
                stop.set()
    finally:
        stop.set()
        for feed in feeds:
            feed.join(timeout=10)
        bucketer.flush_older_than(time.time() + 2)
        for path in store.flush(include_current_hour=True):
            logger.info(f"wrote {path}")
        write_status(status_path, feeds, store, started)
        logger.info(f"price recorder stopped: {vars(store.stats)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
