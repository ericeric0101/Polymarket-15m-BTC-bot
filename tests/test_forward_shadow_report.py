import csv
import sqlite3

from monitoring.lead_lag_db import LeadLagDB
from scripts.forward_shadow_report import OUTPUTS, build_report


def test_forward_shadow_report_writes_required_outputs_and_status(tmp_path):
    db_path = tmp_path / "research.db"
    db = LeadLagDB(str(db_path))
    db.enqueue_decision(run_id="r", slug="btc-updown-15m-a", market_id=None,
                        decision_epoch_ns=1_000_000_000,
                        payload={"event_type": "SHADOW_ENTRY_CANDIDATE", "candidate_id": "s|120_0",
                                 "slug": "s", "entry_config": "120_0", "candidate_side": "UP",
                                 "experiment_class": "WEEKDAY_PRIMARY", "entry_top_ask": .5,
                                 "instrument_id": "token"})
    db.enqueue_decision(run_id="r", slug="s", market_id=None, decision_epoch_ns=2_000_000_000,
                        payload={"event_type": "SHADOW_POSITION_MARK", "candidate_id": "s|120_0",
                                 "position_id": "s|120_0|ENTRY_TOP_ASK", "entry_variant": "ENTRY_TOP_ASK",
                                 "entry_config": "120_0", "best_bid": .51, "mfe_bid_pct": .02,
                                 "mae_bid_pct": -.01})
    db.enqueue_decision(run_id="r", slug="s", market_id=None, decision_epoch_ns=3_000_000_000,
                        payload={"event_type": "SHADOW_SETTLEMENT", "candidate_id": "s|120_0",
                                 "position_id": "s|120_0|ENTRY_TOP_ASK", "entry_variant": "ENTRY_TOP_ASK",
                                 "entry_config": "120_0", "exit_policy": "HOLD", "pnl_usdc": 5.0,
                                 "is_weekend": False, "mfe_bid_pct": .02, "mae_bid_pct": -.01,
                                 "settlement_status": "observed", "event_ts": 3.0})
    db.stop()

    output = tmp_path / "reports" / "forward_shadow"
    status = build_report(db_path, output)
    assert status["markets"] == 1
    assert status["weekday_candidates"] == 1
    assert all((output / name).exists() for name in OUTPUTS)
    assert (output / "summary.md").exists()
    with (output / "candidate_entries.csv").open(encoding="utf-8") as handle:
        assert next(csv.DictReader(handle))["candidate_id"] == "s|120_0"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM lead_lag_decisions").fetchone()[0] == 3
