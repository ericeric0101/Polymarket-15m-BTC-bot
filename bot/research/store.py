"""Read-only, restart-aware data access for offline prediction research."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo


class ResearchStore:
    """Never opens the source database read-write and never initializes schema."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)

    def rows(self, *, run_id: str | None = None, slug: str | None = None,
             event_type: str | None = None) -> Iterator[tuple[str, str, int, dict[str, Any]]]:
        clauses, params = [], []
        if run_id:
            clauses.append("run_id = ?"); params.append(run_id)
        if slug:
            clauses.append("slug = ?"); params.append(slug)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            for run, row_slug, epoch_ns, raw in conn.execute(
                "SELECT run_id, slug, decision_epoch_ns, payload_json FROM lead_lag_decisions" + where + " ORDER BY decision_epoch_ns, rowid",
                params,
            ):
                try:
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        continue
                    if event_type is None or payload.get("event_type") == event_type:
                        yield str(run), str(row_slug), int(epoch_ns), payload
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue

    def get_prediction_snapshots(self, *, run_id: str | None = None, slug: str | None = None) -> list[dict[str, Any]]:
        # Same run/market/snapshot key is deduped deterministically by latest DB row.
        dedup: dict[tuple[str, str, float], dict[str, Any]] = {}
        for run, row_slug, epoch, payload in self.rows(run_id=run_id, slug=slug, event_type="PREDICTION_RESEARCH_SNAPSHOT"):
            market = str(payload.get("market_slug") or row_slug)
            ts = float(payload.get("snapshot_ts") or epoch / 1e9)
            dedup[(run, market, ts)] = {**payload, "run_id": run, "market_slug": market, "snapshot_ts": ts}
        return sorted(dedup.values(), key=lambda row: (row["market_slug"], row["snapshot_ts"]))

    def get_settlements(self) -> list[dict[str, Any]]:
        output = {}
        for run, row_slug, epoch, payload in self.rows(event_type="MARKET_TWAP_SUMMARY"):
            market = str(payload.get("market_slug") or row_slug)
            output[(run, market)] = {**payload, "run_id": run, "market_slug": market,
                                     "summary_epoch_ns": epoch}
        return list(output.values())

    def get_research_events(self, *, event_type: str, slug: str | None = None) -> list[dict[str, Any]]:
        """Return deduplicated sparse research events without mutating source DB.

        The decision table is append-only.  For an exact duplicate emitted by
        a reconnecting process, the latest database row for the same run,
        market, timestamp, type, and lifecycle identity wins deterministically.
        """
        dedup: dict[tuple[str, str, float, str, str], dict[str, Any]] = {}
        for run, row_slug, epoch, payload in self.rows(slug=slug, event_type=event_type):
            timestamp = float(
                payload.get("observed_ts") or payload.get("entry_ts") or payload.get("actual_stop_ts")
                or payload.get("settlement_ts") or payload.get("first_adverse_ts") or epoch / 1e9
            )
            market = str(payload.get("market_slug") or payload.get("slug") or row_slug)
            identity = str(payload.get("position_lifecycle_id") or payload.get("client_order_id") or "")
            dedup[(run, market, timestamp, event_type, identity)] = {
                **payload, "run_id": run, "market_slug": market, "event_ts": timestamp,
            }
        return sorted(dedup.values(), key=lambda row: (row["market_slug"], row["event_ts"]))

    def get_market_coverage(self) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        settled_slugs = {row["market_slug"] for row in self.get_settlements()}
        for row in self.get_prediction_snapshots():
            grouped[row["market_slug"]].append(row)
        output = []
        for slug, rows in grouped.items():
            rows.sort(key=lambda row: row["snapshot_ts"])
            gaps = [b["snapshot_ts"] - a["snapshot_ts"] for a, b in zip(rows, rows[1:])]
            span = rows[-1]["snapshot_ts"] - rows[0]["snapshot_ts"]
            settlement = slug in settled_slugs
            joint_rate = sum(row.get("joint_fresh") is True for row in rows) / len(rows)
            quality = ("UNUSABLE" if not settlement or span < 60 else
                       "INTERRUPTED" if len({row["run_id"] for row in rows}) > 1 or max(gaps, default=0.0) > 15 else
                       "PARTIAL" if span < 600 or joint_rate < .25 else
                       "GOOD" if span < 840 or joint_rate < .70 else "FULL")
            output.append({"market_slug": slug, "run_ids": sorted({row["run_id"] for row in rows}),
                           "restart_count": max(0, len({row["run_id"] for row in rows}) - 1),
                           "first_snapshot_ts": rows[0]["snapshot_ts"], "last_snapshot_ts": rows[-1]["snapshot_ts"],
                           "largest_gap_sec": max(gaps, default=0.0),
                           "coverage_ratio": span / 900.0, "joint_fresh_rate": joint_rate,
                           "coverage_quality": quality})
        return output

    def integrity(self) -> dict[str, Any]:
        with self._connect() as conn:
            quick = conn.execute("PRAGMA quick_check").fetchone()[0]
            malformed = non_object = total = 0
            for (raw,) in conn.execute("SELECT payload_json FROM lead_lag_decisions"):
                total += 1
                try:
                    if not isinstance(json.loads(raw), dict):
                        non_object += 1
                except (TypeError, ValueError):
                    malformed += 1
        coverage = self.get_market_coverage()
        intervals = [row["largest_gap_sec"] for row in coverage]
        return {"quick_check": quick, "decision_rows_total": total,
                "malformed_json_rows": malformed, "non_object_payload_rows": non_object,
                "excluded_payload_rows": malformed + non_object,
                "markets_with_snapshots": len(coverage),
                "continuous_markets": sum(row["coverage_quality"] in {"FULL", "GOOD"} for row in coverage),
                "interrupted_markets": sum(row["coverage_quality"] == "INTERRUPTED" for row in coverage),
                "partial_markets": sum(row["coverage_quality"] == "PARTIAL" for row in coverage),
                "unusable_markets": sum(row["coverage_quality"] == "UNUSABLE" for row in coverage),
                "largest_snapshot_gap_sec": max(intervals, default=0.0),
                "multi_run_markets": sum(row["restart_count"] > 0 for row in coverage)}

    def get_run_provenance(self, journal_path: str | Path) -> dict[str, Any]:
        """Join immutable run notes to research coverage using read-only connections.

        Legacy runs without a manifest remain visible with unknown provenance;
        this method never guesses a git/config cohort for them.
        """
        journal = Path(journal_path)
        runs: dict[str, dict[str, Any]] = {}
        warnings: list[str] = []
        if journal.is_file():
            try:
                with sqlite3.connect(journal.resolve().as_uri() + "?mode=ro", uri=True) as conn:
                    columns = {row[1] for row in conn.execute("PRAGMA table_info(strategy_runs)")}
                    if {"run_id", "started_at", "ended_at", "mode", "notes_json"}.issubset(columns):
                        for run_id, started_at, ended_at, mode, raw in conn.execute(
                            "SELECT run_id, started_at, ended_at, mode, notes_json FROM strategy_runs ORDER BY started_at"
                        ):
                            try:
                                notes = json.loads(raw or "{}")
                            except (TypeError, ValueError, json.JSONDecodeError):
                                notes = {}
                            manifest = notes.get("run_manifest") if isinstance(notes, dict) else None
                            runs[str(run_id)] = {
                                "run_id": str(run_id), "started_at": started_at, "ended_at": ended_at,
                                "mode": mode, "manifest": manifest if isinstance(manifest, dict) else None,
                            }
                    else:
                        warnings.append("journal_strategy_runs_schema_unavailable")
            except sqlite3.Error:
                warnings.append("journal_read_failed")
        else:
            warnings.append("journal_missing")

        research_by_run: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"slugs": set(), "first_epoch_ns": None, "last_epoch_ns": None}
        )
        try:
            for run_id, slug, epoch, payload in self.rows(event_type="PREDICTION_RESEARCH_SNAPSHOT"):
                row = research_by_run[run_id]
                row["slugs"].add(str(payload.get("market_slug") or slug))
                row["first_epoch_ns"] = min(row["first_epoch_ns"], epoch) if row["first_epoch_ns"] is not None else epoch
                row["last_epoch_ns"] = max(row["last_epoch_ns"], epoch) if row["last_epoch_ns"] is not None else epoch
        except sqlite3.Error:
            warnings.append("research_snapshot_query_failed")

        all_slugs: set[str] = set()
        git_commits: set[str] = set()
        config_hashes: set[str] = set()
        schema_cohorts: dict[str, set[str]] = defaultdict(set)
        result_runs = []
        for run_id in sorted(set(runs) | set(research_by_run)):
            run = runs.get(run_id, {"run_id": run_id, "started_at": None, "ended_at": None, "mode": None, "manifest": None})
            coverage = research_by_run.get(run_id, {"slugs": set(), "first_epoch_ns": None, "last_epoch_ns": None})
            slugs = set(coverage["slugs"])
            all_slugs.update(slugs)
            manifest = run["manifest"] or {}
            commit, config_hash = manifest.get("git_commit"), manifest.get("config_hash")
            if commit:
                git_commits.add(str(commit))
            if config_hash:
                config_hashes.add(str(config_hash))
            versions = manifest.get("schema_versions") if isinstance(manifest.get("schema_versions"), dict) else {}
            for name, value in versions.items():
                if value is not None:
                    schema_cohorts[str(name)].add(str(value))
            weekend, weekday = 0, 0
            for slug in slugs:
                try:
                    start_epoch = int(slug.rsplit("-", 1)[1])
                    local_weekday = datetime.fromtimestamp(start_epoch, ZoneInfo("Asia/Taipei")).weekday()
                    if local_weekday >= 5:
                        weekend += 1
                    else:
                        weekday += 1
                except (ValueError, OSError, OverflowError):
                    pass
            first_ns, last_ns = coverage["first_epoch_ns"], coverage["last_epoch_ns"]
            result_runs.append({
                **run, "git_commit": commit, "config_hash": config_hash,
                "schema_versions": versions, "market_count": len(slugs),
                "weekday_market_count": weekday, "weekend_market_count": weekend,
                "first_snapshot_ts": first_ns / 1e9 if first_ns is not None else None,
                "last_snapshot_ts": last_ns / 1e9 if last_ns is not None else None,
                "provenance_status": "MANIFESTED" if run["manifest"] else "LEGACY_UNKNOWN",
            })
        unique_weekend, unique_weekday = 0, 0
        for slug in all_slugs:
            try:
                start_epoch = int(slug.rsplit("-", 1)[1])
                if datetime.fromtimestamp(start_epoch, ZoneInfo("Asia/Taipei")).weekday() >= 5:
                    unique_weekend += 1
                else:
                    unique_weekday += 1
            except (ValueError, OSError, OverflowError):
                pass
        return {
            "run_count": len(result_runs), "market_count": len(all_slugs),
            "weekday_market_count": unique_weekday, "weekend_market_count": unique_weekend,
            "first_snapshot_ts": min((r["first_snapshot_ts"] for r in result_runs if r["first_snapshot_ts"] is not None), default=None),
            "last_snapshot_ts": max((r["last_snapshot_ts"] for r in result_runs if r["last_snapshot_ts"] is not None), default=None),
            "git_commits": sorted(git_commits), "config_hashes": sorted(config_hashes),
            "schema_versions": {name: sorted(values) for name, values in sorted(schema_cohorts.items())},
            "legacy_unmanifested_runs": sum(row["provenance_status"] == "LEGACY_UNKNOWN" for row in result_runs),
            "runs": result_runs, "warnings": warnings,
        }
