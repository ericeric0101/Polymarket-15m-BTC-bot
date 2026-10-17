"""MB1 'bid-only maker' simulator (plan: reports/maker_bid_20261017/plan_mb1.md).

Event-driven and clock-explicit so the same code runs live (dry-run) and in tests:
  * tick(now, ...)      -> quoting decisions (place / cancel / replace) for markets in the window
  * on_trade(print)     -> queue consumption and fills of virtual orders from real trade prints
Latency: a placement is on the book LATENCY_SEC after it is sent; a cancel takes effect
LATENCY_SEC after it is sent (the order can still be filled in between = picked off).
Queue: when an order goes live, the size already resting at its price is ahead of it; taker SELL
prints at our price consume that queue first, a print below our price means the level was swept.
Nothing here places real orders.
"""
from __future__ import annotations

import itertools
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

LATENCY_SEC = 0.37
WINDOW_START, WINDOW_END = 120, 10        # quote from T-120 s until T-10 s
BID_MIN, BID_MAX, MAX_SPREAD = 0.05, 0.95, 0.03
SIZE = 5.0
REQUOTE_MIN_SEC = 2.0
ADVERSE_BPS, ADVERSE_LOOKBACK = 3.0, 2.0     # ADVERSE_BPS is superseded by the adaptive threshold (A1)
ADVERSE_SIGMAS, ADVERSE_FLOOR_BPS, SIGMA_WINDOW_SEC = 2.0, 0.5, 900.0
_MIN_SIGMA_SAMPLES = 120
COOLDOWN_SEC = 2.0
_ids = itertools.count(1)


@dataclass
class VirtualOrder:
    order_id: int
    market_slug: str
    outcome: str                      # "Up" / "Down"
    token_id: str
    price: float
    size: float
    sent_ts: float
    live_ts: float
    queue_ahead: Optional[float] = None   # set when the order goes live
    queue_measured_ts: Optional[float] = None  # prints received before this are ignored (no double count)
    filled: float = 0.0
    first_fill_ts: Optional[float] = None
    cancel_sent_ts: Optional[float] = None
    cancel_reason: Optional[str] = None
    picked_off: bool = False          # (part) filled after the cancel was sent
    done: bool = False                # cancelled (effective) or fully filled

    @property
    def cancel_effective_ts(self) -> Optional[float]:
        return None if self.cancel_sent_ts is None else self.cancel_sent_ts + LATENCY_SEC

    def on_book(self, t: float) -> bool:
        if self.done or t < self.live_ts:
            return False
        eff = self.cancel_effective_ts
        return eff is None or t < eff


class PriceHistory:
    """Recent exchange prices per asset (local receipt clock) for the adverse-move cancel."""

    def __init__(self, keep_sec: float = SIGMA_WINDOW_SEC + 30.0) -> None:
        self._d: dict[str, deque] = {}
        self._lock = threading.Lock()
        self.keep = keep_sec

    def add(self, asset: str, t: float, price: float) -> None:
        with self._lock:
            q = self._d.setdefault(asset, deque())
            q.append((t, price))
            while q and q[0][0] < t - self.keep:
                q.popleft()

    def adverse_threshold_bps(self, asset: str, now: float) -> Optional[float]:
        """MB1-A1: 2 x std of non-overlapping 2 s returns over the last 15 min (floor 0.5 bps)."""
        with self._lock:
            q = [(t, p) for t, p in self._d.get(asset, ()) if now - SIGMA_WINDOW_SEC <= t <= now]
        if not q:
            return None
        last_px: dict[int, float] = {}
        for t, p in q:
            last_px[int(t // ADVERSE_LOOKBACK)] = p        # last price in each 2 s bucket
        keys = sorted(last_px)
        rets = [(last_px[b] / last_px[a] - 1.0) * 1e4 for a, b in zip(keys, keys[1:]) if b == a + 1]
        if len(rets) < _MIN_SIGMA_SAMPLES:
            return None
        mu = sum(rets) / len(rets)
        sd = (sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
        return max(ADVERSE_FLOOR_BPS, ADVERSE_SIGMAS * sd)

    def ret_bps(self, asset: str, now: float, lookback: float) -> Optional[float]:
        with self._lock:
            q = list(self._d.get(asset, ()))
        now_px = [p for t, p in q if t <= now]
        then_px = [p for t, p in q if t <= now - lookback]
        if not now_px or not then_px:
            return None
        return (now_px[-1] / then_px[-1] - 1.0) * 1e4


@dataclass
class TokenState:
    order: Optional[VirtualOrder] = None
    filled_shares: float = 0.0
    fill_cost: float = 0.0
    last_send_ts: float = -1e18
    cooldown_until: float = -1e18
    done: bool = False                # filled once (MB1: one fill per token per market)


class MakerBidSim:
    """Holds every virtual order; emits event dicts through `log`."""

    def __init__(self, log: Callable[[dict], None]) -> None:
        self.log = log
        self.tokens: dict[tuple[str, str], TokenState] = {}     # (slug, token) -> state
        self.meta: dict[str, dict[str, Any]] = {}               # token -> {slug, outcome, end_ts}
        self._pc: list[VirtualOrder] = []    # replaced orders still fillable until their cancel is effective
        self._lock = threading.Lock()

    # ---- helpers ------------------------------------------------------------------------
    def _event(self, kind: str, o: Optional[VirtualOrder], t: float, **kw) -> None:
        row = {"event": kind, "t": round(t, 3)}
        if o is not None:
            row.update({k: v for k, v in asdict(o).items() if k not in ("done",)})
        row.update(kw)
        self.log(row)

    def _send(self, st: TokenState, slug: str, outcome: str, token: str, price: float, t: float) -> None:
        o = VirtualOrder(next(_ids), slug, outcome, token, round(price, 4), SIZE, t, t + LATENCY_SEC)
        st.order, st.last_send_ts = o, t
        self._event("PLACE_SENT", o, t)

    def _cancel(self, st: TokenState, t: float, reason: str) -> None:
        o = st.order
        if o is None or o.cancel_sent_ts is not None or o.done:
            return
        o.cancel_sent_ts, o.cancel_reason = t, reason
        st.last_send_ts = t
        self._event("CANCEL_SENT", o, t, reason=reason)

    # ---- time advance -------------------------------------------------------------------
    def _advance(self, t: float, level_size: Callable[[str, float], Optional[float]]) -> None:
        for (slug, token), st in self.tokens.items():
            o = st.order
            if o is None or o.done:
                continue
            eff = o.cancel_effective_ts
            if o.queue_ahead is None and t >= o.live_ts and (eff is None or o.live_ts < eff):
                q = level_size(token, o.price)
                o.queue_ahead, o.queue_measured_ts = float(q or 0.0), t
                self._event("LIVE", o, o.live_ts, queue_ahead=o.queue_ahead)
            if eff is not None and t >= eff:
                o.done = True
                self._event("CANCEL_EFFECTIVE", o, eff)
                st.order = None

    def tick(self, now: float, markets: dict[str, dict[str, Any]], best: Callable[[str], Optional[dict]],
             level_size: Callable[[str, float], Optional[float]], prices: PriceHistory, feed_ok: Callable[[str], bool]) -> None:
        """markets: token -> {slug, outcome, end_ts}. best(token) -> {best_bid, best_ask, ...} or None."""
        with self._lock:
            self.meta.update(markets)
            self._advance(now, level_size)
            for token, m in markets.items():
                slug, end = m["slug"], float(m["end_ts"])
                tte = end - now
                if tte > WINDOW_START or tte < -5:
                    continue
                st = self.tokens.setdefault((slug, token), TokenState())
                o = st.order
                if tte <= WINDOW_END:
                    if o is not None:
                        self._cancel(st, now, "WINDOW_END")
                    continue
                if st.done:
                    continue
                b = best(token)
                asset = slug.split("-", 1)[0]
                ok = (b is not None and b.get("best_bid") is not None and b.get("best_ask") is not None
                      and BID_MIN <= b["best_bid"] <= BID_MAX and b["best_ask"] - b["best_bid"] <= MAX_SPREAD + 1e-9
                      and feed_ok(slug))
                r = prices.ret_bps(asset, now, ADVERSE_LOOKBACK)
                thr = prices.adverse_threshold_bps(asset, now)
                if thr is None:                       # not enough history yet: fail closed, do not quote
                    if o is not None:
                        self._cancel(st, now, "NO_VOL_HISTORY")
                    continue
                adverse = r is not None and ((m["outcome"] == "Up" and r <= -thr) or
                                             (m["outcome"] == "Down" and r >= thr))
                if adverse:
                    if o is not None:
                        self._cancel(st, now, f"ADVERSE_{r:+.2f}bps_thr{thr:.2f}")
                    st.cooldown_until = now + COOLDOWN_SEC
                    continue
                if not ok:
                    if o is not None:
                        self._cancel(st, now, "CONDITIONS")
                    continue
                if now < st.cooldown_until or now - st.last_send_ts < REQUOTE_MIN_SEC:
                    continue
                if o is None:
                    self._send(st, slug, m["outcome"], token, b["best_bid"], now)
                elif o.cancel_sent_ts is None and abs(o.price - b["best_bid"]) > 1e-9:
                    self._cancel(st, now, "REQUOTE")
                    st.order = None             # the old order keeps living until its cancel is effective
                    self._pending_cancels.append(o)
                    self._send(st, slug, m["outcome"], token, b["best_bid"], now)
            self._advance_pending(now)

    @property
    def _pending_cancels(self) -> list[VirtualOrder]:
        return self._pc

    def _advance_pending(self, t: float) -> None:
        keep = []
        for o in self._pending_cancels:
            if t >= o.cancel_effective_ts:
                o.done = True
                self._event("CANCEL_EFFECTIVE", o, o.cancel_effective_ts)
            else:
                keep.append(o)
        self._pc = keep

    # ---- fills from real prints ---------------------------------------------------------
    def on_trade(self, tr: dict[str, Any]) -> None:
        """tr: a parsed trade print (token_id, price, size, side, received_ms)."""
        if str(tr.get("side", "")).upper() != "SELL":      # only taker sells hit bids
            return
        t = tr["received_ms"] / 1000.0
        with self._lock:
            orders = [st.order for (s, tok), st in self.tokens.items() if tok == tr["token_id"] and st.order is not None]
            orders += [o for o in self._pending_cancels if o.token_id == tr["token_id"]]
            for o in orders:
                if not o.on_book(t) or o.queue_ahead is None or t <= o.queue_measured_ts:
                    continue
                px, sz = float(tr["price"]), float(tr["size"])
                if px > o.price + 1e-9:
                    continue
                remaining = o.size - o.filled
                if px < o.price - 1e-9:
                    take = remaining                         # level swept through
                else:
                    use = min(o.queue_ahead, sz)
                    o.queue_ahead -= use
                    take = min(remaining, sz - use)
                if take <= 1e-9:
                    continue
                o.filled += take
                o.first_fill_ts = o.first_fill_ts or t
                if o.cancel_sent_ts is not None and t >= o.cancel_sent_ts:
                    o.picked_off = True
                st = self.tokens[(o.market_slug, o.token_id)]
                st.filled_shares += take
                st.fill_cost += take * o.price
                st.done = True
                self._event("FILL", o, t, fill_shares=take, print_price=px, print_size=sz,
                            print_tx=tr.get("tx_hash"))
                # One fill per token: cancel any other order of this token (it can still be hit
                # during the cancel latency -- realistic, logged as a second filled order).
                for sib in [st.order] + [x for x in self._pending_cancels if x.token_id == o.token_id]:
                    if sib is not None and sib is not o and not sib.done and sib.cancel_sent_ts is None:
                        sib.cancel_sent_ts, sib.cancel_reason = t, "SIBLING_FILLED"
                        self._event("CANCEL_SENT", sib, t, reason="SIBLING_FILLED")
                        if st.order is sib:
                            st.order = None
                            self._pending_cancels.append(sib)
                if o.filled >= o.size - 1e-9:
                    o.done = True
                    if st.order is o:
                        st.order = None

    def positions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"market_slug": s, "token_id": tok, "outcome": self.meta.get(tok, {}).get("outcome"),
                     "end_ts": self.meta.get(tok, {}).get("end_ts"), "shares": st.filled_shares, "cost": st.fill_cost}
                    for (s, tok), st in self.tokens.items() if st.filled_shares > 0]
