from __future__ import annotations

import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import httpx
from loguru import logger


_CONDITION_RE = re.compile(r"^(0x[a-fA-F0-9]{64})-([0-9]+)\.POLYMARKET$")


def extract_condition_id_from_instrument_id(instrument_id: Any) -> str:
    match = _CONDITION_RE.match(str(instrument_id or ""))
    return match.group(1) if match else ""


def extract_token_id_from_instrument_id(instrument_id: Any) -> str:
    match = _CONDITION_RE.match(str(instrument_id or ""))
    return match.group(2) if match else ""


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _norm_direction(value: Any) -> str:
    txt = str(value or "").strip().upper()
    if txt in {"UP", "YES"} or "UP" in txt:
        return "UP"
    if txt in {"DOWN", "NO"} or "DOWN" in txt:
        return "DOWN"
    return ""


@dataclass(frozen=True)
class SmartMoneyConfig:
    enabled: bool = False
    shadow_enabled: bool = True
    data_api_base_url: str = "https://data-api.polymarket.com"
    poll_interval_sec: float = 3.0
    request_timeout_sec: float = 2.5
    trades_limit: int = 250
    min_cash_filter: float = 10.0
    recent_window_sec: float = 180.0
    stale_after_sec: float = 12.0
    fomo_cutoff_sec: float = 120.0
    entry_threshold: Decimal = Decimal("0.62")
    min_directional_wallets: int = 2
    conflict_size_multiplier: Decimal = Decimal("0.5")
    skip_strong_conflict: bool = False
    position_refresh_sec: float = 30.0
    position_limit: int = 100
    hedge_ratio: float = 0.25
    bot_size_cv_threshold: float = 0.05
    min_wallet_trades: int = 3
    directional_min_cash: float = 20.0
    wallet_db_path: str = "./logs/smart_money_wallets.db"
    wallet_label_cache_ttl_sec: float = 60.0
    weight_smart: float = 2.0
    weight_directional: float = 1.0
    weight_unknown: float = 0.25


@dataclass(frozen=True)
class SmartMoneySignal:
    state: str
    action: str
    active_side: str
    direction: Optional[str]
    score: Decimal
    reason: str
    shadow_only: bool
    features: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        payload = {
            "state": self.state,
            "action": self.action,
            "active_side": self.active_side,
            "direction": self.direction,
            "score": float(self.score),
            "reason": self.reason,
            "shadow_only": bool(self.shadow_only),
        }
        payload.update(self.features)
        return payload


@dataclass(frozen=True)
class _TradeEvent:
    proxy_wallet: str
    side: str
    direction: str
    asset: str
    size: float
    price: float
    usdc_size: float
    timestamp: int
    transaction_hash: str


@dataclass
class _MarketCache:
    condition_id: str
    slug: str = ""
    token_direction_by_asset: dict[str, str] | None = None
    trades: list[_TradeEvent] | None = None
    hedger_wallets: set[str] | None = None
    updated_ts: float = 0.0
    positions_updated_ts: float = 0.0
    last_error: str = ""


@dataclass(frozen=True)
class WalletLabel:
    label: str
    confidence: float
    updated_at: int


class WalletLabelStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path or "")

    def load_labels(self, wallets: set[str]) -> dict[str, WalletLabel]:
        if not self.db_path or not wallets:
            return {}
        path = Path(self.db_path)
        if not path.exists():
            return {}
        wallet_list = sorted({str(wallet or "").lower() for wallet in wallets if wallet})
        if not wallet_list:
            return {}
        out: dict[str, WalletLabel] = {}
        try:
            with sqlite3.connect(str(path), timeout=2) as conn:
                conn.row_factory = sqlite3.Row
                for i in range(0, len(wallet_list), 250):
                    chunk = wallet_list[i : i + 250]
                    placeholders = ",".join("?" for _ in chunk)
                    rows = conn.execute(
                        f"""
                        SELECT proxy_wallet, label, confidence, updated_at
                        FROM smart_money_wallets
                        WHERE proxy_wallet IN ({placeholders})
                        """,
                        chunk,
                    ).fetchall()
                    for row in rows:
                        wallet = str(row["proxy_wallet"] or "").lower()
                        label = str(row["label"] or "UNKNOWN").upper()
                        out[wallet] = WalletLabel(
                            label=label,
                            confidence=_as_float(row["confidence"], 0.0),
                            updated_at=int(_as_float(row["updated_at"], 0.0)),
                        )
        except Exception as exc:
            logger.debug(f"Smart money wallet label load failed: {exc}")
        return out


class SmartMoneyTracker:
    """
    Background Data API poller for market-level directional flow.

    The strategy hot path only calls evaluate(), which reads a local cache.
    Network calls happen on this daemon thread so quote refresh is not blocked
    by Data API latency.
    """

    def __init__(self, config: SmartMoneyConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._markets: dict[str, _MarketCache] = {}
        self._active_condition_id = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._wallet_store = WalletLabelStore(config.wallet_db_path)
        self._wallet_label_cache: dict[str, tuple[WalletLabel | None, float]] = {}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="smart-money-tracker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def watch_market(
        self,
        *,
        condition_id: str,
        slug: str,
        up_token_id: str = "",
        down_token_id: str = "",
    ) -> None:
        condition_id = str(condition_id or "").strip()
        if not condition_id:
            return
        token_map: dict[str, str] = {}
        if up_token_id:
            token_map[str(up_token_id)] = "UP"
        if down_token_id:
            token_map[str(down_token_id)] = "DOWN"
        with self._lock:
            cache = self._markets.get(condition_id)
            if cache is None:
                cache = _MarketCache(condition_id=condition_id)
                self._markets[condition_id] = cache
            cache.slug = str(slug or cache.slug or "")
            cache.token_direction_by_asset = token_map or cache.token_direction_by_asset or {}
            self._active_condition_id = condition_id
        self.start()

    def evaluate(
        self,
        *,
        condition_id: str,
        active_side: str,
        market_end_ts: Optional[float],
        now_ts: Optional[float] = None,
    ) -> SmartMoneySignal:
        now = float(now_ts if now_ts is not None else time.time())
        side = str(active_side or "").upper()
        condition_id = str(condition_id or "").strip()
        if side not in {"UP", "DOWN"} or not condition_id:
            return self._signal(
                state="unavailable",
                action="observe",
                active_side=side or "NONE",
                direction=None,
                score=Decimal("0"),
                reason="missing_side_or_condition",
                features={},
            )
        if market_end_ts is not None and (float(market_end_ts) - now) < self.config.fomo_cutoff_sec:
            return self._signal(
                state="unavailable",
                action="observe",
                active_side=side,
                direction=None,
                score=Decimal("0"),
                reason="fomo_cutoff_window",
                features={"time_left_sec": max(0.0, float(market_end_ts) - now)},
            )

        with self._lock:
            cache = self._markets.get(condition_id)
            trades = list(cache.trades or []) if cache is not None else []
            hedgers = set(cache.hedger_wallets or set()) if cache is not None else set()
            updated_ts = float(cache.updated_ts if cache is not None else 0.0)
            last_error = str(cache.last_error if cache is not None else "")

        age = now - updated_ts if updated_ts > 0 else None
        if not trades:
            return self._signal(
                state="unavailable",
                action="observe",
                active_side=side,
                direction=None,
                score=Decimal("0"),
                reason="no_cached_trades",
                features={"cache_age_sec": age, "last_error": last_error},
            )
        if age is None or age > self.config.stale_after_sec:
            return self._signal(
                state="stale",
                action="observe",
                active_side=side,
                direction=None,
                score=Decimal("0"),
                reason="stale_cache",
                features={"cache_age_sec": age, "last_error": last_error},
            )

        cutoff = int(now - self.config.recent_window_sec)
        recent = [t for t in trades if t.timestamp >= cutoff and t.side == "BUY" and t.direction in {"UP", "DOWN"}]
        return self._compute_signal(
            recent=recent,
            hedgers=hedgers,
            active_side=side,
            cache_age_sec=age,
            last_error=last_error,
        )

    def _signal(
        self,
        *,
        state: str,
        action: str,
        active_side: str,
        direction: Optional[str],
        score: Decimal,
        reason: str,
        features: dict[str, Any],
    ) -> SmartMoneySignal:
        return SmartMoneySignal(
            state=state,
            action=action if self.config.enabled else "observe",
            active_side=active_side,
            direction=direction,
            score=score,
            reason=reason,
            shadow_only=not self.config.enabled,
            features=features,
        )

    def _compute_signal(
        self,
        *,
        recent: list[_TradeEvent],
        hedgers: set[str],
        active_side: str,
        cache_age_sec: Optional[float],
        last_error: str,
    ) -> SmartMoneySignal:
        wallet_events: dict[str, list[_TradeEvent]] = defaultdict(list)
        for trade in recent:
            if trade.proxy_wallet:
                wallet_events[trade.proxy_wallet].append(trade)
        offline_labels = self._load_offline_labels(set(wallet_events.keys()))

        weighted_cash = defaultdict(float)
        directional_wallets = Counter()
        label_counts = Counter()
        raw_cash = defaultdict(float)

        for wallet, events in wallet_events.items():
            label = self._classify_wallet(
                wallet=wallet,
                events=events,
                hedgers=hedgers,
                offline_label=offline_labels.get(wallet),
            )
            label_counts[label] += 1
            if label in {"HEDGER", "BOT_LIKE", "NOISE"}:
                continue
            weight = self._label_weight(label)
            by_direction = defaultdict(float)
            for event in events:
                by_direction[event.direction] += event.usdc_size
            for direction, cash in by_direction.items():
                raw_cash[direction] += cash
                weighted_cash[direction] += cash * weight
            if label in {"SMART", "DIRECTIONAL"}:
                for direction, cash in by_direction.items():
                    if cash >= self.config.directional_min_cash:
                        directional_wallets[direction] += 1

        total = weighted_cash["UP"] + weighted_cash["DOWN"]
        features = {
            "cache_age_sec": cache_age_sec,
            "last_error": last_error,
            "recent_trade_count": len(recent),
            "wallet_count": len(wallet_events),
            "hedger_wallet_count": len(hedgers),
            "label_counts": dict(label_counts),
            "offline_label_count": sum(1 for label in offline_labels.values() if label is not None),
            "raw_cash_up": raw_cash["UP"],
            "raw_cash_down": raw_cash["DOWN"],
            "weighted_cash_up": weighted_cash["UP"],
            "weighted_cash_down": weighted_cash["DOWN"],
            "directional_wallets_up": int(directional_wallets["UP"]),
            "directional_wallets_down": int(directional_wallets["DOWN"]),
        }
        if total <= 0:
            return self._signal(
                state="neutral",
                action="observe",
                active_side=active_side,
                direction=None,
                score=Decimal("0"),
                reason="no_weighted_directional_flow",
                features=features,
            )

        up_score = weighted_cash["UP"] / total
        down_score = weighted_cash["DOWN"] / total
        direction = "UP" if up_score >= down_score else "DOWN"
        score_f = up_score if direction == "UP" else down_score
        score = Decimal(str(round(score_f, 6)))
        enough_wallets = directional_wallets[direction] >= self.config.min_directional_wallets
        threshold_met = score >= self.config.entry_threshold

        state = "support" if direction == active_side and threshold_met and enough_wallets else "neutral"
        reason = "directional_flow_supports_active_side" if state == "support" else "below_threshold_or_wallet_count"
        action = "observe"
        if direction != active_side and threshold_met and enough_wallets:
            state = "conflict"
            reason = "directional_flow_conflicts_active_side"
            action = "skip" if self.config.skip_strong_conflict else "reduce_size"

        return self._signal(
            state=state,
            action=action,
            active_side=active_side,
            direction=direction,
            score=score,
            reason=reason,
            features=features,
        )

    def _classify_wallet(
        self,
        *,
        wallet: str,
        events: list[_TradeEvent],
        hedgers: set[str],
        offline_label: WalletLabel | None = None,
    ) -> str:
        if wallet in hedgers:
            return "HEDGER"
        if offline_label is not None:
            label = str(offline_label.label or "UNKNOWN").upper()
            if label in {"SMART", "DIRECTIONAL", "HEDGER", "BOT_LIKE", "NOISE"}:
                return label
        if len(events) < self.config.min_wallet_trades:
            return "UNKNOWN"
        sizes = [event.usdc_size for event in events if event.usdc_size > 0]
        if len(sizes) >= max(3, self.config.min_wallet_trades):
            mean_size = sum(sizes) / len(sizes)
            if mean_size > 0:
                variance = sum((size - mean_size) ** 2 for size in sizes) / len(sizes)
                cv = (variance ** 0.5) / mean_size
                if cv < self.config.bot_size_cv_threshold:
                    return "BOT_LIKE"
        directions = {event.direction for event in events if event.direction}
        total_cash = sum(event.usdc_size for event in events)
        if len(directions) == 1 and total_cash >= self.config.directional_min_cash:
            return "DIRECTIONAL"
        return "UNKNOWN"

    def _label_weight(self, label: str) -> float:
        label = str(label or "UNKNOWN").upper()
        if label == "SMART":
            return max(0.0, float(self.config.weight_smart))
        if label == "DIRECTIONAL":
            return max(0.0, float(self.config.weight_directional))
        if label in {"HEDGER", "BOT_LIKE", "NOISE"}:
            return 0.0
        return max(0.0, float(self.config.weight_unknown))

    def _load_offline_labels(self, wallets: set[str]) -> dict[str, WalletLabel | None]:
        now = time.time()
        ttl = max(1.0, float(self.config.wallet_label_cache_ttl_sec))
        needed: set[str] = set()
        labels: dict[str, WalletLabel | None] = {}
        with self._lock:
            for wallet in wallets:
                cached = self._wallet_label_cache.get(wallet)
                if cached is None or (now - cached[1]) > ttl:
                    needed.add(wallet)
                else:
                    labels[wallet] = cached[0]
        loaded = self._wallet_store.load_labels(needed)
        if needed:
            with self._lock:
                for wallet in needed:
                    label = loaded.get(wallet)
                    self._wallet_label_cache[wallet] = (label, now)
                    labels[wallet] = label
        return labels

    def _run(self) -> None:
        base_url = self.config.data_api_base_url.rstrip("/")
        timeout = httpx.Timeout(self.config.request_timeout_sec, connect=self.config.request_timeout_sec)
        with httpx.Client(base_url=base_url, timeout=timeout) as client:
            while not self._stop.is_set():
                condition_id = ""
                with self._lock:
                    condition_id = self._active_condition_id
                if condition_id:
                    self._refresh_market(client=client, condition_id=condition_id)
                self._stop.wait(max(0.5, float(self.config.poll_interval_sec)))

    def _refresh_market(self, *, client: httpx.Client, condition_id: str) -> None:
        with self._lock:
            cache = self._markets.get(condition_id)
            token_map = dict(cache.token_direction_by_asset or {}) if cache is not None else {}
            positions_due = (
                cache is None
                or (time.time() - float(cache.positions_updated_ts or 0.0)) >= self.config.position_refresh_sec
            )
        try:
            trades = self._fetch_trades(client=client, condition_id=condition_id, token_map=token_map)
            hedgers: set[str] | None = None
            if positions_due:
                hedgers = self._fetch_hedgers(client=client, condition_id=condition_id)
            with self._lock:
                cache = self._markets.setdefault(condition_id, _MarketCache(condition_id=condition_id))
                cache.trades = trades
                cache.updated_ts = time.time()
                cache.last_error = ""
                if hedgers is not None:
                    cache.hedger_wallets = hedgers
                    cache.positions_updated_ts = time.time()
        except Exception as exc:
            logger.debug(f"Smart money refresh failed condition={condition_id[:16]}...: {exc}")
            with self._lock:
                cache = self._markets.setdefault(condition_id, _MarketCache(condition_id=condition_id))
                cache.last_error = f"{type(exc).__name__}: {exc}"

    def _fetch_trades(
        self,
        *,
        client: httpx.Client,
        condition_id: str,
        token_map: dict[str, str],
    ) -> list[_TradeEvent]:
        params: dict[str, Any] = {
            "market": condition_id,
            "side": "BUY",
            "takerOnly": "false",
            "limit": max(1, min(10000, int(self.config.trades_limit))),
        }
        if self.config.min_cash_filter > 0:
            params["filterType"] = "CASH"
            params["filterAmount"] = self.config.min_cash_filter
        response = client.get("/trades", params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []
        out: list[_TradeEvent] = []
        seen: set[tuple[str, str, int]] = set()
        for item in payload:
            if not isinstance(item, dict):
                continue
            direction = _norm_direction(item.get("outcome"))
            asset = str(item.get("asset") or "")
            if not direction and asset:
                direction = token_map.get(asset, "")
            if direction not in {"UP", "DOWN"}:
                continue
            price = _as_float(item.get("price"))
            size = _as_float(item.get("size"))
            usdc_size = price * size
            ts = int(_as_float(item.get("timestamp"), 0.0))
            tx = str(item.get("transactionHash") or "")
            wallet = str(item.get("proxyWallet") or "").lower()
            key = (wallet, tx, ts)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                _TradeEvent(
                    proxy_wallet=wallet,
                    side=str(item.get("side") or "").upper(),
                    direction=direction,
                    asset=asset,
                    size=size,
                    price=price,
                    usdc_size=usdc_size,
                    timestamp=ts,
                    transaction_hash=tx,
                )
            )
        return out

    def _fetch_hedgers(self, *, client: httpx.Client, condition_id: str) -> set[str]:
        response = client.get(
            "/v1/market-positions",
            params={
                "market": condition_id,
                "status": "OPEN",
                "sortBy": "TOTAL_PNL",
                "sortDirection": "DESC",
                "limit": max(1, min(500, int(self.config.position_limit))),
            },
        )
        response.raise_for_status()
        payload = response.json()
        by_wallet: dict[str, dict[str, float]] = defaultdict(lambda: {"UP": 0.0, "DOWN": 0.0})
        if not isinstance(payload, list):
            return set()
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
                by_wallet[wallet][direction] += max(0.0, exposure)
        hedgers: set[str] = set()
        for wallet, sides in by_wallet.items():
            up = sides["UP"]
            down = sides["DOWN"]
            total = up + down
            if total <= 0:
                continue
            if min(up, down) / total >= self.config.hedge_ratio:
                hedgers.add(wallet)
        return hedgers


def apply_smart_money_adjustment(
    *,
    desired_entry: dict[str, Any],
    side: str,
    signal: SmartMoneySignal,
    config: SmartMoneyConfig,
) -> dict[str, Any]:
    if side != "buy":
        return desired_entry
    desired_entry["smart_money_confirmation"] = signal.as_payload()
    if not config.enabled or not desired_entry.get("should_quote", False):
        return desired_entry
    if signal.action == "skip":
        desired_entry["should_quote"] = False
        desired_entry["diag_reason"] = "smart_money_confirmation_skip"
        return desired_entry
    if signal.action == "reduce_size":
        prior_multiplier = Decimal(str(desired_entry.get("size_multiplier", Decimal("1")) or "1"))
        adjusted_multiplier = max(Decimal("0"), prior_multiplier * config.conflict_size_multiplier)
        desired_entry["size_multiplier"] = adjusted_multiplier
        desired_entry["smart_money_size_adjustment"] = {
            "prior_size_multiplier": prior_multiplier,
            "adjusted_size_multiplier": adjusted_multiplier,
            "multiplier": config.conflict_size_multiplier,
            "state": signal.state,
            "direction": signal.direction,
            "score": signal.score,
        }
        diag_reason = str(desired_entry.get("diag_reason", "") or "")
        adjustment_reason = (
            "smart_money_confirmation_reduce "
            f"state={signal.state} direction={signal.direction} "
            f"score={float(signal.score):.3f} "
            f"mult={float(prior_multiplier):.3f}->{float(adjusted_multiplier):.3f}"
        )
        desired_entry["diag_reason"] = (
            f"{diag_reason}; {adjustment_reason}" if diag_reason else adjustment_reason
        )
    return desired_entry
