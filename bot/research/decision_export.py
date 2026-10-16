"""Tier B research export: how the bot chose UP/DOWN and what happened to each entry.

Per completed UTC day, three Parquet tables are written from the trade journal
(opened read-only):

* ``side_decisions``  - SIDE_DECISION: fair up/down, score components, sources.
* ``entry_decisions`` - ENTRY_DECISION_TRACE: gate outcome and reason, fair,
  entry price, strike distance, sigma, plus the flattened research snapshot and
  decision trace (prefixes ``snap_`` and ``trace_``).
* ``executions``      - simulated and real order lifecycle, shadow fills and
  settlements, side invalidations and strike locks (raw payload kept as JSON).

Each table is read back and hashed; the day manifest is verified only when all
three match. Like tier A it is permanent and may be mirrored offsite.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from bot.research.daily_export import SUMMARY_SLACK_SEC, _day_bounds, _mirror, _sha256

EXPORT_VERSION = 1
EXECUTION_ORDER_EVENTS = (
    "ORDER_MAKER_INTENT", "ORDER_SUBMIT", "ORDER_FILLED", "ORDER_CANCELED", "ORDER_REJECTED",
    "ORDER_TAKER_EXIT_SUBMIT", "ORDER_ORPHAN_CANCEL_ON_START", "ORDER_DRY_RUN_SUBMITTED",
    "ORDER_DRY_RUN_CANCELLED", "SHADOW_SIM_ENTRY_CANDIDATE", "SHADOW_SIM_ENTRY_FILLED",
    "SHADOW_SIM_ENTRY_EXPIRED", "SHADOW_SIM_ENTRY_CANCELLED", "SHADOW_SIM_MARKOUT", "SHADOW_SIM_SETTLED",
)
EXECUTION_STRATEGY_EVENTS = (
    "SIDE_INVALIDATION_CONFIRMED", "SIDE_INVALIDATION_CLEARED", "SIDE_FORCE_UNLOCKED",
    "MARKET_STRIKE_LOCKED", "MARKET_SETTLEMENT", "MARKET_CYCLE_PNL", "STARTUP_INVENTORY_REHYDRATED",
)
TABLES = ("side_decisions", "entry_decisions", "executions")


def _flatten(payload: dict[str, Any], *, nested: dict[str, str] | None = None) -> dict[str, Any]:
    """Keep scalars; flatten selected nested dicts one level; JSON-encode the rest."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        prefix = (nested or {}).get(key)
        if prefix and isinstance(value, dict):
            for inner, inner_value in value.items():
                out[f"{prefix}{inner}"] = (json.dumps(inner_value, sort_keys=True, default=str)
                                           if isinstance(inner_value, (dict, list)) else inner_value)
        elif isinstance(value, (dict, list)):
            out[key] = json.dumps(value, sort_keys=True, default=str)
        else:
            out[key] = value
    return out


def _to_table(rows: list[dict[str, Any]]) -> pa.Table:
    """Column-wise typing: numeric/bool columns stay typed, anything mixed becomes string."""
    columns: dict[str, list[Any]] = {}
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns[key] = []
                keys.append(key)
    for key in keys:
        columns[key] = [row.get(key) for row in rows]
    arrays = {}
    for key in keys:
        values = columns[key]
        present = [v for v in values if v is not None]
        if present and all(isinstance(v, bool) for v in present):
            arrays[key] = pa.array(values, type=pa.bool_())
        elif present and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present):
            arrays[key] = pa.array([None if v is None else float(v) for v in values], type=pa.float64())
        else:
            arrays[key] = pa.array([None if v is None else str(v) for v in values], type=pa.string())
    return pa.table(arrays) if arrays else pa.table({"_empty": pa.array([], type=pa.string())})


def _iso_bounds(day: date) -> tuple[str, str]:
    nxt = day + timedelta(days=1)
    return f"{day.isoformat()}T00:00:00", f"{nxt.isoformat()}T00:00:00"


def build_tables(journal_path: Path, day: date) -> dict[str, list[dict[str, Any]]]:
    lo, hi = _iso_bounds(day)
    conn = sqlite3.connect(f"file:{Path(journal_path).resolve()}?mode=ro", uri=True, timeout=10)
    try:
        def strategy_rows(*types: str):
            marks = ",".join("?" * len(types))
            return conn.execute(f"SELECT id, ts, run_id, event_type, payload_json FROM strategy_events "
                                f"WHERE event_type IN ({marks}) AND ts >= ? AND ts < ? ORDER BY id",
                                (*types, lo, hi))

        side = [{"journal_id": i, "ts": ts, "run_id": run, **_flatten(json.loads(p or "{}"))}
                for i, ts, run, _et, p in strategy_rows("SIDE_DECISION")]
        entry = [{"journal_id": i, "ts": ts, "run_id": run,
                  **_flatten(json.loads(p or "{}"), nested={"research_snapshot": "snap_", "decision_trace": "trace_"})}
                 for i, ts, run, _et, p in strategy_rows("ENTRY_DECISION_TRACE")]
        executions = []
        marks = ",".join("?" * len(EXECUTION_ORDER_EVENTS))
        for row in conn.execute(
                f"SELECT id, ts, run_id, event_type, client_order_id, side, price, qty, status, reason, "
                f"instrument_id, payload_json FROM order_events WHERE event_type IN ({marks}) "
                f"AND ts >= ? AND ts < ? ORDER BY id", (*EXECUTION_ORDER_EVENTS, lo, hi)):
            payload = json.loads(row[11] or "{}")
            executions.append({"source": "order_events", "journal_id": row[0], "ts": row[1], "run_id": row[2],
                               "event_type": row[3], "client_order_id": row[4], "side": row[5], "price": row[6],
                               "qty": row[7], "status": row[8], "reason": row[9], "instrument_id": row[10],
                               "slug": payload.get("slug") or payload.get("market_slug"),
                               "payload_json": json.dumps(payload, sort_keys=True, default=str)})
        for i, ts, run, et, p in strategy_rows(*EXECUTION_STRATEGY_EVENTS):
            payload = json.loads(p or "{}")
            executions.append({"source": "strategy_events", "journal_id": i, "ts": ts, "run_id": run,
                               "event_type": et, "slug": payload.get("slug") or payload.get("market_slug"),
                               "payload_json": json.dumps(payload, sort_keys=True, default=str)})
        executions.sort(key=lambda r: (r["ts"], r["source"], r["journal_id"]))
    finally:
        conn.close()
    return {"side_decisions": side, "entry_decisions": entry, "executions": executions}


def export_day(journal_path: Path, day: date, out_root: Path, *, offsite_root: Path | None = None,
               now: float | None = None, allow_partial: bool = False) -> dict[str, Any]:
    _start, day_end = _day_bounds(day)
    now = time.time() if now is None else now
    complete_day = now >= day_end + SUMMARY_SLACK_SEC
    if not complete_day and not allow_partial:
        raise ValueError(f"{day} is not complete yet")
    tables = build_tables(journal_path, day)
    folder = out_root / "B_decisions" / day.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    files, problems = {}, []
    for name in TABLES:
        target = folder / f"{name}.parquet"
        temporary = target.with_suffix(".tmp")
        pq.write_table(_to_table(tables[name]), temporary, compression="zstd")
        os.replace(temporary, target)
        read_back = pq.read_table(target)
        expected = len(tables[name])
        if (read_back.num_rows if tables[name] else 0) != expected:
            problems.append(f"{name}: rows {read_back.num_rows} != {expected}")
        if tables[name] and read_back.column("journal_id").to_pylist() != [float(r["journal_id"]) for r in tables[name]]:
            problems.append(f"{name}: journal_id sequence mismatch")
        files[name] = {"file": f"{day.isoformat()}/{target.name}", "rows": expected, "sha256": _sha256(target)}
    manifest = {
        "tier": "B_decisions", "export_version": EXPORT_VERSION, "date_utc": day.isoformat(),
        "exported_at_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "source_journal": str(journal_path), "files": files, "complete_day": complete_day,
        "verification_problems": problems, "verified": complete_day and not problems, "offsite": None,
        "note": ("ENTRY_DECISION_TRACE was byte-sampled by the journal diagnostic budget only between "
                 "commits d24b576 and 14245c4 (2026-10-08 ~15:00-16:00Z); all other periods are complete."),
    }
    if offsite_root is not None:
        mirrors = {name: _mirror(folder / f"{name}.parquet",
                                 offsite_root / "B_decisions" / day.isoformat() / f"{name}.parquet",
                                 files[name]["sha256"]) for name in TABLES}
        manifest["offsite"] = mirrors
        manifest["verified"] = manifest["verified"] and all(m["verified"] for m in mirrors.values())
    manifest_path = out_root / "manifests" / f"B_{day.isoformat()}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    os.replace(temporary, manifest_path)
    if offsite_root is not None:
        _mirror(manifest_path, offsite_root / "manifests" / manifest_path.name, _sha256(manifest_path))
    return manifest


def day_is_exported(out_root: Path, day: date) -> bool:
    try:
        manifest = json.loads((out_root / "manifests" / f"B_{day.isoformat()}.json").read_text())
    except (OSError, ValueError):
        return False
    if not manifest.get("verified"):
        return False
    for meta in manifest.get("files", {}).values():
        target = out_root / "B_decisions" / meta["file"]
        if not target.is_file() or _sha256(target) != meta["sha256"]:
            return False
    return set(manifest.get("files", {})) == set(TABLES)
