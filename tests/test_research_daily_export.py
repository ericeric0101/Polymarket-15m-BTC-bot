"""Tier A daily export: correctness, provenance gate, verification and offsite mirror (tmp only)."""
import csv
import json
import sqlite3
from datetime import date, datetime, timezone

import pytest

from bot.research.daily_export import FIELDS, day_is_exported, export_day
from bot.research.store import ResearchStore

DAY = date(2026, 10, 7)
DAY_START = int(datetime(2026, 10, 7, tzinfo=timezone.utc).timestamp())
MARKET = DAY_START + 900 * 4
SLUG = f"btc-updown-15m-{MARKET}"
AFTER_DAY = DAY_START + 86400 + 3600


def _db(tmp_path, rows, name="research.db"):
    path = tmp_path / name
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE lead_lag_decisions (id INTEGER PRIMARY KEY, run_id TEXT, slug TEXT, "
                     "market_id INTEGER, decision_epoch_ns INTEGER, payload_json TEXT)")
        conn.executemany("INSERT INTO lead_lag_decisions (run_id, slug, decision_epoch_ns, payload_json) "
                         "VALUES ('run', ?, ?, ?)", [(slug, int(ts * 1e9), json.dumps(p)) for slug, ts, p in rows])
    return ResearchStore(path)


def _snapshot(offset, twap, *, version=2, **extra):
    return (SLUG, MARKET + offset, {
        "event_type": "PREDICTION_RESEARCH_SNAPSHOT", "freshness_clock_semantics_version": version,
        "market_slug": SLUG, "snapshot_ts": MARKET + offset, "official_twap": twap, "strike": 100.0,
        "twap_fresh": True, "joint_fresh": True, "required_move_sigma": 1.5,
        "required_move_z_diffusion": 0.8, "p_terminal_flip_diffusion": 0.21, "sigma_ex_market": 0.5,
        "time_left_sec": 900 - offset, **extra})


def _summary(side="DOWN"):
    return (SLUG, MARKET + 901, {"event_type": "MARKET_TWAP_SUMMARY", "market_slug": SLUG,
                                 "canonical_settlement_side": side, "settlement_reference_is_canonical": True,
                                 "twap_cross_count": 2, "projected_cross_count": 9})


def _path(up_until=500):
    # UP leader until `up_until` seconds after open, DOWN after; 1 s cadence.
    return [_snapshot(t, 100.5 if t < up_until else 99.5) for t in range(2, 900)]


def _read(tmp_path):
    with (tmp_path / "out" / "A_market_summary" / f"{DAY}.csv").open() as handle:
        return {row["market_slug"]: row for row in csv.DictReader(handle)}


def test_every_slot_of_the_day_gets_a_row_with_cutoff_flips(tmp_path):
    store = _db(tmp_path, [*_path(), _summary("DOWN")])
    manifest = export_day(store, DAY, tmp_path / "out", now=AFTER_DAY)
    rows = _read(tmp_path)
    assert len(rows) == 96 and manifest["rows"] == 96 and manifest["verified"] is True
    row = rows[SLUG]
    assert row["settlement_side"] == "DOWN" and row["complete_observation"] == "True"
    assert row["t300_leader"] == "DOWN" and row["t300_flip"] == "0"      # T-300 = 600 s: already DOWN
    # A market whose leader only turns DOWN at 800 s: UP at T-300 (600 s) is a settlement flip.
    store2 = _db(tmp_path, [*_path(up_until=800), _summary("DOWN")], name="late.db")
    export_day(store2, DAY, tmp_path / "out2", now=AFTER_DAY)
    with (tmp_path / "out2" / "A_market_summary" / f"{DAY}.csv").open() as handle:
        late = {r["market_slug"]: r for r in csv.DictReader(handle)}[SLUG]
    assert late["t300_leader"] == "UP" and late["t300_flip"] == "1"       # UP at 600 s, settled DOWN
    assert late["t60_leader"] == "DOWN" and late["t60_flip"] == "0"
    assert late["t300_required_move_z_diffusion"] == "0.8"


def test_non_native_snapshots_are_excluded_and_counted(tmp_path):
    rows = [*_path(), _summary()]
    rows += [_snapshot(t + 0.5, 50.0, version=1) for t in range(2, 900, 10)]
    rows += [_snapshot(t + 0.25, 50.0, version=None) for t in range(2, 900, 10)]
    manifest = export_day(_db(tmp_path, rows), DAY, tmp_path / "out", now=AFTER_DAY)
    assert manifest["native_v2_exclusions"] == {"LEGACY_FRESHNESS_CLOCK_V1": 90, "MISSING_FRESHNESS_CLOCK_VERSION": 90}
    assert _read(tmp_path)[SLUG]["n_native_v2_snapshots"] == str(len(_path()))


def test_incomplete_day_is_refused_and_partial_export_is_never_verified(tmp_path):
    store = _db(tmp_path, [*_path(), _summary()])
    with pytest.raises(ValueError):
        export_day(store, DAY, tmp_path / "out", now=DAY_START + 3600)
    manifest = export_day(store, DAY, tmp_path / "out", now=DAY_START + 3600, allow_partial=True)
    assert manifest["verified"] is False
    assert day_is_exported(tmp_path / "out", DAY) is False


def test_tampered_export_no_longer_authorizes_tier_c_deletion(tmp_path):
    export_day(_db(tmp_path, [*_path(), _summary()]), DAY, tmp_path / "out", now=AFTER_DAY)
    assert day_is_exported(tmp_path / "out", DAY) is True
    target = tmp_path / "out" / "A_market_summary" / f"{DAY}.csv"
    target.write_text(target.read_text().replace("DOWN", "UP", 1))
    assert day_is_exported(tmp_path / "out", DAY) is False


def test_offsite_copy_is_hash_verified_and_failure_blocks_verification(tmp_path):
    store = _db(tmp_path, [*_path(), _summary()])
    ok = export_day(store, DAY, tmp_path / "out", offsite_root=tmp_path / "drive", now=AFTER_DAY)
    assert ok["offsite"]["verified"] is True
    assert (tmp_path / "drive" / "A_market_summary" / f"{DAY}.csv").is_file()
    assert (tmp_path / "drive" / "manifests" / f"A_{DAY}.json").is_file()
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    bad = export_day(store, DAY, tmp_path / "out3", offsite_root=blocked, now=AFTER_DAY)
    assert bad["offsite"]["verified"] is False and bad["verified"] is False


def test_header_is_stable():
    assert FIELDS[:3] == ("export_version", "date_utc", "market_slug")
    assert "t120_required_move_z_diffusion" in FIELDS
