"""Bounded future-pair loading on the existing node loop; no subscription authority."""
from __future__ import annotations

import asyncio
import threading
import time

from bot.market_data import fetch_gamma_market_by_slug
from bot.market_discovery import extract_instrument_ids_from_gamma_market, hydrate_gamma_market_details
from bot.quote_recovery_observability import diagnostic

# The unchanged 3h timer spans 12 fifteen-minute slots. Allow one next-market
# prewarm slot and one boundary/startup buffer: 14 distinct dynamic markets.
# Startup instruments are separate; with the profile's cap of 6, this permits
# at most 20 markets / 40 BTC instruments per node, even if rollover is deferred.
MAX_DYNAMIC_MARKETS = 12 + 1 + 1
MAX_ATTEMPTS = 3
LOAD_TIMEOUT_SEC = 20.0


async def resolve_pair_ids(slug):
    """Reuse the launcher's Gamma parsing, without its synchronous asyncio.run."""
    market = await fetch_gamma_market_by_slug(slug)
    if not market:
        raise ValueError("market_not_published")
    ids = extract_instrument_ids_from_gamma_market(market)
    if not ids:
        ids = extract_instrument_ids_from_gamma_market(await hydrate_gamma_market_details(market))
    if len(ids) != 2 or len(set(map(str, ids))) != 2:
        raise ValueError("incomplete_pair_ids")
    return ids


class InstrumentAdmission:
    def __init__(self, strategy, provider, loop):
        self.strategy, self.provider, self.loop = strategy, provider, loop
        # Selection and pair publication share this repo-owned lock. Network
        # work never holds it; no await occurs during cache publication.
        lock = getattr(strategy, "_market_selection_lock", None)
        if lock is None:
            lock = threading.RLock()
            strategy._market_selection_lock = lock
        self.lock = lock
        self.records = {}  # bounded by MAX_DYNAMIC_MARKETS, failures included
        self.pending = None  # one queued/running task across all slugs
        self.task = None
        self.closed = False
        self.bound_reported = False

    def cached_pair(self, slug):
        sides = set()
        for instrument in self.strategy.cache.instruments():
            if (getattr(instrument, "info", None) or {}).get("market_slug") == slug:
                sides.add(str(self.strategy._extract_outcome_from_instrument(instrument)).lower())
        return {"up", "down"} <= sides

    def selectable(self, slug):
        record = self.records.get(slug)
        return record is None or record["status"] == "completed"

    def request(self, slug):
        now = time.time()
        try:
            start = int(slug.removeprefix("btc-updown-15m-"))
            if slug != f"btc-updown-15m-{start}" or start % 900:
                return False
        except (ValueError, AttributeError):
            return False
        # Only current/next windows are admitted, never an arbitrary horizon.
        if start > now + 1800 or start + 900 <= now:
            return False
        with self.lock:
            if self.closed or getattr(self.strategy, "_stopping", False):
                return False
            record = self.records.get(slug)
            if self.cached_pair(slug) and self.selectable(slug):
                return True
            if self.pending is not None or (record and (
                    record["attempts"] >= MAX_ATTEMPTS or now < record["retry_at"]
                    or record["status"] == "completed")):
                return False
            if record is None:
                if len(self.records) >= MAX_DYNAMIC_MARKETS:
                    if not self.bound_reported:
                        self.bound_reported = True
                        diagnostic(self.strategy, "DYNAMIC_INSTRUMENT_ADMISSION_FAILED",
                                   slug=slug, instrument_ids=[], request_ts=now,
                                   reason="per_node_admission_bound", pair_complete=False,
                                   cache_visible=False)
                    return False  # unchanged lifecycle misses remain authoritative
                record = self.records[slug] = dict(attempts=0, ids=[], status="pending", retry_at=0)
            record.update(attempts=record["attempts"] + 1, status="pending", request_ts=now)
            self.pending = slug
            diagnostic(self.strategy, "DYNAMIC_INSTRUMENT_ADMISSION_REQUESTED",
                       slug=slug, instrument_ids=record["ids"], request_ts=now,
                       already_cached=False, pair_complete=False, cache_visible=False)
            try:
                self.loop.call_soon_threadsafe(self._start, slug)
            except RuntimeError:
                record["status"] = "failed"
                self.pending = None
                return False
            return True

    def _start(self, slug):
        with self.lock:
            if self.closed or getattr(self.strategy, "_stopping", False):
                self.pending = None
                return
            self.task = self.loop.create_task(self._run(slug))

    async def _load(self, slug, record):
        ids = await resolve_pair_ids(slug)
        if record["ids"] and set(map(str, ids)) != set(record["ids"]):
            raise ValueError("pair_ids_changed_on_retry")
        record["ids"] = list(map(str, ids))
        await self.provider.load_ids_async(ids)
        pair = [self.provider.find(i) for i in ids]
        if any(i is None for i in pair):
            raise ValueError("incomplete_provider_pair")
        if any(str(i.id) != str(expected) for i, expected in zip(pair, ids)):
            raise ValueError("wrong_instrument_ids")
        if any((i.info or {}).get("market_slug") != slug for i in pair):
            raise ValueError("wrong_market_pair")
        sides = {str(self.strategy._extract_outcome_from_instrument(i)).lower() for i in pair}
        if sides != {"up", "down"}:
            raise ValueError("incomplete_outcome_pair")
        return pair

    async def _run(self, slug):
        record = self.records[slug]
        started = time.time()
        timer = time.monotonic()
        diagnostic(self.strategy, "DYNAMIC_INSTRUMENT_ADMISSION_STARTED", slug=slug,
                   instrument_ids=record["ids"], request_ts=record["request_ts"], start_ts=started,
                   already_cached=False, pair_complete=False, cache_visible=False)
        try:
            pair = await asyncio.wait_for(self._load(slug, record), LOAD_TIMEOUT_SEC)
            with self.lock:
                now = time.time()
                current = str(getattr(self.strategy, "current_market_slug", "") or "")
                current_start = int(current.rsplit("-", 1)[-1]) if current else 0
                start = int(slug.rsplit("-", 1)[-1])
                if (self.closed or getattr(self.strategy, "_stopping", False)
                        or start + 900 <= now or start < current_start):
                    raise ValueError("obsolete_or_stopping")
                # Pending/failed records quarantine the entire slug, including
                # any partial public-cache write or adapter periodic publication.
                for instrument in pair:
                    self.strategy.cache.add_instrument(instrument)
                if not self.cached_pair(slug):
                    raise ValueError("pair_not_cache_visible")
                record["status"] = "completed"
            diagnostic(self.strategy, "DYNAMIC_INSTRUMENT_ADMISSION_COMPLETED", slug=slug,
                       instrument_ids=record["ids"], request_ts=record["request_ts"], start_ts=started,
                       complete_ts=now, duration_ms=(time.monotonic()-timer)*1000,
                       already_cached=False, pair_complete=True, cache_visible=True)
        except (Exception, asyncio.CancelledError) as exc:
            with self.lock:
                record.update(status="failed", retry_at=time.time() + max(
                    1.0, float(getattr(self.strategy, "market_next_poll_sec", 10))))
            diagnostic(self.strategy, "DYNAMIC_INSTRUMENT_ADMISSION_FAILED", slug=slug,
                       instrument_ids=record["ids"], request_ts=record["request_ts"], start_ts=started,
                       complete_ts=time.time(), duration_ms=(time.monotonic()-timer)*1000,
                       error_class=type(exc).__name__, reason=str(exc)[:250],
                       already_cached=False, pair_complete=False, cache_visible=False)
        finally:
            with self.lock:
                self.pending = self.task = None

    def stop(self):
        with self.lock:
            self.closed = True
            task = self.task
        if task is not None and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # Closed-loop races must never prevent node shutdown.


def bind_instrument_admission(node, strategy):
    """Read the existing adapter/provider reference; never mutate private cache state."""
    for client in node.kernel.data_engine._clients.values():
        provider = getattr(client, "_instrument_provider", None)
        if provider is not None and client.__class__.__module__.startswith(
                "nautilus_trader.adapters.polymarket"):
            strategy._instrument_admission = InstrumentAdmission(strategy, provider, node.kernel.loop)
            return


def request_instrument_admission(strategy, slug):
    admission = getattr(strategy, "_instrument_admission", None)
    if admission is not None:
        admission.request(slug)


def stop_instrument_admission(strategy):
    admission = getattr(strategy, "_instrument_admission", None)
    if admission is not None:
        admission.stop()
