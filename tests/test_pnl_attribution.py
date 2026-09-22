import json
import sqlite3

import pytest

from monitoring.pnl_attribution import (
    load_fast_follow_pnl_summary,
    load_market_pnl_attribution,
    load_market_pnl_attributions,
)


def _event(conn, table, event_type, payload, **columns):
    names = ["ts", "run_id", "event_type", "payload_json", *columns.keys()]
    values = ["2026-08-13T00:00:00+00:00", "run", event_type, json.dumps(payload), *columns.values()]
    conn.execute(
        f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
        values,
    )


def test_pnl_attribution_keeps_taker_and_reconciliation_separate(tmp_path):
    db = tmp_path / "journal.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE order_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT,
          client_order_id TEXT, side TEXT, price REAL, qty REAL, payload_json TEXT);
        CREATE TABLE strategy_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT,
          payload_json TEXT);
        """,
    )
    slug = "btc-updown-15m-test"
    _event(conn, "order_events", "ORDER_FILLED", {"slug": slug, "effective_fee_usdc": 0.01}, client_order_id="buy", side="BUY", price=0.7, qty=10)
    _event(conn, "order_events", "ORDER_FILLED", {"slug": slug}, client_order_id="maker-sell", side="SELL", price=0.97, qty=5)
    _event(conn, "order_events", "ORDER_TAKER_EXIT_SUBMIT", {"slug": slug}, client_order_id="exit")
    _event(conn, "order_events", "ORDER_FILLED", {"slug": slug, "effective_fee_usdc": 0.01}, client_order_id="exit", side="SELL", price=0.4, qty=4.99)
    _event(conn, "strategy_events", "MARKET_SETTLEMENT", {"slug": slug, "redeem_value_usdc": 0.01})
    _event(conn, "strategy_events", "MARKET_CYCLE_PNL", {"slug": slug, "cycle_combined_pnl_usdc": -0.2})
    conn.commit()
    conn.close()

    result = load_market_pnl_attribution(db, slug)

    assert result["buy_notional_usdc"] == 7.0
    assert result["maker_sell_proceeds_usdc"] == 4.85
    assert result["taker_exit_proceeds_usdc"] == pytest.approx(1.996)
    assert result["computed_pnl_usdc"] == pytest.approx(-0.164)
    assert result["reconciliation_adjustment_usdc"] == pytest.approx(-0.036)


def test_pnl_attribution_prefers_confirmed_redeem_over_settlement_estimate(tmp_path):
    db = tmp_path / "journal.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE order_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT,
          client_order_id TEXT, side TEXT, price REAL, qty REAL, payload_json TEXT);
        CREATE TABLE strategy_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT,
          payload_json TEXT);
        """
    )
    slug = "btc-updown-15m-redeem"
    _event(conn, "order_events", "ORDER_FILLED", {"slug": slug}, client_order_id="buy", side="BUY", price=0.7, qty=10)
    _event(conn, "strategy_events", "MARKET_SETTLEMENT", {"slug": slug, "redeem_value_usdc": 10.0})
    _event(conn, "strategy_events", "REDEEM_EXECUTED", {"slug": slug, "condition_id": "0x1", "status": 1, "redeem_cash_usdc": 9.9})
    conn.commit()
    conn.close()

    result = load_market_pnl_attribution(db, slug)

    assert result["redeem_value_source"] == "onchain_redeem"
    assert result["redeem_value_usdc"] == 9.9
    assert result["computed_pnl_usdc"] == pytest.approx(2.9)


def test_pnl_attribution_does_not_treat_position_shares_as_redeem_cash(tmp_path):
    db = tmp_path / "journal.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE order_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT,
          client_order_id TEXT, side TEXT, price REAL, qty REAL, payload_json TEXT);
        CREATE TABLE strategy_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT,
          payload_json TEXT);
        """
    )
    slug = "btc-updown-15m-loser"
    _event(conn, "order_events", "ORDER_FILLED", {"slug": slug}, client_order_id="buy", side="BUY", price=0.7, qty=10)
    _event(conn, "strategy_events", "MARKET_SETTLEMENT", {"slug": slug, "redeem_value_usdc": 0.0})
    _event(conn, "strategy_events", "REDEEM_EXECUTED", {"slug": slug, "condition_id": "0x1", "status": 1, "redeem_position_size_shares": 10})
    conn.commit()
    conn.close()

    result = load_market_pnl_attribution(db, slug)

    assert result["redeem_value_source"] == "settlement_estimate"
    assert result["redeem_value_usdc"] == 0.0
    assert result["computed_pnl_usdc"] == -7.0


def test_pnl_attribution_marks_unmatched_sell_as_pre_journal_inventory(tmp_path):
    db = tmp_path / "journal.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE order_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT,
          client_order_id TEXT, side TEXT, price REAL, qty REAL, payload_json TEXT);
        CREATE TABLE strategy_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT,
          payload_json TEXT);
        """
    )
    slug = "btc-updown-15m-prejournal"
    _event(conn, "order_events", "ORDER_FILLED", {"slug": slug}, client_order_id="old-sell", side="SELL", price=0.97, qty=10)
    _event(conn, "strategy_events", "MARKET_CYCLE_PNL", {"slug": slug, "cycle_combined_pnl_usdc": 9.7})
    conn.commit()
    conn.close()

    result = load_market_pnl_attributions(db)[slug]

    assert result["computed_pnl_usdc"] == pytest.approx(9.7)
    assert result["accounting_status"] == "pre_journal_inventory"
    assert result["attributable_pnl_usdc"] is None
    assert result["reconciliation_adjustment_usdc"] is None


def test_fast_follow_pnl_summary_uses_only_source_pure_settled_trades(tmp_path):
    db = tmp_path / "journal.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE order_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT,
          client_order_id TEXT, side TEXT, price REAL, qty REAL, payload_json TEXT);
        CREATE TABLE strategy_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT,
          payload_json TEXT);
        """
    )
    settled = "btc-updown-15m-outcome-settled"
    open_slug = "btc-updown-15m-outcome-open"
    mixed = "btc-updown-15m-mixed"
    # Historic FOK rows did not carry entry_source; the established client ID
    # prefix is the conservative compatibility classification.
    _event(conn, "order_events", "ORDER_FILLED", {"slug": settled},
           client_order_id="BTC-15M-FAST-FOLLOW-BUY-1", side="BUY", price=0.60, qty=10)
    _event(conn, "strategy_events", "MARKET_SETTLEMENT", {"slug": settled, "redeem_value_usdc": 10.0})
    _event(conn, "order_events", "ORDER_FILLED", {"slug": open_slug, "entry_source": "outcome_fast_follow"},
           client_order_id="BTC-15M-FAST-FOLLOW-BUY-2", side="BUY", price=0.60, qty=10)
    _event(conn, "order_events", "ORDER_FILLED", {"slug": mixed, "entry_source": "outcome_fast_follow"},
           client_order_id="BTC-15M-FAST-FOLLOW-BUY-3", side="BUY", price=0.60, qty=5)
    _event(conn, "order_events", "ORDER_FILLED", {"slug": mixed, "entry_source": "normal_maker"},
           client_order_id="BTC-15M-MAKER-BUY-3", side="BUY", price=0.50, qty=5)
    _event(conn, "strategy_events", "MARKET_SETTLEMENT", {"slug": mixed, "redeem_value_usdc": 10.0})
    conn.commit()
    conn.close()

    summary = load_fast_follow_pnl_summary(db)

    assert summary["completed_trade_count"] == 1
    assert summary["completed_pnl_usdc"] == pytest.approx(4.0)
    assert summary["excluded_open_or_unreconciled_count"] == 1
    assert summary["excluded_mixed_source_count"] == 1
