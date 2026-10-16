"""Research consumers read the shared effective PnL (or label their fallback explicitly)."""
import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from bot.settlement_evidence import OUTCOME_EVENT
from monitoring.trade_journal_db import SESSION_CORRECTION_EVENT
from test_effective_pnl import SLUG, Journal, official

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_live_entry_quality_report_uses_whole_market_effective_pnl(tmp_path):
    from scripts.live_entry_quality_report import load_candidates
    j = Journal(tmp_path)
    candidate = f"{SLUG}|111|1"
    snap = {"candidate_id": candidate, "slug": SLUG, "entry_source": "outcome_fast_follow", "outcome_side": "UP",
            "side": "BUY", "entry_price": 0.70, "planned_quantity": 10}
    j.strategy("FAST_FOLLOW_QUOTE_HANDOFF", {"research_snapshot": snap, "research_candidate_id": candidate},
               ts="2026-09-21T13:38:00+00:00")
    j.conn.execute(
        "INSERT INTO order_events (ts, run_id, event_type, client_order_id, side, price, qty, status, payload_json)"
        " VALUES ('2026-09-21T13:38:10+00:00', 'run_a', 'ORDER_FAST_FOLLOW_SUBMIT', 'C1', 'BUY', 0.7, 10, 'SUBMITTED', ?)",
        (json.dumps({"research_candidate_id": candidate, "slug": SLUG}),),
    )
    j.conn.commit()
    j.fill("BUY", 0.70, 10.0, coid="C1")
    j.fill("SELL", 0.90, 4.0)
    # The journal's settlement value covers only the held remainder (and is an estimate).
    j.settle("UP", 6.0, settlement_pnl_usdc=1.8)
    j.strategy(OUTCOME_EVENT, official("UP"))
    j.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # the report opens the journal immutable=1
    (row,) = load_candidates(j.path)
    assert row["journal_settlement_pnl_usdc"] == 1.8
    assert row["market_pnl_basis"] == "OUTCOME_CONFIRMED"
    assert row["settlement_pnl_usdc"] == pytest.approx(3.6 + 6.0 - 7.0)


def test_session_guard_replay_includes_evidence_corrections(tmp_path):
    j = Journal(tmp_path)
    j.strategy("MARKET_CYCLE_PNL", {"slug": SLUG, "cycle_combined_pnl_usdc": 6.76}, ts="2026-09-28T13:00:00+00:00")
    j.strategy(SESSION_CORRECTION_EVENT, {"slug": SLUG, "session_date_taipei": "2026-09-28", "delta_usdc": -5.832},
               ts="2026-09-28T13:05:00+00:00")
    out = tmp_path / "replay.csv"
    result = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "replay_session_pnl_guard.py"),
                             "--db", str(j.path), "--output", str(out)],
                            capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT))
    assert result.returncode == 0, result.stderr
    rows = [r for r in csv.DictReader(out.open()) if r["scenario"] == "NO_GUARD"]
    assert float(rows[0]["actual_final_pnl"]) == pytest.approx(6.76 - 5.832)


def test_forward_shadow_live_join_carries_effective_pnl(tmp_path):
    from scripts.forward_shadow_report import _live_join
    j = Journal(tmp_path)
    j.fill("BUY", 0.70, 10.0)
    j.settle("UP", 10.0, cycle_pnl=3.0)
    j.strategy(OUTCOME_EVENT, official("DOWN"))
    rows = _live_join([{"slug": SLUG, "instrument_id": "0xc-111.POLYMARKET"}], j.path)
    joined = rows[0]
    assert joined["live_cycle_combined_pnl_usdc"] == 3.0                 # journal estimate, kept
    assert joined["live_effective_pnl_usdc"] == pytest.approx(-7.0)       # official outcome: lost
    assert joined["live_effective_pnl_basis"] == "OUTCOME_CONFIRMED"
