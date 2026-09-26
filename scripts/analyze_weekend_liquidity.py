#!/usr/bin/env python3
"""Evidence-first weekday/weekend analysis for BTC 15-minute Polymarket markets."""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.analytics.weekend_liquidity import (
    classify_weekend, compare_samples, compute_liquidity_metrics, deduplicate_fills,
    load_public_cache, market_seconds_to_resolution, parse_timestamp, percentile,
    price_slippage, public_data_source_label, resolution_time_bin, save_public_cache,
    summarize_pnl,
)
from bot.polymarket_data_api import v2_next_cursor, v2_rows

GAMMA = "https://gamma-api.polymarket.com"
DATA_V2 = "https://data-api.polymarket.com/v2"
CLOB = "https://clob.polymarket.com"
ORDER_TYPES = {
    "ORDER_MAKER_INTENT", "ORDER_FAST_FOLLOW_INTENT", "ORDER_SUBMIT",
    "ORDER_FAST_FOLLOW_SUBMIT", "ORDER_TAKER_EXIT_SUBMIT",
    "ORDER_RECOVERY_EXIT_PASSIVE_SUBMIT", "ORDER_FILLED", "ORDER_CANCELED",
    "ORDER_REJECTED", "ORDER_DENIED", "ORDER_CANCEL_REJECTED",
    "ORDER_CANCEL_RECONCILED", "FILL_MARKOUT", "ENTRY_EDGE_OBSERVATION",
    "DEPTH_RISK_SHADOW_CANDIDATE", "DEPTH_RISK_SHADOW_MARKOUT",
}
STRATEGY_TYPES = {
    "QUOTE_TRANSPORT_TELEMETRY", "ENTRY_DECISION_TRACE",
    "ENTRY_CONFIRMATION_OBSERVATION", "EXIT_POLICY_DECISION",
    "STRATEGY_START", "STRATEGY_STOP",
}
SHADOW_TYPES = {"FILL_MARKOUT", "DEPTH_RISK_SHADOW_CANDIDATE", "DEPTH_RISK_SHADOW_MARKOUT"}
ORDER_LIFECYCLE_TYPES = {
    "ORDER_MAKER_INTENT", "ORDER_FAST_FOLLOW_INTENT", "ORDER_SUBMIT",
    "ORDER_FAST_FOLLOW_SUBMIT", "ORDER_TAKER_EXIT_SUBMIT",
    "ORDER_RECOVERY_EXIT_PASSIVE_SUBMIT", "ORDER_FILLED", "ORDER_CANCELED",
    "ORDER_REJECTED", "ORDER_DENIED", "ORDER_CANCEL_REJECTED",
    "ORDER_CANCEL_RECONCILED",
}


def _payload(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _market_weekend(slug: str, timezone_name: str) -> bool | None:
    try:
        return classify_weekend(int(str(slug).rsplit("-", 1)[-1]), timezone_name)
    except (TypeError, ValueError):
        return None


def _bounds(days: int, start: str | None, end: str | None) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    until = parse_timestamp(end) if end else now
    if end and len(end.strip()) == 10 and until is not None:
        until += timedelta(days=1, microseconds=-1)
    since = parse_timestamp(start) if start else until - timedelta(days=max(1, days))
    if since is None or until is None or since >= until:
        raise ValueError("date bounds must be valid ISO-8601 and start < end")
    return since, until


def _read_only_db(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _load_table(conn: sqlite3.Connection, table: str, since: datetime, until: datetime,
                kinds: set[str]) -> list[dict[str, Any]]:
    if not _table_exists(conn, table):
        return []
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if "ts" not in columns:
        return []
    where, params = ['ts >= ?', 'ts <= ?'], [since.isoformat(), until.isoformat()]
    if "event_type" in columns and kinds:
        where.append("event_type IN (" + ",".join("?" for _ in kinds) + ")")
        params.extend(sorted(kinds))
    result = []
    for raw in conn.execute(f"SELECT * FROM {table} WHERE {' AND '.join(where)} ORDER BY id", params):
        row = dict(raw)
        row["payload"] = _payload(row.get("payload_json"))
        row["timestamp"] = parse_timestamp(row.get("ts"))
        row["event_id"] = row.get("id")
        result.append(row)
    return result


def _slug(row: dict[str, Any]) -> str:
    data = row.get("payload") or {}
    return str(data.get("slug") or data.get("market_slug") or row.get("market_slug") or "").strip()


def _order_key(row: dict[str, Any]) -> tuple[str, str]:
    client_id = str(row.get("client_order_id") or "").strip()
    return str(row.get("run_id") or ""), client_id or f"event:{row.get('event_id')}"


def load_local_journal(db_path: str, *, days: int = 90, start: str | None = None,
                       end: str | None = None, timezone_name: str = "America/New_York",
                       market_prefix: str = "btc-updown-15m-") -> dict[str, Any]:
    """Load only the canonical journal in read-only mode; backups are never consulted."""
    since, until = _bounds(days, start, end)
    conn = _read_only_db(db_path)
    try:
        order_rows = _load_table(conn, "order_events", since, until, ORDER_TYPES)
        strategy_rows = _load_table(conn, "strategy_events", since, until, STRATEGY_TYPES)
    finally:
        conn.close()
    order_rows = [row for row in order_rows if not _slug(row) or _slug(row).startswith(market_prefix)]
    strategy_rows = [row for row in strategy_rows if not _slug(row) or _slug(row).startswith(market_prefix)]
    quality = {
        "status": "ok" if order_rows or strategy_rows else "empty",
        "order_event_rows": len(order_rows), "strategy_event_rows": len(strategy_rows),
        "missing_timestamps": sum(r["timestamp"] is None for r in order_rows + strategy_rows),
        "future_timestamps": sum(bool(r["timestamp"] and r["timestamp"] > until) for r in order_rows + strategy_rows),
        "invalid_prices": 0, "nonpositive_sizes": 0, "missing_market_slug": 0,
        "shadow_rows_excluded": sum(r["event_type"] in SHADOW_TYPES for r in order_rows),
        "observed_utc_dates": len({
            r["timestamp"].date() for r in order_rows + strategy_rows if r.get("timestamp")
        }),
        "observed_start_utc": min(
            (r["timestamp"] for r in order_rows + strategy_rows if r.get("timestamp")),
            default=None,
        ),
        "observed_end_utc": max(
            (r["timestamp"] for r in order_rows + strategy_rows if r.get("timestamp")),
            default=None,
        ),
        "observed_market_count": len({_slug(r) for r in order_rows + strategy_rows if _slug(r)}),
    }
    by_order: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in order_rows:
        if row["event_type"] in ORDER_LIFECYCLE_TYPES:
            by_order[_order_key(row)].append(row)
    orders = []
    for key, events in by_order.items():
        source_event = next((event for name in (
            "ORDER_SUBMIT", "ORDER_FAST_FOLLOW_SUBMIT", "ORDER_MAKER_INTENT",
            "ORDER_FAST_FOLLOW_INTENT", "ORDER_TAKER_EXIT_SUBMIT",
            "ORDER_RECOVERY_EXIT_PASSIVE_SUBMIT",
        ) for event in events if event["event_type"] == name), events[0])
        data = source_event["payload"]
        fills = [event for event in events if event["event_type"] == "ORDER_FILLED"]
        quantity = sum(_number(event.get("qty")) or 0 for event in fills)
        notional = sum((_number(event.get("qty")) or 0) * (_number(event.get("price")) or 0) for event in fills)
        submitted = next((event for event in events if event["event_type"] in {"ORDER_SUBMIT", "ORDER_FAST_FOLLOW_SUBMIT"}), None)
        first_fill = min((event for event in fills if event.get("timestamp")), key=lambda e: e["timestamp"], default=None)
        canceled = next((event for event in reversed(events) if event["event_type"] == "ORDER_CANCELED"), None)
        side = str(source_event.get("side") or "").upper()
        entry_source = str(data.get("entry_source") or "")
        if not entry_source:
            if any(event["event_type"].startswith("ORDER_FAST_FOLLOW") for event in events):
                entry_source = "outcome_fast_follow"
            elif side == "BUY" and any(event["event_type"] == "ORDER_MAKER_INTENT" for event in events):
                entry_source = "normal_maker"
        actual_price = notional / quantity if quantity else None
        submitted_size = _number(source_event.get("qty"))
        pnl_values = [_number((event["payload"] or {}).get("realized_net_usdc")) for event in fills]
        pnl_values = [value for value in pnl_values if value is not None]
        order_slug = next((_slug(e) for e in events if _slug(e)), "")
        orders.append({
            "run_id": key[0], "client_order_id": key[1],
            "market_slug": order_slug,
            "weekend_et": _market_weekend(order_slug, timezone_name),
            "weekend_utc": _market_weekend(order_slug, "UTC"),
            "instrument_id": data.get("instrument_id") or source_event.get("instrument_id") or "",
            "side": side, "entry_source": entry_source or "unknown",
            "order_type": "fast_follow" if "fast_follow" in entry_source else ("maker" if data.get("maker") else "other"),
            "intended_price": _number(source_event.get("price")), "submitted_size": submitted_size,
            "filled_size": quantity, "fill_vwap": actual_price, "fill_count": len(fills),
            "was_submitted": submitted is not None, "was_filled": quantity > 0, "was_canceled": canceled is not None,
            "partial_fill": bool(quantity > 0 and submitted_size and quantity + 1e-9 < submitted_size),
            "fill_rate": min(1.0, quantity / submitted_size) if quantity and submitted_size else None,
            "entry_slippage": price_slippage("BUY", _number(source_event.get("price")), actual_price)
            if quantity and side == "BUY" else None,
            "exit_slippage": price_slippage("SELL", _number(source_event.get("price")), actual_price)
            if quantity and side == "SELL" else None,
            "submit_to_fill_sec": (
                (first_fill["timestamp"] - submitted["timestamp"]).total_seconds()
                if first_fill and submitted and first_fill["timestamp"] and submitted["timestamp"] else None
            ),
            "cancel_latency_sec": (
                (canceled["timestamp"] - submitted["timestamp"]).total_seconds()
                if canceled and submitted and canceled["timestamp"] and submitted["timestamp"] else None
            ),
            "first_event_ts": min((e["timestamp"] for e in events if e["timestamp"]), default=None),
            "first_fill_ts": first_fill["timestamp"] if first_fill else None,
            "exit_reason": data.get("exit_reason") or source_event.get("reason"),
            "realized_pnl": sum(pnl_values) if pnl_values else None, "events": events,
        })
    order_index = {key: order for key, order in zip(by_order, orders)}
    fills = []
    for row in order_rows:
        if row["event_type"] != "ORDER_FILLED":
            continue
        payload = row["payload"]
        order = order_index.get(_order_key(row), {})
        if _number(row.get("price")) is None:
            quality["invalid_prices"] += 1
        if (_number(row.get("qty")) or 0) <= 0:
            quality["nonpositive_sizes"] += 1
        market_slug = _slug(row) or order.get("market_slug", "")
        if not market_slug:
            quality["missing_market_slug"] += 1
        fills.append({
            "event_id": row["event_id"], "fill_id": payload.get("fill_id"), "run_id": row.get("run_id"),
            "client_order_id": row.get("client_order_id"), "market_slug": market_slug,
            "instrument_id": payload.get("instrument_id") or row.get("instrument_id") or order.get("instrument_id", ""),
            "side": str(row.get("side") or "").upper(), "price": _number(row.get("price")),
            "qty": _number(row.get("qty")), "pnl": _number(payload.get("realized_net_usdc")),
            "timestamp": row["timestamp"], "entry_source": order.get("entry_source", "unknown"),
            "intended_price": order.get("intended_price"),
            "exit_reason": payload.get("exit_reason") or order.get("exit_reason"),
            "payload": payload, "weekend_et": _market_weekend(market_slug, timezone_name),
            "weekend_utc": _market_weekend(market_slug, "UTC"),
        })
    raw_fill_count = len(fills)
    fills = deduplicate_fills(fills)
    quality["duplicate_fill_rows_removed"] = raw_fill_count - len(fills)
    quality["sell_fill_pnl_missing"] = sum(row["side"] == "SELL" and row["pnl"] is None for row in fills)
    quality["sell_fill_pnl_present"] = sum(row["side"] == "SELL" and row["pnl"] is not None for row in fills)
    lots: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
    trades = []
    for fill in sorted(fills, key=lambda r: r["timestamp"] or datetime.min.replace(tzinfo=timezone.utc)):
        identity = (str(fill.get("run_id") or ""), str(fill.get("instrument_id") or ""))
        if fill["side"] == "BUY":
            buy_order = order_index.get((str(fill.get("run_id") or ""),
                                         str(fill.get("client_order_id") or "")), {})
            lots[identity].append({"qty": fill.get("qty") or 0, "timestamp": fill["timestamp"],
                                  "entry_price": fill.get("price"), "market_slug": fill["market_slug"],
                                  "entry_source": fill["entry_source"], "weekend_et": fill["weekend_et"],
                                  "weekend_utc": fill["weekend_utc"],
                                  "intended_price": buy_order.get("intended_price")})
            continue
        if fill["side"] != "SELL":
            continue
        remaining = fill.get("qty") or 0
        while remaining > 1e-9 and lots[identity]:
            lot = lots[identity][0]
            matched = min(remaining, lot["qty"])
            lot["qty"] -= matched
            remaining -= matched
            trades.append({
                "market_slug": lot["market_slug"] or fill["market_slug"], "instrument_id": identity[1],
                "run_id": fill["run_id"], "entry_source": lot["entry_source"],
                "entry_price": lot["entry_price"], "exit_price": fill["price"], "qty": matched,
                "hold_sec": (fill["timestamp"] - lot["timestamp"]).total_seconds()
                if fill["timestamp"] and lot["timestamp"] else None,
                "pnl": fill["pnl"] * matched / fill["qty"] if fill["pnl"] is not None and fill["qty"] else None,
                "exit_reason": fill["exit_reason"], "weekend_et": lot["weekend_et"],
                "weekend_utc": lot["weekend_utc"],
                "entry_slippage": price_slippage("BUY", lot.get("intended_price"), lot["entry_price"]),
                "exit_slippage": price_slippage("SELL", fill["intended_price"], fill["price"]),
                "time_to_resolution_sec": market_seconds_to_resolution(lot["market_slug"], lot["timestamp"])
                if lot["market_slug"] and lot["timestamp"] else None,
            })
            if lot["qty"] <= 1e-9:
                lots[identity].popleft()
        if remaining > 1e-9:
            trades.append({
                "market_slug": fill["market_slug"], "instrument_id": identity[1],
                "run_id": fill["run_id"], "entry_source": "unmatched_inventory_history",
                "entry_price": None, "exit_price": fill["price"], "qty": remaining,
                "hold_sec": None,
                "pnl": fill["pnl"] * remaining / fill["qty"]
                if fill["pnl"] is not None and fill["qty"] else None,
                "exit_reason": fill["exit_reason"], "weekend_et": fill["weekend_et"],
                "weekend_utc": fill["weekend_utc"],
                "entry_slippage": None,
                "exit_slippage": price_slippage("SELL", fill["intended_price"], fill["price"]),
                "time_to_resolution_sec": None,
            })

    markets: dict[str, dict[str, Any]] = {}
    for row in order_rows + strategy_rows:
        market_slug = _slug(row)
        if not market_slug:
            continue
        try:
            market_start_ts = int(market_slug.rsplit("-", 1)[-1])
        except ValueError:
            market_start_ts = None
        market = markets.setdefault(market_slug, {
            "market_slug": market_slug,
            "weekend_et": classify_weekend(market_start_ts, timezone_name) if market_start_ts else None,
            "weekend_utc": classify_weekend(market_start_ts, "UTC") if market_start_ts else None,
        })
        kind = row["event_type"]
        if kind == "ENTRY_EDGE_OBSERVATION":
            market.setdefault("_edges", []).append(row["payload"])
        elif kind == "QUOTE_TRANSPORT_TELEMETRY":
            market.setdefault("_quotes", []).append(row["payload"])
            if row.get("timestamp"):
                market.setdefault("_quote_timestamps", []).append(row["timestamp"])
        elif kind == "FILL_MARKOUT":
            market.setdefault("_markouts", []).append(row["payload"])
    for market in markets.values():
        edges, quotes, marks = market.pop("_edges", []), market.pop("_quotes", []), market.pop("_markouts", [])
        quote_timestamps = market.pop("_quote_timestamps", [])
        spread = [_number(item.get("spread_ps")) for item in edges]
        age = [_number(item.get("quote_age_sec")) for item in edges]
        depth = [_number(item.get("entry_bid_depth")) for item in marks] + [
            _number(item.get("entry_ask_depth")) for item in marks
        ]
        elapsed = (max(quote_timestamps) - min(quote_timestamps)).total_seconds() if len(quote_timestamps) > 1 else 0
        market.update({
            "entry_edge_samples": len(edges), "local_quote_samples": len(quotes),
            "fill_markout_samples": len(marks),
            "spread": percentile([x for x in spread if x is not None], .5),
            "quote_age_sec": percentile([x for x in age if x is not None], .5),
            "depth_1c": None, "depth_2c": None,
            "depth_5c": percentile([x for x in depth if x is not None], .5),
            "top_bid_size_median": percentile([x for x in (_number(q.get("bid_size")) for q in quotes) if x is not None], .5),
            "top_ask_size_median": percentile([x for x in (_number(q.get("ask_size")) for q in quotes) if x is not None], .5),
            "quote_update_rate": (len(quotes) - 1) / elapsed if len(quotes) > 1 and elapsed > 0 else None,
            "depth_source": "LOCAL_RECORDED, fill-conditioned marks; not historical full L2",
        })
    return {
        "start": since, "end": until, "orders": orders, "fills": fills, "trades": trades,
        "markets": compute_liquidity_metrics(list(markets.values())),
        "order_events": order_rows, "strategy_events": strategy_rows, "quality": quality,
    }


def _http_json(url: str, params: dict[str, Any] | None = None) -> Any:
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    request = Request(url, headers={"User-Agent": "PolymarketWeekendLiquidityResearch/1.0"})
    for attempt in range(4):
        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise
            try:
                delay = min(30.0, float(exc.headers.get("Retry-After", 2 ** attempt)))
            except (TypeError, ValueError):
                delay = float(2 ** attempt)
            time.sleep(delay)
        except (URLError, TimeoutError):
            if attempt == 3:
                raise
            time.sleep(min(8.0, 2 ** attempt))
    raise RuntimeError("public API retry budget exhausted")


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, list) else []
        except ValueError:
            return []
    return []


def fetch_market_public_history(slug: str, *, cache_dir: str, start_ts: int, end_ts: int,
                                force_refresh: bool = False) -> tuple[dict[str, Any] | None, str | None]:
    if not force_refresh and (cached := load_public_cache(cache_dir, slug)) is not None:
        return cached, None
    try:
        response = _http_json(f"{GAMMA}/markets", {"slug": slug, "limit": 5})
        market = response[0] if isinstance(response, list) and response else (
            response if isinstance(response, dict) else None
        )
        if not market:
            return None, "Gamma returned no matching market"
        condition = market.get("conditionId") or market.get("condition_id")
        token_ids = _list(market.get("clobTokenIds") or market.get("clob_token_ids"))
        if not condition or len(token_ids) < 2:
            return None, "Gamma metadata missing condition or outcome tokens"
        trades, cursor = [], None
        for _ in range(100):
            params: dict[str, Any] = {"condition": condition, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            page = _http_json(f"{DATA_V2}/trades", params)
            rows = v2_rows(page)
            for row in rows:
                try:
                    ts = int(row.get("timestamp") or 0)
                except (ValueError, TypeError):
                    continue
                if start_ts <= ts <= end_ts:
                    trades.append(row)
            cursor = v2_next_cursor(page)
            if not cursor or not rows:
                break
        price_history = {}
        for token in token_ids[:2]:
            response = _http_json(f"{CLOB}/prices-history", {
                "market": token, "startTs": start_ts, "endTs": end_ts, "fidelity": 1,
            })
            price_history[str(token)] = response.get("history", []) if isinstance(response, dict) else []
        result = {"source": public_data_source_label("api"), "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                  "market": market, "trades": trades, "price_history": price_history}
        save_public_cache(cache_dir, slug, result)
        return result, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _public_market_metrics(slug: str, data: dict[str, Any], timezone_name: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    start, end = int(slug.rsplit("-", 1)[-1]), int(slug.rsplit("-", 1)[-1]) + 900
    trades = []
    for row in data.get("trades", []):
        ts, size, price = _number(row.get("timestamp")), _number(row.get("size")), _number(row.get("price"))
        if ts is None or size is None or size <= 0 or price is None or not 0 <= price <= 1:
            continue
        left = max(0.0, end - ts)
        trades.append({
            "market_slug": slug, "timestamp": ts, "size": size, "price": price,
            "side": row.get("side"), "token_id": row.get("asset") or row.get("token_id"),
            "seconds_to_resolution": left, "resolution_bin": resolution_time_bin(left),
            "weekend_et": classify_weekend(start, timezone_name),
            "weekend_utc": classify_weekend(start, "UTC"),
            "public_source": public_data_source_label("public"),
        })
    trades.sort(key=lambda row: row["timestamp"])
    sizes = [row["size"] for row in trades]
    gaps = [trades[0]["timestamp"] - start] if trades else []
    gaps.extend(b["timestamp"] - a["timestamp"] for a, b in zip(trades, trades[1:]))
    if trades:
        gaps.append(end - trades[-1]["timestamp"])
    price_deltas = []
    history_count = 0
    for history in (data.get("price_history") or {}).values():
        token_points = []
        for point in history if isinstance(history, list) else []:
            if isinstance(point, dict) and _number(point.get("p")) is not None:
                token_points.append((_number(point.get("t")) or 0, float(point["p"])))
        token_points.sort()
        history_count += len(token_points)
        price_deltas.extend(b[1] - a[1] for a, b in zip(token_points, token_points[1:]))
    return ({
        "market_slug": slug, "weekend_et": classify_weekend(start, timezone_name),
        "weekend_utc": classify_weekend(start, "UTC"), "public_trade_count": len(trades),
        "public_volume_shares": sum(sizes),
        "public_volume_usdc_estimate": sum(row["size"] * row["price"] for row in trades),
        "median_trade_size": percentile(sizes, .5), "p10_trade_size": percentile(sizes, .1),
        "p90_trade_size": percentile(sizes, .9),
        "max_no_trade_interval_sec": max(gaps) if gaps else 900,
        "trade_frequency_per_sec": len(trades) / 900,
        "price_history_points": history_count,
        "realized_volatility": math.sqrt(sum(delta * delta for delta in price_deltas)) if price_deltas else None,
        "price_jump_count_gt_5c": sum(abs(delta) >= .05 for delta in price_deltas),
        "historical_spread": None, "historical_l2_depth": None,
        "price_history_source": public_data_source_label("public"),
    }, trades)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys or ["empty"], extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: value.isoformat() if isinstance(value, datetime) else
                             json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
                             for key, value in row.items()})


def _comparison_rows(markets: list[dict[str, Any]], trades: list[dict[str, Any]],
                     orders: list[dict[str, Any]], *,
                     weekend_field: str = "weekend_et", timezone_label: str = "America/New_York") -> list[dict[str, Any]]:
    metrics = [
        ("public_trade_count", markets), ("public_volume_shares", markets),
        ("median_trade_size", markets), ("max_no_trade_interval_sec", markets),
        ("realized_volatility", markets), ("spread", markets), ("quote_age_sec", markets),
        ("depth_5c", markets), ("entry_slippage", orders), ("exit_slippage", orders),
        ("fill_rate", orders),
        ("submit_to_fill_sec", orders), ("pnl", trades), ("hold_sec", trades),
    ]
    output = []
    for name, rows in metrics:
        weekday, weekend = [], []
        for row in rows:
            value = _number(row.get(name))
            if value is None:
                continue
            if row.get(weekend_field) is True:
                weekend.append(value)
            elif row.get(weekend_field) is False:
                weekday.append(value)
        stats = compare_samples(weekday, weekend)
        wm, em = stats.get("weekday_mean"), stats.get("weekend_mean")
        output.append({
            "metric": name, "timezone": timezone_label, "weekday_n": len(weekday), "weekend_n": len(weekend),
            "weekday_mean": wm, "weekend_mean": em, "difference": stats.get("mean_difference"),
            "relative_difference_pct": (em - wm) / abs(wm) * 100 if wm not in (None, 0) and em is not None else None,
            "bootstrap_ci95": stats.get("mean_difference_ci95"), "permutation_p": stats.get("permutation_p"),
            "cliffs_delta": stats.get("cliffs_delta"), "weekday_median": stats.get("weekday_median"),
            "weekend_median": stats.get("weekend_median"), "sample_warning": stats.get("sample_warning"),
        })
    order_metrics = (
        ("order_fill_probability", lambda row: 1.0 if row.get("was_filled") else 0.0),
        ("partial_fill_probability", lambda row: 1.0 if row.get("partial_fill") else 0.0),
        ("cancel_probability", lambda row: 1.0 if row.get("was_canceled") else 0.0),
        ("cancel_latency_sec", lambda row: row.get("cancel_latency_sec")),
    )
    for name, select_value in order_metrics:
        samples = {False: [], True: []}
        for order in orders:
            if not order.get("was_submitted") or order.get(weekend_field) not in samples:
                continue
            value = _number(select_value(order))
            if value is not None:
                samples[order[weekend_field]].append(value)
        stats = compare_samples(samples[False], samples[True])
        output.append({
            "metric": name, "timezone": timezone_label,
            "weekday_n": len(samples[False]), "weekend_n": len(samples[True]),
            "weekday_mean": stats.get("weekday_mean"), "weekend_mean": stats.get("weekend_mean"),
            "difference": stats.get("mean_difference"),
            "bootstrap_ci95": stats.get("mean_difference_ci95"),
            "permutation_p": stats.get("permutation_p"), "cliffs_delta": stats.get("cliffs_delta"),
            "sample_warning": stats.get("sample_warning"),
        })
    return output


def _stratified_rows(markets: list[dict[str, Any]], trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scores = [float(row["liquidity_score"]) for row in markets if _number(row.get("liquidity_score")) is not None]
    if not scores:
        return []
    low, high = percentile(scores, 1 / 3), percentile(scores, 2 / 3)
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_market[trade["market_slug"]].append(trade)
    result = []
    for market in markets:
        score = _number(market.get("liquidity_score"))
        if score is None:
            continue
        regime = "low" if score <= low else ("high" if score >= high else "medium")
        summary = summarize_pnl(by_market.get(market["market_slug"], []))
        result.append({
            "market_slug": market["market_slug"], "weekend_et": market.get("weekend_et"),
            "liquidity_score": score, "liquidity_regime": regime,
            "closed_trade_count": summary["sample_size"], "realized_pnl": summary["total_pnl"],
            "pnl_source": "LOCAL_RECORDED sell fills; settlement PnL may be absent",
        })
    return result


def _parameter_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values: dict[str, dict[bool | None, list[str]]] = defaultdict(lambda: defaultdict(list))
    for event in events:
        if event["event_type"] != "STRATEGY_START":
            continue
        payload = event["payload"]
        slug = str(payload.get("selected_slug") or "")
        try:
            ts = int(slug.rsplit("-", 1)[-1])
        except ValueError:
            ts = event["timestamp"].timestamp() if event.get("timestamp") else 0
        weekend = classify_weekend(ts, "America/New_York") if ts else None
        for key, value in payload.items():
            if any(word in key.lower() for word in ("threshold", "spread", "depth", "size", "cutoff",
                                                      "timeout", "stop_loss", "slippage", "expected_net")):
                values[key][weekend].append(str(value))
    return [{
        "parameter": key,
        "weekday_values": ",".join(sorted(set(by_day[False]))),
        "weekend_values": ",".join(sorted(set(by_day[True]))),
        "weekday_runs": len(by_day[False]), "weekend_runs": len(by_day[True]),
        "counterfactual_status": "unsupported; observed config values only",
    } for key, by_day in sorted(values.items())]


def _time_rows(public_trades: list[dict[str, Any]], events: list[dict[str, Any]],
               timezone_name: str = "America/New_York") -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[tuple[float, str]]] = defaultdict(list)
    for row in public_trades:
        groups[(str(row.get("weekend_et")), row["resolution_bin"])].append((row["size"], "public_trade_size"))
    for row in events:
        payload = row["payload"]
        left = _number(payload.get("time_left_sec"))
        spread = _number(payload.get("spread_ps"))
        if left is not None and spread is not None:
            weekend = classify_weekend(row["timestamp"], timezone_name) if row.get("timestamp") else None
            groups[(str(weekend), resolution_time_bin(left))].append((spread, "local_spread"))
    return [{
        "weekend_et": key[0], "resolution_bin": key[1], "observations": len(vals),
        "mean_public_trade_size": (sum(v for v, src in vals if src == "public_trade_size") /
                                   sum(src == "public_trade_size" for _, src in vals)
                                   if any(src == "public_trade_size" for _, src in vals) else None),
        "mean_local_spread": (sum(v for v, src in vals if src == "local_spread") /
                              sum(src == "local_spread" for _, src in vals)
                              if any(src == "local_spread" for _, src in vals) else None),
    } for key, vals in sorted(groups.items())]


def _summary_md(local: dict[str, Any], markets: list[dict[str, Any]], public_markets: list[dict[str, Any]],
                public_trades: list[dict[str, Any]], comparisons: list[dict[str, Any]], warnings: list[dict[str, Any]],
                timezone_name: str) -> str:
    def fmt(value: Any) -> str:
        return "n/a" if value is None else f"{float(value):.4f}"
    weekday = summarize_pnl([row for row in local["trades"] if row.get("weekend_et") is False])
    weekend = summarize_pnl([row for row in local["trades"] if row.get("weekend_et") is True])
    stop_loss = summarize_pnl([
        row for row in local["trades"] if "stop_loss" in str(row.get("exit_reason") or "").lower()
    ])
    emergency = summarize_pnl([
        row for row in local["trades"] if "emergency" in str(row.get("exit_reason") or "").lower()
    ])
    et_weekend_markets = sum(row.get("weekend_et") is True for row in markets)
    utc_weekend_markets = sum(row.get("weekend_utc") is True for row in markets)
    cmp = {row["metric"]: row for row in comparisons}
    lines = [
        "# 週末 vs 平日：BTC 15 分鐘市場流動性、執行與 PnL", "",
        "## Dataset", "",
        f"- Canonical journal：{local['quality']['order_event_rows']:,} order rows、{local['quality']['strategy_event_rows']:,} strategy rows；{local['quality']['status']}。",
        f"- Local markets：{len(markets)}；public markets：{len(public_markets)}；public trades：{len(public_trades):,}。",
        f"- Journal 實際記錄跨 {local['quality']['observed_utc_dates']} 個 UTC 日期、"
        f"{local['quality']['observed_market_count']} 個 slug；目前市場週末數 ET={et_weekend_markets}、UTC={utc_weekend_markets}。",
        f"- 查詢 window：{local['start'].isoformat()} — {local['end'].isoformat()}。",
        f"- Journal 實際事件範圍：{local['quality']['observed_start_utc'].isoformat() if local['quality']['observed_start_utc'] else 'n/a'} — {local['quality']['observed_end_utc'].isoformat() if local['quality']['observed_end_utc'] else 'n/a'}；市場週末時區：{timezone_name}。",
        f"- SELL fill missing PnL：{local['quality']['sell_fill_pnl_missing']}；excluded shadow rows：{local['quality']['shadow_rows_excluded']}。", "",
        "## Weekday vs weekend", "",
        "| Metric | weekday n / mean | weekend n / mean | difference | 95% bootstrap CI | permutation p |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for metric in ("public_trade_count", "public_volume_shares", "median_trade_size", "max_no_trade_interval_sec",
                   "realized_volatility", "spread", "quote_age_sec", "depth_5c", "entry_slippage",
                   "exit_slippage", "fill_rate", "order_fill_probability", "partial_fill_probability",
                   "cancel_probability", "cancel_latency_sec", "submit_to_fill_sec", "pnl", "hold_sec"):
        row = cmp[metric]
        ci = row["bootstrap_ci95"]
        ci_text = f"[{fmt(ci[0])}, {fmt(ci[1])}]" if ci else "n/a"
        lines.append(f"| {metric} | {row['weekday_n']} / {fmt(row['weekday_mean'])} | {row['weekend_n']} / {fmt(row['weekend_mean'])} | {fmt(row['difference'])} | {ci_text} | {fmt(row['permutation_p'])} |")
    lines.extend([
        "", "## PnL and left-tail", "",
        f"- Weekday paired sell trades n={weekday['sample_size']}; total={fmt(weekday['total_pnl'])} USDC; win={fmt(weekday['win_rate'])}; P10/P5/P1={fmt(weekday['pnl_p10'])}/{fmt(weekday['pnl_p5'])}/{fmt(weekday['pnl_p1'])}.",
        f"- Weekend paired sell trades n={weekend['sample_size']}; total={fmt(weekend['total_pnl'])} USDC; win={fmt(weekend['win_rate'])}; P10/P5/P1={fmt(weekend['pnl_p10'])}/{fmt(weekend['pnl_p5'])}/{fmt(weekend['pnl_p1'])}.",
        f"- Weekday profit factor / max drawdown / average win / average loss: {fmt(weekday['profit_factor'])} / {fmt(weekday['max_drawdown'])} / {fmt(weekday['average_win'])} / {fmt(weekday['average_loss'])}; worst={fmt(weekday['worst_trade'])}.",
        f"- Weekend profit factor / max drawdown / average win / average loss: {fmt(weekend['profit_factor'])} / {fmt(weekend['max_drawdown'])} / {fmt(weekend['average_win'])} / {fmt(weekend['average_loss'])}; worst={fmt(weekend['worst_trade'])}.",
        f"- Stop-loss exits n={stop_loss['sample_size']}, total PnL={fmt(stop_loss['total_pnl'])}, P1={fmt(stop_loss['pnl_p1'])}; emergency exits n={emergency['sample_size']}, total PnL={fmt(emergency['total_pnl'])}, P1={fmt(emergency['pnl_p1'])}.",
        "- PnL uses journaled realized_net_usdc on SELL fills, FIFO-paired to observed BUY lots. Missing PnL and settlement/redemption outcomes are not imputed.", "",
        "## Interpretation and limitations", "",
        "- Weekend is defined by America/New_York Saturday/Sunday; UTC sensitivity is included per market in market_level.csv.",
        "- Statistical differences are associations, not causal evidence; markets are the resampling/comparison units for public liquidity. Empty weekday/weekend groups mean no difference test is available.",
        "- weekday_vs_weekend_utc.csv repeats comparisons with UTC Saturday/Sunday to show timezone sensitivity; primary grouping is the requested ET.",
        "- Compare time_to_resolution.csv for settlement windows. Public trade size and local spread are separate metrics.",
        "- Historical L2 is not available in this journal. Historical price/trade feeds do not reconstruct BBO/depth or fill probability; unknown fields stay empty.",
        "- Local depth observations come only from fill-conditioned markout payloads, so they are selected observations, not an unbiased market-time sample.",
        "- Parameter sensitivity reports observed STRATEGY_START config values only; no unsupported counterfactual replay or live parameter recommendation.",
        f"- Public API warnings/not-fetched markets: {len(warnings)}. Cache lives in data/polymarket_history/.", "",
        "## Outputs", "",
        "- market_level.csv", "- trade_level.csv", "- execution_level.csv", "- weekday_vs_weekend.csv",
        "- weekday_vs_weekend_utc.csv",
        "- time_to_resolution.csv", "- parameter_sensitivity.csv", "- liquidity_regime.csv", "",
    ])
    return "\n".join(lines)


def generate_report(*, db_path: str, output_dir: str, days: int = 90, timezone_name: str = "America/New_York",
                    start: str | None = None, end: str | None = None, cache_dir: str = "data/polymarket_history",
                    offline: bool = False, refresh_public_data: bool = False,
                    max_public_markets: int = 500,
                    market_prefix: str = "btc-updown-15m-") -> dict[str, Any]:
    local = load_local_journal(
        db_path, days=days, start=start, end=end, timezone_name=timezone_name,
        market_prefix=market_prefix,
    )
    slugs = sorted({r["market_slug"] for r in local["markets"]
                    if r.get("market_slug", "").startswith(market_prefix)})
    public_data, warnings = {}, []
    low, high = int(local["start"].timestamp()), int(local["end"].timestamp())
    for index, slug in enumerate(slugs):
        cached = None if refresh_public_data else load_public_cache(cache_dir, slug)
        if cached is not None:
            public_data[slug] = cached
        elif not offline and index < max_public_markets:
            result, error = fetch_market_public_history(slug, cache_dir=cache_dir, start_ts=low, end_ts=high,
                                                        force_refresh=refresh_public_data)
            if result:
                public_data[slug] = result
            if error:
                warnings.append({"market_slug": slug, "error": error})
            time.sleep(.05)
        else:
            warnings.append({"market_slug": slug, "error": "not fetched (offline or public-market cap)"})
    public_markets, public_trades = [], []
    for slug, data in public_data.items():
        market, trades = _public_market_metrics(slug, data, timezone_name)
        public_markets.append(market)
        public_trades.extend(trades)
    by_slug = {row["market_slug"]: row for row in public_markets}
    markets = []
    for slug in sorted(set(r["market_slug"] for r in local["markets"]) | set(by_slug)):
        row = next((dict(r) for r in local["markets"] if r["market_slug"] == slug), {"market_slug": slug})
        row.update(by_slug.get(slug, {}))
        markets.append(row)
    orders_by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    events_by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for order in local["orders"]:
        if order.get("market_slug"):
            orders_by_market[order["market_slug"]].append(order)
    for event in local["order_events"] + local["strategy_events"]:
        market_slug = _slug(event)
        if market_slug:
            events_by_market[market_slug].append(event)
    for market in markets:
        orders_here = orders_by_market.get(market["market_slug"], [])
        events_here = events_by_market.get(market["market_slug"], [])
        market.update({
            "signal_observations": sum(
                event["event_type"] in {"ENTRY_DECISION_TRACE", "ENTRY_EDGE_OBSERVATION"}
                for event in events_here
            ),
            "entry_attempts": sum(
                order["side"] == "BUY" and any(
                    event["event_type"] in {"ORDER_MAKER_INTENT", "ORDER_FAST_FOLLOW_INTENT"}
                    for event in order["events"]
                ) for order in orders_here
            ),
            "submitted_orders": sum(order["was_submitted"] for order in orders_here),
            "filled_orders": sum(order["was_filled"] for order in orders_here),
            "cancelled_orders": sum(order["was_canceled"] for order in orders_here),
            "stop_loss_exits": sum(
                "stop_loss" in str(order.get("exit_reason") or "").lower()
                for order in orders_here if order["side"] == "SELL"
            ),
            "emergency_exits": sum(
                "emergency" in str(order.get("exit_reason") or "").lower()
                for order in orders_here if order["side"] == "SELL"
            ),
        })
    markets = compute_liquidity_metrics(markets)
    comparisons = _comparison_rows(markets, local["trades"], local["orders"])
    utc_comparisons = _comparison_rows(
        markets, local["trades"], local["orders"],
        weekend_field="weekend_utc", timezone_label="UTC",
    )
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    market_rows = []
    for row in markets:
        market_rows.append(row)
    trade_rows = [{k: v.isoformat() if isinstance(v, datetime) else v for k, v in row.items()} for row in local["trades"]]
    execution_rows = [{k: v for k, v in row.items() if k != "events"} for row in local["orders"]]
    _write_csv(out / "market_level.csv", market_rows)
    _write_csv(out / "trade_level.csv", trade_rows)
    _write_csv(out / "execution_level.csv", execution_rows)
    _write_csv(out / "weekday_vs_weekend.csv", comparisons)
    _write_csv(out / "weekday_vs_weekend_utc.csv", utc_comparisons)
    _write_csv(out / "time_to_resolution.csv", _time_rows(
        public_trades, [r for r in local["order_events"] if r["event_type"] == "ENTRY_EDGE_OBSERVATION"],
        timezone_name,
    ))
    _write_csv(out / "parameter_sensitivity.csv", _parameter_rows(local["strategy_events"]))
    _write_csv(out / "liquidity_regime.csv", _stratified_rows(markets, local["trades"]))
    (out / "summary.md").write_text(
        _summary_md(local, markets, public_markets, public_trades, comparisons, warnings, timezone_name),
        encoding="utf-8",
    )
    return {"local": local, "markets": markets, "public_markets": public_markets,
            "public_trades": public_trades, "comparisons": comparisons, "warnings": warnings,
            "output_dir": str(out)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="logs/trade_journal.db")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--start", help="ISO-8601 date/time inclusive")
    parser.add_argument("--end", help="ISO-8601 date/time inclusive")
    parser.add_argument("--timezone", default="America/New_York")
    parser.add_argument("--market-prefix", default="btc-updown-15m-")
    parser.add_argument("--offline", action="store_true", help="Only local DB plus already cached public data")
    parser.add_argument("--refresh-public-data", action="store_true", help="Force public API cache refresh")
    parser.add_argument("--output", default="reports/weekend_liquidity")
    parser.add_argument("--cache-dir", default="data/polymarket_history")
    parser.add_argument("--max-public-markets", type=int, default=500)
    args = parser.parse_args(argv)
    try:
        result = generate_report(
            db_path=args.db, output_dir=args.output, days=args.days, timezone_name=args.timezone,
            start=args.start, end=args.end, cache_dir=args.cache_dir, offline=args.offline,
            refresh_public_data=args.refresh_public_data, max_public_markets=args.max_public_markets,
            market_prefix=args.market_prefix,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"analysis failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"Analysis complete: markets={len(result['markets'])} public_markets={len(result['public_markets'])} "
          f"public_trades={len(result['public_trades'])} output={result['output_dir']}")
    if result["warnings"]:
        print(f"Public data warnings: {len(result['warnings'])}")
        for warning in result["warnings"][:10]:
            print(f"  {warning['market_slug']}: {warning['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
