"""MB1 bid-only maker -- live DRY-RUN (virtual orders only; nothing is ever sent to Polymarket).

Plan: reports/maker_bid_20261017/plan_mb1.md. Same rule code as tests (bot/research/maker_bid.py).

    .venv/bin/python scripts/maker_bid_dryrun.py --streams btc-updown-5m --duration-sec 1800   # rolling check
    .venv/bin/python scripts/maker_bid_dryrun.py                                              # all 8 streams

Output: data/maker_bid_dryrun/events_YYYY-MM-DD.jsonl (PLACE_SENT / LIVE / CANCEL_SENT /
CANCEL_EFFECTIVE / FILL / SETTLED) and _status.json.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import signal
import sys
import threading
import time
from typing import Optional

from loguru import logger

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for p in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from bot.price_streams import BINANCE_AGGTRADE_WS_URL, extract_binance_aggtrade_tick, safe_json_loads  # noqa: E402
from bot.research.maker_bid import MakerBidSim, PriceHistory  # noqa: E402
from bot.research.price_recorder import (  # noqa: E402
    BINANCE_ALT_STREAM_URL, MARKET_STREAMS, book_feed_name, book_stream_prefix, parse_binance_combined_aggtrade,
)
from bot.research.twap_z_executor import LiveBookSampler  # noqa: E402
from price_recorder import Feed, MarketTracker, build_book_feeds  # noqa: E402

BOOK_FEED_MAX_SILENCE_SEC = 5.0
TICK_SEC = 0.1


def fetch_official_outcome(slug: str) -> Optional[str]:
    import httpx
    from fetch_official_resolutions import fetch_one
    with httpx.Client(timeout=20) as client:
        return fetch_one(client, "https://gamma-api.polymarket.com", slug).get("official_outcome")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--streams", default="", help="e.g. btc-updown-5m,eth-updown-5m (default: all 8)")
    ap.add_argument("--out-dir", default=str(REPO_ROOT / "data" / "maker_bid_dryrun"))
    ap.add_argument("--duration-sec", type=float, default=0.0)
    a = ap.parse_args(argv)
    want = {s for s in a.streams.split(",") if s}
    tags = {900: "15m", 300: "5m"}
    streams = tuple(st for st in MARKET_STREAMS if not want or f"{st[0]}-updown-{tags[st[1]]}" in want)
    out = pathlib.Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    log_lock = threading.Lock()
    counts: dict[str, int] = {}

    def log(row: dict) -> None:
        counts[row["event"]] = counts.get(row["event"], 0) + 1
        with log_lock, open(out / f"events_{time.strftime('%Y-%m-%d', time.gmtime(row['t']))}.jsonl", "a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        # PnL is written to the event file only, never to the console (blind until the confirmatory read).
        if row["event"] == "FILL":
            logger.info(f"FILL {row.get('market_slug')} {row.get('outcome')} @{row.get('price')} "
                        f"{row.get('fill_shares')} sh picked_off={row.get('picked_off')}")
        elif row["event"] == "SETTLED":
            logger.info(f"SETTLED {row.get('market_slug')} {row.get('outcome')} (result hidden)")

    sim = MakerBidSim(log)
    prices = PriceHistory()
    books = LiveBookSampler()
    trades: list[dict] = []
    trades_lock = threading.Lock()

    def on_trades(rows: list) -> None:
        with trades_lock:
            trades.extend(rows)

    tracker = MarketTracker(books, stop, streams)
    tracker.refresh()
    tracker.start()
    book_feeds = {f.feed_name: f for f in build_book_feeds(books, tracker, stop, on_trades)}

    def bn_btc(raw) -> int:
        tick = extract_binance_aggtrade_tick(raw)
        if tick is None:
            return 0
        prices.add("btc", tick.received_at_ts, float(tick.price))
        return 1

    def bn_alt(raw) -> int:
        parsed = parse_binance_combined_aggtrade(safe_json_loads(raw))
        if parsed is None:
            return 0
        prices.add(parsed[0], time.time(), parsed[1])
        return 1

    feeds = list(book_feeds.values()) + [Feed("binance", BINANCE_AGGTRADE_WS_URL, stop, lambda ws: None, bn_btc),
                                         Feed("binance_alt", BINANCE_ALT_STREAM_URL, stop, lambda ws: None, bn_alt)]
    for f in feeds:
        f.start()

    def feed_ok(slug: str) -> bool:
        f = book_feeds.get(book_feed_name(book_stream_prefix(slug)))
        return bool(f and f.last_tick_monotonic and time.monotonic() - f.last_tick_monotonic <= BOOK_FEED_MAX_SILENCE_SEC)

    settled: set[str] = set()
    started = last_status = last_settle = time.time()
    logger.info(f"MB1 maker dry-run started streams={[f'{x[0]}-{tags[x[1]]}' for x in streams]} -> {out}")
    try:
        while not stop.wait(TICK_SEC):
            now = time.time()
            with trades_lock:
                batch, trades[:] = sorted(trades, key=lambda r: r["received_ms"]), []
            for tr in batch:                                   # fills first, then time advance
                sim.on_trade(tr)
            sim.tick(now, books.market_meta(), books.best, books.level_size, prices, feed_ok)
            if now - last_settle >= 60:
                last_settle = now
                for p in sim.positions():
                    key = f"{p['market_slug']}|{p['token_id']}"
                    if key in settled or not p["end_ts"] or now < p["end_ts"] + 60:
                        continue
                    try:
                        oc = fetch_official_outcome(p["market_slug"])
                    except Exception:
                        oc = None
                    if oc in ("UP", "DOWN"):
                        settled.add(key)
                        win = (p["outcome"] or "").upper() == oc
                        log({"event": "SETTLED", "t": now, **p, "official": oc,
                             "pnl": round((p["shares"] if win else 0.0) - p["cost"], 6)})
            if now - last_status >= 60:
                last_status = now
                (out / "_status.json").write_text(json.dumps({
                    "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "uptime_sec": round(now - started),
                    "events": counts, "feeds": {f.feed_name: {"connects": f.connects, "ticks": f.ticks} for f in feeds}},
                    indent=1))
                logger.info(f"status {counts}")
            if a.duration_sec and now - started >= a.duration_sec:
                stop.set()
    finally:
        stop.set()
        for f in feeds:
            f.join(timeout=10)
        logger.info(f"MB1 maker dry-run stopped: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
