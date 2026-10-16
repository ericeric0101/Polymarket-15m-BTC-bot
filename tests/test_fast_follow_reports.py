import json
import sqlite3

from scripts.fast_follow_execution_report import build_report, rejection_class
from scripts.feed_health_report import build_report as build_feed_health_report


def _event(conn, table, event_type, payload, **columns):
    names = ["ts", "run_id", "event_type", "payload_json", *columns]
    values = ["2026-09-14T00:00:00+00:00", "r", event_type, json.dumps(payload), *columns.values()]
    conn.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})", values)


def test_fast_follow_report_separates_fok_and_precision_rejections(tmp_path):
    db = tmp_path / "journal.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
      CREATE TABLE order_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT,
        client_order_id TEXT, side TEXT, price REAL, qty REAL, reason TEXT, payload_json TEXT);
      CREATE TABLE strategy_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT, payload_json TEXT);
    """)
    order_id = "BTC-15M-FAST-FOLLOW-BUY-1"
    _event(conn, "order_events", "ORDER_FAST_FOLLOW_SUBMIT", {"limit_price": 0.61}, client_order_id=order_id, side="BUY", price=.61, qty=10, reason=None)
    _event(conn, "order_events", "ORDER_REJECTED", {}, client_order_id=order_id, side="BUY", reason="order couldn't be fully filled. FOK orders are fully filled or killed.")
    other = "BTC-15M-FAST-FOLLOW-BUY-2"
    _event(conn, "order_events", "ORDER_FAST_FOLLOW_SUBMIT", {}, client_order_id=other, side="BUY", price=.60, qty=10, reason=None)
    _event(conn, "order_events", "ORDER_REJECTED", {}, client_order_id=other, side="BUY", reason="maker amount precision invalid")
    conn.commit()
    conn.close()

    report = build_report(str(db))
    assert report["fok_unfilled"]["count"] == 1
    assert report["amount_precision"]["count"] == 1
    assert rejection_class("FOK orders are fully filled or killed") == "fok_unfilled"


def test_feed_health_report_summarizes_recovery_and_outcome_disconnects(tmp_path):
    db = tmp_path / "journal.db"
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE strategy_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT, payload_json TEXT);")
    _event(conn, "strategy_events", "POLYMARKET_TWAP_SILENT_STALL", {"silence_sec": 16.0})
    _event(conn, "strategy_events", "POLYMARKET_TWAP_WS_RECOVERED", {"feed_unavailable_sec": 2.0, "first_valid_twap_after_connect_sec": .2})
    _event(conn, "strategy_events", "HYPERLIQUID_OUTCOME_OBSERVER_DISCONNECTED", {"retry_delay_sec": 4, "error_type": "ConnectionClosedError"})
    conn.commit()
    conn.close()

    report = build_feed_health_report(str(db))
    assert report["twap_silent_stall_sec"]["max"] == 16.0
    assert report["twap_feed_unavailable_sec"]["mean"] == 2.0
    assert report["outcome_disconnect_errors"] == {"ConnectionClosedError": 1}
