import json
import sqlite3

import pytest

from monitoring.invalidation_counterfactual import load_invalidation_counterfactual


def _event(conn, table, event_type, payload, **columns):
    names = ["ts", "run_id", "event_type", "payload_json", *columns.keys()]
    values = ["2026-08-13T00:00:00+00:00", "run", event_type, json.dumps(payload), *columns.values()]
    conn.execute(
        f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
        values,
    )


def test_counterfactual_uses_journaled_invalidation_bid_only(tmp_path):
    db = tmp_path / "journal.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE order_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT,
          client_order_id TEXT, side TEXT, price REAL, qty REAL, payload_json TEXT, reason TEXT);
        CREATE TABLE strategy_events (id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT,
          payload_json TEXT);
        """,
    )
    slug = "btc-updown-15m-test"
    _event(conn, "strategy_events", "EXIT_AUDIT", {
        "slug": slug, "locked_side_invalidated": True, "best_bid": 0.45,
        "sellable_qty": 9.99, "avg_entry": 0.69, "time_left_sec": 400,
        "recovery_ratio": 0.65,
    })
    _event(conn, "strategy_events", "MARKET_CYCLE_PNL", {"slug": slug, "cycle_combined_pnl_usdc": -6.9})
    conn.commit()
    conn.close()

    result = load_invalidation_counterfactual(db, slug)

    assert result is not None
    assert result["evidence_source"] == "exit_audit"
    assert result["counterfactual_gross_pnl_usdc"] == pytest.approx(-2.3976)
    assert result["gross_improvement_vs_actual_usdc"] == pytest.approx(4.5024)
