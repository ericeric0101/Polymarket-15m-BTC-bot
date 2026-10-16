"""Outcome provenance priority: official > canonical TWAP; journal is diagnostic only (tmp, offline)."""
import csv
import json
import sqlite3

from scripts.build_outcome_provenance import build


def _setup(tmp_path):
    cache = {"content_sha256": "abc", "fetched_at_utc": "t", "markets": [
        {"slug": "m-official-agree", "official_outcome": "UP"},
        {"slug": "m-official-vs-twap", "official_outcome": "UP"},
        {"slug": "m-unresolved", "official_outcome": "UNRESOLVED"},
    ]}
    (tmp_path / "cache.json").write_text(json.dumps(cache))
    a = tmp_path / "export" / "A_market_summary"
    a.mkdir(parents=True)
    with (a / "2026-10-07.csv").open("w", newline="") as handle:
        w = csv.DictWriter(handle, fieldnames=["market_slug", "settlement_side"])
        w.writeheader()
        w.writerows([{"market_slug": "m-official-agree", "settlement_side": "UP"},
                     {"market_slug": "m-official-vs-twap", "settlement_side": "DOWN"},
                     {"market_slug": "m-unresolved", "settlement_side": "DOWN"}])
    with sqlite3.connect(tmp_path / "j.db") as conn:
        conn.execute("CREATE TABLE strategy_events (id INTEGER PRIMARY KEY, event_type TEXT, payload_json TEXT)")
        for slug, outcome in (("m-official-agree", "DOWN"), ("m-journal-only", "UP"), ("m-unresolved", "UP")):
            conn.execute("INSERT INTO strategy_events (event_type, payload_json) VALUES ('MARKET_SETTLEMENT', ?)",
                         (json.dumps({"slug": slug, "outcome": outcome, "spot": 1, "strike": 1}),))
    return build(tmp_path / "cache.json", tmp_path / "export", tmp_path / "j.db")


def test_priority_and_mismatch_flags(tmp_path):
    rows, summary = _setup(tmp_path)
    by = {r["market_slug"]: r for r in rows}
    agree = by["m-official-agree"]
    assert agree["outcome_used"] == "UP" and agree["outcome_source_used"] == "POLYMARKET_OFFICIAL"
    assert agree["journal_mismatch"] == 1 and agree["twap_mismatch"] == 0
    conflict = by["m-official-vs-twap"]
    assert conflict["outcome_used"] == "UP" and conflict["market_resolution_confidence"] == "HIGH_OFFICIAL_TWAP_CONFLICT"
    twap_only = by["m-unresolved"]
    assert twap_only["outcome_used"] == "DOWN" and twap_only["outcome_source_used"] == "CANONICAL_TWAP"
    assert summary["twap_vs_official"] == {"compared": 2, "mismatch": 1, "rate": 0.5}


def test_journal_is_never_primary_truth(tmp_path):
    rows, _ = _setup(tmp_path)
    journal_only = {r["market_slug"]: r for r in rows}["m-journal-only"]
    assert journal_only["journal_outcome"] == "UP"
    assert journal_only["outcome_used"] == "UNKNOWN" and journal_only["outcome_source_used"] == "NONE"
    assert journal_only["journal_mismatch"] == ""  # nothing to compare against
