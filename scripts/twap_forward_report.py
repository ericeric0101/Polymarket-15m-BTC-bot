#!/usr/bin/env python3
"""Export compact, event-driven TWAP-forward research telemetry."""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import sqlite3
from collections import Counter
from pathlib import Path


EVENTS = {"TWAP_STRIKE_CROSS", "TWAP_PROJECTED_SIDE_CHANGE", "TMINUS_CHECKPOINT", "MARKET_TWAP_SUMMARY", "MARKET_OPENING_TWAP_SAMPLE", "SETTLEMENT_PATH_THRESHOLD_CROSS", "RESEARCH_STORAGE_GUARD_TRIGGERED"}


def _fresh_market_quote(row: dict) -> bool:
    """Only use market-mid rows with explicit, in-limit source and receive ages."""
    try:
        max_age = float(row["market_bbo_max_age_sec"])
        source_age = float(row["market_bbo_up_source_age_sec"])
        received_age = float(row["market_bbo_up_received_age_sec"])
    except (KeyError, TypeError, ValueError):
        return False
    return 0.0 <= source_age <= max_age and 0.0 <= received_age <= max_age


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = sorted({key for row in rows for key in row}) or ["observed_ts"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)


def probability_research_rows(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Build provenance-strict checkpoint calibration, buckets, and lead rows."""
    summaries = {r.get("market_slug"): r for r in rows if r.get("event_type") == "MARKET_TWAP_SUMMARY"}
    checkpoints = [r for r in rows if r.get("event_type") == "TMINUS_CHECKPOINT"]
    canonical_candidates = []
    for row in checkpoints:
        summary = summaries.get(row.get("market_slug")) or {}
        try:
            checkpoint_sec = float(row.get("checkpoint_sec"))
            time_left = float(row.get("time_left_sec"))
        except (TypeError, ValueError):
            # Legacy checkpoint rows without a measured positive horizon are
            # retained in the raw event table, but cannot enter calibration.
            continue
        if not (0.0 < time_left <= checkpoint_sec):
            continue
        if (summary.get("settlement_reference_is_canonical") is True
                and row.get("sigma_ex_market_fresh") is True
                and row.get("p_up_ex_market") is not None):
            canonical_candidates.append({**row, "settlement_side": summary.get("settlement_side")})
    # A market contributes at most once to a given horizon. If duplicated
    # checkpoint events exist, retain the sample nearest its target T-minus;
    # ties resolve to the latest observed source row deterministically.
    deduped = {}
    for row in canonical_candidates:
        try:
            checkpoint = int(row.get("checkpoint_sec") or 0)
            distance = abs(float(row.get("time_left_sec", checkpoint)) - checkpoint)
            observed = float(row.get("observed_ts") or 0.0)
        except (TypeError, ValueError):
            continue
        key = (row.get("market_slug"), checkpoint)
        rank = (distance, -observed)
        if key not in deduped or rank < deduped[key][0]:
            deduped[key] = (rank, row)
    canonical = [item[1] for item in deduped.values()]
    metric_rows = []
    for checkpoint in (120, 60, 30, 15, 10, 5):
        group = [r for r in canonical if int(r.get("checkpoint_sec") or 0) == checkpoint
                 and r.get("settlement_side") in {"UP", "DOWN"} and r.get("p_up_ex_market") is not None]
        outcomes = [1.0 if r["settlement_side"] == "UP" else 0.0 for r in group]
        probs = [min(1 - 1e-12, max(1e-12, float(r["p_up_ex_market"]))) for r in group]
        market_pairs = [(min(1 - 1e-12, max(1e-12, float(r["market_mid_probability_up"]))), y)
                        for r, y in zip(group, outcomes)
                        if r.get("market_mid_probability_up") is not None and _fresh_market_quote(r)]
        def scores(pairs):
            if not pairs:
                return None, None, None
            brier = sum((p - y) ** 2 for p, y in pairs) / len(pairs)
            logloss = -sum(y * math.log(p) + (1-y) * math.log(1-p) for p, y in pairs) / len(pairs)
            accuracy = sum((p >= .5) == bool(y) for p, y in pairs) / len(pairs)
            return brier, logloss, accuracy
        brier, logloss, accuracy = scores(list(zip(probs, outcomes)))
        mbrier, mlogloss, maccuracy = scores(market_pairs)
        metric_rows.append({"checkpoint_sec": checkpoint, "canonical_n": len(group),
                            "brier_ex_market": brier, "logloss_ex_market": logloss,
                            "direction_accuracy_ex_market": accuracy,
                            "mean_p_up_ex_market": sum(probs)/len(probs) if probs else None,
                            "actual_up_rate": sum(outcomes)/len(outcomes) if outcomes else None,
                            "market_n": len(market_pairs), "brier_market_mid": mbrier,
                            "logloss_market_mid": mlogloss, "direction_accuracy_market_mid": maccuracy,
                            "descriptive_only": True})
    buckets = ((0, .10), (.10, .25), (.25, .40), (.40, .60), (.60, .75), (.75, .90), (.90, .95), (.95, .975), (.975, 1.000001))
    complete = [r for r in canonical if r.get("settlement_side") in {"UP", "DOWN"} and r.get("p_up_ex_market") is not None]
    bucket_rows = []
    for checkpoint in (120, 60, 30, 15, 10, 5):
        checkpoint_rows = [r for r in complete if int(r.get("checkpoint_sec") or 0) == checkpoint]
        for low, high in buckets:
            group = [r for r in checkpoint_rows if low <= float(r["p_up_ex_market"]) < high]
            bucket_rows.append({"checkpoint_sec": checkpoint, "probability_low": low, "probability_high": high,
                                "n_markets": len(group),
                                "mean_predicted": sum(float(r["p_up_ex_market"]) for r in group)/len(group) if group else None,
                                "actual_up_rate": sum(r["settlement_side"] == "UP" for r in group)/len(group) if group else None,
                                "descriptive_only": True})
    # Threshold lead is reconstructed from threshold events, not legacy
    # summary timestamps, because old summaries have no sigma freshness proof.
    threshold_events = [r for r in rows if r.get("event_type") == "SETTLEMENT_PATH_THRESHOLD_CROSS"]
    lead_rows = []
    for slug, summary in summaries.items():
        if summary.get("settlement_reference_is_canonical") is not True or summary.get("settlement_side") not in {"UP", "DOWN"}:
            continue
        is_up = summary["settlement_side"] == "UP"
        for threshold in (.90, .95, .975):
            crossing_threshold = threshold if is_up else 1.0 - threshold

            def first_fresh_cross(measure: str, *, require_fresh: bool) -> float | None:
                candidates = []
                for event in threshold_events:
                    if event.get("market_slug") != slug or event.get("measure") != measure:
                        continue
                    if require_fresh and event.get("sigma_ex_market_fresh") is not True:
                        continue
                    if measure == "market_mid_probability_up" and not _fresh_market_quote(event):
                        continue
                    value_key = "p_up_ex_market" if measure == "p_up_ex_market" else "market_mid_probability_up"
                    if event.get(value_key) is None:
                        continue
                    try:
                        event_threshold = float(event.get("threshold"))
                        observed_ts = float(event.get("observed_ts"))
                    except (TypeError, ValueError):
                        continue
                    if (abs(event_threshold - crossing_threshold) <= 1e-9
                            and event.get("crossing_direction") in {"first_observed_beyond", "entered"}):
                        candidates.append(observed_ts)
                return min(candidates) if candidates else None

            model_ts = first_fresh_cross("p_up_ex_market", require_fresh=True)
            market_ts = first_fresh_cross("market_mid_probability_up", require_fresh=False)
            if model_ts is None and market_ts is None:
                continue
            lead_rows.append({"market_slug": slug, "settlement_side": summary["settlement_side"],
                              "probability_threshold": threshold, "model_first_observed_ts": model_ts,
                              "market_first_observed_ts": market_ts,
                              "model_lead_sec": float(market_ts)-float(model_ts) if model_ts is not None and market_ts is not None else None,
                              "lead_interpretation": "positive=model earlier; negative=model later; null=one side unavailable",
                              "descriptive_only": True})
    return metric_rows, bucket_rows, lead_rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/research/twap_forward_shadow.db")
    parser.add_argument("--output-dir", default="reports/twap_forward")
    args = parser.parse_args(); db = Path(args.db)
    # The separate DB is the live default.  Retain the old shared path only as
    # a read-only compatibility fallback for pre-separation historical reports.
    if not db.is_file() and args.db == "data/research/twap_forward_shadow.db":
        db = Path("data/research/hyperliquid_lead_lag.db")
    if not db.is_file() and args.db == "data/research/hyperliquid_lead_lag.db":
        db = Path("logs/hyperliquid_lead_lag.db")
    if not db.is_file(): parser.error(f"research DB not found: {args.db}")
    out = Path(args.output_dir); out.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        raw = conn.execute("SELECT decision_epoch_ns,payload_json FROM lead_lag_decisions ORDER BY id").fetchall()
    rows = []
    for ts, text in raw:
        payload = json.loads(text or "{}")
        if payload.get("event_type") in EVENTS:
            payload["observed_ts"] = int(ts) / 1_000_000_000
            rows.append(payload)
    summaries = [r for r in rows if r.get("event_type") == "MARKET_TWAP_SUMMARY"]
    opening_samples = [r for r in rows if r.get("event_type") == "MARKET_OPENING_TWAP_SAMPLE"]
    all_checkpoints = [r for r in rows if r.get("event_type") == "TMINUS_CHECKPOINT"]
    checkpoints = []
    for row in all_checkpoints:
        try:
            left, target = float(row.get("time_left_sec")), float(row.get("checkpoint_sec"))
        except (TypeError, ValueError):
            continue
        if 0.0 < left <= target:
            checkpoints.append(row)
    crosses = [r for r in rows if r.get("event_type") == "TWAP_STRIKE_CROSS"]
    projection = [r for r in rows if r.get("event_type") == "TWAP_PROJECTED_SIDE_CHANGE"]
    write_csv(out / "market_twap_summary.csv", summaries)
    write_csv(out / "market_opening_twap_samples.csv", opening_samples)
    write_csv(out / "twap_checkpoints.csv", checkpoints)
    write_csv(out / "twap_cross_events.csv", crosses)
    write_csv(out / "twap_projection_events.csv", projection)
    probability_metrics, probability_buckets, probability_lead = probability_research_rows(rows)
    write_csv(out / "settlement_probability_calibration.csv", probability_metrics)
    write_csv(out / "settlement_probability_buckets.csv", probability_buckets)
    write_csv(out / "settlement_probability_lead_lag.csv", probability_lead)
    canonical_summaries = [r for r in summaries if r.get("settlement_reference_is_canonical") is True]
    proxy_summaries = [r for r in summaries if r not in canonical_summaries]
    canonical_slugs = {r.get("market_slug") for r in canonical_summaries}
    canonical_probability_checkpoints = {
        (r.get("market_slug"), int(r.get("checkpoint_sec") or 0))
        for r in checkpoints
        if (r.get("market_slug") in canonical_slugs
            and 0.0 < float(r.get("time_left_sec") or 0.0) <= float(r.get("checkpoint_sec") or 0.0)
            and r.get("p_up_ex_market") is not None
            and r.get("sigma_ex_market_fresh") is True)
    }
    opening_summaries = [r for r in summaries if "opening_20s_observation_count" in r]
    first_open_ages = [float(r["first_observed_market_age_sec"]) for r in opening_summaries
                       if r.get("first_observed_market_age_sec") is not None]
    # Accuracy needs observed outcome labels and is intentionally restricted to
    # canonical Chainlink-60s settlement references.  Do not turn a Binance or
    # generic external fallback into a primary accuracy observation.
    accuracy_rows = []
    for row in canonical_summaries:
        predicted = row.get("last_projected_side")
        settlement = row.get("settlement_side")
        if predicted in {"UP", "DOWN"} and settlement in {"UP", "DOWN"}:
            accuracy_rows.append({"market_slug": row.get("market_slug"), "predicted_side": predicted,
                                  "settlement_side": settlement, "correct": predicted == settlement,
                                  "settlement_reference_source": row.get("settlement_reference_source")})
    write_csv(out / "projection_accuracy.csv", accuracy_rows)
    # Empty first-generation placeholders are explicit rather than fabricated.
    for name in ("twap_vs_smart_money.csv", "twap_vs_polymarket_move.csv", "fast_spot_lead_lag.csv",
                 "stop_twap_forensics.csv", "twap_exit_urgency.csv"):
        write_csv(out / name, [])
    usage = shutil.disk_usage(db.parent)
    cap, min_free = float(os.getenv("TWAP_RESEARCH_MAX_DB_MB", "500")), float(os.getenv("TWAP_RESEARCH_MIN_FREE_DISK_GB", "10"))
    main_mb = db.stat().st_size / 1024 / 1024
    wal_path, shm_path = Path(f"{db}-wal"), Path(f"{db}-shm")
    wal_mb = wal_path.stat().st_size / 1024 / 1024 if wal_path.is_file() else 0.0
    shm_mb = shm_path.stat().st_size / 1024 / 1024 if shm_path.is_file() else 0.0
    db_size_mb, disk_free_gb = round(main_mb + wal_mb + shm_mb, 3), round(usage.free / 1024 / 1024 / 1024, 3)
    guard_rows = [r for r in rows if r.get("event_type") == "RESEARCH_STORAGE_GUARD_TRIGGERED"]
    cap_exceeded, free_low = db_size_mb >= cap, disk_free_gb < min_free
    effective_guard = bool(guard_rows) or cap_exceeded or free_low
    health = [{"research_db": str(db), "current_db_size_mb": db_size_mb, "db_size_mb": db_size_mb,
               "db_main_mb": round(main_mb, 3), "db_wal_mb": round(wal_mb, 3), "db_shm_mb": round(shm_mb, 3),
               "db_total_disk_mb": db_size_mb, "guard_scope": "TWAP_OPTIONAL_EVENTS_ONLY",
               "configured_db_cap_mb": cap, "db_cap_exceeded": cap_exceeded,
               "disk_free_gb": disk_free_gb, "configured_min_free_gb": min_free, "free_disk_low": free_low,
               "storage_guard_triggered": effective_guard, "guard_reason": guard_rows[-1].get("trigger_reason") if guard_rows else ("db_size_cap" if cap_exceeded else "free_disk_low" if free_low else ""),
               "optional_research_writes_enabled": not effective_guard,
               "event_count": len(rows), "event_counts": json.dumps(Counter(r.get("event_type") for r in rows), sort_keys=True),
               "raw_retention_enabled": False}]
    write_csv(out / "storage_health.csv", health)
    quality_note = "Official TWAP only; reconstructed TWAP is not persisted in this first release."
    if len(canonical_summaries) == 0:
        quality_note += " INSUFFICIENT_CANONICAL_SETTLEMENT_LABELS."
    if not canonical_probability_checkpoints:
        quality_note += " INSUFFICIENT_CANONICAL_SETTLEMENT_PROBABILITY_CHECKPOINTS."
    write_csv(out / "data_quality.csv", [{"event_rows": len(rows), "summary_rows": len(summaries),
                                           "checkpoint_rows_total": len(all_checkpoints),
                                           "checkpoint_rows_post_settlement_or_invalid": len(all_checkpoints) - len(checkpoints),
                                           "canonical_twap_settlements": len(canonical_summaries),
                                           "noncanonical_proxy_settlements": len(proxy_summaries),
                                           "canonical_probability_checkpoint_markets": len(canonical_probability_checkpoints),
                                           "markets_with_opening_20s_telemetry": len(opening_summaries),
                                           "markets_missing_opening_20s_telemetry_legacy": len(summaries) - len(opening_summaries),
                                           "markets_with_opening_20s_observations": len({r.get("market_slug") for r in opening_samples}),
                                           "opening_20s_sample_events": len(opening_samples),
                                           "opening_20s_observation_count_total": sum(int(r.get("opening_20s_observation_count") or 0) for r in opening_summaries),
                                           "first_observation_market_age_mean_sec": (sum(first_open_ages) / len(first_open_ages) if first_open_ages else None),
                                           "note": quality_note}])
    (out / "summary.md").write_text(
        "# TWAP forward research\n\n"
        "This is event-driven, shadow-only telemetry. Official current TWAP is Polymarket RTDS Chainlink 60s TWAP; "
        "the flat/trend settlement projections are estimates and have no live authority. Settlement-probability results use only canonical labels; all metrics are descriptive, not causal. "
        f"Captured events: {len(rows)}; summaries: {len(summaries)}; canonical labels: {len(canonical_summaries)}; "
        f"noncanonical proxy labels: {len(proxy_summaries)}; markets with opening-window observations: "
        f"{len({r.get('market_slug') for r in opening_samples})} markets with {len(opening_samples)} durable opening samples; "
        f"summary telemetry available for {len(opening_summaries)}/{len(summaries)} markets; legacy summaries without opening-window fields: "
        f"{len(summaries) - len(opening_summaries)}; opening-window observations: "
        f"{sum(int(r.get('opening_20s_observation_count') or 0) for r in opening_summaries)}.\n", encoding="utf-8")
    print(f"wrote {len(rows)} TWAP events to {out}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
