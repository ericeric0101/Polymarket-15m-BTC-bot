"""P2 micro-live order path: caps, reservation, FAK request shape and fill parsing (fake client only)."""
import json
import sys
import threading
from pathlib import Path

import pytest

from bot.research.twap_z_live import (
    LiveConfig, LiveOrderClient, OutcomeWatcher, Position, RiskBook, fill_from_response,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
NOW = 1792210000.0


def _book(tmp_path, **kw):
    b = RiskBook(LiveConfig(out_dir=tmp_path, **kw))
    b.load()
    return b


def test_reservation_blocks_simultaneous_orders_from_overrunning_the_daily_cap(tmp_path):
    b = _book(tmp_path, max_daily_loss=15.0)
    results = [b.reserve(f"btc-updown-5m-{i}", NOW, 4.9)[0] for i in range(8)]  # 8 streams at one second
    assert results == [True, True, True, False, False, False, False, False]   # 3 x 4.9 = 14.7 <= 15


def test_settled_losses_and_open_cost_count_toward_caps(tmp_path):
    b = _book(tmp_path, max_daily_loss=15.0, max_total_loss=50.0)
    b.add(Position("eth-updown-5m-1", "up", 30, NOW - 400, 4.8, 5.4))
    b.settle("eth-updown-5m-1", "DOWN")                                   # lost 4.8
    b.add(Position("eth-updown-5m-2", "up", 30, NOW + 100, 4.8, 5.4))      # open: counted as -4.8
    today, total = b.worst_case(NOW)
    assert today == pytest.approx(-9.6) and total == pytest.approx(-9.6)
    assert b.reserve("sol-updown-5m-3", NOW, 4.8) == (True, "OK")           # -14.4 >= -15
    assert b.reserve("xrp-updown-5m-4", NOW, 4.8) == (False, "DAILY_LOSS_CAP")


def test_win_pnl_uses_shares_received_after_fee(tmp_path):
    b = _book(tmp_path)
    b.add(Position("xrp-updown-5m-1", "down", 25, NOW - 400, 4.84, 5.43))
    p = b.settle("xrp-updown-5m-1", "DOWN")
    assert p.pnl == pytest.approx(5.43 - 4.84)
    reloaded = _book(tmp_path)
    assert reloaded.positions[0].pnl == pytest.approx(0.59)                 # persisted


def test_gates_stop_file_stream_filter_and_one_order_per_market(tmp_path):
    b = _book(tmp_path, streams=("xrp-updown-5m",))
    assert b.reserve("btc-updown-5m-1", NOW, 4.0) == (False, "STREAM_NOT_ENABLED")
    assert b.reserve("xrp-updown-5m-1", NOW, 4.0) == (True, "OK")
    assert b.reserve("xrp-updown-5m-1", NOW, 4.0) == (False, "ALREADY_TRADED_MARKET")
    b.release("xrp-updown-5m-1")
    (tmp_path / "STOP").write_text("")
    assert b.reserve("xrp-updown-5m-2", NOW, 4.0) == (False, "STOP_FILE")


class _FakeClob:
    def __init__(self, response=None, raise_on_post=None):
        self.response, self.raise_on_post = response, raise_on_post
        self.posted = []

    def create_order(self, args):
        return {"signed": args}

    def post_order(self, order, order_type=None):
        if self.raise_on_post:
            raise self.raise_on_post
        self.posted.append((order, order_type))
        return self.response


def test_buy_fak_sends_a_fak_limit_buy_at_the_given_price_and_size():
    clob = _FakeClob({"success": True, "orderID": "0xabc", "status": "matched",
                      "makingAmount": "4.84", "takingAmount": "5.43"})
    res = LiveOrderClient(clob).buy_fak("tok", 0.88, 5.5)
    (signed, order_type), = clob.posted
    args = signed["signed"]
    assert order_type == "FAK" and args.side == "BUY" and args.price == 0.88 and args.size == 5.5
    assert res["ok"] and res["order_id"] == "0xabc"
    assert fill_from_response(res, 0.88) == (4.84, 5.43)


def test_unmatched_error_and_exception_book_no_position():
    unmatched = LiveOrderClient(_FakeClob({"success": True, "status": "unmatched", "makingAmount": "0",
                                           "takingAmount": "0"})).buy_fak("t", .9, 5.5)
    rejected = LiveOrderClient(_FakeClob({"success": False, "errorMsg": "no match"})).buy_fak("t", .9, 5.5)
    boom = LiveOrderClient(_FakeClob(raise_on_post=RuntimeError("timeout"))).buy_fak("t", .9, 5.5)
    assert fill_from_response(unmatched, .9) == (0.0, 0.0)
    assert fill_from_response(rejected, .9) == (0.0, 0.0) and rejected["error"] == "no match"
    assert fill_from_response(boom, .9) == (0.0, 0.0) and "timeout" in boom["error"]


def test_matched_without_amounts_is_booked_at_worst_case():
    res = {"ok": True, "status": "matched", "size": 5.5, "making_amount": None, "taking_amount": None}
    assert fill_from_response(res, 0.9) == (pytest.approx(4.95), 5.5)


def test_outcome_watcher_settles_only_after_market_end(tmp_path):
    b = _book(tmp_path)
    b.add(Position("sol-updown-5m-1", "up", 30, NOW, 4.0, 5.4))
    w = OutcomeWatcher(b, lambda slug: "UP", threading.Event())
    assert w.poll_once(now=NOW + 30) == 0
    assert w.poll_once(now=NOW + 61) == 1 and b.positions[0].pnl == pytest.approx(1.4)


def test_live_mode_refuses_without_both_confirmations(monkeypatch, tmp_path):
    import twap_z_executor as ex
    monkeypatch.delenv("TWAP_Z_LIVE_CONFIRM", raising=False)
    assert ex.main(["--mode", "live", "--confirm-live", "--out-dir", str(tmp_path)]) == 2
    monkeypatch.setenv("TWAP_Z_LIVE_CONFIRM", "REAL_MONEY")
    assert ex.main(["--mode", "live", "--out-dir", str(tmp_path)]) == 2   # flag missing


def test_live_context_reserves_submits_and_logs(monkeypatch, tmp_path):
    import twap_z_executor as ex
    from bot.research.twap_z_executor import Decision

    clob = _FakeClob({"success": True, "orderID": "0x1", "status": "matched", "makingAmount": "4.84",
                      "takingAmount": "5.43"})
    monkeypatch.setattr(ex, "fetch_official_outcome", lambda slug: None)
    ctx = ex.LiveContext(LiveConfig(out_dir=tmp_path), LiveOrderClient(clob), tmp_path)

    class Books:
        def market_tokens(self, slug):
            return {"Up": "tu", "Down": "td"}

    d = Decision(market_slug="xrp-updown-5m-1792209900", checkpoint=30, scheduled_ts=1792210170.0,
                 eval_ts=1792210170.003, side="down", ask=0.88, action="WOULD_SUBMIT")
    out = ctx.submit(d, Books(), d.to_row())
    ctx.close()
    assert out["action"] == "SUBMITTED"
    (pos,) = ctx.book.positions
    assert pos.side == "down" and pos.shares == 5.43 and pos.end_ts == 1792209900 + 300
    assert ctx.book.pending == {}
    rows = [json.loads(x) for x in next(tmp_path.glob("orders_*.jsonl")).read_text().splitlines()]
    assert rows[0]["live_action"] == "ORDER" and rows[0]["order"]["filled_shares"] == 5.43
    assert clob.posted[0][0]["signed"].token_id == "td"
