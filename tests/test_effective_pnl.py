"""Effective PnL projection, settlement evidence, confirmation worker and backfill tool.

Hermetic: temporary journals built through TradeJournalDB's own schema; no network
(the worker gets a fake HTTP client); never the production journal.
"""
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.settlement_evidence import (
    OUTCOME_EVENT,
    REDEEM_EVENT,
    TRADE_EVENT,
    parse_gamma_event,
    parse_redeem_activity,
    parse_trade_activity,
)
from monitoring.pnl_attribution import load_effective_market_pnl, summarize_effective_pnl
from monitoring.trade_journal_db import TradeJournalDB

REPO_ROOT = Path(__file__).resolve().parents[1]
SLUG = "btc-updown-15m-1790000000"            # ends 1790000900
END = 1790000900
AFTER = END + 3600
UP, DOWN = "111", "222"
FILL_TS = datetime.fromtimestamp(END - 600, timezone.utc).isoformat()


class Journal:
    def __init__(self, tmp_path: Path):
        self.path = tmp_path / "trade_journal.db"
        db = TradeJournalDB(str(self.path), backup_path=str(tmp_path / "backup.db"))
        db.stop()
        self.conn = sqlite3.connect(str(self.path))
        self.n = 0

    def fill(self, side, price, qty, *, token=UP, slug=SLUG, fee_usdc=0.0, fee_shares=0.0, coid=None,
             ts=None, run_id="run_a"):
        self.n += 1
        payload = {"slug": slug, "effective_fee_usdc": fee_usdc, "effective_fee_shares": fee_shares}
        self.conn.execute(
            """INSERT INTO order_events (ts, run_id, event_type, client_order_id, side, price, qty, status,
               instrument_id, token_id, commission_usdc, payload_json)
               VALUES (?, ?, 'ORDER_FILLED', ?, ?, ?, ?, 'FILLED', ?, ?, ?, ?)""",
            (ts or FILL_TS, run_id, coid or f"C{self.n}", side, price, qty, f"0xc-{token}.POLYMARKET", token, fee_usdc,
             json.dumps(payload)),
        )
        self.conn.commit()

    def submit(self, coid, token, event_type="ORDER_TAKER_EXIT_SUBMIT"):
        self.conn.execute(
            "INSERT INTO order_events (ts, run_id, event_type, client_order_id, side, token_id, payload_json)"
            " VALUES ('2026-09-21T14:04:00+00:00', 'run_a', ?, ?, 'SELL', ?, ?)",
            (event_type, coid, token, json.dumps({"slug": SLUG})),
        )
        self.conn.commit()

    def strategy(self, event_type, payload, run_id="run_a", ts="2026-09-21T14:15:01+00:00"):
        self.conn.execute(
            "INSERT INTO strategy_events (ts, run_id, event_type, payload_json) VALUES (?, ?, ?, ?)",
            (ts, run_id, event_type, json.dumps(payload)),
        )
        self.conn.commit()

    def settle(self, outcome, inventory_shares, cycle_pnl=None, inventory_side="UP", **extra):
        self.strategy("MARKET_SETTLEMENT", {"slug": SLUG, "outcome": outcome, "inventory_shares": inventory_shares,
                                            "inventory_side": inventory_side, **extra})
        if cycle_pnl is not None:
            self.strategy("MARKET_CYCLE_PNL", {"slug": SLUG, "cycle_combined_pnl_usdc": cycle_pnl})

    def project(self, evidence=None, as_of=AFTER):
        return load_effective_market_pnl(self.path, evidence=evidence, as_of_ts=as_of)[SLUG]


def official(winner, *, slug=SLUG, up=UP, down=DOWN):
    prices = ["1", "0"] if winner == "UP" else ["0", "1"]
    return parse_gamma_event(slug, {"markets": [{
        "outcomes": '["Up", "Down"]', "outcomePrices": json.dumps(prices),
        "clobTokenIds": json.dumps([up, down]), "closed": True, "umaResolutionStatus": "resolved",
        "conditionId": "0xcond",
    }]})


def redeem(cash, shares, *, token=UP, tx="0xtx1"):
    return parse_redeem_activity({"type": "REDEEM", "transaction_hash": tx, "condition_id": "0xcond",
                                  "token_id": token, "usdc_size": cash, "size": shares, "slug": SLUG})


def venue_trade(side, price, size, cash, *, token=UP, tx="0xt", ts=END - 600):
    return parse_trade_activity({"type": "TRADE", "transaction_hash": tx, "condition_id": "0xcond", "token_id": token,
                                 "side": side, "price": price, "size": size, "usdc_size": cash, "slug": SLUG,
                                 "timestamp": ts})


@pytest.fixture
def j(tmp_path):
    return Journal(tmp_path)


# --- outcome / payout semantics -----------------------------------------------------------

def test_unknown_outcome_is_pending_never_a_zero_payout(j):
    j.fill("BUY", 0.70, 10.0)
    j.settle("UNKNOWN", 10.0, settlement_pending=True)
    row = j.project()
    assert row["pnl_basis"] == "PENDING" and row["effective_pnl_usdc"] is None
    assert row["redeem_state"] == "AWAITING_OUTCOME"
    assert summarize_effective_pnl({SLUG: row})["final_pnl_usdc"] == 0.0


def test_held_win_lose_and_official_beats_bot_twap(j):
    j.fill("BUY", 0.70, 10.0)
    j.settle("UP", 10.0, cycle_pnl=3.0)
    est = j.project()
    assert est["pnl_basis"] == "ESTIMATED" and est["effective_pnl_usdc"] == pytest.approx(3.0)
    lost = j.project([official("DOWN")])
    assert lost["pnl_basis"] == "OUTCOME_CONFIRMED" and lost["outcome_conflict"] is True
    assert lost["effective_pnl_usdc"] == pytest.approx(-7.0)
    assert lost["journal_vs_effective_usdc"] == pytest.approx(10.0)


def test_partial_and_full_sell(j):
    j.fill("BUY", 0.70, 10.0)
    j.fill("SELL", 0.90, 4.0)
    j.settle("UP", 6.0)
    part = j.project([official("UP")])
    assert part["position_state"] == "PARTIALLY_SOLD_HELD" and part["held_shares"] == pytest.approx(6.0)
    assert part["effective_pnl_usdc"] == pytest.approx(3.6 + 6.0 - 7.0)
    j.fill("SELL", 0.95, 6.0)
    full = j.project([official("DOWN")])
    assert full["position_state"] == "FULLY_SOLD" and full["pnl_basis"] == "FILLS_FINAL"
    assert full["effective_pnl_usdc"] == pytest.approx(3.6 + 5.7 - 7.0)  # outcome irrelevant once sold


def test_fee_shares_reduce_held_shares_and_usdc_fees_count_once(j):
    j.fill("BUY", 0.50, 10.0, fee_shares=0.2)          # v1 taker: fee in shares
    j.fill("SELL", 0.80, 5.0, fee_usdc=0.04)           # taker sell: fee in USDC
    j.settle("UP", 4.8)
    row = j.project([official("UP")])
    assert row["held_shares"] == pytest.approx(4.8)
    assert row["effective_pnl_usdc"] == pytest.approx((4.0 - 0.04) + 4.8 - 5.0)


def test_redeem_cash_confirms_and_corrects_fee_share_estimate(j):
    j.fill("BUY", 0.50, 10.0, fee_shares=0.2)
    j.settle("UP", 9.8)
    row = j.project([official("UP"), redeem(10.0, 10.0)])  # venue burned 10 shares: no fee shares
    assert row["pnl_basis"] == "CASH_CONFIRMED" and row["redeem_state"] == "REDEEMED"
    assert row["effective_pnl_usdc"] == pytest.approx(5.0)
    assert "fee_share_estimate_corrected_by_redeem" in row["issues"]


def test_redeem_above_tracked_position_is_not_strategy_pnl(j):
    j.fill("BUY", 0.50, 5.0)
    j.settle("UP", 5.0)
    row = j.project([official("UP"), redeem(12.0, 12.0)])
    assert row["pnl_basis"] == "INCOMPLETE" and row["effective_pnl_usdc"] is None
    assert "redeem_includes_untracked_shares" in row["issues"]


def test_duplicate_evidence_and_legacy_redeem_are_counted_once(j):
    j.fill("BUY", 0.50, 10.0)
    j.settle("UP", 10.0)
    ev = [official("UP"), redeem(10.0, 10.0)]
    j.strategy(OUTCOME_EVENT, ev[0])
    j.strategy(REDEEM_EVENT, ev[1])
    j.strategy("REDEEM_EXECUTED", {"slug": SLUG, "tx_hash": "0xtx1", "condition_id": "0xcond", "redeem_cash_usdc": 10.0})
    row = j.project(ev + ev)  # journaled + supplied twice + legacy row of the same tx
    assert row["redeem_cash_usdc"] == pytest.approx(10.0) and row["redeem_evidence_count"] == 1
    assert row["effective_pnl_usdc"] == pytest.approx(5.0)


def test_multi_token_market_uses_official_token_map(j):
    j.fill("BUY", 0.40, 5.0, token=UP)
    j.fill("BUY", 0.55, 5.0, token=DOWN)
    j.settle("UP", 10.0)
    row = j.project([official("DOWN")])
    assert "multi_token" in row["issues"] and row["entry_side"] == "MIXED"
    assert row["effective_pnl_usdc"] == pytest.approx(5.0 - (2.0 + 2.75))


def test_cross_run_and_late_fills_aggregate_by_market(j):
    j.fill("BUY", 0.60, 10.0, run_id="run_a", ts=FILL_TS)
    j.settle("UP", 10.0, cycle_pnl=0.0)  # restart booked the cycle flat
    late = datetime.fromtimestamp(END + 90, timezone.utc).isoformat()
    j.fill("SELL", 0.99, 10.0, run_id="run_b", ts=late)  # late fill after market end, new run
    row = j.project([official("UP")])
    assert row["pnl_basis"] == "FILLS_FINAL" and row["effective_pnl_usdc"] == pytest.approx(3.9)


# --- missing / inconsistent journal data -------------------------------------------------

def test_missing_buy_never_inflates_strategy_pnl(j):
    j.fill("SELL", 0.97, 10.0)
    row = j.project([official("UP")])
    assert row["pnl_basis"] == "INCOMPLETE" and row["effective_pnl_usdc"] is None
    assert summarize_effective_pnl({SLUG: row})["final_pnl_usdc"] == 0.0


def test_fill_token_corrected_from_submit_and_zero_price_duplicate(j):
    j.fill("BUY", 0.70, 10.0, token=UP)
    j.submit("EXIT1", UP)
    j.fill("SELL", 0.12, 10.0, token=DOWN, coid="EXIT1")  # journal booked the sibling token
    j.fill("SELL", 0.00, 10.0, token=UP, coid="EXIT1")    # duplicate 0-priced row of the same order
    row = j.project([official("UP")])
    assert {"fill_token_corrected_from_submit", "zero_price_duplicate_fill_dropped"} <= set(row["issues"])
    assert row["position_state"] == "FULLY_SOLD"
    assert row["effective_pnl_usdc"] == pytest.approx(1.2 - 7.0)


def test_lone_zero_price_fill_without_venue_data_is_incomplete(j):
    j.fill("BUY", 0.70, 10.0)
    j.fill("SELL", 0.0, 10.0)
    assert j.project([official("UP")])["pnl_basis"] == "INCOMPLETE"


def test_venue_cash_is_authoritative_and_adopts_missed_sells(j):
    j.fill("BUY", 0.70, 10.0, ts=FILL_TS)
    j.settle("UP", 10.0, cycle_pnl=3.0)
    ev = [official("UP"),
          venue_trade("BUY", 0.70, 10.0, 7.08, tx="0xb", ts=END - 600),    # USDC fee included by the venue
          venue_trade("SELL", 0.97, 10.0, 9.70, tx="0xs", ts=1790000800)]  # resting TP fill the journal missed
    row = j.project(ev)
    assert row["cash_source"] == "venue_activity" and "venue_sell_not_in_journal" in row["issues"]
    assert row["pnl_basis"] == "FILLS_FINAL" and row["effective_pnl_usdc"] == pytest.approx(2.62)


def test_venue_buy_missing_from_journal_is_incomplete(j):
    j.fill("BUY", 0.70, 10.0, ts=FILL_TS)
    ev = [official("UP"), venue_trade("BUY", 0.70, 10.0, 7.0, tx="0xb", ts=END - 600),
          venue_trade("BUY", 0.65, 5.0, 3.25, tx="0xm", ts=1790000750)]
    assert j.project(ev)["pnl_basis"] == "INCOMPLETE"


def test_open_market_is_not_final(j):
    j.fill("BUY", 0.70, 10.0)
    row = j.project([official("UP")], as_of=END - 60)
    assert row["pnl_basis"] == "OPEN" and row["effective_pnl_usdc"] is None


def test_per_market_rows_sum_to_the_total(tmp_path):
    jr = Journal(tmp_path)
    slugs = [f"btc-updown-15m-{1790000000 + 900 * i}" for i in range(3)]
    for i, slug in enumerate(slugs):
        jr.fill("BUY", 0.5, 10.0, slug=slug, token=f"t{i}")
        jr.fill("SELL", 0.6 + 0.1 * i, 10.0, slug=slug, token=f"t{i}")
    rows = load_effective_market_pnl(jr.path, as_of_ts=AFTER + 3600)
    summary = summarize_effective_pnl(rows)
    assert summary["final_pnl_usdc"] == pytest.approx(sum(r["effective_pnl_usdc"] for r in rows.values()))
    assert summary["final_pnl_usdc"] == pytest.approx(1.0 + 2.0 + 3.0)


# --- runtime consumers stay untouched -------------------------------------------------------

def test_evidence_rows_do_not_change_session_pnl_replay(tmp_path):
    jr = Journal(tmp_path)
    jr.fill("BUY", 0.5, 10.0)
    jr.settle("UP", 10.0, cycle_pnl=5.0)
    db = TradeJournalDB(str(jr.path), backup_path=str(tmp_path / "b2.db"))
    try:
        before = db.reconstruct_session_pnl_state("2026-09-21")
        jr.strategy(OUTCOME_EVENT, official("DOWN"))
        jr.strategy(REDEEM_EVENT, redeem(0.0, 10.0))
        after = db.reconstruct_session_pnl_state("2026-09-21")
    finally:
        db.stop()
    assert before == after


def test_projection_opens_the_journal_read_only(j):
    j.fill("BUY", 0.5, 10.0)
    j.conn.close()
    j.path.chmod(0o444)
    try:
        assert j.project([official("UP")])["pnl_basis"] in ("OUTCOME_CONFIRMED", "ESTIMATED", "PENDING")
    finally:
        j.path.chmod(0o644)


# --- confirmation worker ----------------------------------------------------------------------

class _Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


class FakeClient:
    def __init__(self, *, fail=False):
        self.fail, self.calls = fail, []

    def get(self, url, params=None):
        self.calls.append((url, params))
        if self.fail:
            raise RuntimeError("network down")
        if "/events/slug/" in url:
            return _Resp({"markets": [{"outcomes": '["Up","Down"]', "outcomePrices": '["1","0"]',
                                       "clobTokenIds": json.dumps([UP, DOWN]), "closed": True,
                                       "umaResolutionStatus": "resolved", "conditionId": "0xcond"}]})
        kind = (params or {}).get("type")
        rows = []
        if kind == "TRADE":
            rows = [{"type": "TRADE", "transaction_hash": "0xb", "token_id": UP, "side": "BUY", "price": 0.5,
                     "size": 10.0, "usdc_size": 5.0, "slug": SLUG, "timestamp": END - 600, "condition_id": "0xcond"}]
        elif kind == "REDEEM":
            rows = [{"type": "REDEEM", "transaction_hash": "0xr", "condition_id": "0xcond", "token_id": UP,
                     "usdc_size": 10.0, "size": 10.0, "slug": SLUG, "timestamp": END + 600}]
        return _Resp({"data": rows, "pagination": {"has_more": False}})


def _worker(j, client, writes, now=AFTER):
    from bot.settlement_confirmation import SettlementConfirmationWorker

    def write(event_type, payload):
        writes.append(event_type)
        j.strategy(event_type, payload, run_id="live")
        return True

    return SettlementConfirmationWorker(journal_path=j.path, write_event=write, user_address="0xuser",
                                        client_factory=lambda: client, now_fn=lambda: now)


def test_worker_writes_each_fact_once_and_restart_is_idempotent(j):
    j.fill("BUY", 0.5, 10.0)
    writes = []
    stats = _worker(j, FakeClient(), writes).cycle()
    assert stats["outcomes_written"] == 1 and stats["trades_written"] == 1 and stats["redeems_written"] == 1
    _worker(j, FakeClient(), writes).cycle()  # new instance = restart
    assert sorted(writes) == sorted([OUTCOME_EVENT, TRADE_EVENT, REDEEM_EVENT])
    row = j.project()
    assert row["pnl_basis"] == "CASH_CONFIRMED" and row["effective_pnl_usdc"] == pytest.approx(5.0)


def test_worker_waits_for_market_end_and_never_raises(j):
    j.fill("BUY", 0.5, 10.0)
    writes = []
    assert _worker(j, FakeClient(), writes, now=END + 5).cycle()["outcomes_written"] == 0  # within grace
    stats = _worker(j, FakeClient(fail=True), writes).cycle()
    assert writes == [] and stats["errors"]


def test_worker_never_touches_cycle_pnl_or_session_state(j):
    j.fill("BUY", 0.5, 10.0)
    j.settle("UP", 10.0, cycle_pnl=5.0)
    before = j.conn.execute("SELECT count(*), group_concat(payload_json) FROM strategy_events "
                            "WHERE event_type IN ('MARKET_CYCLE_PNL','MARKET_SETTLEMENT')").fetchone()
    _worker(j, FakeClient(), []).cycle()
    after = j.conn.execute("SELECT count(*), group_concat(payload_json) FROM strategy_events "
                           "WHERE event_type IN ('MARKET_CYCLE_PNL','MARKET_SETTLEMENT')").fetchone()
    assert before == after
    assert j.conn.execute("SELECT count(*) FROM session_pnl_state").fetchone()[0] == 0


# --- backfill tool ----------------------------------------------------------------------------

def _tool(*args, env=None):
    import os
    return subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "pnl_evidence_backfill.py"), *args],
                          capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT),
                          env={**os.environ, **(env or {})})


def test_backfill_apply_is_dry_run_by_default_idempotent_and_reversible(j, tmp_path):
    j.fill("BUY", 0.5, 10.0)
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"evidence": [official("UP"), redeem(10.0, 10.0)]}))
    common = ["--journal", str(j.path)]
    assert "DRY-RUN" in _tool("apply", *common, "--cache", str(cache)).stdout
    assert j.conn.execute("SELECT count(*) FROM strategy_events").fetchone()[0] == 0
    first = _tool("apply", *common, "--cache", str(cache), "--confirm", "--unsafe-skip-lock-check",
                  "--backup-dir", str(tmp_path / "bk"))
    assert "rows=2" in first.stdout, first.stderr
    second = _tool("apply", *common, "--cache", str(cache), "--confirm", "--unsafe-skip-lock-check",
                   "--backup-dir", str(tmp_path / "bk"))
    assert "Nothing to apply" in second.stdout
    assert j.project()["pnl_basis"] == "CASH_CONFIRMED"
    batch = first.stdout.split("batch_id=")[1].split()[0]
    assert "REFUSED" in _tool("rollback", *common, "--batch-id", "run_a", "--confirm",
                              "--unsafe-skip-lock-check").stderr
    undone = _tool("rollback", *common, "--batch-id", batch, "--confirm", "--unsafe-skip-lock-check",
                   "--backup-dir", str(tmp_path / "bk"))
    assert "rows=2" in undone.stdout
    assert j.conn.execute("SELECT count(*) FROM strategy_events").fetchone()[0] == 0
    assert list((tmp_path / "bk").glob("*.db"))  # a backup precedes every write


def test_backfill_apply_refuses_while_the_bot_writes(j, tmp_path):
    from bot.journal_path import journal_writer_lock_path
    from bot.process_lock import ProcessLock
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"evidence": [official("UP")]}))
    held = ProcessLock(journal_writer_lock_path(j.path))
    assert held.acquire()
    try:
        result = _tool("apply", "--journal", str(j.path), "--cache", str(cache), "--confirm",
                       "--backup-dir", str(tmp_path / "bk"),
                       env={"LIVE_PROCESS_LOCK_PATH": str(tmp_path / "live.lock")})
    finally:
        held.release()
    assert result.returncode == 4 and "REFUSED" in result.stderr
    assert j.conn.execute("SELECT count(*) FROM strategy_events").fetchone()[0] == 0


# --- dashboard ----------------------------------------------------------------------------------

def test_dashboard_shows_pending_not_loss_and_totals_match_rows(j):
    import dashboard as d
    j.fill("BUY", 0.70, 10.0)
    j.settle("UNKNOWN", 10.0, settlement_pending=True)
    state = d._mock_state()
    source = d.TradeJournalDashboardSource(db_path=j.path, state=state)
    source.refresh_once()
    snap = state.snapshot()
    trade = snap.trades[0]
    assert trade.pnl_basis == "PENDING" and d.BTCDashboard._trade_pnl_amount(trade) is None
    assert "pending" in d.BTCDashboard._pnl_cell(trade).plain
    assert snap.pnl_final_usdc == 0.0 and snap.pnl_unresolved_count == 1 and snap.visible_trades_pnl == 0.0
    assert "?" in d.BTCDashboard._pnl_cell(trade).plain


def test_dashboard_partial_sell_is_not_labelled_sold(j):
    import dashboard as d
    j.fill("BUY", 0.70, 10.0)
    j.fill("SELL", 0.90, 4.0)
    j.strategy(OUTCOME_EVENT, official("UP"))
    state = d._mock_state()
    d.TradeJournalDashboardSource(db_path=j.path, state=state).refresh_once()
    trade = state.snapshot().trades[0]
    assert d.BTCDashboard._position_cell(trade).plain == "part+held"
    assert d.BTCDashboard._redeem_cell(trade).plain.startswith("claim")
    assert d.BTCDashboard._pnl_cell(trade).plain.startswith("=")
    assert state.snapshot().pending_redeem_count == 1
