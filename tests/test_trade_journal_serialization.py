import json
import sqlite3
from decimal import Decimal
from datetime import datetime, timezone

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


def test_journal_recovers_latest_fast_follow_night_risk(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    db.log_strategy_event("run-1", "FAST_FOLLOW_RISK_STATE", {
        "night_key": "2026-09-08", "attempted_entries": 2, "realized_pnl_usdc": -1.25,
    })
    db.log_strategy_event("run-2", "FAST_FOLLOW_RISK_STATE", {
        "night_key": "2026-09-08", "attempted_entries": 3, "realized_pnl_usdc": -2.0,
    })
    assert db.load_fast_follow_night_risk("2026-09-08") == {
        "filled_entries": 0, "pending_entries": 0, "legacy_attempted_entries": 3,
        "attempted_entries": 0, "open_position_instruments": [], "realized_pnl_usdc": -2.0,
    }


def test_journal_recovers_new_fast_follow_filled_and_pending_risk(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    db.log_strategy_event("run", "FAST_FOLLOW_RISK_STATE", {
        "night_key": "2026-09-08", "filled_entries": 4, "pending_entries": 1,
        "attempted_entries": 4, "open_position_instruments": ["up-inst"], "realized_pnl_usdc": -1.25,
    })
    assert db.load_fast_follow_night_risk("2026-09-08") == {
        "filled_entries": 4, "pending_entries": 1, "legacy_attempted_entries": 0,
        "attempted_entries": 4, "open_position_instruments": ["up-inst"], "realized_pnl_usdc": -1.25,
    }


def test_journal_recovers_fast_follow_buy_submit_for_ghost_cost_basis(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    instrument_id = "fast-follow-inst"
    db.log_order_event(
        "run", "ORDER_FAST_FOLLOW_SUBMIT", side="BUY", price=0.67, qty=10,
        instrument_id=instrument_id, status="SUBMITTED",
        payload={"instrument_id": instrument_id},
    )
    rows = db.load_recent_buy_submits(instrument_id)
    assert len(rows) == 1
    assert rows[0]["price"] == pytest.approx(0.67)
    assert rows[0]["qty"] == pytest.approx(10)


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
    # With six values the nearest-rank P90 cap is the maximum, so the
    # winsorized estimate equals the raw mean.
    assert calibration["adverse_markout_per_share"] == pytest.approx(0.025)
    assert calibration["method"] == "winsorized_p90_mean"


def test_journal_uses_regime_markout_only_after_that_regime_has_samples(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    for markout in ("-0.01", "-0.02", "0.01"):
        db.log_order_event(
            "run", "FILL_MARKOUT", side="BUY",
            payload={
                "liquidity_class": "maker", "horizon_sec": 10,
                "signed_markout_ps": Decimal(markout), "entry_regime_bucket": "10_30",
                "entry_side_score": 0.40, "entry_time_left_sec": 480,
            },
        )
    calibrations = db.load_maker_buy_markout_calibrations(
        lookback_hours=24, horizon_sec=10, min_samples=3,
    )
    assert calibrations["global"]["sample_count"] == 3
    assert calibrations["10_30"]["sample_count"] == 3
    assert "30_60" not in calibrations


def test_journal_session_calibration_uses_v2_first_fill_per_taipei_weeknight_market(tmp_path):
    db = TradeJournalDB(tmp_path / "journal.db")
    payload = {
        "liquidity_class": "maker", "horizon_sec": 10,
        "markout_context_schema_version": 2, "slug": "night-market",
    }
    db.log_order_event("run", "FILL_MARKOUT", side="BUY", payload={**payload, "signed_markout_ps": -0.02})
    db.log_order_event("run", "FILL_MARKOUT", side="BUY", payload={**payload, "signed_markout_ps": -0.20})
    db.log_order_event(
        "run", "FILL_MARKOUT", side="BUY",
        payload={**payload, "slug": "day-market", "signed_markout_ps": -0.50},
    )
    with sqlite3.connect(db.db_path) as conn:
        # 2026-08-28 20:00 Taipei (Friday night) and 2026-08-31 10:00 Taipei (Monday day).
        conn.execute("UPDATE order_events SET ts=? WHERE id=1", ("2026-08-28T12:00:00+00:00",))
        conn.execute("UPDATE order_events SET ts=? WHERE id=2", ("2026-08-28T12:01:00+00:00",))
        conn.execute("UPDATE order_events SET ts=? WHERE id=3", ("2026-08-31T02:00:00+00:00",))
        conn.commit()

    calibrations = db.load_maker_buy_markout_calibrations(
        lookback_hours=10000, horizon_sec=10, min_samples=1,
        taipei_weeknight_schema_v2_only=True,
    )

    assert calibrations["global"]["sample_count"] == 1
    assert calibrations["global"]["adverse_markout_per_share"] == pytest.approx(0.02)
    assert calibrations["global"]["source"] == "taipei_weeknight_schema_v2_first_market"
