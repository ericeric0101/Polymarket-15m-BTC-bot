"""Millisecond tape recorder for the stale-quote ("snipe") study. Records only; never places orders.

Runs on a bare VM: Python 3.10+ and `pip install websockets`. No repo imports, no credentials.

    python3 ms_recorder.py --out tape                       # everything in one process
    python3 ms_recorder.py --out tape --sources binance     # or one process per source family
    python3 ms_recorder.py --out tape --sources coinbase
    python3 ms_recorder.py --out tape --sources pm

Each raw WebSocket message is stored as one line `<recv_time_ns>\t<raw text>` in
`<out>/<source>/<YYYYMMDDTHH>.jsonl.gz` (UTC hour). The receive time is taken right after recv()
returns, from the VM clock (keep chrony/NTP on). Sources:
  binance        bookTicker + aggTrade for BTC/ETH/SOL/XRP (USDT)
  coinbase       ticker for BTC/ETH/SOL/XRP-USD
  pm_<a>_<5m|15m> Polymarket CLOB market channel of the current and next Up/Down market, one socket per stream
Market metadata (slug -> tokens, outcome, start, end) goes to `<out>/markets.jsonl`.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

try:
    from websockets.asyncio.client import connect          # websockets >= 13
except ImportError:                                          # pragma: no cover
    from websockets import connect                          # type: ignore

ASSETS = ("btc", "eth", "sol", "xrp")
DURATIONS = {300: "5m", 900: "15m"}
BINANCE_URL = "wss://stream.binance.com:9443/stream?streams=" + "/".join(
    f"{a}usdt@{k}" for a in ASSETS for k in ("bookTicker", "aggTrade"))
COINBASE_URL = "wss://ws-feed.exchange.coinbase.com"
PM_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA = "https://gamma-api.polymarket.com/markets?slug="
PM_PING_SEC = 10.0
FLUSH_SEC = 30.0
STATUS_SEC = 60.0


class HourWriter:
    """Append-only gzip per (source, UTC hour); rotates on the hour, flushes every FLUSH_SEC."""

    def __init__(self, root: Path, source: str):
        self.dir = root / source
        self.dir.mkdir(parents=True, exist_ok=True)
        self.hour, self.fh, self.n, self.last_ns = None, None, 0, 0

    def write(self, recv_ns: int, raw: str) -> None:
        hour = time.strftime("%Y%m%dT%H", time.gmtime(recv_ns / 1e9))
        if hour != self.hour:
            self.close()
            self.hour, self.fh = hour, gzip.open(self.dir / f"{hour}.jsonl.gz", "at", compresslevel=6)
        self.fh.write(f"{recv_ns}\t{raw.replace(chr(10), ' ')}\n")
        self.n += 1
        self.last_ns = recv_ns

    def flush(self) -> None:
        if self.fh:
            self.fh.flush()

    def close(self) -> None:
        if self.fh:
            self.fh.close()
            self.fh = None


class Recorder:
    def __init__(self, out: Path):
        self.out = out
        self.writers: dict[str, HourWriter] = {}
        self.reconnects: dict[str, int] = {}
        self.stop = asyncio.Event()
        self.known_slugs: set[str] = set()
        self.started = time.time()

    def w(self, source: str) -> HourWriter:
        if source not in self.writers:
            self.writers[source] = HourWriter(self.out, source)
        return self.writers[source]

    async def _pump(self, source: str, url: str, subscribe: str | None, until: float | None = None,
                    ping: bool = False) -> None:
        """One connection: subscribe, record every message until `until` (epoch s) or stop."""
        writer = self.w(source)
        async with connect(url, ping_interval=20, max_size=None, open_timeout=10) as ws:
            if subscribe:
                await ws.send(subscribe)
            last_ping = time.monotonic()
            while not self.stop.is_set() and (until is None or time.time() < until):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    raw = None
                if raw is not None:
                    writer.write(time.time_ns(), raw if isinstance(raw, str) else raw.decode())
                if ping and time.monotonic() - last_ping >= PM_PING_SEC:
                    await ws.send("PING")
                    last_ping = time.monotonic()

    async def _forever(self, source: str, url: str, subscribe: str | None) -> None:
        while not self.stop.is_set():
            try:
                await self._pump(source, url, subscribe)
            except Exception as exc:                          # reconnect on any socket error
                print(f"{source}: {type(exc).__name__}: {exc}", flush=True)
            self.reconnects[source] = self.reconnects.get(source, 0) + 1
            await asyncio.sleep(1.0)

    async def binance(self) -> None:
        await self._forever("binance", BINANCE_URL, None)

    async def coinbase(self) -> None:
        sub = json.dumps({"type": "subscribe", "product_ids": [f"{a.upper()}-USD" for a in ASSETS],
                          "channels": ["ticker"]})
        await self._forever("coinbase", COINBASE_URL, sub)

    def _resolve(self, slug: str, asset: str, dur: int) -> dict:
        req = urllib.request.Request(GAMMA + slug, headers={"User-Agent": "ms-recorder/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if not data:
            return {}
        m = data[0]
        tokens = dict(zip(json.loads(m["clobTokenIds"]), json.loads(m["outcomes"])))
        start = int(slug.rsplit("-", 1)[1])
        if slug not in self.known_slugs:
            self.known_slugs.add(slug)
            with open(self.out / "markets.jsonl", "a") as fh:
                fh.write(json.dumps({"slug": slug, "asset": asset, "duration": dur, "start_ts": start,
                                     "end_ts": start + dur, "tokens": tokens}) + "\n")
        return tokens

    async def pm_stream(self, asset: str, dur: int) -> None:
        source = f"pm_{asset}_{DURATIONS[dur]}"
        while not self.stop.is_set():
            start = int(time.time()) // dur * dur
            slugs = [f"{asset}-updown-{DURATIONS[dur]}-{start + k * dur}" for k in (0, 1)]
            tokens: dict = {}
            for s in slugs:
                try:
                    tokens.update(await asyncio.to_thread(self._resolve, s, asset, dur))
                except Exception as exc:
                    print(f"{source}: gamma {s}: {type(exc).__name__}: {exc}", flush=True)
            if not tokens:
                await asyncio.sleep(2.0)
                continue
            # Reconnect 2 s after the current market ends: the new current market is then 2 s old,
            # so no late window is ever cut.
            try:
                await self._pump(source, PM_URL, json.dumps({"assets_ids": list(tokens), "type": "market"}),
                                 until=start + dur + 2, ping=True)
            except Exception as exc:
                print(f"{source}: {type(exc).__name__}: {exc}", flush=True)
                self.reconnects[source] = self.reconnects.get(source, 0) + 1
                await asyncio.sleep(1.0)

    def clock_note(self) -> None:
        for cmd in (["chronyc", "tracking"], ["timedatectl", "show-timesync", "--all"]):
            try:
                txt = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
            except Exception:
                continue
            if txt:
                with open(self.out / "clock.log", "a") as fh:
                    fh.write(f"=== {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {' '.join(cmd)}\n{txt}\n")
                return

    async def housekeeping(self) -> None:
        last_status = last_clock = 0.0
        while not self.stop.is_set():
            await asyncio.sleep(FLUSH_SEC)
            for wr in self.writers.values():
                wr.flush()
            now = time.time()
            if now - last_clock >= 3600:
                last_clock = now
                await asyncio.to_thread(self.clock_note)
            if now - last_status >= STATUS_SEC:
                last_status = now
                st = {"updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                      "uptime_sec": round(now - self.started), "reconnects": self.reconnects,
                      "sources": {k: {"msgs": w.n, "last_age_sec": round(now - w.last_ns / 1e9, 1) if w.last_ns else None}
                                  for k, w in self.writers.items()}}
                (self.out / f"_status_{os.getpid()}.json").write_text(json.dumps(st, indent=1))
                print(json.dumps({k: v["msgs"] for k, v in st["sources"].items()}), flush=True)


async def main_async(a) -> None:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rec = Recorder(out)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, rec.stop.set)
    want = set(a.sources.split(","))
    tasks = [rec.housekeeping()]
    if "binance" in want:
        tasks.append(rec.binance())
    if "coinbase" in want:
        tasks.append(rec.coinbase())
    if "pm" in want:
        tasks += [rec.pm_stream(x, d) for x in ASSETS for d in DURATIONS]
    if a.duration_sec:
        async def timer():
            try:
                await asyncio.wait_for(rec.stop.wait(), a.duration_sec)
            except asyncio.TimeoutError:
                rec.stop.set()
        tasks.append(timer())
    print(f"ms_recorder sources={sorted(want)} -> {out}", flush=True)
    try:
        await asyncio.gather(*[asyncio.create_task(t) for t in tasks])
    finally:
        for wr in rec.writers.values():
            wr.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="tape")
    ap.add_argument("--sources", default="binance,coinbase,pm")
    ap.add_argument("--duration-sec", type=float, default=0.0)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
