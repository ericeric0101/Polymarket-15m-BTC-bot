#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

import httpx
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.smart_money import _norm_direction  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _now_ts() -> int:
    return int(time.time())


def _btc_15m_slugs(lookback_intervals: int, lookahead_intervals: int) -> list[str]:
    now = datetime.now(timezone.utc)
    current_start = int(now.timestamp() // 900) * 900
    start = current_start - max(0, lookback_intervals) * 900
    end = current_start + max(0, lookahead_intervals) * 900
    return [f"btc-updown-15m-{ts}" for ts in range(start, end + 1, 900) if ts > 0]


def _chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


@dataclass
class WalletStats:
    proxy_wallet: str
    total_trades: int = 0
    buy_trades: int = 0
    sell_trades: int = 0
    markets: set[str] = field(default_factory=set)
    market_direction_cash: dict[str, dict[str, float]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(float)))
    trade_sizes: list[float] = field(default_factory=list)
    total_buy_cash: float = 0.0
    total_sell_cash: float = 0.0
    up_buy_cash: float = 0.0
    down_buy_cash: float = 0.0
    hedger_hits: int = 0
    position_total_pnl: float = 0.0
    position_pnl_hits: int = 0
    last_seen_ts: int = 0

    def observe_trade(self, trade: dict[str, Any]) -> None:
        side = str(trade.get("side") or "").upper()
        direction = _norm_direction(trade.get("outcome"))
        condition_id = str(trade.get("conditionId") or "")
        price = _as_float(trade.get("price"))
        size = _as_float(trade.get("size"))
        cash = max(0.0, price * size)
        ts = int(_as_float(trade.get("timestamp"), 0.0))
        self.total_trades += 1
        self.markets.add(condition_id)
        self.last_seen_ts = max(self.last_seen_ts, ts)
        if cash > 0:
            self.trade_sizes.append(cash)
        if side == "BUY":
            self.buy_trades += 1
            self.total_buy_cash += cash
            if direction in {"UP", "DOWN"}:
                self.market_direction_cash[condition_id][direction] += cash
                if direction == "UP":
                    self.up_buy_cash += cash
                else:
                    self.down_buy_cash += cash
        elif side == "SELL":
            self.sell_trades += 1
            self.total_sell_cash += cash

    @property
    def markets_seen(self) -> int:
        return len({market for market in self.markets if market})

    @property
    def avg_trade_cash(self) -> float:
        return mean(self.trade_sizes) if self.trade_sizes else 0.0

    def size_cv(self) -> float | None:
        if len(self.trade_sizes) < 3:
            return None
        avg = self.avg_trade_cash
        if avg <= 0:
            return None
        variance = sum((x - avg) ** 2 for x in self.trade_sizes) / len(self.trade_sizes)
        return (variance ** 0.5) / avg

    def directional_hits(self, *, min_market_cash: float, min_ratio: float) -> int:
        hits = 0
        for cash_by_direction in self.market_direction_cash.values():
            up = cash_by_direction.get("UP", 0.0)
            down = cash_by_direction.get("DOWN", 0.0)
            total = up + down
            if total >= min_market_cash and max(up, down) / total >= min_ratio:
                hits += 1
        return hits

    def mixed_hits(self, *, min_market_cash: float, min_ratio: float) -> int:
        hits = 0
        for cash_by_direction in self.market_direction_cash.values():
            up = cash_by_direction.get("UP", 0.0)
            down = cash_by_direction.get("DOWN", 0.0)
            total = up + down
            if total >= min_market_cash and up > 0 and down > 0 and max(up, down) / total < min_ratio:
                hits += 1
        return hits


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ddl = """
    CREATE TABLE IF NOT EXISTS smart_money_wallets (
        proxy_wallet TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        confidence REAL NOT NULL,
        total_trades INTEGER NOT NULL,
        buy_trades INTEGER NOT NULL,
        sell_trades INTEGER NOT NULL,
        markets_seen INTEGER NOT NULL,
        directional_hits INTEGER NOT NULL,
        mixed_hits INTEGER NOT NULL,
        hedger_hits INTEGER NOT NULL,
        total_buy_cash REAL NOT NULL,
        up_buy_cash REAL NOT NULL,
        down_buy_cash REAL NOT NULL,
        avg_trade_cash REAL NOT NULL,
        size_cv REAL,
        position_total_pnl REAL NOT NULL,
        position_pnl_hits INTEGER NOT NULL,
        last_seen_ts INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        payload_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_smart_money_wallets_label ON smart_money_wallets(label, confidence);
    CREATE INDEX IF NOT EXISTS idx_smart_money_wallets_updated ON smart_money_wallets(updated_at);
    """
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        conn.executescript(ddl)
        conn.commit()


def fetch_markets(client: httpx.Client, gamma_base: str, slugs: list[str]) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    for slug in slugs:
        try:
            response = client.get(
                f"{gamma_base.rstrip('/')}/markets",
                params={
                    "slug": slug,
                    "limit": 1,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list) and payload:
                market = payload[0]
                if isinstance(market, dict):
                    markets.append(market)
        except Exception as exc:
            print(f"gamma skip slug={slug}: {type(exc).__name__}: {exc}")
    return markets


def fetch_trades(
    client: httpx.Client,
    data_base: str,
    condition_id: str,
    *,
    min_cash: float,
    limit: int,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "market": condition_id,
        "takerOnly": "false",
        "limit": max(1, min(10000, int(limit))),
    }
    if min_cash > 0:
        params["filterType"] = "CASH"
        params["filterAmount"] = min_cash
    response = client.get(f"{data_base.rstrip('/')}/trades", params=params)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else []


def fetch_position_hedgers(
    client: httpx.Client,
    data_base: str,
    condition_id: str,
    *,
    limit: int,
    hedge_ratio: float,
) -> tuple[set[str], dict[str, float]]:
    response = client.get(
        f"{data_base.rstrip('/')}/v1/market-positions",
        params={
            "market": condition_id,
            "status": "ALL",
            "sortBy": "TOTAL_PNL",
            "sortDirection": "DESC",
            "limit": max(1, min(500, int(limit))),
        },
    )
    response.raise_for_status()
    payload = response.json()
    exposure_by_wallet: dict[str, dict[str, float]] = defaultdict(lambda: {"UP": 0.0, "DOWN": 0.0})
    pnl_by_wallet: dict[str, float] = defaultdict(float)
    if not isinstance(payload, list):
        return set(), {}
    for token_group in payload:
        if not isinstance(token_group, dict):
            continue
        positions = token_group.get("positions")
        if not isinstance(positions, list):
            continue
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            wallet = str(pos.get("proxyWallet") or "").lower()
            direction = _norm_direction(pos.get("outcome"))
            if not wallet or direction not in {"UP", "DOWN"}:
                continue
            current_value = _as_float(pos.get("currentValue"))
            size = _as_float(pos.get("size"))
            curr_price = _as_float(pos.get("currPrice"))
            exposure = current_value if current_value > 0 else size * curr_price
            exposure_by_wallet[wallet][direction] += max(0.0, exposure)
            pnl_by_wallet[wallet] += _as_float(pos.get("totalPnl"))
    hedgers: set[str] = set()
    for wallet, sides in exposure_by_wallet.items():
        total = sides["UP"] + sides["DOWN"]
        if total > 0 and min(sides["UP"], sides["DOWN"]) / total >= hedge_ratio:
            hedgers.add(wallet)
    return hedgers, dict(pnl_by_wallet)


def classify_wallet(
    stats: WalletStats,
    *,
    min_markets: int,
    min_total_cash: float,
    min_market_cash: float,
    directional_ratio: float,
    min_directional_market_ratio: float,
    bot_size_cv_threshold: float,
    min_bot_trades: int,
) -> tuple[str, float, dict[str, Any]]:
    direction_hits = stats.directional_hits(min_market_cash=min_market_cash, min_ratio=directional_ratio)
    mixed_hits = stats.mixed_hits(min_market_cash=min_market_cash, min_ratio=directional_ratio)
    markets_seen = max(1, stats.markets_seen)
    directional_share = direction_hits / markets_seen
    size_cv = stats.size_cv()
    bot_like = (
        size_cv is not None
        and stats.total_trades >= min_bot_trades
        and size_cv <= bot_size_cv_threshold
    )

    payload = {
        "directional_share": directional_share,
        "directional_hits": direction_hits,
        "mixed_hits": mixed_hits,
        "size_cv": size_cv,
        "position_total_pnl": stats.position_total_pnl,
        "position_pnl_hits": stats.position_pnl_hits,
    }

    if stats.hedger_hits > 0 or mixed_hits >= max(2, direction_hits):
        confidence = min(1.0, 0.5 + 0.1 * stats.hedger_hits + 0.05 * mixed_hits)
        return "HEDGER", confidence, payload
    if bot_like:
        confidence = min(1.0, 1.0 - max(0.0, size_cv or 0.0))
        return "BOT_LIKE", confidence, payload

    enough_markets = stats.markets_seen >= min_markets
    enough_cash = stats.total_buy_cash >= min_total_cash
    directional_enough = directional_share >= min_directional_market_ratio
    pnl_bonus = 0.1 if stats.position_pnl_hits > 0 and stats.position_total_pnl > 0 else 0.0

    if enough_markets and enough_cash and directional_enough:
        market_factor = min(1.0, stats.markets_seen / max(1, min_markets * 2))
        cash_factor = min(1.0, stats.total_buy_cash / max(1.0, min_total_cash * 3))
        confidence = min(1.0, 0.35 * market_factor + 0.45 * directional_share + 0.20 * cash_factor + pnl_bonus)
        return "SMART", confidence, payload
    if direction_hits > 0 and enough_cash:
        confidence = min(0.75, 0.25 + 0.35 * directional_share + 0.15 * min(1.0, stats.total_buy_cash / max(1.0, min_total_cash)))
        return "DIRECTIONAL", confidence, payload
    return "UNKNOWN", 0.0, payload


def write_wallets(
    db_path: Path,
    rows: list[tuple[WalletStats, str, float, dict[str, Any]]],
    *,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    init_db(db_path)
    sql = """
    INSERT INTO smart_money_wallets (
        proxy_wallet, label, confidence, total_trades, buy_trades, sell_trades,
        markets_seen, directional_hits, mixed_hits, hedger_hits, total_buy_cash,
        up_buy_cash, down_buy_cash, avg_trade_cash, size_cv, position_total_pnl,
        position_pnl_hits, last_seen_ts, updated_at, payload_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(proxy_wallet) DO UPDATE SET
        label=excluded.label,
        confidence=excluded.confidence,
        total_trades=excluded.total_trades,
        buy_trades=excluded.buy_trades,
        sell_trades=excluded.sell_trades,
        markets_seen=excluded.markets_seen,
        directional_hits=excluded.directional_hits,
        mixed_hits=excluded.mixed_hits,
        hedger_hits=excluded.hedger_hits,
        total_buy_cash=excluded.total_buy_cash,
        up_buy_cash=excluded.up_buy_cash,
        down_buy_cash=excluded.down_buy_cash,
        avg_trade_cash=excluded.avg_trade_cash,
        size_cv=excluded.size_cv,
        position_total_pnl=excluded.position_total_pnl,
        position_pnl_hits=excluded.position_pnl_hits,
        last_seen_ts=excluded.last_seen_ts,
        updated_at=excluded.updated_at,
        payload_json=excluded.payload_json
    """
    now = _now_ts()
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        for stats, label, confidence, payload in rows:
            conn.execute(
                sql,
                (
                    stats.proxy_wallet,
                    label,
                    confidence,
                    stats.total_trades,
                    stats.buy_trades,
                    stats.sell_trades,
                    stats.markets_seen,
                    int(payload.get("directional_hits") or 0),
                    int(payload.get("mixed_hits") or 0),
                    stats.hedger_hits,
                    stats.total_buy_cash,
                    stats.up_buy_cash,
                    stats.down_buy_cash,
                    stats.avg_trade_cash,
                    payload.get("size_cv"),
                    stats.position_total_pnl,
                    stats.position_pnl_hits,
                    stats.last_seen_ts,
                    now,
                    json.dumps(payload, ensure_ascii=True, sort_keys=True),
                ),
            )
        conn.commit()


def build(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not args.dry_run:
        init_db(db_path)
    slugs = _btc_15m_slugs(args.lookback_intervals, args.lookahead_intervals)
    wallet_stats: dict[str, WalletStats] = {}
    timeout = httpx.Timeout(args.timeout_sec, connect=args.timeout_sec)
    data_base = args.data_api_base.rstrip("/")
    gamma_base = args.gamma_api_base.rstrip("/")

    with httpx.Client(timeout=timeout) as client:
        markets = fetch_markets(client, gamma_base, slugs)
        if args.markets_limit > 0:
            markets = markets[-args.markets_limit :]
        print(f"markets={len(markets)} slugs_scanned={len(slugs)}")
        for idx, market in enumerate(markets, start=1):
            condition_id = str(market.get("conditionId") or market.get("condition_id") or "").strip()
            slug = str(market.get("slug") or "")
            if not condition_id:
                continue
            try:
                trades = fetch_trades(
                    client,
                    data_base,
                    condition_id,
                    min_cash=args.min_cash,
                    limit=args.trades_limit,
                )
            except Exception as exc:
                print(f"[{idx}/{len(markets)}] trades failed slug={slug}: {type(exc).__name__}: {exc}")
                continue
            try:
                hedgers, pnl_by_wallet = fetch_position_hedgers(
                    client,
                    data_base,
                    condition_id,
                    limit=args.positions_limit,
                    hedge_ratio=args.hedge_ratio,
                )
            except Exception as exc:
                hedgers, pnl_by_wallet = set(), {}
                print(f"[{idx}/{len(markets)}] positions failed slug={slug}: {type(exc).__name__}: {exc}")

            seen_wallets: set[str] = set()
            for trade in trades:
                if not isinstance(trade, dict):
                    continue
                wallet = str(trade.get("proxyWallet") or "").lower()
                if not wallet:
                    continue
                stats = wallet_stats.setdefault(wallet, WalletStats(proxy_wallet=wallet))
                stats.observe_trade(trade)
                seen_wallets.add(wallet)
            for wallet in hedgers:
                stats = wallet_stats.setdefault(wallet, WalletStats(proxy_wallet=wallet))
                stats.markets.add(condition_id)
                stats.hedger_hits += 1
            for wallet, pnl in pnl_by_wallet.items():
                if wallet not in seen_wallets and wallet not in hedgers:
                    continue
                stats = wallet_stats.setdefault(wallet, WalletStats(proxy_wallet=wallet))
                stats.position_total_pnl += pnl
                stats.position_pnl_hits += 1
            print(f"[{idx}/{len(markets)}] slug={slug} trades={len(trades)} wallets_total={len(wallet_stats)}")

    rows: list[tuple[WalletStats, str, float, dict[str, Any]]] = []
    counts = Counter()
    for stats in wallet_stats.values():
        label, confidence, payload = classify_wallet(
            stats,
            min_markets=args.min_markets,
            min_total_cash=args.min_total_cash,
            min_market_cash=args.min_market_cash,
            directional_ratio=args.directional_ratio,
            min_directional_market_ratio=args.min_directional_market_ratio,
            bot_size_cv_threshold=args.bot_size_cv_threshold,
            min_bot_trades=args.min_bot_trades,
        )
        rows.append((stats, label, confidence, payload))
        counts[label] += 1
    write_wallets(db_path, rows, dry_run=args.dry_run)
    print(f"wallets={len(rows)} labels={dict(counts)} db={db_path} dry_run={args.dry_run}")
    top = sorted(rows, key=lambda row: (row[1] == "SMART", row[2], row[0].total_buy_cash), reverse=True)[: args.print_top]
    for stats, label, confidence, payload in top:
        print(
            f"{label:11s} conf={confidence:.3f} wallet={stats.proxy_wallet} "
            f"markets={stats.markets_seen} buy_cash={stats.total_buy_cash:.2f} "
            f"up={stats.up_buy_cash:.2f} down={stats.down_buy_cash:.2f} "
            f"dir_share={payload.get('directional_share', 0):.2f} hedger={stats.hedger_hits}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Build offline smart-money wallet labels for BTC 15m markets")
    ap.add_argument("--db", default="./logs/smart_money_wallets.db")
    ap.add_argument("--data-api-base", default="https://data-api.polymarket.com")
    ap.add_argument("--gamma-api-base", default="https://gamma-api.polymarket.com")
    ap.add_argument("--lookback-intervals", type=int, default=96)
    ap.add_argument("--lookahead-intervals", type=int, default=0)
    ap.add_argument("--markets-limit", type=int, default=0)
    ap.add_argument("--min-cash", type=float, default=10.0)
    ap.add_argument("--trades-limit", type=int, default=10000)
    ap.add_argument("--positions-limit", type=int, default=500)
    ap.add_argument("--hedge-ratio", type=float, default=0.25)
    ap.add_argument("--min-markets", type=int, default=3)
    ap.add_argument("--min-total-cash", type=float, default=100.0)
    ap.add_argument("--min-market-cash", type=float, default=20.0)
    ap.add_argument("--directional-ratio", type=float, default=0.80)
    ap.add_argument("--min-directional-market-ratio", type=float, default=0.70)
    ap.add_argument("--bot-size-cv-threshold", type=float, default=0.05)
    ap.add_argument("--min-bot-trades", type=int, default=8)
    ap.add_argument("--timeout-sec", type=float, default=8.0)
    ap.add_argument("--print-top", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    return ap


def main() -> int:
    return build(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
