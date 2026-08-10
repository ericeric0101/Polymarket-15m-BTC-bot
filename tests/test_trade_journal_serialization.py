import json
import sqlite3
from decimal import Decimal

import pytest

from monitoring.trade_journal_db import TradeJournalDB


def test_journal_serializes_decimal_payloads_as_numeric_json(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    payload = {"top_level": Decimal("0.895215"), "nested": {"price": Decimal("64890.80")}}

    db.log_strategy_event("run", "TEST_STRATEGY", payload)
    db.log_order_event("run", "TEST_ORDER", payload=payload)

    with sqlite3.connect(db.db_path) as conn:
        strategy_raw = conn.execute(
            "SELECT payload_json FROM strategy_events WHERE event_type='TEST_STRATEGY'"
        ).fetchone()[0]
        order_raw = conn.execute(
            "SELECT payload_json FROM order_events WHERE event_type='TEST_ORDER'"
        ).fetchone()[0]

    assert json.loads(strategy_raw) == {"top_level": 0.895215, "nested": {"price": 64890.8}}
    assert json.loads(order_raw) == {"top_level": 0.895215, "nested": {"price": 64890.8}}


def test_journal_calibrates_maker_buy_adverse_markout_from_observed_fills(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    # Six observed 10-second markouts: four adverse and two favourable.
    for markout in ("-0.02", "0.01", "-0.04", "-0.06", "0.02", "-0.03"):
        db.log_order_event(
            "run",
            "FILL_MARKOUT",
            side="BUY",
            payload={
                "liquidity_class": "maker",
                "horizon_sec": 10,
                "signed_markout_ps": Decimal(markout),
            },
        )

    calibration = db.load_maker_buy_markout_calibration(
        lookback_hours=24,
        horizon_sec=10,
        min_samples=6,
    )

    assert calibration is not None
    assert calibration["sample_count"] == 6
    # Mean(max(0, -markout)) = (0.02 + 0 + 0.04 + 0.06 + 0 + 0.03) / 6.
    assert calibration["adverse_markout_per_share"] == pytest.approx(0.025)
