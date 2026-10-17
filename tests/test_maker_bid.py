"""MB1 bid-only maker simulator: latency, queue, picked-off fills, requotes and window rules."""
import pytest

from bot.research.maker_bid import LATENCY_SEC, MakerBidSim, PriceHistory

END = 1792220000.0
SLUG = f"btc-updown-5m-{int(END) - 300}"
MKT = {"tu": {"slug": SLUG, "outcome": "Up", "end_ts": END}, "td": {"slug": SLUG, "outcome": "Down", "end_ts": END}}


def _calm_history(prices, asset="btc", t_end=END - 100, px=100000.0):
    """15 min of quiet prices (alternating +/-0.2 bps every 2 s): 2-sigma threshold ~0.5 bps floor."""
    for i in range(460):
        prices.add(asset, t_end - 920 + 2 * i, px * (1 + (0.2e-4 if i % 2 else 0.0)))


class Env:
    def __init__(self):
        self.books = {"tu": {"best_bid": 0.60, "best_ask": 0.61}, "td": {"best_bid": 0.38, "best_ask": 0.40}}
        self.levels = {("tu", 0.60): 10.0, ("td", 0.38): 0.0}
        self.events = []
        self.prices = PriceHistory()
        _calm_history(self.prices)
        self.sim = MakerBidSim(self.events.append)

    def tick(self, t, ok=True):
        self.sim.tick(t, MKT, self.books.get, lambda tok, px: self.levels.get((tok, round(px, 4)), 0.0),
                      self.prices, lambda slug: ok)

    def trade(self, t, token, price, size, side="SELL"):
        self.sim.on_trade({"token_id": token, "price": price, "size": size, "side": side,
                           "received_ms": int(t * 1000), "tx_hash": "0x"})

    def kinds(self, token="tu"):
        return [e["event"] for e in self.events if e.get("token_id") == token]


def test_order_goes_live_after_latency_then_queue_is_consumed_before_fills():
    e = Env()
    t0 = END - 100
    e.tick(t0)                                   # PLACE_SENT both tokens
    e.trade(t0 + 0.2, "tu", 0.60, 50)            # before live: ignored
    e.tick(t0 + LATENCY_SEC + 0.01)              # LIVE, queue 10 measured
    e.trade(t0 + 0.5, "tu", 0.60, 6)             # queue 10 -> 4
    e.trade(t0 + 0.6, "tu", 0.60, 8)             # queue 0, fill 4
    e.trade(t0 + 0.7, "tu", 0.60, 3, side="BUY")  # taker buy: ignored
    e.trade(t0 + 0.8, "tu", 0.60, 3)             # fill 1 -> done
    fills = [x for x in e.events if x["event"] == "FILL" and x["token_id"] == "tu"]
    assert [f["fill_shares"] for f in fills] == [4.0, 1.0]
    live = [x for x in e.events if x["event"] == "LIVE" and x["token_id"] == "tu"][0]
    assert live["queue_ahead"] == 10.0 and live["price"] == 0.60
    (pos,) = [p for p in e.sim.positions() if p["token_id"] == "tu"]
    assert pos["shares"] == 5.0 and pos["cost"] == pytest.approx(3.0)


def test_print_below_our_price_sweeps_the_level():
    e = Env()
    e.tick(END - 100); e.tick(END - 99.5)
    e.trade(END - 99.4, "tu", 0.59, 2)
    (f,) = [x for x in e.events if x["event"] == "FILL"]
    assert f["fill_shares"] == 5.0


def test_adverse_binance_move_cancels_and_late_prints_are_picked_off_until_effective():
    e = Env()
    t0 = END - 100
    e.prices.add("btc", t0 - 5, 100000.0)
    e.tick(t0); e.tick(t0 + 0.5)                 # live, queue 10
    e.prices.add("btc", t0 + 0.9, 99990.0)       # -1 bp within 2 s > ~0.5 bps threshold -> Up bid is adverse
    e.tick(t0 + 1.0)
    cancel = [x for x in e.events if x["event"] == "CANCEL_SENT" and x["token_id"] == "tu"][0]
    assert cancel["reason"].startswith("ADVERSE")
    e.trade(t0 + 1.1, "tu", 0.59, 5)             # inside the 0.37 s cancel latency -> filled, picked off
    f = [x for x in e.events if x["event"] == "FILL" and x["token_id"] == "tu"][0]
    assert f["picked_off"] is True
    # the Down bid is not adverse for a falling price and stays
    assert "CANCEL_SENT" not in e.kinds("td")


def test_cancel_effective_stops_fills():
    e = Env()
    t0 = END - 100
    e.prices.add("btc", t0 - 5, 100000.0)
    e.tick(t0); e.tick(t0 + 0.5)
    e.prices.add("btc", t0 + 0.9, 99990.0)
    e.tick(t0 + 1.0); e.tick(t0 + 1.0 + LATENCY_SEC + 0.01)
    e.trade(t0 + 1.5, "tu", 0.50, 5)
    assert "FILL" not in e.kinds("tu") and "CANCEL_EFFECTIVE" in e.kinds("tu")


def test_requote_on_best_bid_change_keeps_old_order_until_cancel_effective():
    e = Env()
    t0 = END - 100
    e.tick(t0); e.tick(t0 + 0.5)
    e.books["tu"] = {"best_bid": 0.62, "best_ask": 0.63}
    e.tick(t0 + 2.5)                              # cancel old @0.60 + send new @0.62
    sent = [x for x in e.events if x["event"] == "PLACE_SENT" and x["token_id"] == "tu"]
    assert [s["price"] for s in sent] == [0.60, 0.62]
    e.trade(t0 + 2.6, "tu", 0.59, 5)              # old order still fillable (cancel not effective)
    f = [x for x in e.events if x["event"] == "FILL"][0]
    assert f["price"] == 0.60 and f["picked_off"] is True
    sib = [x for x in e.events if x["event"] == "CANCEL_SENT" and x.get("reason") == "SIBLING_FILLED"]
    assert len(sib) == 1 and sib[0]["price"] == 0.62          # the new order is cancelled at once


def test_no_quotes_outside_window_or_conditions_and_window_end_cancels():
    e = Env()
    e.tick(END - 130)
    assert e.events == []
    e.books["tu"] = {"best_bid": 0.97, "best_ask": 0.98}
    e.books["td"] = {"best_bid": 0.30, "best_ask": 0.40}   # spread 0.10 > 0.03
    e.tick(END - 100)
    assert e.events == []
    e.books = {"tu": {"best_bid": 0.60, "best_ask": 0.61}, "td": {"best_bid": 0.38, "best_ask": 0.40}}
    e.tick(END - 50); e.tick(END - 49)
    e.tick(END - 10)
    assert {x["reason"] for x in e.events if x["event"] == "CANCEL_SENT"} == {"WINDOW_END"}


def test_feed_down_blocks_quoting():
    e = Env()
    e.tick(END - 100, ok=False)
    assert e.events == []


def test_adaptive_threshold_is_two_sigma_of_2s_returns_with_floor_and_fail_closed():
    p = PriceHistory()
    assert p.adverse_threshold_bps("btc", END) is None                 # no history -> None
    for i in range(460):                                               # +/-2 bps alternating 2 s moves
        p.add("btc", END - 920 + 2 * i, 100000.0 * (1 + (2e-4 if i % 2 else 0.0)))
    thr = p.adverse_threshold_bps("btc", END)
    assert 3.5 < thr < 4.5                                             # sd ~2 bps -> 2 sigma ~4 bps
    e = Env()
    e.prices = PriceHistory()                                          # no history: no quotes
    e.tick(END - 100)
    assert e.events == []


def test_fill_cancels_the_replaced_sibling_order():
    e = Env()
    t0 = END - 100
    e.tick(t0); e.tick(t0 + 0.5)
    e.books["tu"] = {"best_bid": 0.62, "best_ant": 0.63, "best_ask": 0.63}
    e.tick(t0 + 2.5)                               # old @0.60 cancelling, new @0.62 sent
    e.tick(t0 + 2.9)                               # new live
    e.levels[("tu", 0.62)] = 0.0
    e.trade(t0 + 3.0, "tu", 0.61, 5)               # hits the new @0.62 (swept) -> fill
    kinds = [(x["event"], x.get("price"), x.get("reason")) for x in e.events if x.get("token_id") == "tu"]
    assert ("FILL", 0.62, None) in kinds
    # the old order's cancel was already sent by the requote; no extra placement afterwards
    e.tick(t0 + 6.0)
    assert sum(1 for k in kinds if k[0] == "PLACE_SENT") == 2
