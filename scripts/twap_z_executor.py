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
from bot.research.price_recorder import (  # noqa: E402
    MARKET_STREAMS, book_feed_name, book_stream_prefix, parse_rtds_chainlink, stream_slugs,
)
from bot.research.twap_z_executor import (  # noqa: E402
    CHECKPOINTS,
    LiveBookSampler,
    LiveChainlink,
    evaluate_checkpoint,
)
from price_recorder import Feed, MarketTracker, _rtds_heartbeat, build_book_feeds  # noqa: E402
from bot.research.twap_z_live import (  # noqa: E402
    LIVE_CONFIRM_ENV, LIVE_CONFIRM_VALUE, LiveConfig, LiveOrderClient, OutcomeWatcher, Position, RiskBook,
    fill_from_response,
)

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
    return book_feed_name(book_stream_prefix(slug))


def disk_free_gb(path: pathlib.Path) -> float:
    import shutil
    return shutil.disk_usage(path).free / 2 ** 30


class LiveContext:
    """Real-money order path: risk caps, background submission, outcome settlement, order log."""

    def __init__(self, config: LiveConfig, client: LiveOrderClient, out_dir: pathlib.Path) -> None:
        from concurrent.futures import ThreadPoolExecutor
        self.config, self.client, self.out_dir = config, client, out_dir
        self.book = RiskBook(config)
        self.book.load()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="live")
        self.stop = threading.Event()
        self.watcher = OutcomeWatcher(self.book, fetch_official_outcome, self.stop,
                                      on_settle=lambda p: logger.warning(
                                          f"LIVE settled {p.market_slug} {p.side} outcome={p.outcome} pnl={p.pnl:+.4f}"))
        self.watcher.start()
        self._prefetched: set[str] = set()

    def _log(self, row: dict) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(row.get("scheduled_ts") or time.time()))
        with open(self.out_dir / f"orders_{day}.jsonl", "a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    def warm_up(self, books: LiveBookSampler) -> None:
        tokens = books.tokens()
        if tokens:
            try:
                self.client.warm_up(tokens[0], 0.5, self.config.shares)
                logger.info("LIVE warm-up: order version / tick size / neg-risk cached (nothing posted)")
            except Exception as exc:
                logger.warning(f"LIVE warm-up failed (orders will fetch on demand): {type(exc).__name__}: {exc}")

    def prefetch(self, books: LiveBookSampler, slug: str) -> None:
        for token in books.market_tokens(slug).values():
            if token not in self._prefetched:
                self._prefetched.add(token)
                self.pool.submit(self.client.prefetch, token)

    def submit(self, d, books: LiveBookSampler, decision_row: dict) -> dict:
        est_cost = float(d.ask) * self.config.shares
        ok, reason = self.book.reserve(d.market_slug, time.time(), est_cost)
        if not ok:
            logger.warning(f"LIVE blocked {d.market_slug} T-{d.checkpoint}: {reason}")
            self._log({**decision_row, "live_action": "BLOCKED", "live_reason": reason})
            return {"action": "BLOCKED", "reason": reason}
        token = books.market_tokens(d.market_slug).get("Up" if d.side == "up" else "Down")
        if not token:
            self.book.release(d.market_slug)
            self._log({**decision_row, "live_action": "BLOCKED", "live_reason": "TOKEN_UNKNOWN"})
            return {"action": "BLOCKED", "reason": "TOKEN_UNKNOWN"}
        start = int(d.market_slug.rsplit("-", 1)[-1])
        end = start + (300 if "-5m-" in d.market_slug else 900)
        self.pool.submit(self._execute, d.market_slug, d.side, d.checkpoint, end, token, float(d.ask), decision_row)
        return {"action": "SUBMITTED", "limit_price": float(d.ask), "shares": self.config.shares}

    def _execute(self, slug, side, checkpoint, end, token, price, decision_row) -> None:
        try:
            res = self.client.buy_fak(token, price, self.config.shares)
            cost, shares = fill_from_response(res, price)
            if shares > 0:
                self.book.add(Position(market_slug=slug, side=side, checkpoint=checkpoint, end_ts=end,
                                       cost_usdc=round(cost, 6), shares=round(shares, 6), order_id=res.get("order_id")))
        finally:
            self.book.release(slug)
        res["decision_to_post_ms"] = round((res["t_posted"] - decision_row["eval_ts"]) * 1000, 1)
        res["filled_usdc"], res["filled_shares"] = cost, shares
        self._log({**decision_row, "live_action": "ORDER", "token_id": token, "order": res})
        logger.warning(f"LIVE order {slug} {side} limit={price} -> status={res.get('status')} ok={res.get('ok')} "
                       f"filled={shares:.4f} sh / {cost:.4f} USDC submit={res.get('submit_ms')}ms "
                       f"decision->post={res['decision_to_post_ms']}ms err={res.get('error')}")

    def close(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=False)
        self.stop.set()


def fetch_official_outcome(slug: str) -> Optional[str]:
    import httpx
    from fetch_official_resolutions import fetch_one
    with httpx.Client(timeout=20) as client:
        return fetch_one(client, "https://gamma-api.polymarket.com", slug).get("official_outcome")


def setup_live(args, out_dir: pathlib.Path) -> Optional[LiveContext]:
    import os
    if not args.confirm_live or os.environ.get(LIVE_CONFIRM_ENV) != LIVE_CONFIRM_VALUE:
        logger.error(f"LIVE refused: pass --confirm-live AND set {LIVE_CONFIRM_ENV}={LIVE_CONFIRM_VALUE}")
        return None
    from dotenv import dotenv_values
    env = {**{k: v for k, v in dotenv_values(REPO_ROOT / ".env").items() if v is not None}, **os.environ}
    missing = [k for k in ("POLYMARKET_PK",) if not env.get(k)]
    if missing:
        logger.error(f"LIVE refused: missing {missing} in .env/environment")
        return None
    config = LiveConfig(shares=args.shares, max_daily_loss=args.max_daily_loss, max_total_loss=args.max_total_loss,
                        streams=tuple(s for s in args.streams.split(",") if s), out_dir=out_dir)
    if config.stop_file.exists():
        logger.error(f"LIVE refused: {config.stop_file} exists (delete it to allow trading)")
        return None
    client = LiveOrderClient.from_env(env)
    balance = client.collateral_balance()
    need = config.shares * 3
    logger.warning(f"LIVE mode: collateral balance={balance} USDC; shares={config.shares}; "
                   f"caps daily={config.max_daily_loss} total={config.max_total_loss}; "
                   f"streams={config.streams or 'ALL'}; kill switch: touch {config.stop_file}")
    if balance is None or balance < need:
        logger.error(f"LIVE refused: balance {balance} < {need} USDC")
        return None
    return LiveContext(config, client, out_dir)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--mode", choices=["dry-run", "live"], default="dry-run")
    parser.add_argument("--out-dir", default=None,
                        help="default: data/twap_z_executor (dry-run) or data/twap_z_executor_live (live)")
    parser.add_argument("--duration-sec", type=float, default=0.0)
    parser.add_argument("--assets", default="btc,eth,sol,xrp")
    live_grp = parser.add_argument_group("live (real money; P2 micro-live)")
    live_grp.add_argument("--confirm-live", action="store_true", help=f"required with --mode live, plus "
                          f"{LIVE_CONFIRM_ENV}={LIVE_CONFIRM_VALUE} in the environment")
    live_grp.add_argument("--shares", type=float, default=5.5)
    live_grp.add_argument("--max-daily-loss", type=float, default=15.0)
    live_grp.add_argument("--max-total-loss", type=float, default=50.0)
    live_grp.add_argument("--streams", default="", help="comma list like btc-updown-5m,xrp-updown-5m (default: all)")
    args = parser.parse_args(argv)
    live = args.mode == "live"
    out_dir = pathlib.Path(args.out_dir or (REPO_ROOT / "data" / ("twap_z_executor_live" if live else "twap_z_executor")))
    out_dir.mkdir(parents=True, exist_ok=True)
    live_ctx = setup_live(args, out_dir) if live else None
    if live and live_ctx is None:
        return 2

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    streams = tuple(st for st in MARKET_STREAMS if st[0] in args.assets.split(","))
    chainlinks = {asset: LiveChainlink() for asset in dict.fromkeys(a for a, _d in streams)}
    books = LiveBookSampler()
    tracker = MarketTracker(books, stop, streams)
    tracker.refresh()
    tracker.start()
    if live_ctx is not None:
        live_ctx.warm_up(books)
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
                if live_ctx is not None:
                    if h == CHECKPOINTS[0]:
                        live_ctx.prefetch(books, slug)
                    if d.action == "WOULD_SUBMIT":
                        row["live"] = live_ctx.submit(d, books, row)
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
        if live_ctx is not None:
            live_ctx.close()
        write_status(time.time())
        logger.info(f"twap-z executor stopped: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
