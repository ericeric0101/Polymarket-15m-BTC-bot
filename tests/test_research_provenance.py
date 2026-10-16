from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from bot.research import provenance
from bot.research.store import ResearchStore
from bot.ops import log_strategy_run_start


@dataclass(frozen=True)
class Config:
    maker: dict
    side: dict
    operations: dict
    api_secret: str


def test_run_manifest_hash_is_deterministic_and_excludes_secrets(monkeypatch, tmp_path):
    monkeypatch.setattr(provenance, "_git_metadata", lambda _root: {
        "git_commit": "abc123", "git_branch": "codex/test", "git_dirty": True,
        "dirty_diff_hash": "diff-hash",
    })
    config = Config(
        maker={"price": Decimal("0.7"), "wallet_db_path": "/private/path", "api_token": "do-not-store"},
        side={"threshold": Decimal("0.2")},
        operations={"session_pnl_guard_mode": "shadow_target_scaled_v2"},
        api_secret="never-store",
    )
    first = provenance.build_run_manifest(
        run_id="run-1", config=config, mode="TEST_DRY_RUN", test_mode=True,
        maker_mode=True, repo_root=tmp_path,
    )
    second = provenance.build_run_manifest(
        run_id="run-2", config=config, mode="TEST_DRY_RUN", test_mode=True,
        maker_mode=True, repo_root=tmp_path,
    )
    assert first["config_hash"] == second["config_hash"]
    assert first["git_dirty"] is True
    assert first["dirty_diff_hash"] == "diff-hash"
    assert first["schema_versions"]["session_guard_version"] == 2
    encoded = json.dumps(first, sort_keys=True)
    assert "never-store" not in encoded
    assert "do-not-store" not in encoded
    assert "/private/path" not in encoded
    assert first["safe_config"]["maker"]["price"] == "0.7"


def test_run_provenance_joins_manifest_and_market_counts_read_only(tmp_path):
    research_path = tmp_path / "research.db"
    journal_path = tmp_path / "journal.db"
    with sqlite3.connect(research_path) as conn:
        conn.execute("CREATE TABLE lead_lag_decisions (run_id TEXT, slug TEXT, decision_epoch_ns INTEGER, payload_json TEXT)")
        slug = "btc-updown-15m-1790996400"  # Saturday in Taipei time.
        payload = json.dumps({"event_type": "PREDICTION_RESEARCH_SNAPSHOT"})
        conn.executemany("INSERT INTO lead_lag_decisions VALUES (?, ?, ?, ?)", [
            ("run-a", slug, 1_800_000_000_000_000_000, payload),
            ("run-a", slug, 1_800_000_001_000_000_000, payload),
        ])
    with sqlite3.connect(journal_path) as conn:
        conn.execute("CREATE TABLE strategy_runs (run_id TEXT, started_at TEXT, ended_at TEXT, mode TEXT, notes_json TEXT)")
        manifest = {"git_commit": "abc", "config_hash": "cfg", "schema_versions": {"prediction_schema_version": 1}}
        conn.execute("INSERT INTO strategy_runs VALUES (?, ?, ?, ?, ?)", (
            "run-a", "2026-10-03T00:00:00Z", None, "TEST_DRY_RUN",
            json.dumps({"run_manifest": manifest}),
        ))
    research = ResearchStore(research_path)
    result = research.get_run_provenance(journal_path)
    assert result["run_count"] == 1
    assert result["market_count"] == 1
    assert result["weekend_market_count"] == 1
    assert result["runs"][0]["provenance_status"] == "MANIFESTED"
    assert result["runs"][0]["git_commit"] == "abc"
    with sqlite3.connect(research_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM lead_lag_decisions").fetchone()[0] == 2


def test_legacy_run_provenance_is_unknown_not_backfilled(tmp_path):
    research_path = tmp_path / "research.db"
    journal_path = tmp_path / "journal.db"
    with sqlite3.connect(research_path) as conn:
        conn.execute("CREATE TABLE lead_lag_decisions (run_id TEXT, slug TEXT, decision_epoch_ns INTEGER, payload_json TEXT)")
    with sqlite3.connect(journal_path) as conn:
        conn.execute("CREATE TABLE strategy_runs (run_id TEXT, started_at TEXT, ended_at TEXT, mode TEXT, notes_json TEXT)")
        conn.execute("INSERT INTO strategy_runs VALUES ('legacy', '2026-01-01', NULL, 'TEST_DRY_RUN', '{}')")
    result = ResearchStore(research_path).get_run_provenance(journal_path)
    assert result["legacy_unmanifested_runs"] == 1
    assert result["runs"][0]["provenance_status"] == "LEGACY_UNKNOWN"
    assert result["runs"][0]["config_hash"] is None


def test_run_start_persists_manifest_in_existing_notes_container():
    calls = []

    class Journal:
        def log_run_start(self, **kwargs):
            calls.append(kwargs)

    manifest = {"run_id": "run-x", "config_hash": "hash"}
    log_strategy_run_start(
        Journal(), "run-x", True, False, True, "instrument", "slug",
        "both", 1.0, run_manifest=manifest,
    )
    assert calls[0]["run_id"] == "run-x"
    assert calls[0]["notes"]["run_manifest"] == manifest
