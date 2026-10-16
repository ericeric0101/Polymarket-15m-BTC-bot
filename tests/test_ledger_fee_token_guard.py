"""Taker BUY fee in USDC, fill instrument identity, and the session-guard evidence correction.

Hermetic: in-memory hosts and temporary journals only.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from bot.db_runtime import StrategyDBRuntimeMixin, order_event_token_id
from bot.fill_ledger import interpret_fill_liquidity
from bot.inventory import InventoryLedger
from monitoring.trade_journal_db import SESSION_CORRECTION_EVENT, TradeJournalDB
from run_bot import IntegratedBTCStrategy
from test_fill_instrument_identity import HELD, PAIRED, _fill_events, _flipped_strategy, _sell


# --- 4. taker BUY fee is USDC on top, full shares delivered ------------------------------------

@pytest.mark.parametrize("protocol", ["v1", "v2"])
def test_taker_buy_fee_is_usdc_and_never_reduces_shares(protocol):
    fill = interpret_fill_liquidity(
        liquidity_side="TAKER", raw_commission_dec=Decimal("0"), maker_matched=False, side_for_ledger="buy",
        fill_price_dec=Decimal("0.73"), fill_qty_dec=Decimal("5.547944"), filled_limit_price=Decimal("0"),
        filled_id="FF-1", protocol=protocol,
    )
    assert fill.effective_fee_shares_dec == 0 and fill.effective_fee_usdc_dec > 0


def test_maker_buy_has_no_fee():
    fill = interpret_fill_liquidity(
        liquidity_side="MAKER", raw_commission_dec=Decimal("0"), maker_matched=True, side_for_ledger="buy",
        fill_price_dec=Decimal("0.73"), fill_qty_dec=Decimal("5.5"), filled_limit_price=Decimal("0.73"),
        filled_id="MB-1",
    )
    assert fill.effective_fee_shares_dec == 0 and fill.effective_fee_usdc_dec == 0


def test_usdc_buy_fee_enters_cost_and_is_realized_once_on_sell():
    ledger = {}
    InventoryLedger.update_from_fill(ledger, "up", "buy", Decimal("0.73"), Decimal("5.5"),
                                     Decimal("0.08"), Decimal("0"), 1.0)
    assert ledger["up"]["qty"] == Decimal("5.5")  # matches the venue's delivered size
    realized = InventoryLedger.update_from_fill(ledger, "up", "sell", Decimal("0.97"), Decimal("5.5"),
                                                Decimal("0"), Decimal("0"), 2.0)
    assert realized == pytest.approx(Decimal("5.5") * (Decimal("0.97") - Decimal("0.73")) - Decimal("0.08"))


# --- 5. fills are booked on our own order's instrument ----------------------------------------

def test_fill_event_reporting_the_sibling_token_is_booked_on_the_order_instrument():
    strategy = _flipped_strategy()
    strategy.cache = SimpleNamespace(order=lambda _coid: SimpleNamespace(instrument_id=HELD))
    sibling_report = SimpleNamespace(**{**vars(_sell("5.4945")), "instrument_id": PAIRED})
    IntegratedBTCStrategy.on_order_filled(strategy, sibling_report)
    (event,) = _fill_events(strategy)
    assert event["instrument_id"] == HELD
    assert event["payload"]["fill_event_instrument_id"] == PAIRED
    assert strategy.live_inventory_cost[HELD]["qty"] == Decimal("0.0055")
    assert PAIRED not in strategy.live_inventory_cost


def test_unknown_order_falls_back_to_the_fill_event_instrument():
    strategy = _flipped_strategy()
    strategy.cache = SimpleNamespace(order=lambda _coid: None)
    IntegratedBTCStrategy.on_order_filled(strategy, _sell("5.4945"))
    (event,) = _fill_events(strategy)
    assert event["instrument_id"] == HELD and "fill_event_instrument_id" not in event["payload"]


def test_journal_token_column_is_the_rows_own_token_not_the_active_side():
    host = SimpleNamespace(current_token_id="999")
    assert order_event_token_id(host, "0xcond-123456.POLYMARKET") == "123456"
    assert order_event_token_id(host, None) == "999"


def test_startup_replay_uses_the_submit_token_over_a_mislabeled_fill(tmp_path):
    db = TradeJournalDB(str(tmp_path / "j.db"), backup_path=str(tmp_path / "b.db"))
    try:
        slug = "btc-updown-15m-1790000000"
        with db._connect() as conn:
            rows = [
                ("ORDER_FILLED", "BUY-1", "BUY", 0.70, 10.0, "111", {"slug": slug}),
                ("ORDER_TAKER_EXIT_SUBMIT", "EXIT-1", "SELL", None, None, "111", {"slug": slug}),
                # exit fill booked under the sibling token (old journal artefact)
                ("ORDER_FILLED", "EXIT-1", "SELL", 0.12, 10.0, "222",
                 {"slug": slug, "instrument_id": "0xc-222.POLYMARKET"}),
            ]
            for event_type, coid, side, price, qty, token, payload in rows:
                conn.execute(
                    """INSERT INTO order_events (ts, run_id, event_type, client_order_id, side, price, qty, token_id,
                       payload_json) VALUES ('2026-09-21T13:40:00+00:00', 'r', ?, ?, ?, ?, ?, ?, ?)""",
                    (event_type, coid, side, price, qty, token, json.dumps(payload)),
                )
            conn.commit()
        result = db.reconcile_startup_resolved_cycle(slug=slug, winning_token_id="111", settlement_source="test")
        # Held token 111 was sold: nothing left to pay out; the loss is the whole round trip.
        assert result["settlement_pnl_usdc"] == pytest.approx(0.0)
        assert result["cycle_combined_pnl_usdc"] == pytest.approx(1.2 - 7.0)
    finally:
        db.stop()


# --- 3. session guard evidence correction (open session, idempotent) ---------------------------

TPE = ZoneInfo("Asia/Taipei")
SESSION_NOW = datetime(2026, 9, 28, 22, 0, tzinfo=TPE).timestamp()       # session "2026-09-28"
OPEN_SLUG = "btc-updown-15m-1790599500"                                     # 2026-09-28 21:45..22:00 +08
CLOSED_SLUG = "btc-updown-15m-1790519400"                                   # previous session


class _Host(StrategyDBRuntimeMixin):
    session_pnl_guard_enabled = True
    session_profit_arm_usdc = 8
    session_profit_drawdown_usdc = 4
    session_hard_profit_lock_enabled = False
    session_hard_profit_lock_usdc = 10
    session_max_loss_enabled = True
    session_max_loss_usdc = 6
    current_market_slug = None

    def __init__(self, db):
        self.trade_db = db
        self.run_id = "run_live"

    def _db_strategy_event(self, event_type, payload):
        return self.trade_db.log_strategy_event(self.run_id, event_type, payload)

    def _block_new_buys_for_trade_db(self, reason):
        pass


def _seed_market(db, slug, cycle_ts_local, *, journal_cycle, sell_px):
    start = int(slug.rsplit("-", 1)[1])
    fill_ts = datetime.fromtimestamp(start + 300, timezone.utc).isoformat()
    with db._connect() as conn:
        for coid, side, price in (("B-" + slug, "BUY", 0.81), ("S-" + slug, "SELL", sell_px)):
            conn.execute(
                """INSERT INTO order_events (ts, run_id, event_type, client_order_id, side, price, qty, token_id,
                   payload_json) VALUES (?, 'r', 'ORDER_FILLED', ?, ?, ?, 5.8, '111', ?)""",
                (fill_ts, coid, side, price, json.dumps({"slug": slug})),
            )
        conn.execute(
            "INSERT INTO strategy_events (ts, run_id, event_type, payload_json) VALUES (?, 'r', 'MARKET_CYCLE_PNL', ?)",
            (datetime.fromtimestamp(cycle_ts_local, timezone.utc).isoformat(),
             json.dumps({"slug": slug, "cycle_combined_pnl_usdc": journal_cycle})),
        )
        conn.commit()


@pytest.fixture
def guarded(tmp_path, monkeypatch):
    db = TradeJournalDB(str(tmp_path / "j.db"), backup_path=str(tmp_path / "b.db"))
    host = _Host(db)
    host._initialize_session_pnl_guard(SESSION_NOW)
    yield host, db
    db.stop()


def test_ghost_inventory_overstatement_is_corrected_once_in_the_open_session(guarded):
    host, db = guarded
    # Journal booked +6.76 (ghost payout); the fills alone realize 5.8 x (0.97 - 0.81) = 0.928.
    _seed_market(db, OPEN_SLUG, SESSION_NOW - 600, journal_cycle=6.76, sell_px=0.97)
    host._record_session_realized_pnl(Decimal("6.76"), source="settlement")  # what the guard saw live
    stats = host._apply_settlement_evidence_session_corrections()
    assert stats["applied"] == 1 and stats["applied_usdc"] == pytest.approx(0.928 - 6.76)
    assert host._session_pnl_guard.state.realized_pnl_usdc == pytest.approx(Decimal("0.928"))
    again = host._apply_settlement_evidence_session_corrections()
    assert again["applied"] == 0 and host._session_pnl_guard.state.realized_pnl_usdc == pytest.approx(Decimal("0.928"))


def test_closed_sessions_and_non_final_markets_are_never_rewritten(guarded):
    host, db = guarded
    closed_ts = datetime(2026, 9, 27, 22, 0, tzinfo=TPE).timestamp()
    _seed_market(db, CLOSED_SLUG, closed_ts, journal_cycle=6.76, sell_px=0.97)
    stats = host._apply_settlement_evidence_session_corrections()
    assert stats["applied"] == 0
    with db._connect() as conn:
        assert conn.execute("SELECT count(*) FROM strategy_events WHERE event_type=?",
                            (SESSION_CORRECTION_EVENT,)).fetchone()[0] == 0


def test_reconstruction_after_restart_includes_the_correction(guarded):
    host, db = guarded
    _seed_market(db, OPEN_SLUG, SESSION_NOW - 600, journal_cycle=6.76, sell_px=0.97)
    host._apply_settlement_evidence_session_corrections()
    state = db.reconstruct_session_pnl_state("2026-09-28")
    assert state["realized_pnl_usdc"] == pytest.approx(Decimal("0.928"))


def test_worker_runs_the_correction_hook_and_survives_its_failure(tmp_path):
    from bot.settlement_confirmation import SettlementConfirmationWorker
    import threading
    calls = []

    def hook():
        calls.append(1)
        raise RuntimeError("guard unavailable")

    db = TradeJournalDB(str(tmp_path / "j.db"), backup_path=str(tmp_path / "b.db"))
    db.stop()
    worker = SettlementConfirmationWorker(journal_path=tmp_path / "j.db", write_event=lambda *_: True,
                                          user_address="", after_cycle=hook, interval_sec=10)
    stop = threading.Event()
    thread = threading.Thread(target=worker.run, args=(stop,), daemon=True)
    thread.start()
    import time
    for _ in range(50):
        if calls:
            break
        time.sleep(0.02)
    stop.set()
    thread.join(timeout=2)
    assert calls and not thread.is_alive()
