"""Standalone multi-source 1-second BTC price recorder (research only).

Runs as its own process (``scripts/price_recorder.py``), never inside the trading
bot, so a slow disk, a stuck socket or a crash here cannot touch trading I/O.

Storage design (see reports/spot_fallback_duty_cycle_20261015 discussion):
- one row per source per local-receipt second (last price, high/low, update count,
  last source and receipt clocks), not every tick;
- rows accumulate in memory and are written as one zstd Parquet file per UTC hour
  (``<root>/YYYY-MM-DD/HH.parquet``), so a day is ~24 files, not thousands;
- bounded in-memory buffer (drops are counted, never block the feeds);
- writes stop while free disk is below a floor; day directories older than the
  retention window are deleted.
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

SOURCES = ("chainlink_spot", "chainlink_twap60", "binance_btcusdt", "coinbase_btcusd")
_DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SCHEMA = None


def _schema():
    global _SCHEMA
    if _SCHEMA is None:
        import pyarrow as pa
        _SCHEMA = pa.schema([
            ("source", pa.string()),
            ("ts_sec", pa.int64()),            # local receipt second (UTC epoch)
            ("close", pa.float64()),           # last price received in the second
            ("high", pa.float64()),
            ("low", pa.float64()),
            ("n_updates", pa.int32()),
            ("last_source_ms", pa.int64()),    # feed's own clock for the close; null if absent
            ("last_received_ms", pa.int64()),  # local wall clock for the close
        ])
    return _SCHEMA


@dataclass
class _Bucket:
    ts_sec: int
    close: float
    high: float
    low: float
    n_updates: int
    last_source_ms: Optional[int]
    last_received_ms: int

    def row(self, source: str) -> dict[str, Any]:
        return {"source": source, "ts_sec": self.ts_sec, "close": self.close, "high": self.high,
                "low": self.low, "n_updates": self.n_updates, "last_source_ms": self.last_source_ms,
                "last_received_ms": self.last_received_ms}


class SecondBucketer:
    """Collapses ticks into one row per source per second.

    ``by_source_clock=False`` keys rows by local receipt second (exchanges: one
    local clock for lead/lag). ``by_source_clock=True`` keys by the feed's own
    second (Chainlink spot/TWAP are 1 Hz source-stamped, so every source second
    is kept for TWAP reconstruction); ``grace_sec`` waits for late arrivals.
    """

    def __init__(self, emit: Callable[[dict[str, Any]], None], *, by_source_clock: bool = False,
                 grace_sec: float = 0.0) -> None:
        self._emit = emit
        self.by_source_clock = bool(by_source_clock)
        self.grace_sec = float(grace_sec)
        self._lock = threading.Lock()
        self._open: dict[str, _Bucket] = {}

    def observe(self, source: str, price: float, received_ts: float, source_ms: Optional[int]) -> None:
        if not (price > 0) or price != price:
            return
        if self.by_source_clock:
            if not source_ms:
                return
            sec = int(source_ms // 1000)
        else:
            sec = int(received_ts)
        received_ms = int(received_ts * 1000)
        finished = None
        with self._lock:
            bucket = self._open.get(source)
            if bucket is None or sec > bucket.ts_sec:
                if bucket is not None:
                    finished = bucket.row(source)
                self._open[source] = _Bucket(sec, price, price, price, 1, source_ms, received_ms)
            elif sec == bucket.ts_sec:
                bucket.close = price
                bucket.high = max(bucket.high, price)
                bucket.low = min(bucket.low, price)
                bucket.n_updates += 1
                bucket.last_source_ms = source_ms
                bucket.last_received_ms = received_ms
        if finished is not None:
            self._emit(finished)

    def flush_older_than(self, now_ts: float) -> None:
        """Close buckets whose second has passed (a quiet feed must not hold its last row)."""
        sec = int(now_ts - self.grace_sec)
        done = []
        with self._lock:
            for source, bucket in list(self._open.items()):
                if bucket.ts_sec < sec:
                    done.append(bucket.row(source))
                    del self._open[source]
        for row in done:
            self._emit(row)


def _hour_key(ts_sec: int) -> tuple[str, str]:
    dt = datetime.fromtimestamp(ts_sec, timezone.utc)
    return dt.date().isoformat(), f"{dt.hour:02d}"


def write_hour_file(root: Path, day: str, hour: str, rows: list[dict[str, Any]], schema=None,
                    sort_cols: tuple[str, ...] = ("ts_sec", "source")) -> Path:
    """Atomically write one hour's rows; a second write in the same hour gets a suffix."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    directory = root / day
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{hour}.parquet"
    n = 1
    while final.exists():
        n += 1
        final = directory / f"{hour}_{n}.parquet"
    temp = directory / f".{final.name}.{uuid.uuid4().hex}.tmp"
    rows = sorted(rows, key=lambda r: tuple(str(r[c]) if c != "ts_sec" else r[c] for c in sort_cols))
    try:
        pq.write_table(pa.Table.from_pylist(rows, schema=schema if schema is not None else _schema()),
                       temp, compression="zstd")
        # No-clobber publish: link() fails if the name exists, so two recorder
        # processes overlapping during a restart can never overwrite each other.
        while True:
            try:
                os.link(temp, final)
                break
            except FileExistsError:
                n += 1
                final = directory / f"{hour}_{n}.parquet"
    finally:
        temp.unlink(missing_ok=True)
    return final


@dataclass
class RecorderStats:
    rows_buffered: int = 0
    rows_written: int = 0
    rows_dropped_buffer_full: int = 0
    rows_dropped_disk_low: int = 0
    files_written: int = 0
    write_errors: int = 0
    days_deleted: int = 0
    last_error: Optional[str] = None
    rows_by_source: dict[str, int] = field(default_factory=dict)


class HourlyParquetStore:
    """Bounded in-memory buffer flushed to one Parquet file per UTC hour."""

    def __init__(
        self,
        root: str | Path,
        *,
        retention_days: int = 30,
        min_free_disk_gb: float = 10.0,
        max_buffer_rows: int = 60_000,
        free_disk_provider: Callable[[Path], Any] = shutil.disk_usage,
        clock: Callable[[], float] = time.time,
        schema=None,
        count_key: str = "source",
        sort_cols: tuple[str, ...] = ("ts_sec", "source"),
        partial_flush_sec: float = 0.0,
    ) -> None:
        self.root = Path(root)
        self.schema, self.count_key, self.sort_cols = schema, count_key, sort_cols
        self.retention_days = int(retention_days)
        self.min_free_disk_gb = float(min_free_disk_gb)
        self.max_buffer_rows = int(max_buffer_rows)
        self._free_disk = free_disk_provider
        self._clock = clock
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._buffered = 0
        # >0: also write the current hour as a part file (HH_n.parquet) at this
        # cadence, or when the buffer is 80% full, so a busy store never drops rows.
        self.partial_flush_sec = float(partial_flush_sec)
        self._last_partial_flush = self._clock()
        self.stats = RecorderStats()

    def add(self, row: dict[str, Any]) -> None:
        with self._lock:
            if self._buffered >= self.max_buffer_rows:
                self.stats.rows_dropped_buffer_full += 1
                return
            self._rows.setdefault(_hour_key(int(row["ts_sec"])), []).append(row)
            self._buffered += 1
            self.stats.rows_buffered = self._buffered
            src = str(row.get(self.count_key))
            self.stats.rows_by_source[src] = self.stats.rows_by_source.get(src, 0) + 1

    def disk_ok(self) -> bool:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            free_gb = int(self._free_disk(self.root).free) / (1024 ** 3)
        except Exception as exc:  # unknown disk state: do not write
            self.stats.last_error = f"disk_check_failed:{type(exc).__name__}"
            return False
        return free_gb >= self.min_free_disk_gb

    def flush(self, *, include_current_hour: bool = False) -> list[Path]:
        """Write completed hours (and the current one on shutdown)."""
        now = self._clock()
        current = _hour_key(int(now))
        with self._lock:
            if self.partial_flush_sec > 0 and (now - self._last_partial_flush >= self.partial_flush_sec
                                               or self._buffered >= 0.8 * self.max_buffer_rows):
                include_current_hour = True
                self._last_partial_flush = now
            keys = [k for k in self._rows if include_current_hour or k < current]
            batches = {k: self._rows.pop(k) for k in keys}
            self._buffered -= sum(len(v) for v in batches.values())
            self.stats.rows_buffered = self._buffered
        if not batches:
            return []
        if not self.disk_ok():
            self.stats.rows_dropped_disk_low += sum(len(v) for v in batches.values())
            return []
        written = []
        for (day, hour), rows in sorted(batches.items()):
            try:
                written.append(write_hour_file(self.root, day, hour, rows, self.schema, self.sort_cols))
                self.stats.rows_written += len(rows)
                self.stats.files_written += 1
            except Exception as exc:
                self.stats.write_errors += 1
                self.stats.last_error = f"write_failed:{type(exc).__name__}:{exc}"
        return written

    def enforce_retention(self) -> list[Path]:
        """Delete day directories older than the retention window (only YYYY-MM-DD dirs)."""
        if not self.root.exists():
            return []
        cutoff = (datetime.fromtimestamp(self._clock(), timezone.utc).date()
                  - timedelta(days=self.retention_days))
        removed = []
        for child in self.root.iterdir():
            if not child.is_dir() or not _DAY_DIR.match(child.name):
                continue
            try:
                day = datetime.strptime(child.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed.append(child)
        self.stats.days_deleted += len(removed)
        return removed


def parse_coinbase_ticker(payload: Any) -> Optional[tuple[float, Optional[int]]]:
    """(price, source_ms) from a Coinbase Exchange ``ticker`` message, else None."""
    if not isinstance(payload, dict) or payload.get("type") != "ticker":
        return None
    if str(payload.get("product_id", "")).upper() != "BTC-USD":
        return None
    try:
        price = float(payload.get("price"))
    except (TypeError, ValueError):
        return None
    if not price > 0:
        return None
    source_ms = None
    raw_time = payload.get("time")
    if isinstance(raw_time, str) and raw_time:
        try:
            source_ms = int(datetime.fromisoformat(raw_time.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            source_ms = None
    return price, source_ms


# --- Polymarket order books ---------------------------------------------------------------

_BOOK_SCHEMA = None


def book_schema():
    global _BOOK_SCHEMA
    if _BOOK_SCHEMA is None:
        import pyarrow as pa
        _BOOK_SCHEMA = pa.schema([
            ("ts_sec", pa.int64()),                    # local wall-clock second of the sample
            ("market_slug", pa.string()),
            ("outcome", pa.string()),                  # "Up" / "Down"
            ("token_id", pa.string()),
            ("best_bid", pa.float64()), ("best_ask", pa.float64()),
            ("bid_size", pa.float64()), ("ask_size", pa.float64()),
            ("n_updates", pa.int32()),                 # book messages applied during this second
            ("last_update_received_ms", pa.int64()),   # local receipt of the last change to this book
            ("last_event_ms", pa.int64()),             # venue timestamp of that change, if given
        ])
    return _BOOK_SCHEMA


def _num(value: Any) -> Optional[float]:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _event_ms(value: Any) -> Optional[int]:
    x = _num(value)
    if x is None or x <= 0:
        return None
    return int(x if x > 10_000_000_000 else x * 1000)


class BookSampler:
    """Local L2 books for tracked tokens, sampled once per second (state carried forward).

    Only books initialised by a full ``book`` snapshot on the current connection are
    sampled; ``reset()`` on (re)connect invalidates them, so a sampled row always
    reflects a live, synchronised book.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._meta: dict[str, dict[str, Any]] = {}   # token -> {slug, outcome, end_ts}
        self._books: dict[str, dict[str, Any]] = {}

    def set_tokens(self, meta: dict[str, dict[str, Any]]) -> list[str]:
        """Replace tracked tokens; returns tokens that were not tracked before."""
        with self._lock:
            new = [t for t in meta if t not in self._meta]
            self._meta = dict(meta)
            for token in list(self._books):
                if token not in self._meta:
                    del self._books[token]
            return new

    def tokens(self, prefix: Optional[str] = None) -> list[str]:
        """Tracked tokens, optionally only those whose market slug starts with ``prefix``."""
        with self._lock:
            return [t for t, m in self._meta.items() if prefix is None or m["slug"].startswith(prefix)]

    def reset(self, prefix: Optional[str] = None) -> None:
        """Invalidate books (all, or one connection's ``prefix`` subset) until fresh snapshots arrive."""
        with self._lock:
            if prefix is None:
                self._books.clear()
                return
            for token in [t for t in self._books
                          if self._meta.get(t, {}).get("slug", "").startswith(prefix)]:
                del self._books[token]

    def apply(self, payload: Any, received_ts: float) -> int:
        items = payload if isinstance(payload, list) else [payload]
        applied = 0
        received_ms = int(received_ts * 1000)
        with self._lock:
            for item in items:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("event_type") or item.get("type") or "").lower()
                if kind == "book":
                    token = str(item.get("asset_id") or "")
                    if token not in self._meta:
                        continue
                    book = {"bids": {}, "asks": {}, "n": 0}
                    for side in ("bids", "asks"):
                        for level in item.get(side) or []:
                            price, size = _num(level.get("price")), _num(level.get("size"))
                            if price is not None and size is not None and 0 <= price <= 1 and size > 0:
                                book[side][price] = size
                    prev = self._books.get(token)
                    book["n"] = (prev or {}).get("n", 0) + 1
                    book["last_ms"], book["event_ms"] = received_ms, _event_ms(item.get("timestamp"))
                    self._books[token] = book
                    applied += 1
                elif kind == "price_change":
                    event_ms = _event_ms(item.get("timestamp"))
                    for ch in item.get("price_changes") or item.get("changes") or []:
                        token = str(ch.get("asset_id") or item.get("asset_id") or "")
                        book = self._books.get(token)
                        price, size = _num(ch.get("price")), _num(ch.get("size"))
                        side = {"BUY": "bids", "SELL": "asks"}.get(str(ch.get("side") or "").upper())
                        if book is None or price is None or size is None or side is None:
                            continue
                        if size <= 0:
                            book[side].pop(price, None)
                        else:
                            book[side][price] = size
                        book["n"] += 1
                        book["last_ms"], book["event_ms"] = received_ms, event_ms
                        applied += 1
        return applied

    def sample(self, now_ts: float) -> list[dict[str, Any]]:
        sec = int(now_ts)
        rows = []
        with self._lock:
            for token, book in self._books.items():
                meta = self._meta.get(token)
                if meta is None or now_ts > float(meta["end_ts"]) + 5:
                    continue
                bids, asks = book["bids"], book["asks"]
                bb = max(bids) if bids else None
                ba = min(asks) if asks else None
                rows.append({"ts_sec": sec, "market_slug": meta["slug"], "outcome": meta["outcome"],
                             "token_id": token, "best_bid": bb, "best_ask": ba,
                             "bid_size": bids.get(bb) if bb is not None else None,
                             "ask_size": asks.get(ba) if ba is not None else None,
                             "n_updates": book["n"], "last_update_received_ms": book["last_ms"],
                             "last_event_ms": book["event_ms"]})
                book["n"] = 0
        return rows


def market_tokens_for(slugs: list[str], fetch_market: Callable[[str], Optional[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """token -> {slug, outcome, end_ts} for the given BTC 15m slugs (Up/Down only, v1 or v2)."""
    from bot.protocol_v2 import UnsupportedMarketProtocol, select_market_assets

    meta: dict[str, dict[str, Any]] = {}
    for slug in slugs:
        market = fetch_market(slug)
        if not market:
            continue
        try:
            assets = select_market_assets(market)
        except UnsupportedMarketProtocol:
            continue
        end_ts = float(slug.rsplit("-", 1)[-1]) + slug_duration_sec(slug)
        for outcome, token in zip(assets.outcomes, assets.asset_ids):
            if outcome.strip().lower() in ("up", "down"):
                meta[str(token)] = {"slug": slug, "outcome": outcome.strip().capitalize(), "end_ts": end_ts}
    return meta


# Up/Down markets that settle on the Chainlink 60 s TWAP (same rule text, verified on Gamma
# 2026-10-16). BTC 15m keeps its original feed/source names; the others are additive.
ALT_ASSETS = ("eth", "sol", "xrp")
MARKET_STREAMS = tuple((a, d) for a in ("btc",) + ALT_ASSETS for d in (900, 300))
_DURATION_TAG = {900: "15m", 300: "5m"}


def slug_duration_sec(slug: str) -> int:
    """Market length from a ``<asset>-updown-<5m|15m>-<start>`` slug (default 15m)."""
    return 300 if "-updown-5m-" in str(slug) else 900


def stream_slugs(now_ts: float, streams=MARKET_STREAMS, ahead: int = 1) -> list[str]:
    """Current (and ``ahead`` next) market slugs for every (asset, duration) stream."""
    out = []
    for asset, dur in streams:
        start = int(now_ts) // dur * dur
        out += [f"{asset}-updown-{_DURATION_TAG[dur]}-{start + dur * k}" for k in range(ahead + 1)]
    return out


def alt_source_name(kind: str, asset: str) -> str:
    return f"{'chainlink_twap60' if kind == 'twap' else 'chainlink_spot'}:{asset}"


def _e18_or_value(item: dict[str, Any]) -> Optional[float]:
    raw = item.get("full_accuracy_value")
    try:
        if raw is not None:
            from decimal import Decimal
            v = float(Decimal(str(raw)) / Decimal(10 ** 18))   # same as bot.price_streams.from_e18_decimal
            if v > 0:
                return v
    except (TypeError, ValueError):
        pass
    return _num(item.get("value"))


def parse_rtds_chainlink(payload: Any) -> list[tuple[str, str, float, int]]:
    """RTDS Chainlink frames -> [(kind 'spot'|'twap', asset, price, source_ms)] for any symbol.

    Handles raw spot updates (``crypto_prices_chainlink``), 60 s TWAP updates and the
    TWAP subscribe snapshot (``payload.data`` list). Unknown frames give [].
    """
    if not isinstance(payload, dict):
        return []
    topic = str(payload.get("topic") or "").lower()
    inner = payload.get("payload")
    if not isinstance(inner, dict):
        return []
    if topic == "crypto_prices_chainlink":
        kind = "spot"
    elif topic == "crypto_prices_twap_sixty":
        kind = "twap"
    else:
        return []
    asset = str(inner.get("symbol") or "").lower().split("/")[0]
    if not asset:
        return []
    items = inner.get("data") if isinstance(inner.get("data"), list) else [inner]
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        price, ms = _e18_or_value(item), _event_ms(item.get("timestamp"))
        if price is not None and price > 0 and ms:
            out.append((kind, asset, float(price), int(ms)))
    return out


def book_stream_prefix(slug_or_stream: str) -> str:
    """'eth-updown-5m-1792...' (or 'eth-updown-5m') -> 'eth-updown-5m-' : one CLOB socket per stream."""
    parts = str(slug_or_stream).split("-")
    return "-".join(parts[:3]) + "-"


def book_feed_name(prefix: str) -> str:
    """Status/feed name of a stream's book socket; BTC 15m keeps the original v2 name."""
    stream = prefix.rstrip("-")
    return "polymarket_book" if stream == "btc-updown-15m" else f"polymarket_book_{stream.replace('-updown-', '_')}"
