#!/usr/bin/env python3
"""Standalone forward-only recorder for Polymarket public market-channel L2 events."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import websockets

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.analyze_weekend_liquidity import GAMMA
from bot.analytics.weekend_liquidity import public_data_source_label

DEFAULT_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def subscription_message(token_ids: list[str], *, operation: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "assets_ids": [str(token) for token in token_ids],
        "type": "market",
        "custom_feature_enabled": True,
    }
    if operation:
        message["operation"] = operation
    return message


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _event_time_ms(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = float(value)
        return int(parsed if parsed > 10_000_000_000 else parsed * 1000)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except (TypeError, ValueError, OverflowError):
            return None


def _levels(value: Any) -> dict[float, float]:
    result = {}
    if not isinstance(value, list):
        return result
    for row in value:
        if isinstance(row, dict):
            price, size = _finite(row.get("price") or row.get("px")), _finite(row.get("size") or row.get("sz"))
            if price is not None and size is not None and 0 <= price <= 1 and size > 0:
                result[price] = size
    return result


def _depth(levels: dict[float, float], best: float, *, is_bid: bool, distance: float) -> float:
    if is_bid:
        return sum(size for price, size in levels.items() if best - price <= distance + 1e-9)
    return sum(size for price, size in levels.items() if price - best <= distance + 1e-9)


def _row_for_token(token_id: str, book: dict[str, Any], event: dict[str, Any],
                   metadata: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    info = metadata.get(token_id)
    if not info:
        return None
    bids, asks = book.get("bids", {}), book.get("asks", {})
    best_bid = max(bids) if bids else None
    best_ask = min(asks) if asks else None
    event_payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
    if event.get("event_type") == "best_bid_ask" or event.get("type") == "best_bid_ask":
        best_bid = _finite(event_payload.get("best_bid", event_payload.get("bestBid"))) or best_bid
        best_ask = _finite(event_payload.get("best_ask", event_payload.get("bestAsk"))) or best_ask
    if best_bid is None and best_ask is None and not bids and not asks:
        return None
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    bid_size = bids.get(best_bid) if best_bid is not None else None
    ask_size = asks.get(best_ask) if best_ask is not None else None
    depths = {}
    for cents in (1, 2, 5):
        distance = cents / 100.0
        depths[f"depth_{cents}c_bid"] = _depth(bids, best_bid, is_bid=True, distance=distance) if best_bid is not None else None
        depths[f"depth_{cents}c_ask"] = _depth(asks, best_ask, is_bid=False, distance=distance) if best_ask is not None else None
    depth_bid, depth_ask = depths["depth_5c_bid"], depths["depth_5c_ask"]
    imbalance = ((depth_bid - depth_ask) / (depth_bid + depth_ask)
                 if depth_bid is not None and depth_ask is not None and depth_bid + depth_ask > 0 else None)
    return {
        "received_at_utc": datetime.now(timezone.utc).isoformat(),
        "received_ts_ms": int(time.time() * 1000),
        "event_ts_ms": _event_time_ms(event_payload.get("timestamp") or event.get("timestamp")),
        "market_slug": info.get("slug"), "condition_id": info.get("condition_id"),
        "market_id": event_payload.get("market") or event.get("market"),
        "token_id": token_id, "event_type": event.get("event_type") or event.get("type"),
        "best_bid": best_bid, "best_ask": best_ask, "spread": spread,
        "bid_size": bid_size, "ask_size": ask_size,
        "bids_json": json.dumps([{"price": p, "size": s} for p, s in sorted(bids.items(), reverse=True)]),
        "asks_json": json.dumps([{"price": p, "size": s} for p, s in sorted(asks.items())]),
        **depths, "book_imbalance_5c": imbalance,
        "source": public_data_source_label("l2"),
    }


def _update_levels(book: dict[str, Any], changes: list[dict[str, Any]]) -> list[str]:
    touched = []
    for change in changes:
        token = str(change.get("asset_id") or change.get("assetId") or change.get("token_id") or change.get("tokenId") or "")
        price, size = _finite(change.get("price")), _finite(change.get("size"))
        if not token or price is None or size is None:
            continue
        side = str(change.get("side") or "").upper()
        side_key = "bids" if side in {"BUY", "BID"} else "asks" if side in {"SELL", "ASK"} else ""
        if not side_key:
            continue
        levels = book.setdefault(token, {"bids": {}, "asks": {}})[side_key]
        if size <= 0:
            levels.pop(price, None)
        else:
            levels[price] = size
        touched.append(token)
    return touched


def apply_market_event(state: dict[str, Any], message: Any,
                       metadata: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Update a local L2 book from official market-channel snapshots/deltas."""
    messages = message if isinstance(message, list) else [message]
    output = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        event = dict(item)
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if payload:
            event = {**event, **payload}
        kind = str(event.get("event_type") or event.get("type") or "").lower()
        if kind == "book":
            token = str(event.get("asset_id") or event.get("assetId") or event.get("token_id") or event.get("tokenId") or "")
            if not token:
                continue
            state[token] = {"bids": _levels(event.get("bids")), "asks": _levels(event.get("asks"))}
            row = _row_for_token(token, state[token], item, metadata)
            if row:
                output.append(row)
        elif kind == "price_change":
            changes = event.get("price_changes") or event.get("priceChanges") or []
            for token in dict.fromkeys(_update_levels(state, changes)):
                row = _row_for_token(token, state[token], item, metadata)
                if row:
                    output.append(row)
        elif kind == "best_bid_ask":
            token = str(event.get("asset_id") or event.get("assetId") or event.get("token_id") or event.get("tokenId") or "")
            if token:
                book = state.setdefault(token, {"bids": {}, "asks": {}})
                row = _row_for_token(token, book, item, metadata)
                if row:
                    output.append(row)
    return output


def _market_slugs(lookahead: int) -> list[str]:
    now = int(time.time())
    current = now // 900 * 900
    return [f"btc-updown-15m-{current + 900 * offset}" for offset in range(max(1, lookahead + 1))]


def _fetch_json(url: str) -> Any:
    request = Request(url, headers={"User-Agent": "PolymarketForwardL2Recorder/1.0"})
    with urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _resolve_market(slug: str) -> dict[str, dict[str, Any]]:
    payload = _fetch_json(f"{GAMMA}/markets?{urlencode({'slug': slug, 'limit': 5})}")
    market = payload[0] if isinstance(payload, list) and payload else None
    if not isinstance(market, dict):
        return {}
    tokens = market.get("clobTokenIds") or market.get("clob_token_ids") or []
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except ValueError:
            tokens = []
    condition = market.get("conditionId") or market.get("condition_id")
    if not condition or not isinstance(tokens, list):
        return {}
    return {str(token): {"slug": slug, "condition_id": condition} for token in tokens}


class _DailyParquetSink:
    def __init__(self, output: Path, session_id: str) -> None:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise RuntimeError("Parquet output requires optional dependency pyarrow") from exc
        self.pa, self.pq = pa, pq
        self.output, self.session_id = output, session_id
        self.day: str | None = None
        self.writer = None

    def write(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        rows_by_day: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            day = str(row["received_at_utc"])[:10]
            rows_by_day.setdefault(day, []).append(row)
        for day, day_rows in rows_by_day.items():
            if day != self.day:
                self.close()
                directory = self.output / day
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / f"part-{self.session_id}.parquet"
                table = self.pa.Table.from_pylist(day_rows)
                self.writer = self.pq.ParquetWriter(path, table.schema, compression="zstd")
                self.day = day
            table = self.pa.Table.from_pylist(day_rows)
            self.writer.write_table(table)

    def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            self.writer = None
            self.day = None


async def run_recorder(args: argparse.Namespace) -> None:
    session_id = uuid.uuid4().hex[:10]
    sink = _DailyParquetSink(Path(args.output), session_id)
    metadata: dict[str, dict[str, Any]] = {}
    books: dict[str, Any] = {}
    writer_rows: list[dict[str, Any]] = []
    last_refresh = 0.0
    attempt = 0

    async def refresh_metadata() -> None:
        nonlocal last_refresh
        slugs = args.slug or _market_slugs(args.lookahead_markets)
        for slug in slugs:
            try:
                resolved = await asyncio.to_thread(_resolve_market, slug)
                metadata.update(resolved)
            except Exception as exc:
                print(f"market metadata unavailable slug={slug} error={type(exc).__name__}: {exc}", file=sys.stderr)
        last_refresh = time.monotonic()

    try:
        await refresh_metadata()
        while True:
            try:
                async with websockets.connect(args.websocket, ping_interval=20, ping_timeout=20, open_timeout=15) as ws:
                    subscribed: set[str] = set()

                    async def subscribe_new(*, initial: bool = False) -> None:
                        fresh = sorted(set(metadata) - subscribed)
                        if fresh:
                            await ws.send(json.dumps(subscription_message(
                                fresh, operation=None if initial else "subscribe"
                            )))
                            subscribed.update(fresh)
                            print(f"subscribed public market channel tokens={len(fresh)} session={session_id}")

                    await subscribe_new(initial=True)
                    attempt = 0
                    while True:
                        if time.monotonic() - last_refresh >= args.market_refresh_sec:
                            await refresh_metadata()
                            await subscribe_new()
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=max(1.0, min(args.market_refresh_sec, 30.0))
                            )
                        except asyncio.TimeoutError:
                            continue
                        for row in apply_market_event(books, json.loads(raw), metadata):
                            writer_rows.append(row)
                        if len(writer_rows) >= args.flush_rows:
                            await asyncio.to_thread(sink.write, writer_rows)
                            writer_rows.clear()
            except asyncio.CancelledError:
                raise
            except KeyboardInterrupt:
                break
            except Exception as exc:
                print(f"market websocket disconnected; reconnecting error={type(exc).__name__}: {exc}", file=sys.stderr)
                attempt += 1
                await asyncio.sleep(min(args.max_reconnect_sec, 2 ** min(attempt, 5)))
    finally:
        if writer_rows:
            await asyncio.to_thread(sink.write, writer_rows)
        await asyncio.to_thread(sink.close)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", action="append", help="Market slug; repeat to record explicit markets")
    parser.add_argument("--lookahead-markets", type=int, default=2)
    parser.add_argument("--websocket", default=DEFAULT_WS_URL)
    parser.add_argument("--output", default="data/polymarket_l2")
    parser.add_argument("--market-refresh-sec", type=float, default=30)
    parser.add_argument("--flush-rows", type=int, default=1000)
    parser.add_argument("--max-reconnect-sec", type=int, default=30)
    args = parser.parse_args(argv)
    try:
        asyncio.run(run_recorder(args))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"L2 recorder stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
