import json
import sqlite3

from scripts.trade_path_pnl_report import build_report


def test_report_separates_maker_and_outcome_pnl_and_entry_timing(tmp_path):
    path = tmp_path / "journal.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE order_events (
                id INTEGER PRIMARY KEY, ts TEXT, event_type TEXT,
                client_order_id TEXT, side TEXT, price REAL, qty REAL,
                payload_json TEXT, instrument_id TEXT
            );
            CREATE TABLE strategy_events (
                id INTEGER PRIMARY KEY, ts TEXT, event_type TEXT, payload_json TEXT
            );
            """
        )
        order_rows = [
            (1, "2026-09-25T13:24:58+08:00", "ORDER_MAKER_INTENT", "maker-1", "BUY", .89, 5, {"slug": "btc-updown-15m-1790313300"}, "up-token"),
            (2, "2026-09-25T13:24:59+08:00", "ORDER_SUBMIT", "maker-1", "BUY", .89, 5, {}, "up-token"),
            (3, "2026-09-25T13:25:04+08:00", "ORDER_FILLED", "maker-1", "BUY", .89, 5, {"entry_source": "normal_maker", "slug": "btc-updown-15m-1790313300"}, "up-token"),
            (4, "2026-09-25T13:29:08+08:00", "ORDER_FILLED", "maker-sell", "SELL", .97, 5, {"realized_net_usdc": .4, "slug": "btc-updown-15m-1790313300"}, "up-token"),
            (5, "2026-09-25T13:30:00+08:00", "ORDER_FAST_FOLLOW_INTENT", "fok-1", "BUY", .8, 5, {"slug": "btc-updown-15m-1790314200", "signal_age_ms": 98}, "down-token"),
            (6, "2026-09-25T13:30:01+08:00", "ORDER_FAST_FOLLOW_SUBMIT", "fok-1", "BUY", .8, 5, {}, "down-token"),
            (7, "2026-09-25T13:30:02+08:00", "ORDER_FILLED", "fok-1", "BUY", .8, 5, {"entry_source": "outcome_fast_follow", "slug": "btc-updown-15m-1790314200"}, "down-token"),
        ]
        conn.executemany(
            "INSERT INTO order_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(i, ts, kind, coid, side, px, qty, json.dumps(payload), inst)
             for i, ts, kind, coid, side, px, qty, payload, inst in order_rows],
        )
        conn.execute(
            "INSERT INTO strategy_events VALUES (?, ?, ?, ?)",
            (1, "2026-09-25T13:24:57+08:00", "ENTRY_DECISION_TRACE", json.dumps({
                "state": "ALLOW", "slug": "btc-updown-15m-1790313300", "instrument_id": "up-token"
            })),
        )

    report = build_report(str(path))

    assert report["by_path"]["normal_maker"]["filled_buy_events"] == 1
    assert report["by_path"]["normal_maker"]["realized_net_usdc"] == .4
    assert report["by_path"]["normal_maker"]["mean_submit_to_fill_sec"] == 5
    assert report["by_path"]["normal_maker"]["mean_allow_to_intent_sec"] == 1
    assert report["by_path"]["outcome_fast_follow"]["filled_buy_events"] == 1
    assert report["by_path"]["outcome_fast_follow"]["realized_net_usdc"] == 0
    assert report["by_path"]["outcome_fast_follow"]["mean_signal_to_intent_ms"] == 98
