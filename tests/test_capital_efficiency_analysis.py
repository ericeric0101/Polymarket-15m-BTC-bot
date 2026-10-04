from __future__ import annotations

import csv
import json
import sqlite3

from scripts.research_analysis import (
    _entry_timing_bin,
    _read_shadow_settlements,
    capital_efficiency_analysis,
)


WEEKEND_SLUG = "btc-updown-15m-1791072000"  # 2026-10-04 00:00 UTC / Taipei weekend


def _make_journal(path):
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE order_events (
            id INTEGER PRIMARY KEY, ts TEXT, run_id TEXT, event_type TEXT, payload_json TEXT
        )""")


def _make_research(path):
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE lead_lag_decisions (
            run_id TEXT, slug TEXT, decision_epoch_ns INTEGER, payload_json TEXT
        )""")


def _settled_payload(*, simulation_id="s1", entry_price=0.5, qty=10.0, filled_ts=1791072300.0, time_left=600.0):
    return {
        "simulation_id": simulation_id,
        "slug": WEEKEND_SLUG,
        "side": "UP",
        "entry_price": entry_price,
        "qty": qty,
        "filled_ts": filled_ts,
        "time_left_sec": time_left,
        "simulated_gross_pnl_usdc": 5.0,
        "simulated_pnl_usdc": 4.9,
        "outcome": "UP",
        "won": True,
    }


def test_entry_timing_bins_use_the_fixed_documented_boundaries():
    assert _entry_timing_bin(601) == ">600s"
    assert _entry_timing_bin(600) == "480–600s"
    assert _entry_timing_bin(480) == "480–600s"
    assert _entry_timing_bin(360) == "360–480s"
    assert _entry_timing_bin(120) == "120–240s"
    assert _entry_timing_bin(119.9) == "<120s"


def test_shadow_settlement_reconstruction_derives_capital_and_holding_and_dedupes(tmp_path):
    journal = tmp_path / "journal.db"
    _make_journal(journal)
    valid = _settled_payload()
    duplicate = _settled_payload()
    missing_capital = _settled_payload(simulation_id="s2", entry_price=None)
    with sqlite3.connect(journal) as conn:
        conn.execute("INSERT INTO order_events VALUES (1, ?, 'run', 'SHADOW_SIM_SETTLED', ?)",
                     ("2026-10-04T00:10:00+00:00", json.dumps(valid)))
        conn.execute("INSERT INTO order_events VALUES (2, ?, 'run', 'SHADOW_SIM_SETTLED', ?)",
                     ("2026-10-04T00:10:01+00:00", json.dumps(duplicate)))
        conn.execute("INSERT INTO order_events VALUES (3, ?, 'run', 'SHADOW_SIM_SETTLED', ?)",
                     ("2026-10-04T00:10:00+00:00", json.dumps(missing_capital)))

    rows, audit = _read_shadow_settlements(journal, regime="WEEKEND")

    assert len(rows) == 2
    valid_row = next(row for row in rows if row["simulation_id"] == "s1")
    assert valid_row["status"] == "USABLE"
    assert valid_row["capital_committed_usdc"] == 5.0
    # Deduplication deterministically keeps the most recently persisted
    # settlement event, so its one-second later event timestamp is canonical.
    assert valid_row["holding_sec"] == 301.0
    assert valid_row["capital_minutes"] == 25.083333333333332
    assert valid_row["gross_profit_per_dollar_minute"] == 5.0 / 25.083333333333332
    assert valid_row["settlement_ts_source"] == "SHADOW_SIM_SETTLED_EVENT_TS"
    assert next(row for row in rows if row["simulation_id"] == "s2")["exclusion_reason"] == "CAPITAL_UNKNOWN"
    assert audit[0]["exclusion_reason"] == "duplicate_settlement_superseded"


def test_capital_efficiency_report_excludes_unknown_capital_without_imputation(tmp_path):
    journal, research, output = tmp_path / "journal.db", tmp_path / "research.db", tmp_path / "reports"
    _make_journal(journal)
    _make_research(research)
    with sqlite3.connect(journal) as conn:
        conn.execute("INSERT INTO order_events VALUES (1, ?, 'run', 'SHADOW_SIM_SETTLED', ?)",
                     ("2026-10-04T00:10:00+00:00", json.dumps(_settled_payload())))
        conn.execute("INSERT INTO order_events VALUES (2, ?, 'run', 'SHADOW_SIM_ENTRY_FILLED', ?)",
                     ("2026-10-04T00:05:00+00:00", json.dumps(_settled_payload())))
        conn.execute("INSERT INTO order_events VALUES (3, ?, 'run', 'SHADOW_SIM_SETTLED', ?)",
                     ("2026-10-04T00:10:00+00:00", json.dumps(_settled_payload(simulation_id="missing", qty=None))))

    result = capital_efficiency_analysis(research, journal, output)

    assert result["usable_trades"] == 1
    with (output / "trade_capital_efficiency.csv").open() as handle:
        trades = list(csv.DictReader(handle))
    assert len(trades) == 1
    assert trades[0]["capital_committed_usdc"] == "5.0"
    with (output / "data_quality.csv").open() as handle:
        quality = list(csv.DictReader(handle))
    assert any(row.get("exclusion_reason") == "CAPITAL_UNKNOWN" for row in quality)
    assert (output / "stopped_trade_capital_time.csv").exists()
