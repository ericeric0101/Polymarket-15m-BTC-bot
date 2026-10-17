"""Live (real-money) order path for the TWAP-z late-window executor -- P2 micro-live.

Spec: reports/twap_z_late_window_executor_20261016/spec.md section 3 and 5 (CK-confirmed caps):
FAK limit BUY at the decision-time best ask, 5.5 shares, hold to settlement, no SELL, no retry.
Caps are conservative: every unsettled position counts as a full loss of its cost until its
official outcome is known, so the daily/total caps can never be overrun by open risk.

Secrets are read from the environment by the caller and never logged here.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

LIVE_CONFIRM_ENV = "TWAP_Z_LIVE_CONFIRM"
LIVE_CONFIRM_VALUE = "REAL_MONEY"


@dataclass
class LiveConfig:
    shares: float = 5.5
    max_daily_loss: float = 15.0        # USDC, per UTC day
    max_total_loss: float = 50.0        # USDC, since the live log started
    streams: tuple[str, ...] = ()       # empty = all streams
    out_dir: Path = Path("data/twap_z_executor_live")

    @property
    def stop_file(self) -> Path:
        return Path(self.out_dir) / "STOP"


@dataclass
class Position:
    market_slug: str
    side: str
    checkpoint: int
    end_ts: float
    cost_usdc: float
    shares: float
    order_id: Optional[str] = None
    outcome: Optional[str] = None       # official UP / DOWN once known
    pnl: Optional[float] = None


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


@dataclass
class RiskBook:
    """Positions and caps. Thread-safe; persisted as JSONL so a restart keeps the history."""
    config: LiveConfig
    positions: list[Position] = field(default_factory=list)
    pending: dict[str, float] = field(default_factory=dict)   # slug -> reserved worst-case cost
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def path(self) -> Path:
        return Path(self.config.out_dir) / "positions.jsonl"

    def load(self) -> None:
        if self.path.exists():
            latest: dict[str, Position] = {}
            for line in self.path.read_text().splitlines():
                if line.strip():
                    p = Position(**json.loads(line))
                    latest[p.market_slug] = p
            self.positions = list(latest.values())

    def _persist(self, p: Position) -> None:
        Path(self.config.out_dir).mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as fh:
            fh.write(json.dumps(asdict(p)) + "\n")

    def add(self, p: Position) -> None:
        with self._lock:
            self.positions.append(p)
            self._persist(p)

    def settle(self, slug: str, outcome: str) -> Optional[Position]:
        with self._lock:
            for p in self.positions:
                if p.market_slug == slug and p.outcome is None and outcome in ("UP", "DOWN"):
                    p.outcome = outcome
                    win = (p.side == "up") == (outcome == "UP")
                    p.pnl = round((p.shares if win else 0.0) - p.cost_usdc, 6)
                    self._persist(p)
                    return p
        return None

    def open_positions(self) -> list[Position]:
        with self._lock:
            return [p for p in self.positions if p.outcome is None]

    def traded(self, slug: str) -> bool:
        with self._lock:
            return slug in self.pending or any(p.market_slug == slug for p in self.positions)

    def release(self, slug: str) -> None:
        with self._lock:
            self.pending.pop(slug, None)

    def worst_case(self, now: float) -> tuple[float, float]:
        """(today, total) PnL counting every unsettled position as a total loss of its cost."""
        today = _utc_day(now)
        with self._lock:
            settled_today = sum(p.pnl for p in self.positions if p.pnl is not None and _utc_day(p.end_ts) == today)
            settled_total = sum(p.pnl for p in self.positions if p.pnl is not None)
            open_cost = sum(p.cost_usdc for p in self.positions if p.outcome is None) + sum(self.pending.values())
        return settled_today - open_cost, settled_total - open_cost

    def reserve(self, slug: str, now: float, est_cost: float) -> tuple[bool, str]:
        """Check every gate and, if all pass, reserve est_cost for slug atomically.

        Simultaneous decisions (several streams share a checkpoint second) therefore can
        never jointly overrun a cap. The caller must later call add() and/or release().
        """
        if self.config.stop_file.exists():
            return False, "STOP_FILE"
        if self.config.streams and slug.rsplit("-", 1)[0] not in self.config.streams:
            return False, "STREAM_NOT_ENABLED"
        if self.traded(slug):
            return False, "ALREADY_TRADED_MARKET"
        today, total = self.worst_case(now)
        with self._lock:
            if today - est_cost < -self.config.max_daily_loss:
                return False, "DAILY_LOSS_CAP"
            if total - est_cost < -self.config.max_total_loss:
                return False, "TOTAL_LOSS_CAP"
            self.pending[slug] = float(est_cost)
        return True, "OK"


def _num(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class LiveOrderClient:
    """Thin wrapper over py_clob_client_v2 for one order type: FAK limit BUY."""

    def __init__(self, client: Any, signature_type: int = -1, builder_code: Optional[str] = None) -> None:
        self.client = client
        self.signature_type = signature_type
        self.builder_code = builder_code

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "LiveOrderClient":
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds

        host = env.get("POLYMARKET_CLOB_BASE_URL", "https://clob.polymarket.com")
        funder = env.get("POLYMARKET_FUNDER") or env.get("POLYMARKET_WALLET_ADDRESS") or env.get("WALLET_ADDRESS")
        client = ClobClient(host, int(env.get("POLYMARKET_CHAIN_ID", "137")), key=env["POLYMARKET_PK"],
                            signature_type=int(env.get("POLYMARKET_SIGNATURE_TYPE", "0")), funder=funder or None)
        if env.get("POLYMARKET_API_KEY") and env.get("POLYMARKET_API_SECRET") and env.get("POLYMARKET_PASSPHRASE"):
            client.set_api_creds(ApiCreds(api_key=env["POLYMARKET_API_KEY"], api_secret=env["POLYMARKET_API_SECRET"],
                                          api_passphrase=env["POLYMARKET_PASSPHRASE"]))
        else:
            client.set_api_creds(client.create_or_derive_api_key())
        return cls(client, signature_type=int(env.get("POLYMARKET_SIGNATURE_TYPE", "0")),
                   builder_code=env.get("POLY_BUILDER_CODE") or None)

    def collateral_balance(self) -> Optional[float]:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
        res = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL,
                                                                       signature_type=self.signature_type))
        bal = _num((res or {}).get("balance"))
        return bal / 1e6 if bal is not None else None

    def prefetch(self, token_id: str) -> None:
        """Cache tick size / neg-risk so create_order does no network I/O at decision time."""
        self.client.get_tick_size(token_id)
        self.client.get_neg_risk(token_id)

    def warm_up(self, token_id: str, price: float, size: float) -> None:
        """Sign (never post) one order: caches order version, tick size and neg-risk."""
        self.client.create_order(self._args(token_id, price, size))

    def _args(self, token_id: str, price: float, size: float):
        from py_clob_client_v2.clob_types import OrderArgs
        from py_clob_client_v2.order_builder.constants import BUY
        args = OrderArgs(token_id=token_id, price=float(price), size=float(size), side=BUY)
        if self.builder_code:
            args.builder_code = self.builder_code
        return args

    def buy_fak(self, token_id: str, price: float, size: float) -> dict[str, Any]:
        from py_clob_client_v2.clob_types import OrderType
        t0 = time.time()
        out: dict[str, Any] = {"t_submit_start": t0, "limit_price": price, "size": size}
        try:
            signed = self.client.create_order(self._args(token_id, price, size))
            out["t_signed"] = time.time()
            res = self.client.post_order(signed, order_type=OrderType.FAK)
            out["t_posted"] = time.time()
            res = res if isinstance(res, dict) else {"raw": str(res)}
            out.update({"ok": bool(res.get("success", True)) and not res.get("errorMsg"),
                        "order_id": res.get("orderID") or res.get("orderId"),
                        "status": res.get("status"), "error": res.get("errorMsg") or None,
                        "making_amount": _num(res.get("makingAmount")), "taking_amount": _num(res.get("takingAmount")),
                        "response": {k: v for k, v in res.items() if k not in ("owner",)}})
        except Exception as exc:          # rejected / network: no position assumed
            out.update({"ok": False, "t_posted": time.time(), "error": f"{type(exc).__name__}: {exc}"[:300]})
        out["submit_ms"] = round((out["t_posted"] - t0) * 1000, 1)
        return out


def fill_from_response(order: dict[str, Any], limit_price: float) -> tuple[float, float]:
    """(usdc_spent, shares_received) from a BUY FAK response; (0, 0) if nothing matched.

    For a BUY, makingAmount is the collateral given and takingAmount the shares received.
    A response without amounts but status 'matched' is booked conservatively at the full
    requested size and limit price (worst case for the caps).
    """
    if not order.get("ok"):
        return 0.0, 0.0
    making, taking = order.get("making_amount"), order.get("taking_amount")
    if making is not None and taking is not None and taking > 0:
        return float(making), float(taking)
    if str(order.get("status") or "").lower() in ("matched", "delayed", "mined", "confirmed"):
        return float(order["size"]) * float(limit_price), float(order["size"])
    return 0.0, 0.0


class OutcomeWatcher(threading.Thread):
    """Settles open positions from official Gamma outcomes (polled once a minute)."""

    def __init__(self, book: RiskBook, fetch_outcome: Callable[[str], Optional[str]], stop: threading.Event,
                 on_settle: Callable[[Position], None] = lambda p: None, poll_sec: float = 60.0) -> None:
        super().__init__(name="outcome-watcher", daemon=True)
        self.book, self.fetch_outcome, self.stop_event = book, fetch_outcome, stop
        self.on_settle, self.poll_sec = on_settle, poll_sec

    def poll_once(self, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        n = 0
        for p in self.book.open_positions():
            if now < p.end_ts + 60:
                continue
            try:
                outcome = self.fetch_outcome(p.market_slug)
            except Exception:
                outcome = None
            if outcome in ("UP", "DOWN"):
                settled = self.book.settle(p.market_slug, outcome)
                if settled is not None:
                    self.on_settle(settled)
                    n += 1
        return n

    def run(self) -> None:
        while not self.stop_event.wait(self.poll_sec):
            self.poll_once()
