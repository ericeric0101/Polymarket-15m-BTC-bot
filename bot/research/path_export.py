"""Tier P research export: permanent narrow per-second market paths.

Tier C (full JSON telemetry) is rotated away; this keeps the numeric path data a
crossing / flip hazard model needs, at a fraction of the size. Per UTC day:

* ``paths_native_v2.parquet``      - snapshots accepted by the native v2 gate.
* ``paths_historical_pre_v2.parquet`` - every other snapshot, unmodified, with
  its original clock-semantics version and (when available) the historical
  TWAP recomputation class. These rows are never mixed into native cohorts.
* ``canonical_events.parquet``     - required crossing / checkpoint / settlement
  events from the TWAP research model.

Counts are reconciled against the source and every file is read back and
hashed; tier C deletion requires a verified tier P manifest for that day.
"""
from __future__ import annotations

import csv
import json
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from bot.research.daily_export import SUMMARY_SLACK_SEC, _day_bounds, _mirror, _sha256
from bot.research.decision_export import _flatten, _to_table
from bot.research.store import ResearchStore

EXPORT_VERSION = 1
FILES = ("paths_native_v2", "paths_historical_pre_v2", "canonical_events")
PATH_COLUMNS = (
    "snapshot_ts", "market_slug", "run_id", "market_start_ts", "time_left_sec", "snapshot_trigger",
    "official_twap", "strike", "twap_fresh", "twap_age_sec", "settlement_state_side",
    "btc_spot", "btc_fresh", "best_bid_up", "best_ask_up", "best_bid_down", "best_ask_down",
    "market_mid_up", "market_mid_down", "market_mid_fresh", "p_up_ex_market", "p_ex_fresh",
    "sigma_ex_market", "sigma_ex_market_fresh", "required_move_mode", "required_move_bps",
    "required_move_sigma", "required_move_z_diffusion", "p_terminal_flip_diffusion",
    "required_future_avg_to_flip", "remaining_final_window_sec", "active_side", "side_score",
    "joint_fresh", "research_before_trading_handoff", "freshness_clock_semantics_version",
)
# Required TWAP research events (kept in sync with bot.twap_forward_shadow.REQUIRED_EVENT_TYPES).
CANONICAL_EVENTS = ("MARKET_OPENING_TWAP_SAMPLE", "TWAP_STRIKE_CROSS", "TWAP_PROJECTED_SIDE_CHANGE",
                    "TMINUS_CHECKPOINT", "SETTLEMENT_PATH_THRESHOLD_CROSS", "MARKET_TWAP_SUMMARY")
DEFAULT_RECOMPUTATION_ROWS = Path(
    "reports/research_analysis/prediction_freshness/"
    "historical_freshness_recomputation_rows_20261007_000418_+0800.csv")


def _path_row(snapshot: dict[str, Any], provenance: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {key: snapshot.get(key) for key in PATH_COLUMNS}
    row["freshness_provenance"] = provenance
    row.update(extra or {})
    return row


def _load_recomputation(path: Path | None) -> dict[tuple[str, float], str]:
    if path is None or not Path(path).is_file():
        return {}
    with Path(path).open(newline="") as handle:
        return {(r["market_slug"], round(float(r["snapshot_ts"]), 3)): r["TWAP_classification"]
                for r in csv.DictReader(handle)}


def build_tables(store: ResearchStore, day: date, *, recomputation_rows: Path | None) -> tuple[dict[str, list], dict]:
    start, end = _day_bounds(day)
    native = store.get_prediction_snapshots(start_ts=start, end_ts=end, provenance="NATIVE_V2")
    native_exclusions = dict(store.prediction_exclusions)  # the next read resets the counter
    native_keys = {(s["run_id"], s["market_slug"], s["snapshot_ts"]) for s in native}
    everything = store.get_prediction_snapshots(start_ts=start, end_ts=end, provenance="HISTORICAL_RECOMPUTATION_INPUT")
    historical = [s for s in everything if (s["run_id"], s["market_slug"], s["snapshot_ts"]) not in native_keys]
    recomputed = _load_recomputation(recomputation_rows) if historical else {}
    events = [
        {"run_id": run, "row_slug": slug, "decision_epoch_ns": epoch, **_flatten(payload)}
        for run, slug, epoch, payload in store.rows(start_ts=start, end_ts=end)
        if payload.get("event_type") in CANONICAL_EVENTS
    ]
    tables = {
        "paths_native_v2": [_path_row(s, "NATIVE_V2") for s in native],
        "paths_historical_pre_v2": [
            _path_row(s, "HISTORICAL_PRE_V2", {
                "twap_recomputation_class": recomputed.get((s["market_slug"], round(float(s["snapshot_ts"]), 3)))})
            for s in historical],
        "canonical_events": events,
    }
    counts = {"prediction_snapshots_dedup": len(everything), "native_v2": len(native),
              "historical_pre_v2": len(historical), "canonical_events": len(events),
              "native_v2_exclusions": native_exclusions}
    return tables, counts


def _empty_paths_table() -> pa.Table:
    return pa.table({key: pa.array([], type=pa.string()) for key in (*PATH_COLUMNS, "freshness_provenance")})


def export_day(store: ResearchStore, day: date, out_root: Path, *, offsite_root: Path | None = None,
               recomputation_rows: Path | None = DEFAULT_RECOMPUTATION_ROWS, now: float | None = None,
               allow_partial: bool = False) -> dict[str, Any]:
    now = time.time() if now is None else now
    complete_day = now >= _day_bounds(day)[1] + SUMMARY_SLACK_SEC
    if not complete_day and not allow_partial:
        raise ValueError(f"{day} is not complete yet")
    tables, counts = build_tables(store, day, recomputation_rows=recomputation_rows)
    folder = out_root / "P_paths" / day.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    files, problems = {}, []
    if counts["native_v2"] + counts["historical_pre_v2"] != counts["prediction_snapshots_dedup"]:
        problems.append("native + historical != deduplicated snapshot count")
    for name in FILES:
        rows = tables[name]
        target = folder / f"{name}.parquet"
        temporary = target.with_suffix(".tmp")
        table = _to_table(rows) if rows else _empty_paths_table()
        pq.write_table(table, temporary, compression="zstd")
        os.replace(temporary, target)
        read_back = pq.read_table(target)
        if read_back.num_rows != len(rows):
            problems.append(f"{name}: rows {read_back.num_rows} != {len(rows)}")
        if name != "canonical_events" and rows:
            if read_back.column("snapshot_ts").to_pylist() != [float(r["snapshot_ts"]) for r in rows]:
                problems.append(f"{name}: snapshot_ts sequence mismatch")
        files[name] = {"file": f"{day.isoformat()}/{target.name}", "rows": len(rows), "sha256": _sha256(target)}
    manifest = {
        "tier": "P_paths", "export_version": EXPORT_VERSION, "date_utc": day.isoformat(),
        "exported_at_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(), "source_db": str(store.path),
        "files": files, "source_counts": counts, "complete_day": complete_day,
        "verification_problems": problems, "verified": complete_day and not problems, "offsite": None,
    }
    if offsite_root is not None:
        mirrors = {name: _mirror(folder / f"{name}.parquet", offsite_root / "P_paths" / day.isoformat() / f"{name}.parquet",
                                 files[name]["sha256"]) for name in FILES}
        manifest["offsite"] = mirrors
        manifest["verified"] = manifest["verified"] and all(m["verified"] for m in mirrors.values())
    manifest_path = out_root / "manifests" / f"P_{day.isoformat()}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    os.replace(temporary, manifest_path)
    if offsite_root is not None:
        _mirror(manifest_path, offsite_root / "manifests" / manifest_path.name, _sha256(manifest_path))
    return manifest


def day_is_exported(out_root: Path, day: date, *, require_offsite: bool = False) -> bool:
    try:
        manifest = json.loads((out_root / "manifests" / f"P_{day.isoformat()}.json").read_text())
    except (OSError, ValueError):
        return False
    if not manifest.get("verified") or set(manifest.get("files", {})) != set(FILES):
        return False
    if require_offsite and not (manifest.get("offsite") and all(m.get("verified") for m in manifest["offsite"].values())):
        return False
    for meta in manifest["files"].values():
        target = out_root / "P_paths" / meta["file"]
        if not target.is_file() or _sha256(target) != meta["sha256"]:
            return False
    return True
