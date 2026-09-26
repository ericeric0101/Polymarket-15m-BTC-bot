#!/usr/bin/env python3
"""Offline research backtest for a one-entry BTC trend / hold-to-settlement baseline.

This script is deliberately isolated from live trading. Public trade prints are
execution proxies, not historical asks or proof that a taker order was fillable.
BTC observations use Binance 1-minute candles; sub-minute signals are not
fabricated. Missing evidence stays missing rather than being imputed as zero.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from itertools import product
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_PUBLIC_CACHE = ROOT / "data/polymarket_history_unified_20260727_20260920"
FALLBACK_PUBLIC_CACHE = ROOT / "data/polymarket_history_public_study_20260727_20260920"
BINANCE = "https://api.binance.com/api/v3/klines"
SEGMENTS = ((900, 600, "T-15m_to_T-10m"), (600, 300, "T-10m_to_T-5m"),
            (300, 120, "T-5m_to_T-2m"), (120, 60, "T-2m_to_T-1m"),
            (60, 30, "T-60s_to_T-30s"), (30, 15, "T-30s_to_T-15s"),
            (15, 0, "T-15s_to_settlement"))


def market_start(slug: str) -> int | None:
    try:
        value = int(slug.rsplit("-", 1)[-1])
        return value if value > 0 else None
    except (ValueError, AttributeError):
        return None


def weekend_and_hour(epoch: int, timezone_name: str = "America/New_York") -> tuple[bool, str, str]:
    local = datetime.fromtimestamp(epoch, ZoneInfo(timezone_name))
    return local.weekday() >= 5, local.strftime("%Y-%m-%d"), f"{(local.hour // 6) * 6:02d}-{(local.hour // 6 + 1) * 6:02d}"


def winner_from_gamma(market: dict[str, Any]) -> str | None:
    """Return the resolved winning outcome only when Gamma prices are decisive."""
    if not market or not bool(market.get("closed")):
        return None
    try:
        outcomes = json.loads(market.get("outcomes", "[]")) if isinstance(market.get("outcomes"), str) else market.get("outcomes", [])
        prices = json.loads(market.get("outcomePrices", "[]")) if isinstance(market.get("outcomePrices"), str) else market.get("outcomePrices", [])
        values = {str(name).strip().lower(): float(price) for name, price in zip(outcomes, prices)}
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    for side in ("up", "down"):
        if values.get(side, 0.0) >= 0.99:
            other = "down" if side == "up" else "up"
            if values.get(other, 1.0) <= 0.01:
                return side.upper()
    return None


def threshold_side(return_bps: float | None, threshold_bps: float) -> str | None:
    if return_bps is None or return_bps == 0 or abs(return_bps) < threshold_bps:
        return None
    return "UP" if return_bps > 0 else "DOWN"


def outcome_pnl(side: str, winner: str, entry_price: float, notional: float = 10.0,
                fee_usdc: float = 0.0) -> dict[str, float]:
    if not 0 < entry_price < 1 or notional <= 0:
        raise ValueError("entry_price must be within (0,1) and notional positive")
    shares = notional / entry_price
    gross = shares - notional if side.upper() == winner.upper() else -notional
    return {"shares": shares, "gross_pnl": gross, "fee_usdc": fee_usdc,
            "net_pnl": gross - fee_usdc}


def entry_proxy(trades: list[dict[str, Any]], side: str, signal_ts: float, delay_sec: int,
                model: str, *, slippage_cents: float = 0.0) -> tuple[float | None, str]:
    """Price from subsequent public prints; not a guaranteed executable quote."""
    rows = []
    for row in trades:
        if str(row.get("outcome", "")).upper() != side.upper():
            continue
        try:
            ts, price, size = float(row["timestamp"]), float(row["price"]), float(row["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if ts >= signal_ts + delay_sec and 0 < price < 1 and size > 0:
            rows.append((ts, price, size))
    rows.sort()
    if not rows:
        return None, "no_fill_proxy"
    if model == "first_trade":
        price = rows[0][1]
    else:
        window = 5 if model == "vwap_5s" else 10
        fills = [r for r in rows if r[0] <= signal_ts + delay_sec + window]
        size_sum = sum(r[2] for r in fills)
        price = sum(r[1] * r[2] for r in fills) / size_sum if size_sum else None
    if price is None:
        return None, "no_fill_proxy"
    price = min(0.999999, price + slippage_cents)
    if price >= 1:
        return None, "unpriceable_after_slippage"
    return price, "proxy_trade_print"


def summarize_pnls(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered_rows = sorted(rows, key=lambda r:(r.get("local_date", ""), r.get("slug", "")))
    values = [float(r["net_pnl"]) for r in ordered_rows]
    if not values:
        return {"n": 0, "trades": 0, "win_rate": None, "mean_entry": None,
                "break_even_win_rate": None, "edge_per_share": None, "gross_pnl": 0.0,
                "net_pnl": 0.0, "roi": None, "profit_factor": None, "max_drawdown": None,
                "longest_losing_streak": 0, "average_win": None, "average_loss": None,
                "p10": None, "p5": None, "p1": None, "worst_trade": None}
    entries = [float(r["entry_price"]) for r in rows]
    wins = [float(r["net_pnl"]) for r in ordered_rows if (r.get("side") == r.get("winner") if r.get("side") and r.get("winner") else float(r["net_pnl"]) > 0)]
    losses = [v for v in values if v < 0]
    equity = peak = drawdown = 0.0
    streak = max_streak = 0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        streak = streak + 1 if value < 0 else 0
        max_streak = max(max_streak, streak)
    notional = sum(float(r.get("notional", 10.0)) for r in rows)
    ordered = sorted(values)
    def q(p: float) -> float:
        return ordered[min(len(ordered)-1, max(0, int((len(ordered)-1)*p)))]
    return {"n": len(rows), "trades": len(rows), "no_trades": None,
            "win_rate": len(wins)/len(rows), "mean_entry": statistics.mean(entries),
            "break_even_win_rate": statistics.mean(entries),
            "edge_per_share": sum((1.0 if r["side"] == r["winner"] else 0.0) - r["entry_price"] for r in rows)/len(rows),
            "gross_pnl": sum(float(r["gross_pnl"]) for r in rows), "fee_estimate": sum(float(r.get("fee_usdc", 0)) for r in rows),
            "net_pnl": sum(values), "pnl_per_trade": statistics.mean(values), "roi": sum(values)/notional if notional else None,
            "profit_factor": sum(wins)/abs(sum(losses)) if losses and wins else (float("inf") if wins else 0.0),
            "max_drawdown": drawdown, "longest_losing_streak": max_streak,
            "average_win": statistics.mean(wins) if wins else None,
            "average_loss": statistics.mean(losses) if losses else None,
            "p10": q(.10), "p5": q(.05), "p1": q(.01), "worst_trade": min(values)}


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=keys or ["status"])
        writer.writeheader()
        writer.writerows(rows)


def _json_cache(cache_dirs: list[Path]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for directory in cache_dirs:
        if not directory.exists():
            continue
        for path in directory.glob("btc-updown-15m-*.json"):
            try:
                result[path.name.rsplit("-", 1)[0]] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    return result


def _fetch_binance_day(day: date, *, timeout: float = 15.0) -> list[list[Any]]:
    start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
    end = start + 86_400_000
    rows: list[list[Any]] = []
    cursor = start
    while cursor < end:
        query = urllib.parse.urlencode({"symbol": "BTCUSDT", "interval": "1m", "startTime": cursor, "endTime": end - 1, "limit": 1000})
        request = urllib.request.Request(f"{BINANCE}?{query}", headers={"User-Agent": "historical-research/1.0"})
        error = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    batch = json.loads(response.read().decode("utf-8"))
                error = None
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                error = exc
                if attempt < 2:
                    time.sleep(.5 * (attempt + 1))
        if error is not None:
            raise error
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(batch)
        cursor = int(batch[-1][0]) + 60_000
        if len(batch) < 1000:
            break
        time.sleep(0.08)
    return rows


def load_btc_candles(start_epoch: int, end_epoch: int, cache_dir: Path, *, offline: bool = False,
                     refresh: bool = False) -> tuple[list[dict[str, float]], list[dict[str, Any]]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    first = datetime.fromtimestamp(start_epoch, timezone.utc).date()
    last = datetime.fromtimestamp(end_epoch, timezone.utc).date()
    candles: dict[int, dict[str, float]] = {}
    diagnostics = []
    day = first
    while day <= last:
        cache = cache_dir / f"binance_btcusdt_1m_{day.isoformat()}.json.gz"
        rows = None
        if cache.exists() and not refresh:
            try:
                with gzip.open(cache, "rt", encoding="utf-8") as stream:
                    rows = json.load(stream)
                status = "cache_hit"
            except (OSError, json.JSONDecodeError):
                rows = None
        if rows is None and offline:
            diagnostics.append({"date": day.isoformat(), "source": "Binance BTCUSDT 1m", "status": "missing_offline_cache", "rows": 0})
            day += timedelta(days=1)
            continue
        if rows is None:
            try:
                rows = _fetch_binance_day(day)
                temp = cache.with_suffix(cache.suffix + ".tmp")
                with gzip.open(temp, "wt", encoding="utf-8") as stream:
                    json.dump(rows, stream, separators=(",", ":"))
                temp.replace(cache)
                status = "fetched"
            except Exception as exc:
                diagnostics.append({"date": day.isoformat(), "source": "Binance BTCUSDT 1m", "status": "fetch_failed", "error_type": type(exc).__name__, "error": str(exc), "rows": 0})
                day += timedelta(days=1)
                continue
        for row in rows:
            try:
                candles[int(row[0]) // 1000] = {"open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]), "close_ts": int(row[6]) / 1000}
            except (IndexError, TypeError, ValueError):
                continue
        diagnostics.append({"date": day.isoformat(), "source": "Binance BTCUSDT 1m", "status": status, "rows": len(rows)})
        day += timedelta(days=1)
    return [{"ts": ts, **value} for ts, value in sorted(candles.items())], diagnostics


def _last_closed(candles: list[dict[str, float]], ts: float) -> dict[str, float] | None:
    # Candle close timestamp must be at or before the decision timestamp.
    eligible = [row for row in candles if row["close_ts"] <= ts]
    return eligible[-1] if eligible else None


def _returns_for_market(candles: list[dict[str, float]], start: int) -> dict[int, float | None]:
    opened = next((c for c in candles if c["ts"] == start), None)
    result: dict[int, float | None] = {}
    for seconds in (60, 120, 180, 240, 300):
        endpoint = _last_closed(candles, start + seconds)
        result[seconds] = ((endpoint["close"] / opened["open"] - 1) * 10_000) if opened and endpoint and opened["open"] else None
    return result


def _ema_side(candles: list[dict[str, float]], timestamp: float, fast_minutes: int = 1,
              slow_minutes: int = 3) -> str | None:
    """Minute-resolution EMA proxy. It is not the live 6s/20s EMA."""
    closes = [c["close"] for c in candles if c["close_ts"] <= timestamp]
    def ema(values: list[float], period: int) -> float | None:
        if len(values) < period:
            return None
        alpha = 2 / (period + 1)
        value = values[0]
        for item in values[1:]:
            value = alpha * item + (1 - alpha) * value
        return value
    fast, slow = ema(closes, fast_minutes), ema(closes, slow_minutes)
    if fast is None or slow is None or fast == slow:
        return None
    return "UP" if fast > slow else "DOWN"


def _return_side(candles: list[dict[str, float]], start: int, end_ts: int, lookback_sec: int) -> str | None:
    before = _last_closed(candles, end_ts - lookback_sec)
    after = _last_closed(candles, end_ts)
    if before is None or after is None or before["close"] <= 0:
        return None
    delta = (after["close"] / before["close"] - 1) * 10_000
    return threshold_side(delta, 0)


def _price_asof(trades: list[dict[str, Any]], side: str, ts: float) -> float | None:
    eligible = []
    for row in trades:
        try:
            if str(row.get("outcome", "")).upper() == side and float(row["timestamp"]) <= ts:
                p = float(row["price"])
                if 0 < p < 1:
                    eligible.append((float(row["timestamp"]), p))
        except (TypeError, ValueError, KeyError):
            pass
    return max(eligible)[1] if eligible else None


def _market_leader(trades: list[dict[str, Any]], ts: float) -> str | None:
    up, down = _price_asof(trades, "UP", ts), _price_asof(trades, "DOWN", ts)
    if up is None or down is None or up == down:
        return None
    return "UP" if up > down else "DOWN"


def _bootstrap_ci(values: list[float], *, seed: int = 17, reps: int = 1500) -> tuple[float | None, float | None]:
    if len(values) < 2:
        return None, None
    rng = random.Random(seed)
    means = [statistics.mean(rng.choices(values, k=len(values))) for _ in range(reps)]
    means.sort()
    return means[int(.025 * reps)], means[min(reps-1, int(.975 * reps))]


def _block_bootstrap_ci(rows: list[dict[str, Any]], value_key: str, block_key: str = "local_date",
                        *, seed: int = 17, reps: int = 1500) -> tuple[float | None, float | None]:
    blocks: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row.get(value_key) is not None:
            blocks[str(row.get(block_key) or row.get("slug"))].append(float(row[value_key]))
    keys = list(blocks)
    if len(keys) < 2:
        return None, None
    rng = random.Random(seed)
    means = []
    for _ in range(reps):
        sampled = rng.choices(keys, k=len(keys))
        values = [v for key in sampled for v in blocks[key]]
        means.append(statistics.mean(values))
    means.sort()
    return means[int(.025*reps)], means[min(reps-1,int(.975*reps))]


def _strategy_block_intervals(rows, *, seed=17, reps=2000):
    primary=[r for r in rows if r["observation_sec"]==180 and r["threshold_bps"]==5 and r["entry_delay_sec"]==0 and r["execution_model"]=="vwap_5s" and r["slippage_cents"]==0]
    dates=sorted({r["local_date"] for r in primary});split=max(1,int(len(dates)*.65));dev=set(dates[:split]);hold=set(dates[split:])
    output=[];rng=random.Random(seed)
    for partition,subset in (("all",primary),("development",[r for r in primary if r["local_date"] in dev]),("holdout",[r for r in primary if r["local_date"] in hold])):
        by_day=defaultdict(list)
        for row in subset:by_day[row["local_date"]].append(row)
        keys=list(by_day)
        if len(keys)<2:
            for metric in ("win_rate","ev_net_pnl","roi"):
                output.append({"partition":partition,"metric":metric,"n_trades":len(subset),"block_days":len(keys),"ci_low":None,"ci_high":None})
            continue
        samples={"win_rate":[],"ev_net_pnl":[],"roi":[]}
        for _ in range(reps):
            chosen=rng.choices(keys,k=len(keys));sample=[r for day in chosen for r in by_day[day]]
            samples["win_rate"].append(sum(r["side"]==r["winner"] for r in sample)/len(sample))
            samples["ev_net_pnl"].append(statistics.mean(float(r["net_pnl"]) for r in sample))
            samples["roi"].append(sum(float(r["net_pnl"]) for r in sample)/sum(float(r["notional"]) for r in sample))
        for metric,values in samples.items():
            values.sort();output.append({"partition":partition,"metric":metric,"n_trades":len(subset),"block_days":len(keys),"ci_low":values[int(.025*reps)],"ci_high":values[min(reps-1,int(.975*reps))],"method":"date-block bootstrap"})
    return output


def _liquidity_features(market: dict[str, Any], start: int) -> dict[str, Any]:
    trades = []
    for row in market.get("trades", []):
        try:
            ts, price, size = float(row["timestamp"]), float(row["price"]), float(row["size"])
            if start <= ts < start + 900 and 0 < price < 1 and size > 0:
                trades.append((ts, price, size, str(row.get("outcome", "")).upper()))
        except (KeyError, TypeError, ValueError):
            continue
    trades.sort()
    sizes = [r[2] for r in trades]
    gaps = ([trades[0][0]-start] + [b[0]-a[0] for a,b in zip(trades,trades[1:])] + [start+900-trades[-1][0]]) if trades else [900]
    # Map both outcome tokens onto one common UP-probability axis, then collapse
    # same-second prints. Interleaving raw UP and DOWN prices would fabricate
    # large jumps/volatility that are only the complementary-token relationship.
    up_probability_by_second: dict[int, list[tuple[float,float]]] = defaultdict(list)
    for ts,price,size,outcome in trades:
        if outcome=="UP":up_probability_by_second[int(ts)].append((price,size))
        elif outcome=="DOWN":up_probability_by_second[int(ts)].append((1-price,size))
    directional_prices=[]
    for ts,values in sorted(up_probability_by_second.items()):
        weight=sum(size for _,size in values)
        if weight:directional_prices.append((ts,sum(price*size for price,size in values)/weight))
    prices=[price for _,price in directional_prices]
    log_returns = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
    return {"trade_count": len(trades), "volume_shares": sum(sizes),
            "volume_usdc_estimate": sum(p*s for _, p, s, _ in trades),
            "median_trade_size": statistics.median(sizes) if sizes else None,
            "p10_trade_size": _quantile(sizes, .1), "p90_trade_size": _quantile(sizes, .9),
            "max_no_trade_interval_sec": max(gaps) if gaps else 900 if not trades else None,
            "trade_frequency_per_sec": len(trades)/900,
            "trade_print_realized_volatility": math.sqrt(sum(v*v for v in log_returns)) if log_returns else None,
            "price_jump_count_5c": sum(abs(b-a) >= .05 for a,b in zip(prices, prices[1:])),
            "price_history_points": len(market.get("price_history", [])),
            "historical_bbo_spread": None, "historical_l2_depth": None,
            "_trades": trades}


def _quantile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered)-1, max(0, int((len(ordered)-1)*p)))]


def _number(value: Any) -> float | None:
    try:
        number=float(value)
        return number if math.isfinite(number) else None
    except (TypeError,ValueError):
        return None


def _sample_market_files(cache_dirs: list[Path], sample_csv: Path | None) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    sample = _read_csv(sample_csv) if sample_csv else []
    cache = _json_cache(cache_dirs)
    markets = []
    if sample:
        for item in sample:
            slug = item.get("slug") or item.get("market_slug")
            if slug in cache:
                markets.append(cache[slug])
    else:
        markets = list(cache.values())
    unique = {m.get("slug"): m for m in markets if m.get("slug")}
    return [unique[k] for k in sorted(unique)], sample


def run_research(*, cache_dirs: list[Path], sample_csv: Path | None, output: Path,
                 btc_cache: Path, start_date: date, end_date: date, timezone_name: str,
                 db_path: Path | None = None, local_public_market_level_csv: Path | None = None,
                 notional: float = 10, offline: bool = False, refresh_btc: bool = False,
                 seed: int = 17) -> dict[str, Any]:
    markets, sample_frame = _sample_market_files(cache_dirs, sample_csv)
    starts = [market_start(m["slug"]) for m in markets if market_start(m.get("slug", ""))]
    utc_start = int(datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc).timestamp())
    utc_end = int(datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    if starts:
        utc_start = min(utc_start, min(starts)-1200)
        utc_end = max(utc_end, max(starts)+960)
    candles, btc_diag = load_btc_candles(utc_start, utc_end, btc_cache, offline=offline, refresh=refresh_btc)
    btc_rows = [{"ts": c["ts"], "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"], "volume": c["volume"], "close_ts": c["close_ts"], "source": "Binance BTCUSDT 1m"} for c in candles]
    _write_csv(output / "btc_candles_manifest.csv", btc_diag)
    _write_csv(output / "btc_candles_used.csv", btc_rows)
    market_rows = []
    time_rows = []
    candidate_rows = []
    control_rows = []
    signal_slugs: dict[tuple[int, int], set[str]] = defaultdict(set)
    valid_slugs: dict[int, set[str]] = defaultdict(set)
    data_quality = []
    for market in markets:
        slug = market["slug"]
        start = market_start(slug)
        if start is None:
            continue
        local = datetime.fromtimestamp(start, ZoneInfo(timezone_name))
        if not start_date <= local.date() <= end_date:
            continue
        gamma = (market.get("gamma") or {}).get("market") or {}
        winner = winner_from_gamma(gamma)
        liq = _liquidity_features(market, start)
        weekend = local.weekday() >= 5
        feature = {k:v for k,v in liq.items() if not k.startswith("_")}
        row = {"slug":slug,"market_start_epoch":start,"local_date":local.date().isoformat(),"week":(local.date()-timedelta(days=local.weekday())).isoformat(),"weekday_weekend":"weekend" if weekend else "weekday","et_hour_block":f"{local.hour//6*6:02d}-{(local.hour//6+1)*6:02d}","winner":winner,"resolution_status":"resolved" if winner else "unknown_result", **feature}
        market_rows.append(row)
        data_quality.append({"slug":slug,"gamma_status":(market.get("gamma") or {}).get("status"),"trade_status":market.get("trade_fetch_status"),"price_status":market.get("price_fetch_status"),"trade_count":liq["trade_count"],"has_btc_open":any(c["ts"]==start for c in candles),"winner_status":"resolved" if winner else "unknown_result"})
        trades = liq["_trades"]
        for left,right,label in SEGMENTS:
            subset=[r for r in trades if start+900-left <= r[0] < start+900-right]
            sizes=[r[2] for r in subset]
            bin_start=start+900-left;bin_end=start+900-right
            gaps=([subset[0][0]-bin_start]+[b[0]-a[0] for a,b in zip(subset,subset[1:])]+[bin_end-subset[-1][0]]) if subset else [bin_end-bin_start]
            time_rows.append({"slug":slug,"weekday_weekend":row["weekday_weekend"],"week":row["week"],"et_hour_block":row["et_hour_block"],"time_to_resolution_bin":label,"trade_count":len(subset),"volume_shares":sum(sizes),"volume_usdc_estimate":sum(r[1]*r[2] for r in subset),"median_trade_size":statistics.median(sizes) if sizes else None,"max_no_trade_interval_sec":max(gaps) if gaps else None,"price_jump_count_5c":sum(abs(b[1]-a[1])>=.05 for a,b in zip(subset,subset[1:]))})
        if not winner:
            continue
        returns = _returns_for_market(candles, start)
        for obs in (60,120,180,240,300):
            if returns.get(obs) is not None:
                valid_slugs[obs].add(slug)
        # Controls use the same 180s decision time, print-based entry proxy and
        # $10 notional. They are descriptive benchmarks, not live advice.
        control_sides = {
            "always_up": "UP", "always_down": "DOWN",
            "random_side": "UP" if random.Random(f"{seed}:{slug}").random() < .5 else "DOWN",
            "polymarket_leader": _market_leader(market.get("trades", []), start + 180),
            "btc_trend_180s": threshold_side(returns.get(180), 0),
            "btc_ema_1m_3m": _ema_side(candles, start + 180),
            "btc_continuation_60s": _return_side(candles, start, start + 180, 60),
            "btc_continuation_120s": _return_side(candles, start, start + 180, 120),
        }
        for family, control_side in control_sides.items():
            if not control_side:
                continue
            price, status = entry_proxy(market.get("trades", []), control_side, start + 180, 0, "vwap_5s")
            if price is None:
                continue
            pnl = outcome_pnl(control_side, winner, price, notional, fee_usdc=notional*.01)
            control_rows.append({"slug":slug,"local_date":local.date().isoformat(),"week":row["week"],"weekday_weekend":row["weekday_weekend"],"et_hour_block":row["et_hour_block"],"winner":winner,"signal_family":family,"side":control_side,"observation_sec":180,"entry_price":price,"entry_status":status,"notional":notional,"gross_pnl":pnl["gross_pnl"],"fee_usdc":pnl["fee_usdc"],"net_pnl":pnl["net_pnl"]})
        for obs in (60,120,180,240,300):
            r = returns[obs]
            signal_ts = start+obs
            for threshold in (0,2,5,10,15,20):
                side=threshold_side(r,threshold)
                if side is None: continue
                signal_slugs[(obs,threshold)].add(slug)
                for delay in (0,5,10):
                    for model in ("first_trade","vwap_5s","vwap_10s"):
                        for slip in (0,.01,.02):
                            price,status=entry_proxy([dict(timestamp=t,price=p,size=s,outcome="UP" if row2.get("outcome_index")==0 or str(row2.get("outcome","")).upper()=="UP" else "DOWN") for row2 in market.get("trades",[]) for t,p,s in [(float(row2.get("timestamp",0)),float(row2.get("price",0)),float(row2.get("size",0)))] if start <= t < start+900],side,signal_ts,delay,model,slippage_cents=slip)
                            if price is None:
                                continue
                            pnl=outcome_pnl(side,winner,price,notional,fee_usdc=0)
                            fee_scenario=notional*.01
                            pnl_fee=outcome_pnl(side,winner,price,notional,fee_usdc=fee_scenario)
                            candidate_rows.append({"slug":slug,"local_date":local.date().isoformat(),"week":row["week"],"weekday_weekend":row["weekday_weekend"],"et_hour_block":row["et_hour_block"],"winner":winner,"signal_family":"btc_open_to_observation","observation_sec":obs,"threshold_bps":threshold,"trend_bps":r,"side":side,"signal_ts":signal_ts,"entry_delay_sec":delay,"execution_model":model,"slippage_cents":slip,"entry_price":price,"entry_status":status,"notional":notional,"gross_pnl":pnl["gross_pnl"],"fee_usdc":fee_scenario,"net_pnl":pnl_fee["net_pnl"],"zero_fee_net_pnl":pnl["net_pnl"],"shares":pnl["shares"]})

    cached_slugs={m.get("slug") for m in markets}
    for item in sample_frame:
        slug=item.get("market_slug") or item.get("slug")
        if not slug or slug in cached_slugs:
            continue
        data_quality.append({"slug":slug,"gamma_status":item.get("gamma_status") or "not_cached","trade_status":item.get("trade_fetch_status") or "not_cached","price_status":item.get("price_fetch_status") or "not_cached","trade_count":None,"has_btc_open":any(c["ts"]==market_start(slug) for c in candles) if market_start(slug) else False,"winner_status":"unknown_result","sample_fetch_status":item.get("fetch_status"),"sample_error_code":item.get("error_code"),"sample_error_stage":item.get("error_stage")})

    _write_csv(output/"public_market_level.csv",market_rows)
    _write_csv(output/"public_time_to_resolution.csv",time_rows)
    _write_csv(output/"public_time_to_resolution_summary.csv",_group_time_rows(time_rows))
    _write_csv(output/"data_quality.csv",data_quality)
    _write_csv(output/"simple_backtest_all.csv",candidate_rows)
    _write_csv(output/"control_strategies.csv",control_rows)
    _write_csv(output/"signal_disagreement.csv",_signal_disagreement(markets,candles))
    # Summaries for all parameter combinations; in-sample/holdout split is date ordered.
    configs=defaultdict(list)
    for r in candidate_rows:
        key=(r["observation_sec"],r["threshold_bps"],r["entry_delay_sec"],r["execution_model"],r["slippage_cents"])
        configs[key].append(r)
    summary=[]
    all_dates=sorted({r["local_date"] for r in market_rows})
    split=max(1,int(len(all_dates)*.65)) if all_dates else 0
    dev_dates=set(all_dates[:split]); hold_dates=set(all_dates[split:])
    date_by_slug={m["slug"]:m["local_date"] for m in market_rows}
    _write_csv(output/"control_strategy_summary.csv",_summarize_controls(control_rows,dev_dates,hold_dates))
    for key in sorted(product((60,120,180,240,300),(0,2,5,10,15,20),(0,5,10),("first_trade","vwap_5s","vwap_10s"),(0,.01,.02))):
        rows=configs.get(key,[])
        by_group={"all":rows,"development":[r for r in rows if r["local_date"] in dev_dates],"holdout":[r for r in rows if r["local_date"] in hold_dates]}
        for partition,subset in by_group.items():
            m=summarize_pnls(subset)
            ci=_block_bootstrap_ci(subset,"net_pnl",seed=seed)
            allowed_dates=dev_dates if partition=="development" else hold_dates if partition=="holdout" else None
            valid_set=valid_slugs.get(key[0],set())
            signal_set=signal_slugs.get((key[0],key[1]),set())
            valid_n=len(valid_set if allowed_dates is None else {s for s in valid_set if date_by_slug.get(s) in allowed_dates})
            signal_n=len(signal_set if allowed_dates is None else {s for s in signal_set if date_by_slug.get(s) in allowed_dates})
            filled_n=len({r["slug"] for r in subset})
            summary.append({"observation_sec":key[0],"threshold_bps":key[1],"entry_delay_sec":key[2],"execution_model":key[3],"slippage_cents":key[4],"partition":partition,**m,"markets_with_valid_btc":valid_n,"signal_markets":signal_n,"no_trade_markets":max(0,valid_n-signal_n),"no_fill_markets":max(0,signal_n-filled_n),"bootstrap_mean_pnl_low":ci[0],"bootstrap_mean_pnl_high":ci[1],"method_note":"local-date block bootstrap; fee scenario=1% notional; chronological 65/35 date split"})
    _write_csv(output/"simple_backtest_summary.csv",summary)
    zero_fee_summary=[]
    for key in product((60,120,180,240,300),(0,2,5,10,15,20),(0,5,10),("first_trade","vwap_5s","vwap_10s"),(0,.01,.02)):
        rows=configs.get(key,[])
        by_part={"all":rows,"development":[r for r in rows if r["local_date"] in dev_dates],"holdout":[r for r in rows if r["local_date"] in hold_dates]}
        for partition,subset in by_part.items():
            zero_rows=[{**r,"net_pnl":r["zero_fee_net_pnl"]} for r in subset]
            zero_fee_summary.append({"observation_sec":key[0],"threshold_bps":key[1],"entry_delay_sec":key[2],"execution_model":key[3],"slippage_cents":key[4],"partition":partition,**summarize_pnls(zero_rows),"fee_scenario":"zero fees"})
    _write_csv(output/"zero_fee_backtest_summary.csv",zero_fee_summary)
    _write_csv(output/"strategy_confidence_intervals.csv",_strategy_block_intervals(candidate_rows,seed=seed))
    _write_csv(output/"observation_window_sensitivity.csv",[r for r in summary if r["threshold_bps"]==5 and r["entry_delay_sec"]==0 and r["execution_model"]=="vwap_5s" and r["slippage_cents"]==0])
    _write_csv(output/"trend_threshold_sensitivity.csv",[r for r in summary if r["observation_sec"]==180 and r["entry_delay_sec"]==0 and r["execution_model"]=="vwap_5s" and r["slippage_cents"]==0])
    development_rows=[r for r in summary if r["partition"]=="development" and r["entry_delay_sec"]==0 and r["execution_model"]=="vwap_5s" and r["slippage_cents"]==0 and r["trades"]>=5]
    best_dev=max(development_rows,key=lambda r:r["pnl_per_trade"],default=None)
    if best_dev:
        matching=[r for r in summary if r["partition"]=="holdout" and all(r[k]==best_dev[k] for k in ("observation_sec","threshold_bps","entry_delay_sec","execution_model","slippage_cents"))]
        _write_csv(output/"development_selected_holdout.csv",[{"development_selection":"highest development PnL/trade among configs with >=5 development trades","selection_observation_sec":best_dev["observation_sec"],"selection_threshold_bps":best_dev["threshold_bps"],"holdout_metrics":json.dumps(matching[0] if matching else {})}])
    else:
        _write_csv(output/"development_selected_holdout.csv",[{"development_selection":"no config had at least 5 development trades"}])
    _write_csv(output/"weekday_weekend_backtest.csv",_group_pnl(candidate_rows,lambda r:r["weekday_weekend"]))
    _write_csv(output/"time_of_day_backtest.csv",_group_pnl(candidate_rows,lambda r:r["et_hour_block"]))
    _write_csv(output/"entry_price_buckets.csv",_bucket_pnl(candidate_rows,lambda r:float(r["entry_price"]),[(.5,.55),(.55,.6),(.6,.65),(.65,.7),(.7,.75),(.75,.8),(.8,.85),(.85,.9),(.9,1.0)],"entry_price"))
    _write_csv(output/"trend_strength_buckets.csv",_bucket_pnl(candidate_rows,lambda r:abs(float(r["trend_bps"])),[(0,2),(2,5),(5,10),(10,15),(15,20),(20,1e9)],"abs_trend_bps"))
    liquidity_regimes=_liquidity_regimes(market_rows)
    _write_csv(output/"liquidity_regimes.csv",liquidity_regimes)
    _write_csv(output/"liquidity_regime_frequency.csv",_regime_frequency(liquidity_regimes))
    _write_csv(output/"liquidity_regime_backtest.csv",_join_regime(candidate_rows,liquidity_regimes))
    _write_csv(output/"public_weekday_weekend.csv",_compare_daytypes(market_rows))
    _write_csv(output/"public_time_of_day.csv",_group_metrics(market_rows,lambda r:r["et_hour_block"]))
    _write_csv(output/"public_weekend_hour_interaction.csv",_group_metrics(market_rows,lambda r:f"{r['weekday_weekend']}:{r['et_hour_block']}"))
    _write_csv(output/"public_weekend_hour_comparison.csv",_weekend_hour_comparison(market_rows))
    _write_csv(output/"public_time_to_resolution_comparison.csv",_compare_time_bins(time_rows))
    _write_csv(output/"public_weekly_blocked.csv",_weekly_blocked(market_rows))
    _write_csv(output/"public_multivariate_ols.csv",_ols_weekend(market_rows))
    local_join,local_liquidity,local_counterfactual,local_fetch=_local_analysis(db_path,market_rows,markets,liquidity_regimes,timezone_name,local_public_market_level_csv,offline=offline,cache_dir=cache_dirs[0])
    _write_csv(output/"local_public_join.csv",local_join)
    _write_csv(output/"local_liquidity_vs_pnl.csv",local_liquidity)
    _write_csv(output/"local_stoploss_counterfactual.csv",local_counterfactual)
    _write_csv(output/"local_public_fetch_diagnostics.csv",local_fetch)
    _write_csv(output/"observation_window_win_rate.csv",_select_metric(summary,"win_rate"))
    _write_csv(output/"observation_window_entry_price.csv",_select_metric(summary,"mean_entry"))
    _write_csv(output/"strategy_drawdown.csv",[{"observation_sec":r["observation_sec"],"threshold_bps":r["threshold_bps"],"drawdown":r["max_drawdown"],"partition":r["partition"]} for r in summary if r["partition"]=="all" and r["entry_delay_sec"]==0 and r["execution_model"]=="vwap_5s" and r["slippage_cents"]==0])
    _write_csv(output/"strategy_equity_curve.csv",_equity_curve([r for r in candidate_rows if r["observation_sec"]==180 and r["threshold_bps"]==5 and r["entry_delay_sec"]==0 and r["execution_model"]=="vwap_5s" and r["slippage_cents"]==0]))
    fetch_diagnostics=[{"stage":"public_market_sample","status":"loaded_cache","sample_rows":len(sample_frame),"cached_markets":len(markets)}]
    for stage,key in (("gamma","gamma_status"),("trades","trade_fetch_status"),("prices","price_fetch_status"),("sample_fetch","fetch_status")):
        counts=defaultdict(int)
        for item in sample_frame:counts[item.get(key) or "unknown"]+=1
        fetch_diagnostics.extend({"stage":stage,"status":status,"count":count} for status,count in sorted(counts.items()))
    fetch_diagnostics.append({"stage":"btc_1m","status":"loaded","candles":len(candles)})
    _write_csv(output/"fetch_diagnostics.csv",fetch_diagnostics)
    _write_csv(output/"public_market_sample.csv",sample_frame)
    return {"markets":market_rows,"candidates":candidate_rows,"summaries":summary,"btc_candles":btc_rows,"data_quality":data_quality,"time_rows":time_rows,"local_join":local_join,"local_counterfactual":local_counterfactual,"output":output}


def _group_pnl(rows: list[dict[str, Any]], group_fn) -> list[dict[str, Any]]:
    groups=defaultdict(list)
    for row in rows:
        if row["observation_sec"]==180 and row["threshold_bps"]==5 and row["entry_delay_sec"]==0 and row["execution_model"]=="vwap_5s" and row["slippage_cents"]==0:
            groups[group_fn(row)].append(row)
    return [{"group":k,**summarize_pnls(v)} for k,v in sorted(groups.items())]


def _summarize_controls(rows,dev_dates=None,hold_dates=None):
    groups=defaultdict(list)
    for row in rows:groups[row["signal_family"]].append(row)
    output=[]
    for key,items in sorted(groups.items()):
        for partition,subset in (("all",items),("development",[r for r in items if dev_dates is not None and r["local_date"] in dev_dates]),("holdout",[r for r in items if hold_dates is not None and r["local_date"] in hold_dates])):
            output.append({"signal_family":key,"partition":partition,**summarize_pnls(subset),"win_rate_basis":"resolved winner labels; trades require a post-signal print"})
    return output


def _weekend_hour_comparison(rows):
    output=[]
    for hour in ("00-06","06-12","12-18","18-24"):
        subset=[r for r in rows if r["et_hour_block"]==hour]
        for metric in ("trade_count","volume_shares","volume_usdc_estimate","max_no_trade_interval_sec"):
            wd=[float(r[metric]) for r in subset if r["weekday_weekend"]=="weekday" and r.get(metric) is not None]
            we=[float(r[metric]) for r in subset if r["weekday_weekend"]=="weekend" and r.get(metric) is not None]
            output.append({"et_hour_block":hour,"metric":metric,"weekday_n":len(wd),"weekday_mean":statistics.mean(wd) if wd else None,"weekend_n":len(we),"weekend_mean":statistics.mean(we) if we else None,"weekend_minus_weekday":statistics.mean(we)-statistics.mean(wd) if wd and we else None,"market_level_permutation_p":_permutation_p(wd,we),"warning":"small within-hour samples; exploratory"})
    return output


def _group_time_rows(rows):
    groups=defaultdict(list)
    for row in rows:
        groups[(row["weekday_weekend"],row["et_hour_block"],row["time_to_resolution_bin"])].append(row)
    out=[]
    for (daytype,hour,segment),items in sorted(groups.items()):
        result={"weekday_weekend":daytype,"et_hour_block":hour,"time_to_resolution_bin":segment,"n_market_segments":len(items)}
        for metric in ("trade_count","volume_shares","volume_usdc_estimate","median_trade_size","max_no_trade_interval_sec","price_jump_count_5c"):
            values=[float(row[metric]) for row in items if row.get(metric) is not None]
            result[f"{metric}_mean"]=statistics.mean(values) if values else None
            result[f"{metric}_p10"]=_quantile(values,.1)
            result[f"{metric}_p95"]=_quantile(values,.95)
        out.append(result)
    return out


def _select_metric(rows, metric):
    return [{k:r.get(k) for k in ("observation_sec","threshold_bps","entry_delay_sec","execution_model","slippage_cents","partition","trades",metric,"bootstrap_mean_pnl_low","bootstrap_mean_pnl_high")} for r in rows if r["threshold_bps"]==5 and r["entry_delay_sec"]==0 and r["execution_model"]=="vwap_5s" and r["slippage_cents"]==0]


def _equity_curve(rows):
    result=[];equity=peak=0.0
    for row in sorted(rows,key=lambda r:(r["local_date"],r["slug"])):
        equity+=float(row["net_pnl"]);peak=max(peak,equity)
        result.append({"slug":row["slug"],"local_date":row["local_date"],"net_pnl":row["net_pnl"],"equity":equity,"drawdown":peak-equity})
    return result


def _signal_disagreement(markets,candles):
    groups=defaultdict(list)
    for market in markets:
        start=market_start(market.get("slug",""))
        gamma=(market.get("gamma") or {}).get("market") or {}
        winner=winner_from_gamma(gamma)
        if start is None or not winner:continue
        end=start+180; ret=_returns_for_market(candles,start).get(180)
        btc=threshold_side(ret,0); leader=_market_leader(market.get("trades",[]),end)
        if not btc or not leader:continue
        key="agree" if btc==leader else "disagree"
        side=btc if key=="agree" else btc
        price,_=entry_proxy(market.get("trades",[]),side,end,0,"vwap_5s")
        groups[(key,btc,leader)].append({"correct":btc==winner,"leader_correct":leader==winner,"price":price,"side":side,"winner":winner})
    output=[]
    for (key,btc,leader),rows in sorted(groups.items()):
        prices=[r["price"] for r in rows if r["price"] is not None]
        output.append({"relationship":key,"btc_side":btc,"market_leader":leader,"n":len(rows),"btc_accuracy":sum(r["correct"] for r in rows)/len(rows),"market_leader_accuracy":sum(r["leader_correct"] for r in rows)/len(rows),"btc_entry_mean":statistics.mean(prices) if prices else None,"note":"accuracy among decisive labels; no quote-based execution inference"})
    for relationship in ("agree","disagree"):
        rows=[r for (rel,_,_),items in groups.items() if rel==relationship for r in items]
        if rows:
            prices=[r["price"] for r in rows if r["price"] is not None]
            output.append({"relationship":relationship,"btc_side":"ALL","market_leader":"ALL","n":len(rows),"btc_accuracy":sum(r["correct"] for r in rows)/len(rows),"market_leader_accuracy":sum(r["leader_correct"] for r in rows)/len(rows),"btc_entry_mean":statistics.mean(prices) if prices else None,"note":"aggregated; descriptive, no confidence interval"})
    return output


def _ols_weekend(rows):
    """Small interpretable OLS with week/hour fixed effects; HC1 robust SE."""
    try:
        import numpy as np
    except ImportError:
        return [{"status":"numpy_unavailable"}]
    output=[]
    for response in ("volume_shares","trade_count"):
        valid=[r for r in rows if r.get(response) is not None]
        if len(valid)<12: output.append({"response":f"log1p({response})","status":"insufficient_sample","n":len(valid)});continue
        weeks=sorted({r["week"] for r in valid});hours=sorted({r["et_hour_block"] for r in valid})
        design=[];y=[]
        for r in valid:
            row=[1.0,1.0 if r["weekday_weekend"]=="weekend" else 0.0,math.log1p(float(r["trade_print_realized_volatility"] or 0))]
            row += [1.0 if r["week"]==v else 0.0 for v in weeks[1:]]
            row += [1.0 if r["et_hour_block"]==v else 0.0 for v in hours[1:]]
            design.append(row);y.append(math.log1p(float(r[response])))
        X=np.asarray(design);Y=np.asarray(y);beta=np.linalg.pinv(X.T@X)@X.T@Y;residual=Y-X@beta
        bread=np.linalg.pinv(X.T@X);meat=X.T@np.diag(residual**2)@X;cov=bread@meat@bread*(len(valid)/max(1,len(valid)-X.shape[1]));se=np.sqrt(np.maximum(0,np.diag(cov)))
        z=float(beta[1]/se[1]) if se[1] else None;p=math.erfc(abs(z)/math.sqrt(2)) if z is not None else None
        output.append({"response":f"log1p({response})","term":"weekend","coefficient":float(beta[1]),"robust_se_hc1":float(se[1]),"ci95_low_normal":float(beta[1]-1.96*se[1]),"ci95_high_normal":float(beta[1]+1.96*se[1]),"normal_approx_p_value":p,"n":len(valid),"controls":"week and ET 6h block fixed effects; log1p(trade-print RV)","warning":"exploratory OLS; count overdispersion not modeled with negative binomial"})
    return output


def _local_analysis(db_path, public_rows, public_markets, regimes, timezone_name, public_market_level_csv=None, *, offline=False, cache_dir=None):
    if db_path is None or not Path(db_path).exists(): return [],[],[],[]
    from scripts.analyze_weekend_liquidity import load_local_journal, fetch_market_public_history, parse_polymarket_instrument_id, load_public_cache
    local=load_local_journal(str(db_path),start="2026-07-27T00:00:00Z",end="2026-09-21T23:59:59Z",timezone_name=timezone_name)
    metrics={r["slug"]:r for r in public_rows};reg={r["slug"]:r["liquidity_regime"] for r in regimes}
    if public_market_level_csv and Path(public_market_level_csv).exists():
        for source in _read_csv(Path(public_market_level_csv)):
            slug=source.get("market_slug")
            if not slug or source.get("public_status") not in {"SUCCESS_PUBLIC_HISTORY","CACHE_PUBLIC_HISTORY"}:continue
            metrics[slug]={"slug":slug,"trade_count":_number(source.get("public_trade_count")),"volume_shares":_number(source.get("public_volume_shares")),"volume_usdc_estimate":_number(source.get("public_volume_usdc_estimate")),"median_trade_size":_number(source.get("median_trade_size")),"max_no_trade_interval_sec":_number(source.get("max_no_trade_interval_sec")),"trade_print_realized_volatility":_number(source.get("realized_volatility")),"weekday_weekend":"weekend" if str(source.get("weekend_et")).lower()=="true" else "weekday"}
        fallback=[{"slug":slug,"weekday_weekend":m.get("weekday_weekend","weekday"),"trade_count":m.get("trade_count"),"volume_shares":m.get("volume_shares"),"median_trade_size":m.get("median_trade_size"),"max_no_trade_interval_sec":m.get("max_no_trade_interval_sec"),"trade_print_realized_volatility":m.get("trade_print_realized_volatility")} for slug,m in metrics.items() if m.get("trade_count") is not None]
        fallback_regimes=_liquidity_regimes(fallback)
        reg.update({r["slug"]:r["liquidity_regime"] for r in fallback_regimes})
    local_trades=local.get("trades",[]);joined=[];counter=[]
    market_cache={m["slug"]:m for m in public_markets}
    gamma_cache={}
    for slug,m in market_cache.items():
        gamma_cache[slug]=(m.get("gamma") or {}).get("market") or {}
    fetch_diagnostics=[]
    local_slugs=sorted({str(r.get("market_slug")) for r in local_trades if r.get("market_slug")})
    for slug in local_slugs:
        if slug in gamma_cache and gamma_cache[slug]:
            continue
        identity_source=next((r.get("instrument_id") for r in local_trades if r.get("market_slug")==slug and r.get("instrument_id")),None)
        parsed=parse_polymarket_instrument_id(identity_source) if identity_source else None
        identity={"condition_id":parsed["condition_id"],"token_ids":[parsed["token_id"]]} if parsed else None
        try:
            if offline:
                public=load_public_cache(str(cache_dir),slug) if cache_dir is not None else None
                error=None
                if public is None:
                    fetch_diagnostics.append({"slug":slug,"status":"unavailable_offline"});continue
            else:
                public, error=fetch_market_public_history(slug,cache_dir=str(cache_dir),local_identity=identity)
            gamma=(public.get("gamma") or {}).get("market") or {}
            if gamma:gamma_cache[slug]=gamma
            if public.get("trades"):
                l=_liquidity_features(public,market_start(slug) or 0)
                metrics[slug]={"slug":slug,"trade_count":l["trade_count"],"volume_shares":l["volume_shares"],"volume_usdc_estimate":l["volume_usdc_estimate"],"median_trade_size":l["median_trade_size"],"max_no_trade_interval_sec":l["max_no_trade_interval_sec"],"trade_print_realized_volatility":l["trade_print_realized_volatility"],"weekday_weekend":"weekend" if weekend_and_hour(market_start(slug) or 0,timezone_name)[0] else "weekday"}
            fetch_diagnostics.append({"slug":slug,"status":public.get("status"),"gamma_status":(public.get("gamma") or {}).get("status"),"trade_status":public.get("trade_fetch_status"),"price_status":public.get("price_fetch_status"),"error_code":(error or {}).get("code")})
        except Exception as exc:
            fetch_diagnostics.append({"slug":slug,"status":"fetch_failed","error_type":type(exc).__name__,"error":str(exc)})
    fallback=[{"slug":slug,"weekday_weekend":m.get("weekday_weekend","weekday"),"trade_count":m.get("trade_count"),"volume_shares":m.get("volume_shares"),"median_trade_size":m.get("median_trade_size"),"max_no_trade_interval_sec":m.get("max_no_trade_interval_sec"),"trade_print_realized_volatility":m.get("trade_print_realized_volatility")} for slug,m in metrics.items() if m.get("trade_count") is not None]
    fallback_regimes=_liquidity_regimes(fallback)
    reg.update({r["slug"]:r["liquidity_regime"] for r in fallback_regimes})
    for tr in local_trades:
        slug=tr.get("market_slug");m=metrics.get(slug)
        if not m:continue
        joined_row={"market_slug":slug,"entry_source":tr.get("entry_source"),"entry_price":tr.get("entry_price"),"exit_price":tr.get("exit_price"),"qty":tr.get("qty"),"actual_pnl":tr.get("pnl"),"exit_reason":tr.get("exit_reason"),"hold_sec":tr.get("hold_sec"),"liquidity_regime":reg.get(slug),"public_trade_count":m.get("trade_count"),"public_volume_shares":m.get("volume_shares"),"public_volume_usdc_estimate":m.get("volume_usdc_estimate"),"public_median_trade_size":m.get("median_trade_size"),"public_max_no_trade_gap_sec":m.get("max_no_trade_interval_sec"),"public_realized_volatility":m.get("trade_print_realized_volatility"),"public_liquidity_score":next((r.get("liquidity_score_equal_weight_percentile") for r in regimes if r["slug"]==slug),None)}
        joined.append(joined_row)
        if "stop" not in str(tr.get("exit_reason") or "").lower() or not tr.get("entry_price") or not tr.get("qty"):
            continue
        gamma=gamma_cache.get(slug) or {};winner=winner_from_gamma(gamma)
        token=str(tr.get("instrument_id") or "").rsplit("-",1)[-1].removesuffix(".POLYMARKET")
        try:
            ids=json.loads(gamma.get("clobTokenIds","[]")) if isinstance(gamma.get("clobTokenIds"),str) else gamma.get("clobTokenIds",[])
            outcomes=json.loads(gamma.get("outcomes","[]")) if isinstance(gamma.get("outcomes"),str) else gamma.get("outcomes",[])
            token_side=str(outcomes[ids.index(token)]).upper() if token in ids else None
        except (ValueError,IndexError,TypeError): token_side=None
        if winner and token_side in {"UP","DOWN"}:
            qty=float(tr["qty"]);entry=float(tr["entry_price"]);hold=(qty-entry*qty) if token_side==winner else -entry*qty
            actual=tr.get("pnl")
            counter.append({"market_slug":slug,"entry_source":tr.get("entry_source"),"exit_reason":tr.get("exit_reason"),"qty":qty,"entry_price":entry,"actual_exit_pnl":actual,"hypothetical_hold_gross_pnl":hold,"hold_minus_actual":hold-float(actual) if actual is not None else None,"winner":winner,"held_side":token_side,"comparison":"complete settlement and token mapping"})
    grouped=defaultdict(list)
    for row in joined:
        if row.get("actual_pnl") is not None:grouped[row.get("liquidity_regime") or "unknown"].append(float(row["actual_pnl"]))
    pnl_rows=[{"liquidity_regime":k,"n_local_closed_trades":len(v),"mean_actual_pnl":statistics.mean(v),"total_actual_pnl":sum(v),"positive_share":sum(x>0 for x in v)/len(v)} for k,v in sorted(grouped.items())]
    return joined,pnl_rows,counter,fetch_diagnostics


def _bucket_pnl(rows, value_fn, bounds, name):
    out=[]
    for low,high in bounds:
        group=[r for r in rows if r["observation_sec"]==180 and r["threshold_bps"]==5 and r["entry_delay_sec"]==0 and r["execution_model"]=="vwap_5s" and r["slippage_cents"]==0 and low <= value_fn(r) < high]
        out.append({name:f"{low:g}-{high:g}","lower":low,"upper":high,**summarize_pnls(group)})
    return out


def _rank(values: list[float], invert: bool=False) -> list[float]:
    order=sorted(range(len(values)),key=lambda i:values[i],reverse=invert)
    ranks=[0.0]*len(values)
    for pos,index in enumerate(order): ranks[index]=(pos+.5)/len(values)
    return ranks


def _liquidity_regimes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid=[r for r in rows if r["trade_count"] is not None]
    if not valid:return []
    keys=["trade_count","volume_shares","median_trade_size","trade_frequency_per_sec","max_no_trade_interval_sec","trade_print_realized_volatility"]
    ranks={}
    for key in keys:
        present=[r for r in valid if r.get(key) is not None]
        rr=_rank([float(r[key]) for r in present],invert=key=="max_no_trade_interval_sec")
        ranks[key]={r["slug"]:value for r,value in zip(present,rr)}
    scores=[]
    for r in valid:
        vals=[ranks[k][r["slug"]] for k in keys if r["slug"] in ranks[k]]
        score=statistics.mean(vals) if vals else None
        scores.append((r["slug"],score))
    sorted_scores=sorted(s for _,s in scores if s is not None)
    result=[]
    for slug,score in scores:
        regime="low" if score<=_quantile(sorted_scores,1/3) else "high" if score>_quantile(sorted_scores,2/3) else "medium"
        source=next(r for r in valid if r["slug"]==slug)
        result.append({"slug":slug,"liquidity_score_equal_weight_percentile":score,"liquidity_regime":regime,"weekday_weekend":source["weekday_weekend"],"trade_count":source["trade_count"],"volume_shares":source["volume_shares"],"median_trade_size":source["median_trade_size"],"max_no_trade_interval_sec":source["max_no_trade_interval_sec"],"trade_print_realized_volatility":source["trade_print_realized_volatility"]})
    return result


def _join_regime(rows, regimes):
    reg={r["slug"]:r["liquidity_regime"] for r in regimes}
    groups=defaultdict(list)
    for r in rows:
        if r["observation_sec"]==180 and r["threshold_bps"]==5 and r["entry_delay_sec"]==0 and r["execution_model"]=="vwap_5s" and r["slippage_cents"]==0:
            groups[reg.get(r["slug"],"unknown")].append(r)
    return [{"liquidity_regime":k,**summarize_pnls(v)} for k,v in sorted(groups.items())]


def _group_metrics(rows,group_fn):
    groups=defaultdict(list)
    for r in rows: groups[group_fn(r)].append(r)
    metrics=("trade_count","volume_shares","volume_usdc_estimate","median_trade_size","max_no_trade_interval_sec","trade_frequency_per_sec","trade_print_realized_volatility","price_jump_count_5c")
    out=[]
    for group,items in sorted(groups.items()):
        item={"group":group,"n_markets":len(items)}
        for metric in metrics:
            vals=[float(r[metric]) for r in items if r.get(metric) is not None]
            item[f"{metric}_n"]=len(vals); item[f"{metric}_mean"]=statistics.mean(vals) if vals else None;item[f"{metric}_p10"]=_quantile(vals,.1);item[f"{metric}_p5"]=_quantile(vals,.05);item[f"{metric}_p95"]=_quantile(vals,.95)
        out.append(item)
    return out


def _permutation_p(a,b,*,seed=17,reps=2000):
    if not a or not b:return None
    observed=statistics.mean(b)-statistics.mean(a);combined=list(a)+list(b);rng=random.Random(seed);extreme=0
    for _ in range(reps):
        rng.shuffle(combined);delta=statistics.mean(combined[len(a):])-statistics.mean(combined[:len(a)])
        if abs(delta)>=abs(observed):extreme+=1
    return (extreme+1)/(reps+1)


def _compare_daytypes(rows):
    metrics=("trade_count","volume_shares","volume_usdc_estimate","median_trade_size","max_no_trade_interval_sec","trade_frequency_per_sec","trade_print_realized_volatility","price_jump_count_5c")
    output=[]
    for metric in metrics:
        wd=[float(r[metric]) for r in rows if r["weekday_weekend"]=="weekday" and r.get(metric) is not None]
        we=[float(r[metric]) for r in rows if r["weekday_weekend"]=="weekend" and r.get(metric) is not None]
        rng=random.Random(17);diffs=[]
        if wd and we:
            for _ in range(1500):diffs.append(statistics.mean(rng.choices(we,k=len(we)))-statistics.mean(rng.choices(wd,k=len(wd))))
        diffs.sort();output.append({"metric":metric,"weekday_n":len(wd),"weekday_mean":statistics.mean(wd) if wd else None,"weekend_n":len(we),"weekend_mean":statistics.mean(we) if we else None,"weekend_minus_weekday":statistics.mean(we)-statistics.mean(wd) if wd and we else None,"bootstrap_95_low":diffs[int(.025*len(diffs))] if diffs else None,"bootstrap_95_high":diffs[min(len(diffs)-1,int(.975*len(diffs)))] if diffs else None,"market_level_permutation_p":_permutation_p(wd,we),"note":"market-level uncertainty; paired weekly result is in public_weekly_blocked.csv"})
    return output


def _compare_time_bins(rows):
    metrics=("trade_count","volume_shares","volume_usdc_estimate","median_trade_size","max_no_trade_interval_sec","price_jump_count_5c")
    output=[]
    for segment in (x[2] for x in SEGMENTS):
        sub=[r for r in rows if r["time_to_resolution_bin"]==segment]
        for metric in metrics:
            wd=[float(r[metric]) for r in sub if r["weekday_weekend"]=="weekday" and r.get(metric) is not None]
            we=[float(r[metric]) for r in sub if r["weekday_weekend"]=="weekend" and r.get(metric) is not None]
            output.append({"time_to_resolution_bin":segment,"metric":metric,"weekday_n":len(wd),"weekday_mean":statistics.mean(wd) if wd else None,"weekend_n":len(we),"weekend_mean":statistics.mean(we) if we else None,"weekend_minus_weekday":statistics.mean(we)-statistics.mean(wd) if wd and we else None,"market_level_permutation_p":_permutation_p(wd,we),"note":"descriptive segment comparison; markets are unit"})
    return output


def _weekly_blocked(rows):
    output=[]
    for metric in ("trade_count","volume_shares","volume_usdc_estimate","median_trade_size","max_no_trade_interval_sec","trade_print_realized_volatility"):
        by=defaultdict(lambda:defaultdict(list))
        for r in rows:
            if r.get(metric) is not None:by[r["week"]][r["weekday_weekend"]].append(float(r[metric]))
        differences=[]; paired=[]
        for week,g in sorted(by.items()):
            if g.get("weekday") and g.get("weekend"):
                wd,we=statistics.mean(g["weekday"]),statistics.mean(g["weekend"]);delta=we-wd
                differences.append(delta);paired.append({"metric":metric,"week":week,"weekday_mean":wd,"weekend_mean":we,"weekend_minus_weekday":delta,"weekday_n":len(g["weekday"]),"weekend_n":len(g["weekend"])})
        lo,hi=_bootstrap_ci(differences)
        if differences:
            observed=abs(statistics.mean(differences));extreme=0;total=2**len(differences)
            for signs in product((-1,1),repeat=len(differences)):
                if abs(statistics.mean([v*s for v,s in zip(differences,signs)])) >= observed-1e-12:extreme+=1
            p=extreme/total
        else:p=None
        paired.append({"metric":metric,"week":"SUMMARY","paired_weeks":len(differences),"median_weekly_difference":statistics.median(differences) if differences else None,"bootstrap_low":lo,"bootstrap_high":hi,"paired_sign_flip_p_two_sided":p,"weeks_weekend_lower":sum(d<0 for d in differences),"note":"exact paired sign-flip across week blocks; low power with 8 weeks"})
        output.extend(paired)
    return output


def _regime_frequency(rows):
    groups=defaultdict(list)
    for row in rows:groups[row["weekday_weekend"]].append(row)
    result=[]
    for group,items in sorted(groups.items()):
        n=len(items)
        for regime in ("low","medium","high"):
            count=sum(row["liquidity_regime"]==regime for row in items)
            result.append({"weekday_weekend":group,"regime":regime,"market_count":count,"group_markets":n,"share":count/n if n else None})
    return result


def write_summary(result: dict[str, Any], output: Path, *, start: date, end: date, timezone_name: str) -> None:
    markets=result["markets"]; candidates=result["candidates"]
    resolved=[m for m in markets if m.get("winner")]
    btc_valid=sum(bool(x["has_btc_open"]) for x in result["data_quality"])
    primary=[r for r in candidates if r["observation_sec"]==180 and r["threshold_bps"]==5 and r["entry_delay_sec"]==0 and r["execution_model"]=="vwap_5s" and r["slippage_cents"]==0]
    p=summarize_pnls(primary); ci=_bootstrap_ci([r["net_pnl"] for r in primary])
    def read(name): return _read_csv(output/name) if (output/name).exists() else []
    def metric_line(r):
        if not r:return "n/a"
        line=f"N={r.get('trades')}; win={_fmt_num(r.get('win_rate'))}; entry={_fmt_num(r.get('mean_entry'))}; EV/trade={_fmt_num(r.get('pnl_per_trade'))}; net=${_fmt_num(r.get('net_pnl'))}; ROI={_fmt_pct(r.get('roi'))}"
        if r.get("bootstrap_mean_pnl_low") not in (None, "") and r.get("bootstrap_mean_pnl_high") not in (None, ""):
            line+=f"; 95% block CI EV=[{_fmt_num(r.get('bootstrap_mean_pnl_low'))}, {_fmt_num(r.get('bootstrap_mean_pnl_high'))}]"
        return line
    base_rows=read("simple_backtest_summary.csv")
    def base(obs,part="all",threshold="5"):
        return next((r for r in base_rows if r.get("observation_sec")==str(obs) and r.get("threshold_bps")==threshold and r.get("entry_delay_sec")=="0" and r.get("execution_model")=="vwap_5s" and r.get("slippage_cents")=="0" and r.get("partition")==part),None)
    windows=[base(sec) for sec in (60,120,180,240,300)]
    hold_thresholds=[r for r in read("trend_threshold_sensitivity.csv") if r.get("partition")=="holdout" and r.get("execution_model")=="vwap_5s" and r.get("entry_delay_sec")=="0" and r.get("slippage_cents")=="0"]
    controls=read("control_strategy_summary.csv")
    hold_controls={r["signal_family"]:r for r in controls if r.get("partition")=="holdout"}
    liquidity=read("public_weekday_weekend.csv")
    ldict={r["metric"]:r for r in liquidity}
    trade_l=ldict.get("trade_count",{});vol_l=ldict.get("volume_shares",{})
    status_counts=defaultdict(int)
    for row in _read_csv(output/"public_refresh/public_market_sample.csv"):
        status_counts["gamma_"+str(row.get("gamma_status") or "unknown")]+=1
        status_counts["trades_"+str(row.get("trade_fetch_status") or "unknown")]+=1
        status_counts["prices_"+str(row.get("price_fetch_status") or "unknown")]+=1
    paired=read("public_weekly_blocked.csv")
    paired_summary={r["metric"]:r for r in paired if r.get("week")=="SUMMARY"}
    segment=read("public_time_to_resolution_comparison.csv")
    segvol={(r.get("time_to_resolution_bin"),r.get("metric")):r for r in segment}
    regimes=read("liquidity_regime_frequency.csv")
    low={r["weekday_weekend"]:r for r in regimes if r.get("regime")=="low"}
    ci_rows=read("strategy_confidence_intervals.csv")
    strategy_ci={r["metric"]:r for r in ci_rows if r.get("partition")=="all"}
    controls_lines=[]
    for label,family in (("BTC 180s direction","btc_trend_180s"),("Polymarket leader","polymarket_leader"),("Always UP","always_up"),("Always DOWN","always_down"),("Random side","random_side"),("1m/3m EMA proxy","btc_ema_1m_3m")):
        controls_lines.append(f"- {label}: {metric_line(hold_controls.get(family))}.")
    primary_line=base(180)
    primary_metrics={k:v for k,v in p.items()}
    window_text="; ".join(f"{seconds}s=${_fmt_num(row.get('net_pnl'))} (n={row.get('trades')}, win={_fmt_pct(row.get('win_rate'))}, entry={_fmt_num(row.get('mean_entry'))})" for seconds,row in zip((60,120,180,240,300),windows))
    local_join=len(result.get("local_join",[]));local_cf=len(result.get("local_counterfactual",[]))
    lines=[
        "# 統一歷史策略研究",
        "",
        "## Data",
        "",
        f"- 研究期間：{start} 至 {end}，市場分層時區 {timezone_name}；公開市場按週、日期、weekday/weekend 與 6 小時 ET 區塊分層抽樣。",
        f"- 公開樣本：{len(markets)} 個（預期平日／週末各 100）；Gamma 已解析結果 {len(resolved)}；BTC 開盤 K 線 {btc_valid}；BTC K 線筆數 {len(result['btc_candles'])}。",
        f"- API 狀態：Gamma success={status_counts.get('gamma_success',0)}；trades success/empty/failed={status_counts.get('trades_success',0)}/{status_counts.get('trades_empty',0)}/{status_counts.get('trades_failed',0)}；prices success/empty/failed={status_counts.get('prices_success',0)}/{status_counts.get('prices_empty',0)}/{status_counts.get('prices_failed',0)}。",
        "- BTC source：Binance BTCUSDT 1-minute OHLCV。沒有插值成秒級；無法驗證 live 約 6s/20s EMA，也不是 Polymarket 用於結算的 Chainlink 60s TWAP。",
        "- Settlement truth：Gamma `closed` 且 outcomePrices 為決定性 1/0；entry 價用 signal 後 public trade prints，不是 historical ask/BBO；未出現成交 print 即 no-fill。",
        f"- 交易日誌：只讀 `logs/trade_journal.db`。公開市場特徵成功 join 到本地 paired trade rows：{local_join}；有完整 Gamma winner + token-side mapping 的 stop-loss counterfactual：{local_cf} 筆。",
        "- Historical BBO/L2 未取得；spread、depth、真實可成交性與 order-book impact 不可回測。",
        "",
        "## Liquidity：weekday vs weekend",
        "",
        f"- 平均 trade count：weekday {trade_l.get('weekday_mean')}、weekend {trade_l.get('weekend_mean')}（差 {trade_l.get('weekend_minus_weekday')}；95% market bootstrap CI [{trade_l.get('bootstrap_95_low')}, {trade_l.get('bootstrap_95_high')}], permutation p={trade_l.get('market_level_permutation_p')}）。",
        f"- 平均 share volume：weekday {vol_l.get('weekday_mean')}、weekend {vol_l.get('weekend_mean')}（差 {vol_l.get('weekend_minus_weekday')}；95% CI [{vol_l.get('bootstrap_95_low')}, {vol_l.get('bootstrap_95_high')}], p={vol_l.get('market_level_permutation_p')}）。方向上週末較低，但 CI 含 0，不能說已證明差異。",
        f"- Weekly blocked volume：8 個 paired weeks 中 {paired_summary.get('volume_shares',{}).get('weeks_weekend_lower')} 週週末較低；median weekly difference={paired_summary.get('volume_shares',{}).get('median_weekly_difference')} shares；95% block bootstrap CI=[{paired_summary.get('volume_shares',{}).get('bootstrap_low')}, {paired_summary.get('volume_shares',{}).get('bootstrap_high')}], exact sign-flip p={paired_summary.get('volume_shares',{}).get('paired_sign_flip_p_two_sided')}。",
        f"- Low-liquidity composite tail share：weekday {low.get('weekday',{}).get('share')}、weekend {low.get('weekend',{}).get('share')}；週末樣本較常落在樣本內低三分位，但這是 rank-based composite、100/group 小樣本，不能單獨作為因果解釋。",
        "- settlement 前 60/30/15 秒、ET 時段交互作用、每週 paired 結果請見 `public_time_to_resolution_comparison.csv`、`public_weekend_hour_comparison.csv`、`public_weekly_blocked.csv`。",
        "",
        "## Simple strategy：180 秒等候、5 bps 門檻",
        "",
        f"- All sample：{metric_line(base(180))}；gross PnL=${_fmt_num(primary_metrics.get('gross_pnl'))}，假設費用=${_fmt_num(primary_metrics.get('fee_estimate'))}（notional 的 1% 情境，不代表歷史實際 fee）。",
        f"- Development：{metric_line(base(180,'development'))}。",
        f"- Holdout：{metric_line(base(180,'holdout'))}。Holdout 的點估計為正，但 EV CI 跨 0；不能宣稱已驗證正 EV。",
        f"- Break-even win rate 約等於平均 entry price {p.get('break_even_win_rate')}；觀察 win rate {p.get('win_rate')}。信賴區間與樣本依賴性仍不足以排除零 edge。",
        f"- 60/120/180/240/300 秒（5 bps，5s VWAP，零滑點，fee stress）all-sample net PnL：{window_text}。240 秒 holdout 點估計較高，但 development 為負，屬不穩定選參數，不是可部署優勢。",
        f"- 180 秒 holdout confidence intervals：win rate [{strategy_ci.get('win_rate',{}).get('ci_low')}, {strategy_ci.get('win_rate',{}).get('ci_high')}]; EV/trade [{strategy_ci.get('ev_net_pnl',{}).get('ci_low')}, {strategy_ci.get('ev_net_pnl',{}).get('ci_high')}]; ROI [{strategy_ci.get('roi',{}).get('ci_low')}, {strategy_ci.get('roi',{}).get('ci_high')}]，以日期為 block bootstrap。",
        "- 180 秒 threshold holdout table見 `trend_threshold_sensitivity.csv`：0/2/5 bps 的點估計方向偏正、信賴區間跨 0；10/15/20 bps 樣本很少且點估計偏負。不要把 development 最佳值當成 live 門檻。",
        "- Entry-price、trend-strength bucket、signal disagreement 與 weekday/weekend performance 分別在 `entry_price_buckets.csv`、`trend_strength_buckets.csv`、`signal_disagreement.csv`、`weekday_weekend_backtest.csv`。",
        "",
        "## Controls / incremental information",
        "",
        *controls_lines,
        "- BTC vs market leader agreement/disagreement 的 accuracy 只供描述；disagreement 樣本較小，且不是可成交報價比較，不能確認 BTC signal 有穩定增量資訊。",
        "",
        "## Local bot outcomes and exits",
        "",
        "- Local journal 目前僅有平日已實際成交市場；週末本地交易樣本為 0，因此 local PnL 無法估週末效果。",
        f"- 有完整 winner/token mapping 的 stop-loss trade：{local_cf} 筆；counterfactual 在 `local_stoploss_counterfactual.csv`。本次結果不足以概括止損是救風險還是過早出場。",
        "- Local liquidity/PnL join 的樣本小且依賴公開 market history；請見 `local_public_join.csv`、`local_liquidity_vs_pnl.csv`。不應因 regime 切片差異直接放寬/新增 live gate。",
        "- Current profile `FIRST_ENTRY_MAX_TIME_LEFT_SEC=780` 約等於開盤後 120 秒首次進場窗口；此處 180 秒 simple baseline 是另一個純研究策略。Live bot 有訊號、economics、止盈/止損與 execution gate，結果不可視為 apples-to-apples。",
        "",
        "## Evidence verdict",
        "",
        "- **資料支持（描述性）**：本次分層樣本中，週末平均成交量/筆數較低，且週末低 composite liquidity 區比例較高；weekly/market CI 與 p-values 尚不支持穩定差異，hour/settlement slices 亦需視為探索性。",
        "- **提示但不確定**：180 秒 BTC trend 5 bps 策略在本樣本 development/holdout 點估計皆為正，但日期 block CI 跨 0、樣本只有 73 筆成交；無 robust positive-EV 結論。",
        "- **未獲支持**：目前證據不支持某個 observation window 或 threshold 已有可泛化優勢；240 秒的 holdout 好結果與負 development 不一致。",
        "- **現有資料無法評估**：歷史 ask/BBO、L2 depth、真實 taker fill/slippage、6s/20s EMA、Chainlink 開盤/結算價同步，以及大部份本地交易的 hold-to-settlement counterfactual。",
        "",
        "## Limitations / reproduction",
        "",
        "- Public universe 是每組 100 個分層樣本，不是期間內所有市場普查；API empty/failure 保留在 `public_market_sample.csv`、`fetch_diagnostics.csv` 與 `data_quality.csv`，不當作零成交。",
        "- Fees：同時提供 zero-fee 與明示 1% notional stress scenario；後者不是聲稱歷史費率。交易 prints VWAP 仍不保證該價可成交。",
        "- 8 週、8 個 paired weekly blocks 的檢定力有限；multivariate count OLS 是探索性，未套 Negative Binomial。matplotlib 不在此 venv，沒有輸出 PNG charts。",
        "- 可用已快取資料離線重跑：`scripts/analyze_weekend_liquidity.py --offline` 與 `scripts/backtest_simple_trend_hold.py --offline`；BTC cache 位於 `data/btc_history/`。",
        "- 本報告是歷史研究，不修改 live trading behavior，也不構成 live 參數建議。",
        "",
    ]
    output.mkdir(parents=True,exist_ok=True);(output/"summary.md").write_text("\n".join(lines),encoding="utf-8")


def _fmt_num(value):
    number=_number(value)
    return f"{number:.4f}" if number is not None else "n/a"


def _fmt_pct(value):
    number=_number(value)
    return f"{number*100:.2f}%" if number is not None else "n/a"


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start",default="2026-07-27");parser.add_argument("--end",default="2026-09-20")
    parser.add_argument("--timezone",default="America/New_York");parser.add_argument("--notional",type=float,default=10.0)
    parser.add_argument("--output",default="reports/unified_strategy_research")
    parser.add_argument("--cache-dir",default=str(DEFAULT_PUBLIC_CACHE));parser.add_argument("--sample-csv",default=None)
    parser.add_argument("--db",default="logs/trade_journal.db")
    parser.add_argument("--local-public-market-level",default=None)
    parser.add_argument("--btc-cache-dir",default="data/btc_history");parser.add_argument("--offline",action="store_true");parser.add_argument("--refresh",action="store_true");parser.add_argument("--seed",type=int,default=17)
    args=parser.parse_args(argv)
    output=Path(args.output);cache_dirs=[Path(args.cache_dir),FALLBACK_PUBLIC_CACHE]
    sample=Path(args.sample_csv) if args.sample_csv else (Path(args.cache_dir)/"public_market_sample.csv")
    result=run_research(cache_dirs=cache_dirs,sample_csv=sample,output=output,btc_cache=Path(args.btc_cache_dir),db_path=Path(args.db) if args.db else None,local_public_market_level_csv=Path(args.local_public_market_level) if args.local_public_market_level else None,start_date=date.fromisoformat(args.start),end_date=date.fromisoformat(args.end),timezone_name=args.timezone,notional=args.notional,offline=args.offline,refresh_btc=args.refresh,seed=args.seed)
    write_summary(result,output,start=date.fromisoformat(args.start),end=date.fromisoformat(args.end),timezone_name=args.timezone)
    print(f"markets={len(result['markets'])} candidates={len(result['candidates'])} BTC candles={len(result['btc_candles'])} output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
