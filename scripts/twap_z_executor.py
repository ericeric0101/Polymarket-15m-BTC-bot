"""TWAP-z late-window executor (P1: DRY-RUN only).

Standalone process, independent of the trading bot and of the price recorder. It keeps
live Chainlink RTDS (spot + 60 s TWAP) and the current/next BTC 15m Polymarket books
in memory, evaluates the plan_v2 ZC rule at T-55, T-50 ... T-5 of every market, and
logs one JSON row per market and checkpoint, for BTC/ETH/SOL/XRP 5m and 15m markets. No order is built or sent in this mode.

    .venv/bin/python scripts/twap_z_executor.py --mode dry-run

Output: data/twap_z_executor/YYYY-MM-DD.jsonl (UTC day of the decision) and
data/twap_z_executor/_status.json. Spec: reports/twap_z_late_window_executor_20261016/.
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

from bot.price_streams import (  # noqa: E402
    POLYMARKET_LIVE_WS_URL,
    build_polymarket_chainlink_subscribe_payload,
    safe_json_loads,
)
from bot.research.price_recorder import MARKET_STREAMS, parse_rtds_chainlink, stream_slugs  # noqa: E402
from bot.research.twap_z_executor import (  # noqa: E402
    CHECKPOINTS,
    LiveBookSampler,
    LiveChainlink,
    evaluate_checkpoint,
)
from price_recorder import Feed, MarketTracker, _rtds_heartbeat, build_book_feeds  # noqa: E402

BOOK_FEED_MAX_SILENCE_SEC = 5.0
MIN_FREE_DISK_GB = 10.0


def build_chainlink_feeds(chainlinks: dict[str, LiveChainlink], stop: threading.Event) -> list[Feed]:
    """One raw-spot socket (all symbols) + one 60 s TWAP socket per asset (RTDS streams one TWAP filter per socket)."""

    def on_message(raw) -> int:
        n = 0
        for kind, asset, price, source_ms in parse_rtds_chainlink(safe_json_loads(raw)):
            live = chainlinks.get(asset)
            if live is not None:
                live.observe(kind, price, time.time(), source_ms)
                n += 1
        return n

    def spot_open(ws) -> None:
        ws.send(json.dumps({"action": "subscribe", "subscriptions": [
            {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]}))

    feeds = [Feed("chainlink_spot", POLYMARKET_LIVE_WS_URL, stop, spot_open, on_message, _rtds_heartbeat())]
    for asset in chainlinks:
        payload = build_polymarket_chainlink_subscribe_payload(
            use_twap=True, window_seconds=60, symbol=f"{asset}/usd", include_spot=False)
        feeds.append(Feed(f"chainlink_twap_{asset}", POLYMARKET_LIVE_WS_URL, stop,
                          lambda ws, _p=payload: ws.send(json.dumps(_p)), on_message, _rtds_heartbeat()))
    return feeds


def next_checkpoint(now: float, streams=MARKET_STREAMS) -> tuple[float, int, list[str]]:
    """Earliest checkpoint time after ``now`` and the market slugs that reach it (same T-h)."""
    best: Optional[tuple[float, int]] = None
    for _asset, dur in streams:
        start = int(now) // dur * dur
        for s in (start, start + dur):
            for h in CHECKPOINTS:
                t = float(s + dur - h)
                if t > now and (best is None or t < best[0]):
                    best = (t, h)
    t, h = best
    slugs = [slug for slug in stream_slugs(t - h - 0.5, streams, ahead=0)
             if int(slug.rsplit("-", 1)[-1]) + (300 if "-5m-" in slug else 900) == int(t) + h]
    return t, h, slugs


def _book_feed_name(slug: str) -> str:
    asset = slug.split("-", 1)[0]
    return "polymarket_book" if asset == "btc" else f"polymarket_book_{asset}"


def disk_free_gb(path: pathlib.Path) -> float:
    import shutil
    return shutil.disk_usage(path).free / 2 ** 30


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--mode", choices=["dry-run"], default="dry-run")
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "data" / "twap_z_executor"))
    parser.add_argument("--duration-sec", type=float, default=0.0)
    parser.add_argument("--assets", default="btc,eth,sol,xrp")
    args = parser.parse_args(argv)
    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    streams = tuple(st for st in MARKET_STREAMS if st[0] in args.assets.split(","))
    chainlinks = {asset: LiveChainlink() for asset in dict.fromkeys(a for a, _d in streams)}
    books = LiveBookSampler()
    tracker = MarketTracker(books, stop, streams)
    tracker.refresh()
    tracker.start()
    book_feeds = {f.feed_name: f for f in build_book_feeds(books, tracker, stop)}
    feeds = build_chainlink_feeds(chainlinks, stop) + list(book_feeds.values())
    for f in feeds:
        f.start()
    started = time.time()
    selected: set[str] = set()
    counts: dict[str, int] = {}
    lat: list[float] = []
    logger.info(f"twap-z executor started mode={args.mode} -> {out_dir}")

    def write_status(now: float) -> None:
        srt = sorted(lat[-2000:])
        q = (lambda p: round(srt[min(len(srt) - 1, int(p * len(srt)))], 2)) if srt else (lambda p: None)
        status = {"updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "mode": args.mode,
                  "uptime_sec": round(now - started), "actions": counts, "markets_selected": len(selected),
                  "decision_latency_ms": {"p50": q(.5), "p99": q(.99), "max": srt[-1] if srt else None,
                                          "n": len(srt)},
                  "feeds": {f.feed_name: {"connects": f.connects, "ticks": f.ticks} for f in feeds}}
        tmp = out_dir / "_status.json.tmp"
        tmp.write_text(json.dumps(status, indent=1))
        tmp.replace(out_dir / "_status.json")

    try:
        while not stop.is_set():
            t, h, slugs = next_checkpoint(time.time(), streams)
            # Coarse wait, then spin the last few ms so evaluation starts on the checkpoint.
            while not stop.is_set() and t - time.time() > 0.02:
                stop.wait(min(1.0, t - time.time() - 0.01))
            while not stop.is_set() and time.time() < t:
                pass
            if stop.is_set():
                break
            mono = time.monotonic()
            book_ok = {name: bool(f.last_tick_monotonic) and mono - f.last_tick_monotonic <= BOOK_FEED_MAX_SILENCE_SEC
                       for name, f in book_feeds.items()}
            rows = []
            for slug in slugs:
                now = time.time()
                c0 = time.perf_counter()
                d = evaluate_checkpoint(chainlink=chainlinks[slug.split("-", 1)[0]], books=books, slug=slug,
                                        checkpoint=h, scheduled_ts=t, now=now,
                                        feed_ok={k: v for k, v in book_ok.items() if k == _book_feed_name(slug)},
                                        already_selected=slug in selected)
                d.compute_ms = (time.perf_counter() - c0) * 1000
                if d.action in ("WOULD_SUBMIT", "RULE_MATCH_FAIL_CLOSED"):
                    selected.add(slug)   # plan_v2/v3: only the first matching checkpoint per market
                row = d.to_row()
                row["stream"] = slug.rsplit("-", 1)[0]
                row["decision_latency_ms"] = (now - t) * 1000 + d.compute_ms
                lat.append(row["decision_latency_ms"])
                counts[d.action] = counts.get(d.action, 0) + 1
                rows.append(row)
                if d.action != "SKIP":
                    logger.info(f"{slug} T-{h}: {d.action} z={d.z if d.z is None else round(d.z, 2)} "
                                f"mid={d.mid} side={d.side} ask={d.ask} reasons={','.join(d.reasons) or '-'} "
                                f"lat={row['decision_latency_ms']:.1f}ms")
            if len(selected) > 2000:
                selected = {s for s in selected if int(s.rsplit("-", 1)[-1]) + 900 > time.time()}
            if rows and disk_free_gb(out_dir) > MIN_FREE_DISK_GB:
                day = time.strftime("%Y-%m-%d", time.gmtime(t))
                with open(out_dir / f"{day}.jsonl", "a") as fh:
                    for row in rows:
                        fh.write(json.dumps(row, default=str) + "\n")
            if h == 5:
                write_status(time.time())
            if args.duration_sec and time.time() - started >= args.duration_sec:
                stop.set()
    finally:
        stop.set()
        for f in feeds:
            f.join(timeout=10)
        write_status(time.time())
        logger.info(f"twap-z executor stopped: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
