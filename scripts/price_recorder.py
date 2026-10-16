"""Standalone 1-second recorder: Chainlink RTDS (raw spot + 60 s TWAP) for BTC/ETH/SOL/XRP, Binance BTCUSDT,
Coinbase BTC-USD, and the current/next 5m and 15m Up/Down Polymarket order books of those assets.

Research only. Runs as its own process, independent of the trading bot:

    .venv/bin/python scripts/price_recorder.py

Output: data/price_sources_1s/YYYY-MM-DD/HH.parquet and
data/polymarket_book_1s/YYYY-MM-DD/HH.parquet (one file per UTC hour each),
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
    BookSampler,
    HourlyParquetStore,
    SecondBucketer,
    ALT_ASSETS,
    MARKET_STREAMS,
    alt_source_name,
    book_schema,
    market_tokens_for,
    parse_coinbase_ticker,
    parse_rtds_chainlink,
    stream_slugs,
)

COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
CLOB_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets?slug={slug}&limit=5"
CLOB_PING_SEC = 10.0
MARKET_REFRESH_SEC = 30.0
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


def fetch_gamma_market(slug: str):
    from urllib.request import Request, urlopen
    req = Request(GAMMA_MARKETS_URL.format(slug=slug), headers={"User-Agent": "price-recorder/1.0"})
    with urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload[0] if isinstance(payload, list) and payload else None


class MarketTracker(threading.Thread):
    """Keeps the sampler on the current and next BTC 15m markets; queues new tokens to subscribe."""

    def __init__(self, sampler: BookSampler, stop: threading.Event, streams=MARKET_STREAMS) -> None:
        super().__init__(name="market-tracker", daemon=True)
        self.sampler, self.stop_event, self.streams = sampler, stop, tuple(streams)
        self.pending: list[str] = []
        self.lock = threading.Lock()
        self.errors = 0
        self._previous: dict = {}

    def refresh(self) -> None:
        slugs = stream_slugs(time.time(), self.streams)
        meta = {}
        for slug in slugs:
            try:
                meta.update(market_tokens_for([slug], fetch_gamma_market))
            except Exception as exc:
                self.errors += 1
                logger.warning(f"market lookup failed {slug}: {type(exc).__name__}: {exc}")
        # Keep tokens of a market that just ended until its book rows stop (sampler drops end+5s).
        for token, info in ((t, i) for t, i in self._previous.items() if i["end_ts"] + 10 > time.time()):
            meta.setdefault(token, info)
        self._previous = meta
        new = self.sampler.set_tokens(meta)
        if new:
            with self.lock:
                self.pending.extend(new)

    def take_pending(self, prefix: Optional[str] = None) -> list[str]:
        """New tokens to subscribe; with ``prefix``, only those of markets whose slug starts with it."""
        with self.lock:
            if prefix is None:
                out, self.pending = self.pending, []
                return out
            slug_of = {t: m["slug"] for t, m in self._previous.items()}
            out = [t for t in self.pending if slug_of.get(t, "").startswith(prefix)]
            self.pending = [t for t in self.pending if t not in out]
        return out

    def run(self) -> None:
        while not self.stop_event.is_set():
            self.stop_event.wait(MARKET_REFRESH_SEC)
            if not self.stop_event.is_set():
                self.refresh()


def build_book_feed(sampler: BookSampler, tracker: MarketTracker, stop: threading.Event,
                    prefix: Optional[str] = None) -> Feed:
    """One CLOB market socket for the tracked tokens whose slug starts with ``prefix`` (all if None).

    One socket per asset keeps each subscription's snapshot burst small enough that the
    server does not drop us as a slow consumer (1013), which happened with 32 tokens on one socket.
    """
    last_ping = {"t": 0.0}

    def on_open(ws) -> None:
        sampler.reset(prefix)          # this socket's books are rebuilt from fresh snapshots
        tracker.take_pending(prefix)   # everything tracked is subscribed below
        tokens = sampler.tokens(prefix)
        if tokens:
            ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))

    def heartbeat(ws, now: float) -> None:
        if now - last_ping["t"] >= CLOB_PING_SEC:
            ws.send("PING")
            last_ping["t"] = now
        new = tracker.take_pending(prefix)
        if new:
            ws.send(json.dumps({"assets_ids": new, "operation": "subscribe"}))

    def on_message(raw) -> int:
        if raw == "PONG":
            return 1             # keeps the silence watchdog satisfied on a quiet book
        payload = safe_json_loads(raw)
        if payload is None:
            return 0
        sampler.apply(payload, time.time())
        return 1

    name = "polymarket_book" if prefix is None or prefix == "btc-" else f"polymarket_book_{prefix.rstrip('-')}"
    return Feed(name, CLOB_MARKET_WS_URL, stop, on_open, on_message, heartbeat)


def build_book_feeds(sampler: BookSampler, tracker: MarketTracker, stop: threading.Event) -> list[Feed]:
    assets = list(dict.fromkeys(a for a, _d in tracker.streams))
    return [build_book_feed(sampler, tracker, stop, prefix=f"{a}-") for a in assets]


def build_feeds(bucketer: SecondBucketer, chainlink_bucketer: SecondBucketer, stop: threading.Event) -> list[Feed]:
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
        chainlink_bucketer.observe(source, float(tick.price), tick.received_at_ts, tick.updated_at_ms)
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
        *build_alt_chainlink_feeds(chainlink_bucketer, stop),
        Feed("binance", BINANCE_AGGTRADE_WS_URL, stop, lambda ws: None, binance_message),
        Feed("coinbase", COINBASE_WS_URL, stop, coinbase_open, coinbase_message),
    ]


def _rtds_heartbeat():
    last_ping = {"t": 0.0}

    def heartbeat(ws, now: float) -> None:
        if rtds_application_heartbeat_due(now_monotonic=now, last_sent_monotonic=last_ping["t"]):
            ws.send(RTDS_APPLICATION_HEARTBEAT_TEXT)
            last_ping["t"] = now
    return heartbeat


def build_alt_chainlink_feeds(chainlink_bucketer: SecondBucketer, stop: threading.Event,
                              assets=ALT_ASSETS) -> list[Feed]:
    """ETH/SOL/XRP Chainlink: one raw-spot socket (all symbols) + one TWAP socket per asset.

    RTDS only streams TWAP updates for one symbol filter per connection (probed
    2026-10-16), so each asset gets its own TWAP socket. BTC stays on the original feed.
    """
    wanted = set(assets)

    def on_message(raw) -> int:
        n = 0
        for kind, asset, price, source_ms in parse_rtds_chainlink(safe_json_loads(raw)):
            if asset in wanted:
                chainlink_bucketer.observe(alt_source_name(kind, asset), price, time.time(), source_ms)
                n += 1
        return n

    def spot_open(ws) -> None:
        ws.send(json.dumps({"action": "subscribe", "subscriptions": [
            {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]}))

    feeds = [Feed("chainlink_rtds_alt_spot", POLYMARKET_LIVE_WS_URL, stop, spot_open, on_message, _rtds_heartbeat())]
    for asset in assets:
        payload = build_polymarket_chainlink_subscribe_payload(
            use_twap=True, window_seconds=60, symbol=f"{asset}/usd", include_spot=False)
        feeds.append(Feed(f"chainlink_rtds_twap_{asset}", POLYMARKET_LIVE_WS_URL, stop,
                          lambda ws, _p=payload: ws.send(json.dumps(_p)), on_message, _rtds_heartbeat()))
    return feeds


def write_status(path: pathlib.Path, feeds: list[Feed], store: HourlyParquetStore, started: float,
                 book_store: Optional[HourlyParquetStore] = None) -> None:
    now = time.time()
    status = {
        "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "uptime_sec": round(now - started),
        "feeds": {f.feed_name: {"connects": f.connects, "ticks": f.ticks,
                                "last_tick_age_sec": (round(time.monotonic() - f.last_tick_monotonic, 1)
                                                      if f.last_tick_monotonic else None)}
                  for f in feeds},
        "store": vars(store.stats),
        "book_store": vars(book_store.stats) if book_store is not None else None,
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
    parser.add_argument("--book-dir", default=str(REPO_ROOT / "data" / "polymarket_book_1s"))
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--min-free-disk-gb", type=float, default=10.0)
    parser.add_argument("--max-buffer-rows", type=int, default=60_000)
    parser.add_argument("--duration-sec", type=float, default=0.0, help="stop after N seconds (0 = run forever)")
    args = parser.parse_args(argv)

    store = HourlyParquetStore(args.data_dir, retention_days=args.retention_days,
                               min_free_disk_gb=args.min_free_disk_gb, max_buffer_rows=args.max_buffer_rows)
    bucketer = SecondBucketer(store.add)
    chainlink_bucketer = SecondBucketer(store.add, by_source_clock=True, grace_sec=5.0)
    book_store = HourlyParquetStore(args.book_dir, retention_days=args.retention_days,
                                    min_free_disk_gb=args.min_free_disk_gb, max_buffer_rows=args.max_buffer_rows,
                                    schema=book_schema(), count_key="outcome",
                                    sort_cols=("ts_sec", "market_slug", "outcome"))
    sampler = BookSampler()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    store.enforce_retention()
    book_store.enforce_retention()
    tracker = MarketTracker(sampler, stop)
    tracker.refresh()            # first subscription list before the book socket opens
    tracker.start()
    feeds = build_feeds(bucketer, chainlink_bucketer, stop) + build_book_feeds(sampler, tracker, stop)
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
            chainlink_bucketer.flush_older_than(now)
            for row in sampler.sample(now):
                book_store.add(row)
            for st in (store, book_store):
                for path in st.flush():  # completed UTC hours only
                    logger.info(f"wrote {path}")
                    st.enforce_retention()
            if now - last_status >= 60.0:
                last_status = now
                write_status(status_path, feeds, store, started, book_store)
            if args.duration_sec and now - started >= args.duration_sec:
                stop.set()
    finally:
        stop.set()
        for feed in feeds:
            feed.join(timeout=10)
        bucketer.flush_older_than(time.time() + 2)
        chainlink_bucketer.flush_older_than(time.time() + 10)
        for st in (store, book_store):
            for path in st.flush(include_current_hour=True):
                logger.info(f"wrote {path}")
        write_status(status_path, feeds, store, started, book_store)
        logger.info(f"price recorder stopped: {vars(store.stats)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
