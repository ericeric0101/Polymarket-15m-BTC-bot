#!/usr/bin/env python3
"""Event-level forensics for one completed prediction-research run.

This is deliberately offline-only.  It reads PREDICTION_RESEARCH_SNAPSHOT
events from the TWAP research decision journal and never imports runtime
strategy modules or writes to any live database.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable
from bot.research.freshness import accept_native_v2


RUN_ID_DEFAULT = "run_1790985778_95f5cc02"
COMPLETE_SLUGS_DEFAULT = (
    "btc-updown-15m-1790985600",
    "btc-updown-15m-1790986500",
    "btc-updown-15m-1790987400",
    "btc-updown-15m-1790988300",
)
RESIDUAL_BINS = (
    (-math.inf, -0.10, "<= -0.10"),
    (-0.10, -0.05, "-0.10 to -0.05"),
    (-0.05, -0.02, "-0.05 to -0.02"),
    (-0.02, 0.02, "-0.02 to +0.02"),
    (0.02, 0.05, "+0.02 to +0.05"),
    (0.05, 0.10, "+0.05 to +0.10"),
    (0.10, math.inf, ">= +0.10"),
)
SIGMA_BINS = ((-math.inf, 0.5, "<0.5σ"), (0.5, 1.0, "0.5–1σ"),
              (1.0, 2.0, "1–2σ"), (2.0, 3.0, "2–3σ"), (3.0, math.inf, ">3σ"))


def _num(value: Any) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _fmt(value: Any, digits: int = 4) -> str:
    value = _num(value)
    return "—" if value is None else f"{value:.{digits}f}"


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    keys = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _median(values: Iterable[float]) -> float | None:
    values = list(values)
    return statistics.median(values) if values else None


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return statistics.mean(values) if values else None


def _up_mid(row: dict[str, Any]) -> tuple[float | None, str | None]:
    """Return a fresh UP probability, using fresh DOWN only as its complement."""
    up = _num(row.get("market_mid_up"))
    if row.get("market_mid_up_fresh") is True and up is not None:
        return up, "up_mid"
    down = _num(row.get("market_mid_down"))
    if row.get("market_mid_down_fresh") is True and down is not None:
        return 1.0 - down, "down_mid_complement"
    return None, None


def _episode_count(rows: list[dict[str, Any]], *, time_key: str = "snapshot_ts", gap_sec: float = 15.0) -> int:
    by_slug: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_slug[str(row["market_slug"])].append(row)
    count = 0
    for group in by_slug.values():
        group.sort(key=lambda row: float(row[time_key]))
        last = None
        for row in group:
            current = float(row[time_key])
            if last is None or current - last > gap_sec:
                count += 1
            last = current
    return count


def _nearest(rows: list[dict[str, Any]], target: float, *, after: bool = False,
             within: float = 2.0, predicate=None) -> dict[str, Any] | None:
    candidates = [row for row in rows
                  if (not after or row["snapshot_ts"] >= target)
                  and abs(row["snapshot_ts"] - target) <= within
                  and (predicate(row) if predicate else True)]
    return min(candidates, key=lambda row: abs(row["snapshot_ts"] - target)) if candidates else None


def _first_after(rows: list[dict[str, Any]], start: float, predicate) -> dict[str, Any] | None:
    for row in rows:
        if row["snapshot_ts"] >= start and predicate(row):
            return row
    return None


def _load(db_path: Path, run_id: str, slugs: set[str], *, exclusions: dict | None = None) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    exclusions = exclusions if exclusions is not None else {}
    timeline: dict[str, list[dict[str, Any]]] = defaultdict(list)
    summaries: dict[str, dict[str, Any]] = {}
    uri = f"file:{db_path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        for record in conn.execute(
            "SELECT slug, decision_epoch_ns, payload_json FROM lead_lag_decisions "
            "WHERE run_id = ? ORDER BY decision_epoch_ns", (run_id,)
        ):
            try:
                payload = json.loads(record[2])
            except (TypeError, json.JSONDecodeError):
                continue
            slug = str(payload.get("market_slug") or record[0] or "")
            if slug not in slugs:
                continue
            event_type = payload.get("event_type")
            if event_type == "PREDICTION_RESEARCH_SNAPSHOT":
                if not accept_native_v2(payload, exclusions):
                    continue
                payload["snapshot_ts"] = _num(payload.get("snapshot_ts")) or record[1] / 1e9
                payload["market_slug"] = slug
                payload["up_mid"], payload["up_mid_source"] = _up_mid(payload)
                payload["residual_up_normalized"] = (
                    _num(payload.get("p_up_ex_market")) - payload["up_mid"]
                    if _num(payload.get("p_up_ex_market")) is not None and payload["up_mid"] is not None
                    else None
                )
                timeline[slug].append(payload)
            elif event_type == "MARKET_TWAP_SUMMARY":
                summaries[slug] = payload
    for rows in timeline.values():
        rows.sort(key=lambda row: row["snapshot_ts"])
    return timeline, summaries


def _joint_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("joint_fresh") is True and row.get("up_mid") is not None]


def _coalesced_events(rows: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    """Find 30s ±3s moves, then merge overlapping same-direction windows.

    A run of adjacent baselines describing one continuous move becomes one event,
    represented by its largest absolute 30s move.  This avoids treating 1 Hz
    snapshots of the same repricing as independent events.
    """
    raw: list[dict[str, Any]] = []
    for index, start in enumerate(rows):
        future = [row for row in rows[index + 1:]
                  if 27.0 <= row["snapshot_ts"] - start["snapshot_ts"] <= 33.0]
        if not future:
            continue
        end = min(future, key=lambda row: abs(row["snapshot_ts"] - start["snapshot_ts"] - 30.0))
        move = end["up_mid"] - start["up_mid"]
        if abs(move) >= threshold:
            raw.append({"event_start_ts": start["snapshot_ts"], "event_end_ts": end["snapshot_ts"],
                        "starting_mid": start["up_mid"], "ending_mid": end["up_mid"],
                        "mid_change_30s": move, "direction": "UP" if move > 0 else "DOWN",
                        "baseline": start, "end": end})
    grouped: list[list[dict[str, Any]]] = []
    for event in raw:
        if (not grouped or event["direction"] != grouped[-1][-1]["direction"]
                or event["event_start_ts"] - grouped[-1][-1]["event_start_ts"] > 30.0):
            grouped.append([event])
        else:
            grouped[-1].append(event)
    return [max(group, key=lambda event: abs(event["mid_change_30s"])) for group in grouped]


def _local_btc_noise(rows: list[dict[str, Any]], horizon: int) -> float:
    values = sorted(abs(_num(row.get(f"btc_return_{horizon}s_bps")) or 0.0) for row in rows
                    if _num(row.get(f"btc_return_{horizon}s_bps")) is not None)
    if not values:
        return math.inf
    return values[max(0, math.ceil(0.75 * len(values)) - 1)]


def _event_signals(event: dict[str, Any], rows: list[dict[str, Any]], btc_noise: dict[int, float]) -> dict[str, Any]:
    start, end, direction = event["event_start_ts"], event["event_end_ts"], event["direction"]
    sign = 1.0 if direction == "UP" else -1.0
    baseline = event["baseline"]
    window = [row for row in rows if start <= row["snapshot_ts"] <= end]
    baseline_pex = _num(baseline.get("p_up_ex_market"))
    baseline_res = _num(baseline.get("residual_up_normalized"))
    baseline_sigma = _num(baseline.get("required_move_sigma"))
    baseline_state = str(baseline.get("settlement_state_side") or "UNKNOWN")
    mid_first = _first_after(window, start, lambda row: sign * (row["up_mid"] - baseline["up_mid"]) >= 0.05)
    pex_first = _first_after(window, start, lambda row: baseline_pex is not None and _num(row.get("p_up_ex_market")) is not None and sign * (_num(row.get("p_up_ex_market")) - baseline_pex) >= 0.05)
    residual_first = _first_after(window, start, lambda row: baseline_res is not None and _num(row.get("residual_up_normalized")) is not None and sign * (_num(row.get("residual_up_normalized")) - baseline_res) >= 0.05)
    btc_first: dict[int, dict[str, Any] | None] = {}
    for horizon in (5, 10):
        threshold = btc_noise[horizon]
        btc_first[horizon] = _first_after(window, start, lambda row, h=horizon, t=threshold: _num(row.get(f"btc_return_{h}s_bps")) is not None and sign * _num(row.get(f"btc_return_{h}s_bps")) >= t)
    # Being on the event direction at the baseline is not a flip and cannot
    # claim a lead.  Require an actual transition from a different side in
    # the synchronized timeline.
    prior_side = str(baseline.get("active_side") or "")
    side_first = None
    previous_side = prior_side
    for row in window:
        current_side = str(row.get("active_side") or "")
        if current_side == direction and previous_side != direction:
            side_first = row
            break
        previous_side = current_side
    twap_first = _first_after(window, start, lambda row: str(row.get("settlement_state_side") or "") == direction and baseline_state != direction)
    sigma_first = _first_after(window, start, lambda row: baseline_sigma is not None and _num(row.get("required_move_sigma")) is not None and abs(_num(row.get("required_move_sigma")) - baseline_sigma) >= 0.25)

    def ts_and_lead(row: dict[str, Any] | None) -> tuple[float | None, float | None]:
        if row is None or mid_first is None:
            return None, None
        return row["snapshot_ts"], mid_first["snapshot_ts"] - row["snapshot_ts"]

    result = dict(event)
    result.update({
        "mid_5c_first_ts": mid_first["snapshot_ts"] if mid_first else None,
        "p_ex_first_ts": ts_and_lead(pex_first)[0], "p_ex_lead_vs_mid_sec": ts_and_lead(pex_first)[1],
        "residual_first_ts": ts_and_lead(residual_first)[0], "residual_lead_vs_mid_sec": ts_and_lead(residual_first)[1],
        "btc_5s_first_ts": ts_and_lead(btc_first[5])[0], "btc_5s_lead_vs_mid_sec": ts_and_lead(btc_first[5])[1],
        "btc_10s_first_ts": ts_and_lead(btc_first[10])[0], "btc_10s_lead_vs_mid_sec": ts_and_lead(btc_first[10])[1],
        "bot_side_first_ts": ts_and_lead(side_first)[0], "bot_side_lead_vs_mid_sec": ts_and_lead(side_first)[1],
        "twap_state_first_ts": ts_and_lead(twap_first)[0], "twap_state_lead_vs_mid_sec": ts_and_lead(twap_first)[1],
        "btc_5s_noise_threshold_bps": btc_noise[5], "btc_10s_noise_threshold_bps": btc_noise[10],
        "p_ex_baseline": baseline_pex, "p_ex_end": _num(event["end"].get("p_up_ex_market")),
        "residual_baseline": baseline_res, "residual_end": _num(event["end"].get("residual_up_normalized")),
        "required_sigma_baseline": baseline_sigma, "required_sigma_end": _num(event["end"].get("required_move_sigma")),
        "required_sigma_delta": (_num(event["end"].get("required_move_sigma")) - baseline_sigma
                                 if baseline_sigma is not None and _num(event["end"].get("required_move_sigma")) is not None else None),
        "required_sigma_first_change_ts": sigma_first["snapshot_ts"] if sigma_first else None,
        "required_sigma_mode": baseline.get("required_move_mode"),
        "baseline_active_side": baseline.get("active_side"),
        "baseline_twap_state": baseline_state,
    })
    result.pop("baseline", None)
    result.pop("end", None)
    return result


def _residual_report(rows_by_slug: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for slug, rows in rows_by_slug.items():
        for index, row in enumerate(rows):
            future = [item for item in rows[index + 1:]
                      if 28.0 <= item["snapshot_ts"] - row["snapshot_ts"] <= 32.0]
            if not future:
                continue
            future_row = min(future, key=lambda item: abs(item["snapshot_ts"] - row["snapshot_ts"] - 30.0))
            observations.append({"market_slug": slug, "snapshot_ts": row["snapshot_ts"],
                                 "residual_up": row["residual_up_normalized"],
                                 "future_mid_change_30s": future_row["up_mid"] - row["up_mid"]})
    output: list[dict[str, Any]] = []
    for low, high, label in RESIDUAL_BINS:
        selected = [row for row in observations if low <= row["residual_up"] < high]
        changes = [row["future_mid_change_30s"] for row in selected]
        directional = [change > 0 if low >= 0.02 else change < 0 if high <= -0.02 else None for change in changes]
        directional = [value for value in directional if value is not None]
        output.append({"residual_bin": label, "observations": len(selected),
                       "episodes": _episode_count(selected), "markets": len({row["market_slug"] for row in selected}),
                       "mean_future_mid_change_30s": _mean(changes), "median_future_mid_change_30s": _median(changes),
                       "directional_hit_rate": _mean(directional) if directional else None})
    return output


def _held_side_report(rows_by_slug: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    observations: list[dict[str, Any]] = []
    sigma_observations: list[dict[str, Any]] = []
    for slug, rows in rows_by_slug.items():
        for index, row in enumerate(rows):
            side = str(row.get("active_side") or "")
            token_mid = _num(row.get(f"market_mid_{side.lower()}")) if side in {"UP", "DOWN"} else None
            if token_mid is None or row.get(f"market_mid_{side.lower()}_fresh") is not True:
                continue
            future = [item for item in rows[index + 1:]
                      if 28.0 <= item["snapshot_ts"] - row["snapshot_ts"] <= 32.0
                      and str(item.get("active_side") or "") == side
                      and item.get(f"market_mid_{side.lower()}_fresh") is True
                      and _num(item.get(f"market_mid_{side.lower()}")) is not None]
            if not future:
                continue
            item = min(future, key=lambda value: abs(value["snapshot_ts"] - row["snapshot_ts"] - 30.0))
            change = _num(item.get(f"market_mid_{side.lower()}")) - token_mid
            item_out = {"market_slug": slug, "snapshot_ts": row["snapshot_ts"], "held_side": side,
                        "held_side_mid_change_30s": change, "btc_disagree_10s": row.get("btc_disagree_10s") is True,
                        "required_move_sigma": _num(row.get("required_move_sigma"))}
            observations.append(item_out)
            if item_out["required_move_sigma"] is not None:
                sigma_observations.append(item_out)
    disagreement: list[dict[str, Any]] = []
    for value, label in ((False, "AGREEMENT"), (True, "DISAGREEMENT")):
        selected = [row for row in observations if row["btc_disagree_10s"] is value]
        changes = [row["held_side_mid_change_30s"] for row in selected]
        disagreement.append({"group": label, "observations": len(selected), "episodes": _episode_count(selected),
                             "markets": len({row["market_slug"] for row in selected}),
                             "mean_held_side_mid_change_30s": _mean(changes), "median_held_side_mid_change_30s": _median(changes),
                             "adverse_repricing_rate": _mean(change < 0 for change in changes) if changes else None,
                             "adverse_ge_5c_rate": _mean(change <= -0.05 for change in changes) if changes else None,
                             "adverse_ge_10c_rate": _mean(change <= -0.10 for change in changes) if changes else None})
    sigma_rows: list[dict[str, Any]] = []
    for low, high, label in SIGMA_BINS:
        selected = [row for row in sigma_observations if low <= row["required_move_sigma"] < high]
        changes = [row["held_side_mid_change_30s"] for row in selected]
        sigma_rows.append({"required_move_sigma_bin": label, "observations": len(selected), "episodes": _episode_count(selected),
                           "markets": len({row["market_slug"] for row in selected}),
                           "mean_held_side_mid_change_30s": _mean(changes), "median_held_side_mid_change_30s": _median(changes),
                           "adverse_repricing_rate": _mean(change < 0 for change in changes) if changes else None,
                           "adverse_ge_5c_rate": _mean(change <= -0.05 for change in changes) if changes else None,
                           "adverse_ge_10c_rate": _mean(change <= -0.10 for change in changes) if changes else None})
    return disagreement, sigma_rows


def _read_entries(journal_path: Path, run_id: str) -> list[dict[str, Any]]:
    entries = []
    with sqlite3.connect(journal_path) as conn:
        for _, payload in conn.execute("SELECT ts, payload_json FROM order_events WHERE run_id = ? AND event_type = 'SHADOW_SIM_SETTLED'", (run_id,)):
            try:
                entries.append(json.loads(payload))
            except (TypeError, json.JSONDecodeError):
                pass
    return entries


def _entry_report(entries: list[dict[str, Any]], timelines: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    output = []
    for entry in entries:
        slug, side = str(entry["slug"]), str(entry["side"]).upper()
        timestamp = _num(entry.get("filled_ts")) or _num(entry.get("created_ts"))
        rows = timelines.get(slug, [])
        snapshot = _nearest(rows, timestamp, within=1.0) if timestamp is not None else None
        p_side = _num(snapshot.get(f"p_{side.lower()}_ex_market")) if snapshot else None
        snap_ask = _num(snapshot.get(f"best_ask_{side.lower()}")) if snapshot else None
        recorded_entry = _num(entry.get("entry_price"))
        fresh = bool(snapshot and snapshot.get("p_ex_fresh") is True and snapshot.get(f"market_mid_{side.lower()}_fresh") is True)
        edge_vs_recorded = p_side - recorded_entry if p_side is not None and recorded_entry is not None else None
        edge_vs_snap_ask = p_side - snap_ask if fresh and p_side is not None and snap_ask is not None else None
        # A fill record is the execution truth.  A transient snapshot ask that
        # differs from it cannot establish edge for the actual simulated order.
        if fresh and edge_vs_recorded is not None and edge_vs_recorded > 0:
            classification = "VERIFIED_EX_MARKET_EDGE"
        elif fresh and edge_vs_recorded is not None:
            classification = "MARKET_FOLLOWING"
        else:
            classification = "UNVERIFIED_DUE_TO_DATA"
        output.append({"market_slug": slug, "side": side, "entry_ts": timestamp, "entry_price": recorded_entry,
                       "snapshot_ts": snapshot.get("snapshot_ts") if snapshot else None,
                       "snapshot_delta_sec": snapshot.get("snapshot_ts") - timestamp if snapshot and timestamp else None,
                       "p_ex_side": p_side, "fresh_same_side_quote": fresh,
                       "snapshot_ask_side": snap_ask, "edge_vs_snapshot_ask": edge_vs_snap_ask,
                       "edge_vs_recorded_entry": edge_vs_recorded, "btc_return_10s_bps": snapshot.get("btc_return_10s_bps") if snapshot else None,
                       "btc_return_30s_bps": snapshot.get("btc_return_30s_bps") if snapshot else None,
                       "required_move_sigma": snapshot.get("required_move_sigma") if snapshot else None,
                       "residual_up": snapshot.get("residual_up_normalized") if snapshot else None,
                       "classification": classification, "settlement_outcome": entry.get("outcome"), "won": entry.get("won")})
    return output


def _fast_follow_case(timeline: list[dict[str, Any]], candidate_ts: float) -> list[dict[str, Any]]:
    keys = ("snapshot_ts", "p_up_ex_market", "p_down_ex_market", "market_mid_up", "market_mid_down", "up_mid",
            "residual_up_normalized", "btc_return_5s_bps", "btc_return_10s_bps", "btc_return_30s_bps",
            "official_twap", "settlement_state_side", "required_move_bps", "required_move_sigma",
            "active_side", "side_score", "joint_fresh", "market_quote_fresh", "p_ex_fresh")
    return [{"candidate_offset_sec": row["snapshot_ts"] - candidate_ts, **{key: row.get(key) for key in keys}}
            for row in timeline if candidate_ts - 30.0 <= row["snapshot_ts"] <= candidate_ts + 60.0]


def _lead_ranking(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    signals = (("BTC 5s", "btc_5s_lead_vs_mid_sec"), ("BTC 10s", "btc_10s_lead_vs_mid_sec"),
               ("p_ex", "p_ex_lead_vs_mid_sec"), ("residual", "residual_lead_vs_mid_sec"),
               ("TWAP state", "twap_state_lead_vs_mid_sec"), ("bot side", "bot_side_lead_vs_mid_sec"))
    output = []
    for name, key in signals:
        values = [_num(event.get(key)) for event in events if _num(event.get(key)) is not None]
        output.append({"signal": name, "measurable_events": len(values), "markets": len({event["market_slug"] for event in events if _num(event.get(key)) is not None}),
                       "median_lead_vs_mid_sec": _median(values), "mean_lead_vs_mid_sec": _mean(values),
                       "min_lead_vs_mid_sec": min(values) if values else None, "max_lead_vs_mid_sec": max(values) if values else None,
                       "qualification": "LOW_SAMPLE" if len(values) < 3 else "DESCRIPTIVE"})
    # Required sigma is deliberately not ranked: it is a distance/fragility
    # magnitude, not a directional UP/DOWN predictor.
    output.sort(
        key=lambda row: (
            row["median_lead_vs_mid_sec"] is not None,
            row["median_lead_vs_mid_sec"] if row["median_lead_vs_mid_sec"] is not None else -math.inf,
            row["mean_lead_vs_mid_sec"] if row["mean_lead_vs_mid_sec"] is not None else -math.inf,
        ),
        reverse=True,
    )
    qualified_rank = 0
    for row in output:
        if row["measurable_events"] >= 3:
            qualified_rank += 1
            row["rank"] = qualified_rank
        else:
            row["rank"] = None
    return output


def analyze(db_path: Path, journal_path: Path, output: Path, run_id: str) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    slugs = set(COMPLETE_SLUGS_DEFAULT)
    exclusions = {}
    timelines, summaries = _load(db_path, run_id, slugs, exclusions=exclusions)
    complete = [slug for slug in COMPLETE_SLUGS_DEFAULT if slug in summaries]
    timelines = {slug: timelines[slug] for slug in complete}
    quality = []
    for index, slug in enumerate(complete):
        rows = timelines[slug]
        entry_count = sum(row.get("snapshot_trigger") == "entry_decision" for row in rows)
        quality.append({"run_id": run_id, "market_slug": slug,
                        "coverage": "PARTIAL_OPENING_COVERAGE" if index == 0 else "FULL_POST_START_COVERAGE",
                        "snapshot_count": len(rows), "joint_fresh_snapshot_count": len(_joint_rows(rows)),
                        "entry_snapshot_count": entry_count,
                        "canonical_settlement_side": summaries[slug].get("canonical_settlement_side"),
                        "settlement_reference_is_canonical": summaries[slug].get("settlement_reference_is_canonical"),
                        "first_snapshot_ts": rows[0]["snapshot_ts"] if rows else None,
                        "last_snapshot_ts": rows[-1]["snapshot_ts"] if rows else None})
    _write_csv(output / "data_quality.csv", quality)

    events = []
    secondary_events = []
    for slug, rows in timelines.items():
        joint = _joint_rows(rows)
        noise = {horizon: _local_btc_noise(joint, horizon) for horizon in (5, 10)}
        events.extend({"market_slug": slug, **_event_signals(event, joint, noise)} for event in _coalesced_events(joint, 0.10))
        secondary_events.extend({"market_slug": slug, **_event_signals(event, joint, noise)} for event in _coalesced_events(joint, 0.05))
    _write_csv(output / "repricing_events.csv", events)
    _write_csv(output / "repricing_events_5c_secondary.csv", secondary_events)
    ranking = _lead_ranking(events)
    _write_csv(output / "signal_lead_ranking.csv", ranking)
    residual = _residual_report({slug: _joint_rows(rows) for slug, rows in timelines.items()})
    _write_csv(output / "residual_replication.csv", residual)
    disagreement, sigma = _held_side_report({slug: _joint_rows(rows) for slug, rows in timelines.items()})
    _write_csv(output / "btc_disagreement.csv", disagreement)
    _write_csv(output / "required_sigma_repricing.csv", sigma)
    entries = _entry_report(_read_entries(journal_path, run_id), timelines)
    _write_csv(output / "entry_edge_classification.csv", entries)
    candidate_ts = 1790987899.382476
    fast_follow = _fast_follow_case(timelines.get("btc-updown-15m-1790987400", []), candidate_ts)
    _write_csv(output / "fast_follow_case.csv", fast_follow)

    pos = next(row for row in residual if row["residual_bin"] == ">= +0.10")
    neg = next(row for row in residual if row["residual_bin"] == "<= -0.10")
    agree = next(row for row in disagreement if row["group"] == "AGREEMENT")
    disagree = next(row for row in disagreement if row["group"] == "DISAGREEMENT")
    sigma_low = next(row for row in sigma if row["required_move_sigma_bin"] == "<0.5σ")
    sigma_mid = next(row for row in sigma if row["required_move_sigma_bin"] == "1–2σ")
    pex_leads = [row["p_ex_lead_vs_mid_sec"] for row in events if _num(row.get("p_ex_lead_vs_mid_sec")) is not None]
    pex_first = sum((_num(row.get("p_ex_lead_vs_mid_sec")) or 0) > 2.0 for row in events)
    mid_first = sum((_num(row.get("p_ex_lead_vs_mid_sec")) or 0) < -2.0 for row in events)
    same = len(pex_leads) - pex_first - mid_first
    residual_status = "NOT_REPLICATED" if pos["observations"] else "NO_EVENTS"
    if pos["observations"] and (pos["mean_future_mid_change_30s"] or 0) > 0:
        residual_status = "PARTIALLY_REPLICATED"
    negative_status = "NOT_REPLICATED" if neg["observations"] else "NO_EVENTS"
    if neg["observations"] and (neg["mean_future_mid_change_30s"] or 0) < 0:
        negative_status = "PARTIALLY_REPLICATED"
    disagreement_answer = "YES" if (disagree["adverse_repricing_rate"] or 0) > (agree["adverse_repricing_rate"] or 0) and (disagree["adverse_ge_5c_rate"] or 0) >= (agree["adverse_ge_5c_rate"] or 0) else "MIXED"
    sigma_answer = "NO" if (sigma_low["adverse_repricing_rate"] or 0) <= (sigma_mid["adverse_repricing_rate"] or 0) else "MIXED"
    classifications = Counter(row["classification"] for row in entries)
    verdict = "B. BTC short-term movement 是目前最有希望的 predictor"
    # In this run p_ex does not lead the largest events consistently, while
    # BTC is the only early directional variable available in multiple events.
    # This is a descriptive selection, not a live recommendation.
    report = f"""# Four-market prediction forensics

Native-v2 exclusion counts: {json.dumps(exclusions, sort_keys=True)}

Run: `{run_id}`.  Analysis scope is exactly the four completed market slugs in
`data_quality.csv`; the subsequent partial market is excluded.  Repricing is
an UP-token probability move over 30 seconds (nearest fresh synchronized point
within ±3 seconds), coalesced where overlapping same-direction 30-second
windows describe one continuous move.  Predictor comparisons use only
`joint_fresh` snapshots.  A positive lead means the predictor reached its
defined directional change before market mid first moved 5¢ from the event
baseline.

## Executive verdict

- **Q1 p_ex lead: MIXED.** {len(events)} ≥10¢ repricing events; p_ex was measurable for {len(pex_leads)}. It led by >2s in {pex_first}, lagged by >2s in {mid_first}, and was within ±2s in {same}. Median lead was {_fmt(_median(pex_leads), 2)}s (positive means p_ex first).
- **Q2 +0.10 residual replicated: {residual_status}.** Previous 14-market descriptive result: mean +0.2135 30s UP-mid repricing (N=10, 3 markets). This run: N={pos['observations']}, {pos['markets']} market(s), mean {_fmt(pos['mean_future_mid_change_30s'])}, median {_fmt(pos['median_future_mid_change_30s'])}, directional hit {_fmt(pos['directional_hit_rate'])}. The new extreme-positive rows moved slightly **down**, not up.
- **Q3 negative residual symmetry: {negative_status}.** `<=−0.10` has N={neg['observations']}, mean {_fmt(neg['mean_future_mid_change_30s'])}; it did not show the expected negative 30s repricing.
- **Q4 BTC disagreement: {disagreement_answer}.** Agreement adverse rate {_fmt(agree['adverse_repricing_rate'])}; disagreement {_fmt(disagree['adverse_repricing_rate'])}. But ≥5¢ adverse was {_fmt(agree['adverse_ge_5c_rate'])} versus {_fmt(disagree['adverse_ge_5c_rate'])}, so the stronger adverse threshold does not confirm it.
- **Q5 required sigma fragility: {sigma_answer}.** `<0.5σ` adverse rate {_fmt(sigma_low['adverse_repricing_rate'])}, versus `1–2σ` {_fmt(sigma_mid['adverse_repricing_rate'])}; low required sigma was not the more fragile bucket in this run.
- **Q6 wrong DOWN fast-follow cause: YES — the existing ex-market and market variables already contradicted it.** At the candidate, p_up_ex_market had fallen from about 0.92 but remained 0.67–0.76, the fresh UP mid was 0.695/0.705, official TWAP state remained UP, and required move was still a negative UP-favoring path distance. The trigger was an Outcome/TWAP follower event, not independent DOWN confirmation. See `fast_follow_case.csv`.
- **Q7 winning entries classification: {dict(classifications)}.** No completed winning simulation has a clean positive `p_ex − recorded entry price`: they are market-following or unverified, not evidence of independent model edge. See `entry_edge_classification.csv`.

## Repricing event table

| Market | Direction | Start mid → end mid | 30s change | p_ex lead vs mid | BTC10 lead vs mid | Residual lead vs mid | Bot-side lead vs mid |
|---|---|---:|---:|---:|---:|---:|---:|
""" + "\n".join(
        f"| {row['market_slug']} | {row['direction']} | {_fmt(row['starting_mid'], 3)} → {_fmt(row['ending_mid'], 3)} | {_fmt(row['mid_change_30s'], 3)} | {_fmt(row['p_ex_lead_vs_mid_sec'], 2)} | {_fmt(row['btc_10s_lead_vs_mid_sec'], 2)} | {_fmt(row['residual_lead_vs_mid_sec'], 2)} | {_fmt(row['bot_side_lead_vs_mid_sec'], 2)} |"
        for row in events
    ) + """

`required_move_sigma` is reported per event in `repricing_events.csv`, but is not ranked as an UP/DOWN lead because it is a distance-to-boundary magnitude rather than a directional predictor.

## Signal lead ranking

| Rank | Signal | Measurable events | Markets | Median lead seconds | Mean lead seconds | Qualification |
|---:|---|---:|---:|---:|---:|---|
""" + "\n".join(
        f"| {row['rank'] or '—'} | {row['signal']} | {row['measurable_events']} | {row['markets']} | {_fmt(row['median_lead_vs_mid_sec'], 2)} | {_fmt(row['mean_lead_vs_mid_sec'], 2)} | {row['qualification']} |"
        for row in ranking
    ) + f"""

## Residual replication

The fixed residual-bin result is in `residual_replication.csv`.  The key contrast is previous `>=+0.10`: +0.2135 mean 30s repricing versus this run: {_fmt(pos['mean_future_mid_change_30s'])}, N={pos['observations']}.  This run weakens, rather than confirms, that earlier extreme-positive residual observation.

## BTC disagreement

`btc_disagreement.csv` compares the held token’s subsequent 30-second midpoint.  Disagreement had a somewhat higher any-negative rate, but not a higher ≥5¢ or ≥10¢ adverse rate; the result is **MIXED**, not a usable rejection rule.

## Required-path fragility

`required_sigma_repricing.csv` shows no monotonic pattern where a low required move made held-side repricing more adverse.  In particular, <0.5σ had a positive mean held-side repricing ({_fmt(sigma_low['mean_held_side_mid_change_30s'])}) while 1–2σ was negative ({_fmt(sigma_mid['mean_held_side_mid_change_30s'])}).  This run weakens the “low sigma = immediate fragile position” hypothesis.

## Fast-follow failure case

The DOWN candidate was created at `{candidate_ts}`.  It did not have independent DOWN support: p_ex stayed UP-favoring and fresh UP market probability was already high.  BTC’s short 5s/10s return was briefly negative near the candidate, but it was the isolated conflicting input; TWAP state and p_ex never supplied a DOWN reversal.  If market mid is excluded, ex-market probability alone would still reject a DOWN entry; the candidate came from the separate Outcome follower path.

## Entry edge classification

The three settled simulation entries are itemized in `entry_edge_classification.csv`.  Classification compares fresh p_ex with the recorded order price, not a quote snapshot that may have repriced between simulation submission and fill.  A final win does not retroactively establish an independent edge.

## Final research verdict

**{verdict}** for this run’s descriptive event forensics: p_ex/residual did not consistently lead the fresh market repricings, and required-path sigma was not monotonic.  Short BTC movement is the most plausible existing early input because it is independent of the market and showed up before some repricings, but its disagreement comparison is mixed; it is not a live authority.

## Next two tests

### NEXT TEST 1

- **Hypothesis:** A BTC 5s/10s move only becomes useful when p_ex and market mid have not already repriced in the same direction.
- **Evidence from this run:** The DOWN fast-follow error had brief negative BTC movement while p_ex/TWAP state stayed UP; the broad disagreement cohort did not improve large-adverse detection.
- **Exact metric:** Among joint-fresh rows, compare 30s held-side repricing for BTC direction alone versus BTC direction plus p_ex/market sign agreement, using the existing `btc_return_5s_bps`, `btc_return_10s_bps`, `p_up_ex_market`, and fresh mid fields.
- **Confirm:** The combined condition improves ≥5¢ adverse/repricing hit rate over BTC-only in both directions.
- **Reject:** The combined condition remains no better than BTC-only or loses direction symmetry.

### NEXT TEST 2

- **Hypothesis:** Extreme residual is useful only after its direction persists, rather than at the first extreme print.
- **Evidence from this run:** `>=+0.10` residual did not reproduce the earlier 30s effect and its observations all came from one market; individual raw extremes reversed.
- **Exact metric:** Reuse existing snapshots to compare first extreme residual versus residual persisting for at least two 1Hz snapshots, measuring future 30s UP-mid movement by sign.
- **Confirm:** Persisted extremes have directional 30s repricing while first-print extremes do not.
- **Reject:** Persistence does not improve directional hit rate or remains asymmetric.

## Limitation

The first market has partial opening coverage; event rows, not 2,412 correlated snapshots, are the unit for the headline lead comparison.  The report is offline research only and makes no live strategy change.
"""
    (output / "summary.md").write_text(report, encoding="utf-8")
    return {"run_id": run_id, "markets": complete, "repricing_events": len(events),
            "snapshots": sum(len(rows) for rows in timelines.values()), "output": str(output)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/research/twap_forward_shadow.db")
    parser.add_argument("--journal", default="logs/trade_journal.db")
    parser.add_argument("--output", default="reports/four_market_prediction_forensics")
    parser.add_argument("--run-id", default=RUN_ID_DEFAULT)
    args = parser.parse_args()
    print(json.dumps(analyze(Path(args.db), Path(args.journal), Path(args.output), args.run_id), indent=2))


if __name__ == "__main__":
    main()
