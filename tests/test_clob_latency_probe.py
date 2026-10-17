"""Latency probe: request shapes (FAK and post-only at 0.01, then cancel) with a fake client."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import clob_latency_probe as probe  # noqa: E402
from bot.research.twap_z_live import LiveOrderClient  # noqa: E402


class FakeClob:
    def __init__(self, ask="0.55"):
        self.ask, self.posts, self.cancels = ask, [], []

    def get_server_time(self):
        return 1

    def get_order_book(self, token):
        return {"asks": [{"price": self.ask, "size": "10"}], "bids": []}

    def create_order(self, args):
        return args

    def post_order(self, order, order_type=None, post_only=False):
        self.posts.append((order, order_type, post_only))
        if order_type == "FAK":
            return {"success": False, "errorMsg": "no match", "status": None}
        return {"success": True, "orderID": "0x9", "status": "live"}

    def cancel_order(self, payload):
        self.cancels.append(payload.orderID)
        return {"canceled": [payload.orderID], "not_canceled": {}}


def test_probe_sends_unmatchable_fak_and_cancelled_post_only():
    clob = FakeClob()
    r = probe.probe_once(LiveOrderClient(clob), "tok")
    (fak, t1, po1), (gtc, t2, po2) = clob.posts
    assert t1 == "FAK" and not po1 and fak.price == 0.01 and fak.size == 5.0 and fak.side == "BUY"
    assert t2 == "GTC" and po2 and gtc.price == 0.01
    assert clob.cancels == ["0x9"] and r["cancel_ok"] is True
    for k in ("get_time_ms", "get_book_ms", "sign_ms", "fak_post_ms", "gtc_post_ms", "cancel_ms"):
        assert isinstance(r[k], float)
    s = probe.summarize([r])
    assert s["fak_post_ms"]["n"] == 1 and s["fills"] == [] and s["cancel_failures"] == 0


def test_probe_skips_when_ask_is_at_the_probe_price():
    clob = FakeClob(ask="0.01")
    assert probe.probe_once(LiveOrderClient(clob), "tok")["skipped"] == "ask_too_low" and clob.posts == []


def test_refuses_without_confirmation(monkeypatch):
    monkeypatch.delenv("CLOB_PROBE_CONFIRM", raising=False)
    assert probe.main(["--confirm"]) == 2
