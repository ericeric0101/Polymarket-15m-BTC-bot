"""Tier A research export: one verified row per 15-minute market per UTC day.

The high-frequency research store (tier C) is rotated away after 7 days. This
module turns a completed UTC day into a small, permanent per-market table that
answers the core research questions: how each market settled, who led at fixed
cutoffs before expiry, whether that leader flipped at settlement, and how hard
the flip was by distance / sigma measures at that cutoff.

Only native freshness-v2 prediction snapshots are used (ResearchStore gate).
Every export is read back and verified; tier C files may only be deleted for a
day whose manifest says ``verified: true`` (see ``day_is_exported``).
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from bot.research.store import ResearchStore

EXPORT_VERSION = 1
MARKET_SEC = 900
CUTOFFS = (300, 180, 120, 60, 30)
CUTOFF_TOLERANCE_SEC = 5.0
MAX_FIRST_OFFSET_SEC = 30.0
MAX_GAP_SEC = 10.0
# Settlement summaries are written just after expiry; allow slack past the day.
SUMMARY_SLACK_SEC = 2 * MARKET_SEC
CUTOFF_FIELDS = ("snapshot_lag_sec", "leader", "flip", "twap_minus_strike_bps", "required_move_sigma",
                 "required_move_z_diffusion", "p_terminal_flip_diffusion", "sigma_ex_market", "time_left_sec")
BASE_FIELDS = (
    "export_version", "date_utc", "market_slug", "market_start_utc", "weekday_utc",
    "settlement_side", "settlement_reference_canonical", "strike", "twap_cross_count",
    "projected_cross_count", "n_native_v2_snapshots", "first_snapshot_offset_sec", "max_gap_sec",
    "complete_observation", "joint_fresh_pct", "run_ids",
)
FIELDS = BASE_FIELDS + tuple(f"t{k}_{field}" for k in CUTOFFS for field in CUTOFF_FIELDS)


def _day_bounds(day: date) -> tuple[float, float]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    return start, start + 86400


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def build_market_rows(store: ResearchStore, day: date) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (rows, source_counts) for every 15-minute slot of ``day``."""
    day_start, day_end = _day_bounds(day)
    snapshots: dict[str, list[dict[str, Any]]] = {}
    for snapshot in store.get_prediction_snapshots(start_ts=day_start, end_ts=day_end + MARKET_SEC,
                                                   provenance="NATIVE_V2"):
        snapshots.setdefault(snapshot["market_slug"], []).append(snapshot)
    settlements: dict[str, dict[str, Any]] = {}
    for _run, row_slug, _epoch, payload in store.rows(event_type="MARKET_TWAP_SUMMARY", start_ts=day_start,
                                                      end_ts=day_end + SUMMARY_SLACK_SEC):
        settlements[str(payload.get("market_slug") or row_slug)] = payload
    rows = []
    for start in range(int(day_start), int(day_end), MARKET_SEC):
        slug = f"btc-updown-15m-{start}"
        rows.append(_market_row(day, slug, start, sorted(snapshots.get(slug, []), key=lambda s: s["snapshot_ts"]),
                                settlements.get(slug)))
    counts = {"native_v2_snapshots": sum(len(v) for v in snapshots.values()),
              "native_v2_exclusions": dict(store.prediction_exclusions),
              "settlement_summaries": sum(1 for r in rows if r["settlement_side"])}
    return rows, counts


def _market_row(day: date, slug: str, start: int, snaps: list[dict[str, Any]],
                summary: dict[str, Any] | None) -> dict[str, Any]:
    end = start + MARKET_SEC
    inside = [s for s in snaps if start <= float(s["snapshot_ts"]) <= end]
    times = [float(s["snapshot_ts"]) for s in inside]
    max_gap = max((b - a for a, b in zip(times, times[1:])), default=None)
    settle = (summary or {}).get("canonical_settlement_side")
    settle = settle if settle in ("UP", "DOWN") else ""
    strikes = [_number(s.get("strike")) for s in inside if _number(s.get("strike"))]
    row: dict[str, Any] = {
        "export_version": EXPORT_VERSION,
        "date_utc": day.isoformat(),
        "market_slug": slug,
        "market_start_utc": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "weekday_utc": datetime.fromtimestamp(start, timezone.utc).strftime("%a"),
        "settlement_side": settle,
        "settlement_reference_canonical": (summary or {}).get("settlement_reference_is_canonical"),
        "strike": strikes[0] if strikes else None,
        "twap_cross_count": (summary or {}).get("twap_cross_count"),
        "projected_cross_count": (summary or {}).get("projected_cross_count"),
        "n_native_v2_snapshots": len(inside),
        "first_snapshot_offset_sec": round(times[0] - start, 3) if times else None,
        "max_gap_sec": round(max_gap, 3) if max_gap is not None else None,
        "complete_observation": bool(times) and times[0] - start <= MAX_FIRST_OFFSET_SEC
        and end - times[-1] <= MAX_GAP_SEC and (max_gap or 0.0) <= MAX_GAP_SEC,
        "joint_fresh_pct": round(100.0 * sum(1 for s in inside if s.get("joint_fresh")) / len(inside), 2) if inside else None,
        "run_ids": "|".join(sorted({str(s.get("run_id")) for s in inside})),
    }
    for cutoff in CUTOFFS:
        target = end - cutoff
        fresh = [s for s in inside if target - CUTOFF_TOLERANCE_SEC <= float(s["snapshot_ts"]) <= target
                 and s.get("twap_fresh") is True and _number(s.get("official_twap")) and _number(s.get("strike"))]
        values = dict.fromkeys(CUTOFF_FIELDS)
        if fresh:
            snap = fresh[-1]
            twap, strike = float(snap["official_twap"]), float(snap["strike"])
            leader = "UP" if twap > strike else "DOWN" if twap < strike else "TIE"
            values.update({
                "snapshot_lag_sec": round(target - float(snap["snapshot_ts"]), 3),
                "leader": leader,
                "flip": (int(leader != settle) if settle and leader != "TIE" else None),
                "twap_minus_strike_bps": round((twap - strike) / strike * 1e4, 4),
                "required_move_sigma": _number(snap.get("required_move_sigma")),
                "required_move_z_diffusion": _number(snap.get("required_move_z_diffusion")),
                "p_terminal_flip_diffusion": _number(snap.get("p_terminal_flip_diffusion")),
                "sigma_ex_market": _number(snap.get("sigma_ex_market")),
                "time_left_sec": _number(snap.get("time_left_sec")),
            })
        row.update({f"t{cutoff}_{field}": value for field, value in values.items()})
    return row


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_atomic_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: ("" if row.get(key) is None else row.get(key)) for key in FIELDS})
    os.replace(temporary, path)


def _verify_csv(path: Path, rows: list[dict[str, Any]]) -> list[str]:
    problems = []
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != FIELDS:
            problems.append("header_mismatch")
        written = list(reader)
    if len(written) != len(rows):
        problems.append(f"row_count {len(written)} != {len(rows)}")
    for expected, actual in zip(rows, written):
        for key in ("market_slug", "settlement_side", "n_native_v2_snapshots", "t300_flip", "t120_flip"):
            want = "" if expected.get(key) is None else str(expected.get(key))
            if actual.get(key) != want:
                problems.append(f"{expected['market_slug']}:{key}")
                break
    return problems


def export_day(store: ResearchStore, day: date, out_root: Path, *, offsite_root: Path | None = None,
               now: float | None = None, allow_partial: bool = False) -> dict[str, Any]:
    """Export ``day`` to ``out_root``, verify it, optionally mirror to ``offsite_root``."""
    _day_start, day_end = _day_bounds(day)
    now = time.time() if now is None else now
    complete_day = now >= day_end + SUMMARY_SLACK_SEC
    if not complete_day and not allow_partial:
        raise ValueError(f"{day} is not complete yet; settlements may still be pending")
    rows, counts = build_market_rows(store, day)
    target = out_root / "A_market_summary" / f"{day.isoformat()}.csv"
    _write_atomic_csv(target, rows)
    problems = _verify_csv(target, rows)
    manifest = {
        "tier": "A_market_summary", "export_version": EXPORT_VERSION, "date_utc": day.isoformat(),
        "exported_at_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "source_db": str(store.path), "file": target.name, "sha256": _sha256(target), "rows": len(rows),
        "markets_settled": counts["settlement_summaries"],
        "markets_with_native_v2": sum(1 for r in rows if r["n_native_v2_snapshots"]),
        "native_v2_snapshots": counts["native_v2_snapshots"],
        "native_v2_exclusions": counts["native_v2_exclusions"],
        "complete_day": complete_day, "verification_problems": problems,
        "verified": complete_day and not problems,
        "offsite": None,
    }
    if offsite_root is not None:
        manifest["offsite"] = _mirror(target, offsite_root / "A_market_summary" / target.name, manifest["sha256"])
        manifest["verified"] = manifest["verified"] and manifest["offsite"]["verified"]
    manifest_path = out_root / "manifests" / f"A_{day.isoformat()}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    os.replace(temporary, manifest_path)
    if offsite_root is not None:
        _mirror(manifest_path, offsite_root / "manifests" / manifest_path.name, _sha256(manifest_path))
    return manifest


def _mirror(source: Path, target: Path, sha256: str) -> dict[str, Any]:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
        copied = _sha256(target)
        return {"path": str(target), "sha256": copied, "verified": copied == sha256}
    except OSError as exc:
        return {"path": str(target), "error": f"{type(exc).__name__}: {exc}", "verified": False}


def day_is_exported(out_root: Path, day: date) -> bool:
    """Deletion authority for tier C: only a verified, complete-day manifest counts."""
    path = out_root / "manifests" / f"A_{day.isoformat()}.json"
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    target = out_root / "A_market_summary" / str(manifest.get("file") or "")
    return bool(manifest.get("verified")) and target.is_file() and _sha256(target) == manifest.get("sha256")


def completed_days(store_path: Path, *, now: float | None = None, lookback_days: int = 14) -> list[date]:
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, timezone.utc).date()
    days = [today - timedelta(days=offset) for offset in range(1, lookback_days + 1)]
    return sorted(d for d in days if now >= _day_bounds(d)[1] + SUMMARY_SLACK_SEC)
