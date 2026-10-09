"""Tier B decision export and tier C partitions / deletion eligibility (tmp only, no deletion)."""
import json
import os
import sqlite3
import stat
from datetime import date, datetime, timezone

import pyarrow.parquet as pq
import pytest

from bot.research import daily_export, decision_export, partitions
from bot.research.store import ResearchStore

DAY = date(2026, 10, 7)
DAY_START = datetime(2026, 10, 7, tzinfo=timezone.utc).timestamp()
AFTER = DAY_START + 86400 + 3600


# --- Tier B -----------------------------------------------------------------------------------

@pytest.fixture
def journal(tmp_path):
    from monitoring.trade_journal_db import TradeJournalDB
    db = TradeJournalDB(tmp_path / "journal.db")
    db.log_strategy_event("run", "SIDE_DECISION", {"slug": "m", "fair_up": 0.6, "market_consensus": 0.19,
                                                   "btc_trend": 0.02, "strike_proximity": 0.3})
    for state in ("REJECT", "ALLOW"):
        db.log_strategy_event("run", "ENTRY_DECISION_TRACE", {
            "slug": "m", "state": state, "final_reason": "directional_entry_gate", "fair": 0.6,
            "research_snapshot": {"abs_distance_bps": 4.2, "safety_sigma": 1.1},
            "decision_trace": {"required_move_sigma": 1.3, "l2": {"status": "L2_AVAILABLE"}}})
    db.log_order_event("run", "ORDER_DRY_RUN_SUBMITTED", client_order_id="DRY-1", side="BUY", price=0.6, qty=5,
                       payload={"slug": "m"})
    db.log_strategy_event("run", "SIDE_INVALIDATION_CONFIRMED", {"slug": "m", "hits": 3})
    db.log_strategy_event("run", "QUOTE_TRANSPORT_TELEMETRY", {"ignored": True})
    db.stop()
    # Pin every row into DAY (the journal stamps wall-clock time).
    with sqlite3.connect(tmp_path / "journal.db") as conn:
        conn.execute("UPDATE strategy_events SET ts = '2026-10-07T12:00:00+00:00'")
        conn.execute("UPDATE order_events SET ts = '2026-10-07T12:00:01+00:00'")
    return tmp_path / "journal.db"


def test_tier_b_tables_capture_side_entry_and_execution_evidence(journal, tmp_path):
    manifest = decision_export.export_day(journal, DAY, tmp_path / "out", now=AFTER)
    assert manifest["verified"] is True
    assert {k: v["rows"] for k, v in manifest["files"].items()} == {
        "side_decisions": 1, "entry_decisions": 2, "executions": 2}
    entry = pq.read_table(tmp_path / "out" / "B_decisions" / str(DAY) / "entry_decisions.parquet").to_pylist()
    assert entry[0]["snap_abs_distance_bps"] == 4.2 and entry[0]["trace_required_move_sigma"] == 1.3
    assert json.loads(entry[0]["trace_l2"]) == {"status": "L2_AVAILABLE"}
    side = pq.read_table(tmp_path / "out" / "B_decisions" / str(DAY) / "side_decisions.parquet").to_pylist()
    assert side[0]["market_consensus"] == 0.19
    executions = pq.read_table(tmp_path / "out" / "B_decisions" / str(DAY) / "executions.parquet").to_pylist()
    assert [r["event_type"] for r in executions] == ["SIDE_INVALIDATION_CONFIRMED", "ORDER_DRY_RUN_SUBMITTED"]
    assert decision_export.day_is_exported(tmp_path / "out", DAY) is True


def test_tier_b_tamper_and_incomplete_day(journal, tmp_path):
    with pytest.raises(ValueError):
        decision_export.export_day(journal, DAY, tmp_path / "out", now=DAY_START + 3600)
    decision_export.export_day(journal, DAY, tmp_path / "out", now=AFTER)
    (tmp_path / "out" / "B_decisions" / str(DAY) / "executions.parquet").write_bytes(b"tampered")
    assert decision_export.day_is_exported(tmp_path / "out", DAY) is False


# --- Tier C -----------------------------------------------------------------------------------

def _live(tmp_path, rows):
    path = tmp_path / "live.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE lead_lag_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, "
                     "slug TEXT NOT NULL, market_id INTEGER, decision_epoch_ns INTEGER NOT NULL, payload_json TEXT NOT NULL)")
        conn.execute("CREATE INDEX idx_t ON lead_lag_decisions(decision_epoch_ns)")
        conn.executemany("INSERT INTO lead_lag_decisions (run_id, slug, decision_epoch_ns, payload_json) "
                         "VALUES ('r', 'm', ?, ?)", [(int(ts * 1e9), json.dumps(p)) for ts, p in rows])
    return path


def _rows():
    return ([(DAY_START + 60 * i, {"event_type": "X", "i": i}) for i in range(50)]
            + [(DAY_START - 30, {"event_type": "before"}), (DAY_START + 86400 + 5, {"event_type": "after"})])


def test_split_copies_exactly_one_day_read_only_and_verified(tmp_path):
    live = _live(tmp_path, _rows())
    manifest = partitions.split_day(live, DAY, tmp_path / "parts", now=AFTER, disk_margin_bytes=0)
    target = tmp_path / "parts" / f"C_{DAY}.db"
    assert manifest["verified"] is True and manifest["rows"] == 50
    assert not os.stat(target).st_mode & stat.S_IWUSR
    with sqlite3.connect(f"file:{target}?mode=ro", uri=True) as conn:
        events = {json.loads(r[0])["event_type"] for r in conn.execute("SELECT payload_json FROM lead_lag_decisions")}
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name='idx_t'").fetchone()[0] == 1
    assert events == {"X"}
    assert partitions.split_day(live, DAY, tmp_path / "parts", now=AFTER, disk_margin_bytes=0) == manifest  # idempotent


def test_split_refuses_incomplete_day_and_low_disk(tmp_path):
    live = _live(tmp_path, _rows())
    with pytest.raises(ValueError):
        partitions.split_day(live, DAY, tmp_path / "parts", now=DAY_START + 3600, disk_margin_bytes=0)
    with pytest.raises(OSError):
        partitions.split_day(live, DAY, tmp_path / "parts", now=AFTER, disk_margin_bytes=1 << 62)


def _exports(tmp_path, *, offsite=True):
    store_rows = [(f"btc-updown-15m-{int(DAY_START)}", DAY_START + 901,
                   {"event_type": "MARKET_TWAP_SUMMARY", "market_slug": f"btc-updown-15m-{int(DAY_START)}",
                    "canonical_settlement_side": "UP", "settlement_reference_is_canonical": True})]
    path = tmp_path / "store.db"
    if path.exists():
        path.unlink()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE lead_lag_decisions (id INTEGER PRIMARY KEY, run_id TEXT, slug TEXT, "
                     "market_id INTEGER, decision_epoch_ns INTEGER, payload_json TEXT)")
        conn.executemany("INSERT INTO lead_lag_decisions (run_id, slug, decision_epoch_ns, payload_json) "
                         "VALUES ('r', ?, ?, ?)", [(s, int(t * 1e9), json.dumps(p)) for s, t, p in store_rows])
    from monitoring.trade_journal_db import TradeJournalDB
    TradeJournalDB(tmp_path / "j.db").stop()
    offsite_root = tmp_path / "icloud" if offsite else None
    daily_export.export_day(ResearchStore(path), DAY, tmp_path / "export", offsite_root=offsite_root, now=AFTER)
    decision_export.export_day(tmp_path / "j.db", DAY, tmp_path / "export", offsite_root=offsite_root, now=AFTER)


def test_eligibility_requires_partition_exports_offsite_and_unchanged_live_rows(tmp_path):
    live = _live(tmp_path, _rows())
    kwargs = dict(live_db=live, part_dir=tmp_path / "parts", export_root=tmp_path / "export")
    nothing = partitions.deletion_eligibility(DAY, now=AFTER, **kwargs)
    assert nothing["live_rows_deletion_eligible"] is False and nothing["deletion_enabled"] is False
    partitions.split_day(live, DAY, tmp_path / "parts", now=AFTER, disk_margin_bytes=0)
    _exports(tmp_path, offsite=False)
    no_offsite = partitions.deletion_eligibility(DAY, now=AFTER, **kwargs)
    assert no_offsite["checks"]["A_offsite_verified"] is False and not no_offsite["live_rows_deletion_eligible"]
    _exports(tmp_path, offsite=True)
    ready = partitions.deletion_eligibility(DAY, now=AFTER, **kwargs)
    assert ready["live_rows_deletion_eligible"] is True
    assert ready["partition_deletion_eligible"] is False  # retention not reached
    later = partitions.deletion_eligibility(DAY, now=AFTER + 7 * 86400, **kwargs)
    assert later["partition_deletion_eligible"] is True and later["deletion_enabled"] is False
    # A late write into the partitioned day invalidates live-row deletion.
    with sqlite3.connect(live) as conn:
        conn.execute("INSERT INTO lead_lag_decisions (run_id, slug, decision_epoch_ns, payload_json) "
                     "VALUES ('r', 'm', ?, '{}')", (int((DAY_START + 10) * 1e9),))
    late = partitions.deletion_eligibility(DAY, now=AFTER, **kwargs)
    assert late["checks"]["live_rows_match_partition"] is False and not late["live_rows_deletion_eligible"]


def test_module_contains_no_deletion_code():
    import inspect
    source = inspect.getsource(partitions)
    for forbidden in ("DELETE FROM", "unlink(", "os.remove", "rmtree", "VACUUM"):
        assert forbidden not in source.replace("temporary.unlink(missing_ok=True)", "")
