"""Live decision core for the TWAP-z late-window executor.

Applies to every Up/Down market settled on the Chainlink 60 s TWAP (BTC/ETH/SOL/XRP,
5m and 15m): the settlement window is the last 60 s regardless of market length.

The rule is plan_v2 sections 2-4 of reports/twap_z_forward_shadow_20261015 (ZC variant):
at T-55, T-50 ... T-5 evaluate z on live Chainlink data; take the first checkpoint per
market with 0.10 <= mid <= 0.90, |z| >= 2 and a valid book on the z side, and buy that
side at its best ask. This module re-implements, on live in-memory state, the as-of
mechanics of ``recorder_mechanics.ChainlinkSeries`` (same window, strike, required
average, sigma and spot freshness) so live decisions can be checked against the
recorder shadow. No I/O and no order placement here.
"""
from __future__ import annotations

import math
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from bot.research.price_recorder import BookSampler, slug_duration_sec

MARKET_SEC = 900
TWAP_WINDOW = 60
WINDOW_OFFSET = -62                 # TWAP stamped at S = mean of spot seconds [S-62, S-3]
CHECKPOINTS = tuple(range(55, 0, -5))
YR_SEC = 365 * 86400
YR3 = 3 * YR_SEC
SPOT_MAX_AGE_SEC = 3.0
MAX_MISSING_FILLED = 3
SIGMA_LOOKBACK = 300
SIGMA_MIN_RETURNS = 240
MID_LO, MID_HI = 0.10, 0.90
Z_MIN = 2.0
FEE_COEF = 0.07                      # fee = 0.07 * p * (1 - p) per share (taker)
KEEP_SEC = 1800


class LiveChainlink:
    """Chainlink spot / TWAP keyed by Chainlink source second, with local receipt times."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spot: dict[int, tuple[float, float]] = {}   # sec -> (price, received_ts)
        self._twap: dict[int, tuple[float, float]] = {}

    def observe(self, kind: str, price: float, received_ts: float, source_ms: Optional[int]) -> None:
        if source_ms is None or source_ms <= 0 or not (price > 0):
            return
        sec = int(source_ms // 1000)
        store = self._spot if kind == "spot" else self._twap
        with self._lock:
            store[sec] = (float(price), float(received_ts))   # last tick of a source second wins
            if len(store) > KEEP_SEC + 120 and sec % 60 == 0:
                cutoff = sec - KEEP_SEC
                for s in [s for s in store if s < cutoff]:
                    del store[s]

    def _known_spot(self, now: float) -> dict[int, float]:
        with self._lock:
            return {s: p for s, (p, r) in self._spot.items() if r <= now}

    def strike(self, market_start: int, now: float) -> Optional[float]:
        with self._lock:
            hit = self._twap.get(int(market_start))
        return hit[0] if hit is not None and hit[1] <= now else None

    def spot_asof(self, now: float, max_age: float = SPOT_MAX_AGE_SEC) -> tuple[Optional[float], Optional[float]]:
        """(latest received spot, its receipt age) or (None, age) when stale."""
        with self._lock:
            known = [(r, s, p) for s, (p, r) in self._spot.items() if r <= now]
        if not known:
            return None, None
        r, _s, p = max(known)
        age = now - r
        return (p if age <= max_age else None), age

    def last_received_ts(self) -> tuple[Optional[float], Optional[float]]:
        with self._lock:
            spot = max((r for _p, r in self._spot.values()), default=None)
            twap = max((r for _p, r in self._twap.values()), default=None)
        return spot, twap

    @staticmethod
    def window(market_end: int) -> range:
        first = int(market_end) + WINDOW_OFFSET
        return range(first, first + TWAP_WINDOW)

    def required_future_avg(self, market_end: int, strike: float, now: float,
                            known: Optional[dict[int, float]] = None) -> tuple[Optional[float], int, int]:
        """Same semantics as ChainlinkSeries.required_future_avg (forward-fill, <= 3 gaps)."""
        known = self._known_spot(now) if known is None else known
        if not known:
            return None, 0, 0
        last_s = max(known)
        win = list(self.window(market_end))
        observed = [s for s in win if s <= last_s]
        remaining = len(win) - len(observed)
        if remaining <= 0:
            return None, 0, 0
        missing = sum(1 for s in observed if s not in known)
        filled: dict[int, float] = {}
        prev = None
        for s in range(min(win[0], last_s) - 5, last_s + 1):
            if s in known:
                prev = known[s]
            filled[s] = prev
        vals = [filled.get(s) for s in observed]
        if missing > MAX_MISSING_FILLED or any(v is None for v in vals):
            return None, remaining, missing
        need = (TWAP_WINDOW * strike - sum(vals)) / remaining
        return need, remaining, missing

    def sigma(self, now: float, known: Optional[dict[int, float]] = None) -> Optional[float]:
        """Annualised std of 1 s log returns over the trailing 300 s (pairs of consecutive seconds)."""
        known = self._known_spot(now) if known is None else known
        if not known:
            return None
        last_s = max(known)
        rets = []
        for s in range(last_s - SIGMA_LOOKBACK + 1, last_s + 1):
            a, b = known.get(s - 1), known.get(s)
            if a is not None and b is not None and a > 0 and b > 0:
                rets.append(math.log(b / a))
        if len(rets) < SIGMA_MIN_RETURNS:
            return None
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((x - mean) ** 2 for x in rets) / (len(rets) - 1))
        return sd * math.sqrt(YR_SEC) if sd > 0 else None


class LiveBookSampler(BookSampler):
    """BookSampler plus a live best-bid/ask read for one token."""

    def best(self, token: str) -> Optional[dict[str, Any]]:
        with self._lock:
            book = self._books.get(token)
            if book is None:
                return None
            bids, asks = book["bids"], book["asks"]
            bb = max(bids) if bids else None
            ba = min(asks) if asks else None
            return {"best_bid": bb, "best_ask": ba,
                    "bid_size": bids.get(bb) if bb is not None else None,
                    "ask_size": asks.get(ba) if ba is not None else None,
                    "last_update_received_ms": book.get("last_ms")}

    def market_tokens(self, slug: str) -> dict[str, str]:
        with self._lock:
            return {m["outcome"]: t for t, m in self._meta.items() if m["slug"] == slug}


def book_valid(b: Optional[dict[str, Any]]) -> bool:
    if not b:
        return False
    bid, ask = b.get("best_bid"), b.get("best_ask")
    return bid is not None and ask is not None and 0 <= bid <= ask and 0.001 <= ask <= 1


@dataclass
class Decision:
    market_slug: str
    checkpoint: int
    scheduled_ts: float
    eval_ts: float
    strike: Optional[float] = None
    required_future_avg_to_flip: Optional[float] = None
    remaining_final_window_sec: int = 0
    observed_missing_filled: int = 0
    sigma: Optional[float] = None
    spot: Optional[float] = None
    spot_age_sec: Optional[float] = None
    twap_age_sec: Optional[float] = None
    up: Optional[dict[str, Any]] = None
    down: Optional[dict[str, Any]] = None
    mid: Optional[float] = None
    z: Optional[float] = None
    side: Optional[str] = None
    ask: Optional[float] = None
    ask_size: Optional[float] = None
    eligible: bool = False
    action: str = "SKIP"
    reasons: list[str] = field(default_factory=list)
    compute_ms: Optional[float] = None

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_checkpoint(*, chainlink: LiveChainlink, books: LiveBookSampler, slug: str, checkpoint: int,
                        scheduled_ts: float, now: float, feed_ok: dict[str, bool],
                        already_selected: bool) -> Decision:
    """One plan_v2 grid row on live state, plus the executor's fail-closed reasons."""
    start = int(slug.rsplit("-", 1)[-1])
    end = start + slug_duration_sec(slug)
    d = Decision(market_slug=slug, checkpoint=checkpoint, scheduled_ts=scheduled_ts, eval_ts=now)
    known = chainlink._known_spot(now)
    d.strike = chainlink.strike(start, now)
    if d.strike is not None:
        d.required_future_avg_to_flip, d.remaining_final_window_sec, d.observed_missing_filled = \
            chainlink.required_future_avg(end, d.strike, now, known)
    d.sigma = chainlink.sigma(now, known)
    d.spot, d.spot_age_sec = chainlink.spot_asof(now)
    _spot_rcv, twap_rcv = chainlink.last_received_ts()
    d.twap_age_sec = (now - twap_rcv) if twap_rcv is not None else None
    tokens = books.market_tokens(slug)
    d.up = books.best(tokens["Up"]) if "Up" in tokens else None
    d.down = books.best(tokens["Down"]) if "Down" in tokens else None
    vu, vd = book_valid(d.up), book_valid(d.down)
    if vu:
        d.mid = (d.up["best_bid"] + d.up["best_ask"]) / 2
    elif vd:
        d.mid = 1 - (d.down["best_bid"] + d.down["best_ask"]) / 2
    d.eligible = (d.strike is not None and d.required_future_avg_to_flip is not None
                  and d.remaining_final_window_sec > 0 and (d.sigma or 0) > 0 and (vu or vd))
    if d.eligible and d.spot is not None:
        s = d.spot
        d.z = (s - d.required_future_avg_to_flip) / (s * d.sigma * math.sqrt(d.remaining_final_window_sec / YR3))

    r = d.reasons
    if d.strike is None:
        r.append("STRIKE_UNAVAILABLE")
    if d.required_future_avg_to_flip is None:
        r.append("REQUIRED_AVG_UNAVAILABLE")
    if not (d.sigma or 0) > 0:
        r.append("SIGMA_UNAVAILABLE")
    if d.spot is None:
        r.append("SPOT_STALE")
    if not (vu or vd):
        r.append("BOOK_INVALID")
    if d.z is not None:
        if d.mid is None or not (MID_LO <= d.mid <= MID_HI):
            r.append("MID_OUT_OF_RANGE")
        if abs(d.z) < Z_MIN:
            r.append("Z_BELOW_THRESHOLD")
        d.side = "up" if d.z > 0 else "down"
        side_book = d.up if d.side == "up" else d.down
        if not book_valid(side_book):
            r.append("SIDE_BOOK_INVALID")
        else:
            d.ask, d.ask_size = side_book["best_ask"], side_book.get("ask_size")
    rule_match = d.z is not None and not r
    # Executor-only fail-closed checks (not part of the shadow rule).
    if d.twap_age_sec is None or d.twap_age_sec > SPOT_MAX_AGE_SEC:
        r.append("TWAP_FEED_STALE")
    for name, ok in feed_ok.items():
        if not ok:
            r.append(f"FEED_DOWN:{name}")
    if already_selected:
        d.action = "ALREADY_SELECTED"
    elif rule_match and not r:
        d.action = "WOULD_SUBMIT"
    elif rule_match:
        d.action = "RULE_MATCH_FAIL_CLOSED"
    return d
