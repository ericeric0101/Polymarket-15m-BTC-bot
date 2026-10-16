"""TWAP-z late-window executor (P1: DRY-RUN only).

Standalone process, independent of the trading bot and of the price recorder. It keeps
live Chainlink RTDS (spot + 60 s TWAP) and the current/next BTC 15m Polymarket books
in memory, evaluates the plan_v2 ZC rule at T-55, T-50 ... T-5 of every market, and
logs one JSON row per checkpoint. No order is built or sent in this mode.

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
    RTDS_APPLICATION_HEARTBEAT_TEXT,
    build_polymarket_chainlink_subscribe_payload,
    extract_polymarket_chainlink_tick,
    rtds_application_heartbeat_due,
    safe_json_loads,
)
from bot.research.twap_z_executor import (  # noqa: E402
    CHECKPOINTS,
    MARKET_SEC,
    LiveBookSampler,
    LiveChainlink,
    evaluate_checkpoint,
)
from price_recorder import Feed, MarketTracker, build_book_feed  # noqa: E402

BOOK_FEED_MAX_SILENCE_SEC = 5.0
MIN_FREE_DISK_GB = 10.0


def build_chainlink_feed(chainlink: LiveChainlink, stop: threading.Event) -> Feed:
    last_ping = {"t": 0.0}

    def on_open(ws) -> None:
        ws.send(json.dumps(build_polymarket_chainlink_subscribe_payload(
            use_twap=True, window_seconds=60, symbol="btc/usd", include_spot=True)))

    def heartbeat(ws, now: float) -> None:
        if rtds_application_heartbeat_due(now_monotonic=now, last_sent_monotonic=last_ping["t"]):
            ws.send(RTDS_APPLICATION_HEARTBEAT_TEXT)
            last_ping["t"] = now

    def on_message(raw) -> int:
        payload = safe_json_loads(raw)
        if payload is None:
            return 0
        tick = extract_polymarket_chainlink_tick(payload)
        if tick is None:
            return 0
        if tick.source.startswith("polymarket_chainlink_twap_60"):
            kind = "twap"
        elif tick.source == "polymarket_chainlink_ws":
            kind = "spot"
        else:
            return 0
        chainlink.observe(kind, float(tick.price), tick.received_at_ts, tick.updated_at_ms)
        return 1

    return Feed("chainlink_rtds", POLYMARKET_LIVE_WS_URL, stop, on_open, on_message, heartbeat)


def next_checkpoint(now: float) -> tuple[str, int, float]:
    """Earliest (slug, checkpoint, ts) strictly after ``now``."""
    start = int(now) // MARKET_SEC * MARKET_SEC
    for s in (start, start + MARKET_SEC):
        end = s + MARKET_SEC
        for h in CHECKPOINTS:
            t = float(end - h)
            if t > now:
                return f"btc-updown-15m-{s}", h, t
    raise AssertionError("unreachable")


def disk_free_gb(path: pathlib.Path) -> float:
    import shutil
    return shutil.disk_usage(path).free / 2 ** 30


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--mode", choices=["dry-run"], default="dry-run")
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "data" / "twap_z_executor"))
    parser.add_argument("--duration-sec", type=float, default=0.0)
    args = parser.parse_args(argv)
    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    chainlink = LiveChainlink()
    books = LiveBookSampler()
    tracker = MarketTracker(books, stop)
    tracker.refresh()
    tracker.start()
    feeds = [build_chainlink_feed(chainlink, stop), build_book_feed(books, tracker, stop)]
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
            slug, h, t = next_checkpoint(time.time())
            # Coarse wait, then spin the last few ms so evaluation starts on the checkpoint.
            while not stop.is_set() and t - time.time() > 0.02:
                stop.wait(min(1.0, t - time.time() - 0.01))
            while not stop.is_set() and time.time() < t:
                pass
            if stop.is_set():
                break
            now = time.time()
            mono = time.monotonic()
            feed_ok = {"polymarket_book": bool(feeds[1].last_tick_monotonic)
                       and mono - feeds[1].last_tick_monotonic <= BOOK_FEED_MAX_SILENCE_SEC}
            c0 = time.perf_counter()
            d = evaluate_checkpoint(chainlink=chainlink, books=books, slug=slug, checkpoint=h,
                                    scheduled_ts=t, now=now, feed_ok=feed_ok, already_selected=slug in selected)
            d.compute_ms = (time.perf_counter() - c0) * 1000
            if d.action in ("WOULD_SUBMIT", "RULE_MATCH_FAIL_CLOSED"):
                selected.add(slug)   # plan_v2: only the first matching checkpoint per market
            if len(selected) > 200:
                selected = {s for s in selected if int(s.rsplit("-", 1)[-1]) + MARKET_SEC > now}
            row = d.to_row()
            row["decision_latency_ms"] = (now - t) * 1000 + d.compute_ms
            lat.append(row["decision_latency_ms"])
            counts[d.action] = counts.get(d.action, 0) + 1
            if disk_free_gb(out_dir) > MIN_FREE_DISK_GB:
                day = time.strftime("%Y-%m-%d", time.gmtime(t))
                with open(out_dir / f"{day}.jsonl", "a") as fh:
                    fh.write(json.dumps(row, default=str) + "\n")
            if d.action != "SKIP" or h == 5:
                logger.info(f"{slug} T-{h}: {d.action} z={d.z if d.z is None else round(d.z, 2)} "
                            f"mid={d.mid} side={d.side} ask={d.ask} reasons={','.join(d.reasons) or '-'} "
                            f"lat={row['decision_latency_ms']:.1f}ms")
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
