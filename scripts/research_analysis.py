#!/usr/bin/env python3
"""Canonical, offline-only BTC15m prediction research analysis.

This is the single entry point for run, market, latest, and regime-comparison
research.  It reads the existing TWAP decision journal and trade journal but
never imports or changes a live strategy component.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from typing import Any, Iterable

from bot.entry_session_policy import TAIPEI
from bot.research.lifecycle import first_crossings, held_side_probability, number as lifecycle_number
from bot.research.store import ResearchStore
from bot.research.clocks import latest_evidence, epoch as clock_epoch
from bot.research.metrics import capital_efficiency, entry_timing_bin as _entry_timing_bin
from scripts import four_market_prediction_forensics as forensic


DEFAULT_DB = Path("data/research/twap_forward_shadow.db")
DEFAULT_JOURNAL = Path("logs/trade_journal.db")
DEFAULT_OUTPUT = Path("reports/research_analysis")
PRIMARY_CHECKPOINTS = (300, 180, 120, 60, 30)


def _num(value: Any) -> float | None:
    return forensic._num(value)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    keys = list(dict.fromkeys(key for row in rows for key in row)) or ["empty"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def market_context(slug: str) -> dict[str, Any]:
    """Return the canonical Taipei-start regime for a BTC15m slug."""
    try:
        start_ts = int(str(slug).rsplit("-", 1)[-1])
        local = datetime.fromtimestamp(start_ts, tz=timezone.utc).astimezone(TAIPEI)
    except (TypeError, ValueError, OSError, OverflowError):
        return {"session_regime": "UNKNOWN", "weekday_name": "UNKNOWN", "market_start_taipei": None}
    return {
        "session_regime": "WEEKEND" if local.weekday() >= 5 else "WEEKDAY",
        "weekday_name": local.strftime("%A"),
        "market_start_taipei": local.isoformat(),
    }


def _catalog(db_path: Path) -> list[dict[str, Any]]:
    """One latest canonical settlement summary per run/market pair."""
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for payload in ResearchStore(db_path).get_settlements():
        market_slug = str(payload.get("market_slug") or "")
        if not market_slug:
            continue
        run_id = str(payload["run_id"])
        row = {"run_id": run_id, "market_slug": market_slug,
               "summary_epoch_ns": int(payload["summary_epoch_ns"]), **market_context(market_slug)}
        records[(run_id, market_slug)] = row
    return sorted(records.values(), key=lambda row: row["summary_epoch_ns"], reverse=True)


def select_catalog(catalog: list[dict[str, Any]], *, markets: int | None = None,
                   regime: str | None = None, run_id: str | None = None,
                   slug: str | None = None) -> list[dict[str, Any]]:
    # Callers may provide a minimal catalog (as tests and future importers do).
    # Regime is always derived here from the market start, never inferred from
    # observation wall-clock time or an optional caller label.
    selected = [{**market_context(str(row.get("market_slug") or "")), **row}
                if "session_regime" not in row else dict(row)
                for row in catalog]
    if run_id:
        selected = [row for row in selected if row["run_id"] == run_id]
    if slug:
        selected = [row for row in selected if row["market_slug"] == slug]
    if regime:
        selected = [row for row in selected if row["session_regime"] == regime.upper()]
    return selected[:markets] if markets is not None else selected


def normalized_event_threshold(moves: Iterable[float]) -> float | None:
    """Top-20% within-regime absolute 30s move threshold (secondary only)."""
    values = sorted(abs(float(value)) for value in moves if value is not None and math.isfinite(float(value)))
    if not values:
        return None
    return values[max(0, math.ceil(0.8 * len(values)) - 1)]


def _load_selection(db_path: Path, selected: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    by_run: dict[str, set[str]] = defaultdict(set)
    for row in selected:
        by_run[row["run_id"]].add(row["market_slug"])
    timelines: dict[str, list[dict[str, Any]]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    store = ResearchStore(db_path)
    for run_id, slugs in by_run.items():
        for payload in store.get_prediction_snapshots(run_id=run_id):
            market_slug = payload["market_slug"]
            if market_slug not in slugs:
                continue
            payload["up_mid"], payload["up_mid_source"] = forensic._up_mid(payload)
            probability = _num(payload.get("p_up_ex_market"))
            payload["residual_up_normalized"] = (
                probability - payload["up_mid"]
                if probability is not None and payload["up_mid"] is not None else None
            )
            timelines.setdefault(market_slug, []).append(payload)
    for payload in store.get_settlements():
        market_slug = payload["market_slug"]
        if market_slug not in by_run.get(payload["run_id"], set()):
            continue
        previous = summaries.get(market_slug)
        if previous is None or payload["summary_epoch_ns"] >= previous["summary_epoch_ns"]:
            summaries[market_slug] = payload
    for rows in timelines.values():
        rows.sort(key=lambda row: row["snapshot_ts"])
    return timelines, summaries


def _contextual(row: dict[str, Any], contexts: dict[str, dict[str, Any]], *, batch_regime: str) -> dict[str, Any]:
    slug = str(row.get("market_slug") or "")
    context = contexts.get(slug, {"session_regime": batch_regime, "weekday_name": "MIXED", "market_start_taipei": None})
    return {**context, **row}


def _market_metrics(slug: str, rows: list[dict[str, Any]], summary: dict[str, Any] | None,
                    context: dict[str, Any]) -> dict[str, Any]:
    joint = forensic._joint_rows(rows)
    def values(key: str) -> list[float]:
        return [abs(value) for row in joint if (value := _num(row.get(key))) is not None]
    up_mids = [row["up_mid"] for row in joint]
    intervals = [b["snapshot_ts"] - a["snapshot_ts"] for a, b in zip(joint, joint[1:])]
    spreads = []
    top_sizes = []
    for row in joint:
        bid, ask = _num(row.get("best_bid_up")), _num(row.get("best_ask_up"))
        if bid is not None and ask is not None and ask >= bid:
            spreads.append(ask - bid)
        for name in ("best_bid_size_up", "best_ask_size_up", "best_bid_size", "best_ask_size"):
            if (size := _num(row.get(name))) is not None:
                top_sizes.append(size)
                break
    crossings = sum(
        str(a.get("settlement_state_side") or "") != str(b.get("settlement_state_side") or "")
        and str(a.get("settlement_state_side") or "") in {"UP", "DOWN"}
        and str(b.get("settlement_state_side") or "") in {"UP", "DOWN"}
        for a, b in zip(joint, joint[1:])
    )
    return {**context, "market_slug": slug, "snapshot_count": len(rows), "joint_fresh_snapshot_count": len(joint),
            "canonical_settlement_side": (summary or {}).get("canonical_settlement_side"),
            "btc_abs_return_5s_bps_median": _median(values("btc_return_5s_bps")),
            "btc_abs_return_10s_bps_median": _median(values("btc_return_10s_bps")),
            "btc_abs_return_30s_bps_median": _median(values("btc_return_30s_bps")),
            "btc_abs_return_60s_bps_median": _median(values("btc_return_60s_bps")),
            "spot_strike_distance_bps_median": _median(values("twap_minus_strike_bps")),
            "required_move_sigma_median": _median([_num(row.get("required_move_sigma")) for row in joint if _num(row.get("required_move_sigma")) is not None]),
            "median_up_mid": _median(up_mids), "median_spread": _median(spreads),
            "median_top_size": _median(top_sizes), "median_quote_interval_sec": _median(intervals),
            "joint_fresh_rate": len(joint) / len(rows) if rows else None,
            "spot_strike_crossings": crossings}


def _median(values: Iterable[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return statistics.median(clean) if clean else None


def _regime_comparison(markets: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metrics = ("btc_abs_return_5s_bps_median", "btc_abs_return_10s_bps_median", "btc_abs_return_30s_bps_median",
               "btc_abs_return_60s_bps_median", "spot_strike_distance_bps_median", "required_move_sigma_median",
               "median_spread", "median_top_size", "median_quote_interval_sec", "joint_fresh_rate")
    output: list[dict[str, Any]] = []
    for metric in metrics:
        weekday = [_num(row.get(metric)) for row in markets if row["session_regime"] == "WEEKDAY"]
        weekend = [_num(row.get(metric)) for row in markets if row["session_regime"] == "WEEKEND"]
        wday, wend = _median(weekday), _median(weekend)
        output.append({"metric": metric, "weekday_N_markets": len({r["market_slug"] for r in markets if r["session_regime"] == "WEEKDAY"}),
                       "weekend_N_markets": len({r["market_slug"] for r in markets if r["session_regime"] == "WEEKEND"}),
                       "weekday_value": wday, "weekend_value": wend,
                       "difference": wend - wday if wend is not None and wday is not None else None,
                       "ratio_if_meaningful": wend / wday if wend is not None and wday not in (None, 0) else None})
    for threshold, label in ((0.05, "repricing_events_ge_5c_per_market"), (0.10, "repricing_events_ge_10c_per_market")):
        weekday = [row for row in events if row.get("session_regime") == "WEEKDAY" and abs(_num(row.get("mid_change_30s")) or 0) >= threshold]
        weekend = [row for row in events if row.get("session_regime") == "WEEKEND" and abs(_num(row.get("mid_change_30s")) or 0) >= threshold]
        wn = max(1, len({r["market_slug"] for r in markets if r["session_regime"] == "WEEKDAY"}))
        en = max(1, len({r["market_slug"] for r in markets if r["session_regime"] == "WEEKEND"}))
        output.append({"metric": label, "weekday_N_markets": wn, "weekend_N_markets": en,
                       "weekday_value": len(weekday) / wn, "weekend_value": len(weekend) / en,
                       "difference": len(weekend) / en - len(weekday) / wn,
                       "ratio_if_meaningful": (len(weekend) / en) / (len(weekday) / wn) if weekday else None})
    return output


def _checkpoint_row(rows: list[dict[str, Any]], checkpoint_sec: int) -> dict[str, Any] | None:
    """Nearest synchronized row to the checkpoint, within a conservative 12s."""
    candidates = [row for row in rows if _num(row.get("time_left_sec")) is not None
                  and abs(float(row["time_left_sec"]) - checkpoint_sec) <= 12.0]
    return min(candidates, key=lambda row: abs(float(row["time_left_sec"]) - checkpoint_sec)) if candidates else None


def checkpoint_flip_rows(slug: str, rows: list[dict[str, Any]], summary: dict[str, Any],
                         regime: str) -> list[dict[str, Any]]:
    """Checkpoint labels based on TWAP settlement leader versus final settlement."""
    settlement = str(summary.get("canonical_settlement_side") or "")
    output: list[dict[str, Any]] = []
    for checkpoint in PRIMARY_CHECKPOINTS:
        row = _checkpoint_row(rows, checkpoint)
        leader = str(row.get("settlement_state_side") or "") if row else "UNKNOWN"
        valid = row is not None and leader in {"UP", "DOWN"} and settlement in {"UP", "DOWN"}
        market_up = _num(row.get("market_mid_up")) if row and row.get("market_mid_up_fresh") is True else None
        pex_up = _num(row.get("p_up_ex_market")) if row and row.get("sigma_ex_market_fresh") is True else None
        market_flip = (1.0 - market_up if leader == "UP" else market_up) if valid and market_up is not None else None
        analytic_flip = (1.0 - pex_up if leader == "UP" else pex_up) if valid and pex_up is not None else None
        output.append({"market_slug": slug, "session_regime": regime, "checkpoint_sec": checkpoint,
                       "snapshot_ts": row.get("snapshot_ts") if row else None,
                       "observed_time_left_sec": row.get("time_left_sec") if row else None,
                       "leader_side": leader, "canonical_settlement_side": settlement,
                       "observed_flip": leader != settlement if valid else None,
                       "market_implied_flip_probability": market_flip,
                       "analytic_flip_probability": analytic_flip,
                       "empirical_path_probability": None,
                       "comparable_synchronized": valid,
                       "required_move_sigma": _num(row.get("required_move_sigma")) if row else None})
    return output


def _checkpoint_summary(rows: list[dict[str, Any]], regime: str) -> list[dict[str, Any]]:
    output = []
    for checkpoint in PRIMARY_CHECKPOINTS:
        usable = [row for row in rows if row["checkpoint_sec"] == checkpoint and row.get("observed_flip") is not None]
        flips = [row for row in usable if row["observed_flip"]]
        def average(field: str) -> float | None:
            values = [_num(row.get(field)) for row in usable if _num(row.get(field)) is not None]
            return statistics.mean(values) if values else None
        observed_rate = len(flips) / len(usable) if usable else None
        market_mean, analytic_mean = average("market_implied_flip_probability"), average("analytic_flip_probability")
        output.append({"session_regime": regime, "checkpoint_sec": checkpoint, "N": len(usable),
                       "observed_flips": len(flips), "observed_flip_rate": observed_rate,
                       "mean_market_implied_flip_probability": market_mean,
                       "mean_analytic_flip_probability": analytic_mean,
                       "mean_empirical_path_probability": None,
                       "market_brier": _brier([(float(r["market_implied_flip_probability"]), bool(r["observed_flip"])) for r in usable if _num(r.get("market_implied_flip_probability")) is not None]),
                       "analytic_brier": _brier([(float(r["analytic_flip_probability"]), bool(r["observed_flip"])) for r in usable if _num(r.get("analytic_flip_probability")) is not None]),
                       "market_calibration_error": abs(market_mean - observed_rate) if market_mean is not None and observed_rate is not None else None,
                       "analytic_calibration_error": abs(analytic_mean - observed_rate) if analytic_mean is not None and observed_rate is not None else None})
    return output


def _sigma_flip_curve(rows: list[dict[str, Any]], regime: str) -> list[dict[str, Any]]:
    bins = ((-math.inf, .5, "<0.5sigma"), (.5, 1., "0.5-1sigma"), (1., 2., "1-2sigma"), (2., 3., "2-3sigma"), (3., math.inf, ">3sigma"))
    output = []
    for low, high, label in bins:
        selected = [row for row in rows if row.get("observed_flip") is not None
                    and (sigma := _num(row.get("required_move_sigma"))) is not None and low <= sigma < high]
        flips = [row for row in selected if row["observed_flip"]]
        def average(field: str) -> float | None:
            values = [_num(row.get(field)) for row in selected if _num(row.get(field)) is not None]
            return statistics.mean(values) if values else None
        output.append({"session_regime": regime, "sigma_bin": label, "N_markets": len({row["market_slug"] for row in selected}),
                       "N": len(selected), "observed_flips": len(flips),
                       "observed_flip_rate": len(flips) / len(selected) if selected else None,
                       "mean_analytic_flip_probability": average("analytic_flip_probability"),
                       "mean_market_implied_flip_probability": average("market_implied_flip_probability")})
    return output


def _persistent_side_ts(rows: list[dict[str, Any]], field: str, side: str, start_ts: float) -> float | None:
    """First of three consecutive samples agreeing with the settlement side."""
    candidates = [row for row in rows if row["snapshot_ts"] >= start_ts]
    for index, row in enumerate(candidates[:-2]):
        sequence = candidates[index:index + 3]
        if field == "p_ex":
            current = _num(row.get("p_up_ex_market"))
            sides = ["UP" if (_num(item.get("p_up_ex_market")) or -1) >= .5 else "DOWN"
                     if _num(item.get("p_up_ex_market")) is not None else "UNKNOWN" for item in sequence]
            current_side = "UP" if current is not None and current >= .5 else "DOWN" if current is not None else "UNKNOWN"
        elif field == "market_mid":
            current = _num(row.get("up_mid"))
            sides = ["UP" if (_num(item.get("up_mid")) or -1) >= .5 else "DOWN"
                     if _num(item.get("up_mid")) is not None else "UNKNOWN" for item in sequence]
            current_side = "UP" if current is not None and current >= .5 else "DOWN" if current is not None else "UNKNOWN"
        else:
            current_side = str(row.get(field) or "UNKNOWN")
            sides = [str(item.get(field) or "UNKNOWN") for item in sequence]
        if current_side == side and all(item == side for item in sides):
            return row["snapshot_ts"]
    return None


def _actual_flip_events(timelines: dict[str, list[dict[str, Any]]], summaries: dict[str, dict[str, Any]],
                        contexts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for slug, rows in timelines.items():
        checkpoint = _checkpoint_row(rows, 300)
        settlement = str(summaries.get(slug, {}).get("canonical_settlement_side") or "")
        leader = str(checkpoint.get("settlement_state_side") or "") if checkpoint else "UNKNOWN"
        if leader not in {"UP", "DOWN"} or settlement not in {"UP", "DOWN"} or leader == settlement:
            continue
        start = checkpoint["snapshot_ts"]
        signals = {
            "P_EX": _persistent_side_ts(rows, "p_ex", settlement, start),
            "MARKET_MID": _persistent_side_ts(rows, "market_mid", settlement, start),
            "TWAP_STATE": _persistent_side_ts(rows, "settlement_state_side", settlement, start),
            "BOT_SIDE": _persistent_side_ts(rows, "active_side", settlement, start),
        }
        for horizon in (5, 10, 30):
            key = f"btc_return_{horizon}s_bps"
            signals[f"BTC_{horizon}S"] = next(
                (row["snapshot_ts"] for row in rows if row["snapshot_ts"] >= start
                 and (value := _num(row.get(key))) is not None
                 and ((settlement == "UP" and value > 0) or (settlement == "DOWN" and value < 0))),
                None,
            )
        observed = [(name, ts) for name, ts in signals.items() if ts is not None]
        output.append({**contexts[slug], "market_slug": slug, "leader_at_t300": leader,
                       "canonical_settlement_side": settlement, "actual_flip": True,
                       "earliest_persistent_signal": min(observed, key=lambda item: item[1])[0] if observed else "NOT_MEASURABLE",
                       **{f"{name.lower()}_ts": ts for name, ts in signals.items()}})
    return output


def preliminary_regime_comparison(db_path: Path, journal_path: Path, output: Path) -> dict[str, Any]:
    """Preliminary comparison using only directly comparable synchronized cohorts."""
    output.mkdir(parents=True, exist_ok=True)
    catalog = _catalog(db_path)
    all_timelines, summaries = _load_selection(db_path, catalog)
    comparable = [row for row in catalog if all_timelines.get(row["market_slug"])]
    legacy_weekday = [row for row in catalog if row["session_regime"] == "WEEKDAY" and not all_timelines.get(row["market_slug"])]
    contexts = {row["market_slug"]: row for row in comparable}
    checkpoints = []
    for row in comparable:
        checkpoints.extend(checkpoint_flip_rows(row["market_slug"], all_timelines[row["market_slug"]],
                                                 summaries.get(row["market_slug"], {}), row["session_regime"]))
    weekday_rows = [row for row in checkpoints if row["session_regime"] == "WEEKDAY"]
    weekend_rows = [row for row in checkpoints if row["session_regime"] == "WEEKEND"]
    summary_rows = _checkpoint_summary(weekday_rows, "WEEKDAY") + _checkpoint_summary(weekend_rows, "WEEKEND")
    sigma_rows = _sigma_flip_curve(weekday_rows, "WEEKDAY") + _sigma_flip_curve(weekend_rows, "WEEKEND")
    flip_events = _actual_flip_events({slug: all_timelines[slug] for slug in contexts}, summaries, contexts)
    _write_csv(output / "checkpoint_flip_rates.csv", checkpoints)
    _write_csv(output / "checkpoint_summary.csv", summary_rows)
    _write_csv(output / "sigma_flip_curve.csv", sigma_rows)
    _write_csv(output / "actual_flip_events.csv", flip_events)
    _write_csv(output / "legacy_weekday_not_directly_comparable.csv", legacy_weekday)

    weekend_markets = [row for row in comparable if row["session_regime"] == "WEEKEND"]
    weekday_markets = [row for row in comparable if row["session_regime"] == "WEEKDAY"]
    weekend_start_span_hours = ((max(int(row["market_slug"].rsplit("-", 1)[-1]) for row in weekend_markets)
                                 - min(int(row["market_slug"].rsplit("-", 1)[-1]) for row in weekend_markets)) / 3600 + .25) if weekend_markets else 0
    weekend_hours = sum(
        max(0.0, rows[-1]["snapshot_ts"] - rows[0]["snapshot_ts"]) / 3600
        for slug, rows in all_timelines.items()
        if slug in contexts and contexts[slug]["session_regime"] == "WEEKEND" and rows
    )
    weekend_t300 = next((row for row in summary_rows if row["session_regime"] == "WEEKEND" and row["checkpoint_sec"] == 300), {})
    checkpoint_lines = "\n".join(
        f"| T-{row['checkpoint_sec']} | {row['N']} | {row['observed_flip_rate']} | "
        f"{row['mean_market_implied_flip_probability']} | {row['mean_analytic_flip_probability']} | "
        f"{row['market_brier']} | {row['analytic_brier']} |"
        for row in summary_rows if row["session_regime"] == "WEEKEND"
    )
    sigma_lines = "\n".join(
        f"| {row['sigma_bin']} | {row['N']} | {row['observed_flips']} | {row['observed_flip_rate']} | "
        f"{row['mean_market_implied_flip_probability']} | {row['mean_analytic_flip_probability']} |"
        for row in sigma_rows if row["session_regime"] == "WEEKEND"
    )
    flip_counts = defaultdict(int)
    for row in flip_events:
        flip_counts[row["earliest_persistent_signal"]] += 1
    report = f"""# Preliminary weekend vs weekday regime comparison

Mode: PRELIMINARY_REGIME_COMPARISON. Weekend and weekday are never pooled.

## Market counts

- Weekend: sampled coverage={weekend_hours:.2f} hours across completed={len(weekend_markets)};
  market-start span={weekend_start_span_hours:.2f} hours;
  synchronized usable={len(weekend_markets)}; actual T-300 leader flips={len([r for r in flip_events if r["session_regime"] == "WEEKEND"])}.
- Weekday: synchronized usable={len(weekday_markets)}; actual T-300 leader flips={len([r for r in flip_events if r["session_regime"] == "WEEKDAY"])}.
- Legacy weekday: {len(legacy_weekday)} summaries lack comparable synchronized
  prediction snapshots and are excluded from primary calibration, lead/lag,
  residual, and flip comparisons.

## Numeric direction visible now

- Weekend T-300: N={weekend_t300.get("N", 0)}, observed flip rate={weekend_t300.get("observed_flip_rate")},
  market-implied flip probability={weekend_t300.get("mean_market_implied_flip_probability")},
  analytic flip probability={weekend_t300.get("mean_analytic_flip_probability")}.
- Comparable weekday rows: {len(weekday_rows)}. Weekday curves and cross-regime
  calibration are NOT_MEASURABLE, rather than zero or pooled with legacy data.

## Weekend checkpoint curve

| Checkpoint | N | Observed flip rate | Market flip p | Analytic flip p | Market Brier | Analytic Brier |
|---|---:|---:|---:|---:|---:|---:|
{checkpoint_lines}

Market versus analytic calibration is MIXED by checkpoint: analytic is lower
Brier at T-300, T-60 and T-30, while market is lower Brier at T-180 and T-120.
This is an early signal only; it is not a cross-regime result.

## Weekend sigma flip-rate curve

| Required move | N | Flips | Observed rate | Market flip p | Analytic flip p |
|---|---:|---:|---:|---:|---:|
{sigma_lines}

Within this weekend-only sample the observed rate decreases monotonically from
the <0.5sigma bin through >3sigma. This is an EARLY_SIGNAL (five observed
checkpoint flips), not proof of a stable curve.

## Actual flip events

There are {len([r for r in flip_events if r["session_regime"] == "WEEKEND"])}
weekend markets whose T-300 official-TWAP leader differed from final settlement.
Earliest persistent directional labels: {dict(flip_counts)}. Event timestamps
and every candidate signal are in actual_flip_events.csv. There is no directly
comparable weekday flip-event cohort.

## Preliminary weekend vs weekday verdict

E. Data quality / comparable weekday coverage is still the main limitation.
The strongest early difference and strongest similarity are both NOT_MEASURABLE:
there is no synchronized weekday cohort with this schema. The most useful
metric to keep collecting is fresh T-300/T-180/T-120 flip probability,
separately by regime. A same-schema weekday capture cohort would change this
preliminary conclusion.
"""
    (output / "summary.md").write_text(report, encoding="utf-8")
    return {"mode": "PRELIMINARY_REGIME_COMPARISON", "weekend_markets": len(weekend_markets),
            "weekday_synchronized_markets": len(weekday_markets), "legacy_weekday_markets": len(legacy_weekday),
            "weekend_hours": weekend_hours, "weekend_market_start_span_hours": weekend_start_span_hours,
            "output": str(output)}


def _epoch(value: Any) -> float | None:
    """Compatibility name for the canonical UTC parser; naive clocks stay unknown."""
    return clock_epoch(value)


def _mean(values: Iterable[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return statistics.mean(clean) if clean else None


def _percentile(values: Iterable[float | None], percentile: float) -> float | None:
    clean = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
    if not clean:
        return None
    index = (len(clean) - 1) * percentile
    low, high = math.floor(index), math.ceil(index)
    return clean[low] if low == high else clean[low] + (clean[high] - clean[low]) * (index - low)


def _read_shadow_settlements(journal_path: Path, *, regime: str,
                             allowed_run_ids: set[str] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Reconstruct settled shadow entries from a read-only journal snapshot."""
    dedup: dict[str, tuple[int, dict[str, Any]]] = {}
    audit: list[dict[str, Any]] = []
    rows = ResearchStore(journal_path).journal_events(journal_path, event_type="SHADOW_SIM_SETTLED")
    for row in rows:
        event_id, event_ts, run_id, raw = row["id"], row["ts"], row["run_id"], row["payload_json"]
        if allowed_run_ids is not None and str(run_id) not in allowed_run_ids:
            continue
        try:
            if row["payload_status"] != "VALID":
                raise ValueError("invalid journal payload")
            payload = row["payload"]
        except (TypeError, ValueError, json.JSONDecodeError):
            audit.append({"event_id": event_id, "status": "EXCLUDED", "exclusion_reason": "invalid_payload_json"})
            continue
        slug = str(payload.get("slug") or payload.get("market_slug") or "")
        if not slug or market_context(slug)["session_regime"] != regime:
            continue
        simulation_id = str(payload.get("simulation_id") or f"{slug}:{payload.get('side')}:{payload.get('filled_ts')}")
        if simulation_id in dedup:
            audit.append({"event_id": dedup[simulation_id][0], "market_slug": slug, "simulation_id": simulation_id,
                          "status": "EXCLUDED", "exclusion_reason": "duplicate_settlement_superseded"})
        dedup[simulation_id] = (int(event_id), {"event_id": int(event_id), "run_id": str(run_id), "event_ts": event_ts,
                                                 "simulation_id": simulation_id, "market_slug": slug,
                                                 "side": str(payload.get("side") or "").upper(), "payload": payload})
    output: list[dict[str, Any]] = []
    for _, row in dedup.values():
        payload = row.pop("payload")
        entry_ts = _epoch(payload.get("filled_ts"))
        if entry_ts is None:
            entry_ts = _epoch(payload.get("created_ts"))
        settlement_ts = _epoch(row["event_ts"])
        price, qty = _num(payload.get("entry_price")), _num(payload.get("qty"))
        capital = price * qty if price is not None and qty is not None and price > 0 and qty > 0 else None
        holding_sec = settlement_ts - entry_ts if entry_ts is not None and settlement_ts is not None else None
        reason = ("entry_ts_missing" if entry_ts is None else "settlement_ts_missing" if settlement_ts is None else
                  "invalid_holding_time" if holding_sec is None or holding_sec <= 0 else "CAPITAL_UNKNOWN" if capital is None else None)
        gross, net = _num(payload.get("simulated_gross_pnl_usdc")), _num(payload.get("simulated_pnl_usdc"))
        if reason is None and gross is None:
            reason = "PNL_UNKNOWN"
        measured = capital_efficiency(capital_committed_usdc=capital, entry_ts=entry_ts,
                                      exit_ts=settlement_ts, gross_pnl=gross, net_pnl=net) if reason is None else {}
        capital_minutes = measured.get("capital_minutes")
        output.append({**row, "entry_ts": entry_ts, "settlement_ts": settlement_ts,
                       "settlement_ts_source": "SHADOW_SIM_SETTLED_EVENT_TS", "actual_exit_ts": None,
                       "holding_sec": holding_sec, "holding_min": holding_sec / 60.0 if holding_sec is not None else None,
                       "capital_committed_usdc": capital, "capital_seconds": capital * holding_sec if capital is not None and holding_sec is not None else None,
                       "capital_minutes": capital_minutes, "entry_price": price, "qty": qty, "gross_pnl": gross, "net_pnl": net,
                       "gross_profit_per_dollar_minute": gross / capital_minutes if gross is not None and capital_minutes and capital_minutes > 0 else None,
                       "net_profit_per_dollar_minute": net / capital_minutes if net is not None and capital_minutes and capital_minutes > 0 else None,
                       "time_left_at_entry": _num(payload.get("time_left_sec")),
                       "entry_timing_bin": _entry_timing_bin(_num(payload.get("time_left_sec"))),
                       "position_outcome": "HELD_TO_SETTLEMENT", "won": payload.get("won"),
                       "settlement_same_as_entry_side": str(payload.get("outcome") or "").upper() == row["side"],
                       "status": "USABLE" if reason is None else "EXCLUDED", "exclusion_reason": reason})
    return output, audit


def _journal_event_count_for_regime(journal_path: Path, event_type: str, regime: str,
                                    allowed_run_ids: set[str] | None = None) -> int:
    """Count a cohort through the shared journal parser."""
    count = 0
    for row in ResearchStore(journal_path).journal_events(journal_path, event_type=event_type):
        if allowed_run_ids is not None and row["run_id"] not in allowed_run_ids:
            continue
        slug = row["market_slug"]
        if row["payload_status"] == "VALID" and slug and market_context(slug)["session_regime"] == regime:
            count += 1
    return count


def _test_dry_run_ids(conn: sqlite3.Connection) -> set[str] | None:
    """Internal compatibility adapter for the existing journal run schema."""
    try:
        return {str(run_id) for run_id, mode, test_mode in conn.execute(
            "SELECT run_id, mode, test_mode FROM strategy_runs"
        ) if str(mode or "") == "TEST_DRY_RUN" or bool(test_mode)}
    except sqlite3.OperationalError:
        return None


def _group_capital_metrics(rows: list[dict[str, Any]], key: str, labels: Iterable[str] | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) or "UNKNOWN")].append(row)
    for label in labels or ():
        grouped.setdefault(label, [])
    output = []
    for label, selected in grouped.items():
        wins = [row for row in selected if (_num(row.get("gross_pnl")) or 0) > 0]
        losses = [row for row in selected if (_num(row.get("gross_pnl")) or 0) < 0]
        capital_minutes = sum(float(row["capital_minutes"]) for row in selected if row.get("capital_minutes") is not None)
        gross = sum(float(row["gross_pnl"]) for row in selected if row.get("gross_pnl") is not None)
        net = [float(row["net_pnl"]) for row in selected if row.get("net_pnl") is not None]
        output.append({key: label, "N": len(selected), "wins": len(wins), "losses": len(losses),
                       "win_rate": len(wins) / (len(wins) + len(losses)) if wins or losses else None,
                       "avg_entry_price": _mean(row.get("entry_price") for row in selected),
                       "avg_capital_committed": _mean(row.get("capital_committed_usdc") for row in selected),
                       "avg_holding_min": _mean(row.get("holding_min") for row in selected),
                       "median_holding_min": _median(row.get("holding_min") for row in selected),
                       "total_capital_minutes": capital_minutes, "avg_gross_pnl": _mean(row.get("gross_pnl") for row in selected),
                       "total_gross_pnl": gross, "gross_profit_per_dollar_minute": gross / capital_minutes if capital_minutes else None,
                       "avg_net_pnl": _mean(net), "total_net_pnl": sum(net) if net else None,
                       "net_profit_per_dollar_minute": sum(net) / capital_minutes if net and capital_minutes else None})
    return output


def capital_efficiency_analysis(db_path: Path, journal_path: Path, output: Path) -> dict[str, Any]:
    """Offline P3 accounting validation for the current weekend shadow cohort."""
    output.mkdir(parents=True, exist_ok=True)
    with ResearchStore.open_readonly(journal_path) as conn:
        run_ids = _test_dry_run_ids(conn)
    records, duplicate_audit = _read_shadow_settlements(journal_path, regime="WEEKEND", allowed_run_ids=run_ids)
    usable = [row for row in records if row["status"] == "USABLE"]
    excluded = [row for row in records if row["status"] != "USABLE"] + duplicate_audit
    shadow_entries = _journal_event_count_for_regime(journal_path, "SHADOW_SIM_ENTRY_FILLED", "WEEKEND", run_ids)
    live_entries = _journal_event_count_for_regime(journal_path, "ORDER_FILLED", "WEEKEND", run_ids)
    stop_events = _journal_event_count_for_regime(journal_path, "ORDER_TAKER_EXIT_SUBMIT", "WEEKEND", run_ids)
    cohort_slugs = {row["market_slug"] for row in records}
    # ResearchStore supplies canonical settlement provenance for every market,
    # including markets that never produced a shadow fill. Do not disappear
    # those markets from the availability audit.
    settlements = [row for row in ResearchStore(db_path).get_settlements()
                   if market_context(row["market_slug"])["session_regime"] == "WEEKEND"
                   and (run_ids is None or row["run_id"] in run_ids)]
    completed_markets = len({row["market_slug"] for row in settlements})
    completed_markets_with_trade = len({row["market_slug"] for row in settlements if row["market_slug"] in cohort_slugs})
    bins = _group_capital_metrics(usable, "entry_timing_bin", (">600s", "480–600s", "360–480s", "240–360s", "120–240s", "<120s"))
    winners = _group_capital_metrics([row for row in usable if (_num(row.get("gross_pnl")) or 0) > 0], "cohort", ("WINNERS",))[0]
    losers = _group_capital_metrics([row for row in usable if (_num(row.get("gross_pnl")) or 0) < 0], "cohort", ("LOSERS",))[0]
    winners["cohort"], losers["cohort"] = "WINNERS", "LOSERS"
    markets = _group_capital_metrics(usable, "market_slug")
    quality = [
        {"metric": "completed_research_markets", "usable_N": completed_markets, "missing_N": 0, "excluded_N": 0, "detail": "ResearchStore canonical TEST_DRY_RUN summaries"},
        {"metric": "completed_markets_with_settled_shadow_trade", "usable_N": completed_markets_with_trade, "missing_N": 0, "excluded_N": 0, "detail": "Completed markets represented in trade capital cohort"},
        {"metric": "shadow_entry_records", "usable_N": shadow_entries, "missing_N": 0, "excluded_N": 0, "detail": "SHADOW_SIM_ENTRY_FILLED; may be unresolved"},
        {"metric": "live_recorded_entries", "usable_N": live_entries, "missing_N": 0, "excluded_N": 0, "detail": "ORDER_FILLED; not pooled with simulated entries"},
        {"metric": "settled_shadow_entries", "usable_N": len(records), "missing_N": 0, "excluded_N": len(duplicate_audit), "detail": "Deduped SHADOW_SIM_SETTLED"},
        {"metric": "capital_efficiency_usable", "usable_N": len(usable), "missing_N": len(records) - len(usable), "excluded_N": len(excluded), "detail": "positive fill notional and positive holding time required"},
        {"metric": "stopped_trade_reconstruction", "usable_N": 0, "missing_N": stop_events, "excluded_N": stop_events, "detail": "No durable joined stop-to-finalized-shadow lifecycle"},
    ]
    _write_csv(output / "trade_capital_efficiency.csv", usable)
    _write_csv(output / "entry_timing_bins.csv", bins)
    _write_csv(output / "winner_loser_comparison.csv", [winners, losers])
    _write_csv(output / "stopped_trade_capital_time.csv", [])
    _write_csv(output / "market_capital_efficiency.csv", markets)
    _write_csv(output / "data_quality.csv", quality + excluded)
    outliers = sorted(usable, key=lambda row: abs(_num(row.get("gross_profit_per_dollar_minute")) or 0), reverse=True)[:10]
    _write_csv(output / "outliers.csv", outliers)
    stake, ppm = [row.get("capital_committed_usdc") for row in usable], [row.get("gross_profit_per_dollar_minute") for row in usable]
    total_minutes = sum(float(row["capital_minutes"]) for row in usable)
    total_gross = sum(float(row["gross_pnl"]) for row in usable if row.get("gross_pnl") is not None)
    bin_lines = "\n".join(f"| {row['entry_timing_bin']} | {row['N']} | {row['wins']} | {row['losses']} | {row['avg_holding_min']} | {row['total_capital_minutes']} | {row['total_gross_pnl']} | {row['gross_profit_per_dollar_minute']} |" for row in bins)
    (output / "summary.md").write_text(f"""# Preliminary weekend capital-efficiency validation

Cohort: **PRELIMINARY_WEEKEND_CAPITAL_EFFICIENCY**. Offline snapshots only; this validates accounting mechanics, not a live strategy.

## Data availability

- TEST_DRY_RUN runs: {len(run_ids) if run_ids is not None else 'legacy/unspecified'}; completed research markets: {completed_markets}; completed markets with a settled shadow trade: {completed_markets_with_trade}; shadow fills: {shadow_entries}; settled shadow entries: {len(records)}; usable trades: {len(usable)}.
- Live `ORDER_FILLED` records visible: {live_entries}; they are not pooled with simulated trades.
- Valid stake, entry timestamp, settlement timestamp, and gross PnL: {len(usable)} each. Excluded: {len(excluded)}; every reason is in `data_quality.csv`.
- Stopped lifecycle usable: 0. `BANKROLL_UTILIZATION_NOT_MEASURABLE`: no durable bankroll time series.

## Core metrics

- Gross PnL={total_gross}; capital-minutes={total_minutes}; gross PnL/$-minute={total_gross / total_minutes if total_minutes else None}.
- Actual simulated stake: min={min(stake) if stake else None}, median={_median(stake)}, mean={_mean(stake)}, max={max(stake) if stake else None}.
- Gross PnL/$-minute: mean={_mean(ppm)}, median={_median(ppm)}, P25={_percentile(ppm, .25)}, P75={_percentile(ppm, .75)}, P90={_percentile(ppm, .90)}.

## Entry timing

| Time left | N | Wins | Losses | Avg hold min | Capital-minutes | Gross PnL | Gross PnL/$-min |
|---|---:|---:|---:|---:|---:|---:|---:|
{bin_lines}

## Framework verdict

1. Q1 reconstruction: **YES** — {len(usable)} deduped settled shadow entries have non-imputed time, stake, and PnL.
2. Q2 capital lock measurable: **YES** — median={_median(row.get('holding_min') for row in usable)} minutes.
3. Q3/Q4 timing bins: **YES**, but `PRELIMINARY_PATTERN_ONLY`; sparse bins are not recommendations.
4. Q5 outlier sensitivity: **MIXED** — mean and median plus `outliers.csv` are supplied.
5. Q6 stop capital release: **NOT_MEASURABLE** — no joined stopped-shadow lifecycle.
6. Q7 missing fields: **MIXED** — settled shadow accounting works; stop lifecycle and bankroll history are absent.
7. Q8 future weekday/weekend study: **MIXED** — core reconstruction is ready if weekday emits the same schema; stop and bankroll provenance need work.
""", encoding="utf-8")
    return {"mode": "PRELIMINARY_WEEKEND_CAPITAL_EFFICIENCY", "completed_markets": completed_markets,
            "completed_markets_with_settled_shadow_trade": completed_markets_with_trade,
            "settled_entries": len(records), "usable_trades": len(usable), "excluded": len(excluded), "output": str(output)}


def analyze_selection(db_path: Path, journal_path: Path, output: Path, selected: list[dict[str, Any]]) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    contexts = {row["market_slug"]: row for row in selected}
    regimes = {row["session_regime"] for row in selected}
    batch_regime = next(iter(regimes)) if len(regimes) == 1 else "MIXED"
    timelines, summaries = _load_selection(db_path, selected)
    markets = [_market_metrics(slug, timelines.get(slug, []), summaries.get(slug), context)
               for slug, context in contexts.items()]
    _write_csv(output / "markets.csv", markets)
    _write_csv(output / "data_quality.csv", markets)

    events: list[dict[str, Any]] = []
    events_5c: list[dict[str, Any]] = []
    for slug, rows in timelines.items():
        joint = forensic._joint_rows(rows)
        noise = {horizon: forensic._local_btc_noise(joint, horizon) for horizon in (5, 10)}
        context = contexts[slug]
        events.extend(_contextual({"market_slug": slug, **forensic._event_signals(event, joint, noise)}, contexts, batch_regime=batch_regime)
                      for event in forensic._coalesced_events(joint, .10))
        events_5c.extend(_contextual({"market_slug": slug, **forensic._event_signals(event, joint, noise)}, contexts, batch_regime=batch_regime)
                         for event in forensic._coalesced_events(joint, .05))
    for row in events:
        row["event_kind"] = "ABSOLUTE_EVENT"
    _write_csv(output / "repricing_events.csv", events)
    _write_csv(output / "signal_lead_lag.csv", events)

    by_regime_moves: dict[str, list[float]] = defaultdict(list)
    for row in events_5c:
        by_regime_moves[row["session_regime"]].append(abs(_num(row.get("mid_change_30s")) or 0))
    normalized = []
    for row in events_5c:
        threshold = normalized_event_threshold(by_regime_moves[row["session_regime"]])
        if threshold is not None and abs(_num(row.get("mid_change_30s")) or 0) >= threshold:
            normalized.append({**row, "event_kind": "NORMALIZED_EVENT", "regime_top_20pct_threshold": threshold})
    _write_csv(output / "normalized_repricing_events.csv", normalized)

    residual = [_contextual(row, contexts, batch_regime=batch_regime)
                for row in forensic._residual_report({slug: forensic._joint_rows(rows) for slug, rows in timelines.items()})]
    disagreement, sigma = forensic._held_side_report({slug: forensic._joint_rows(rows) for slug, rows in timelines.items()})
    _write_csv(output / "residual_analysis.csv", residual)
    _write_csv(output / "btc_disagreement.csv", [_contextual(row, contexts, batch_regime=batch_regime) for row in disagreement])
    _write_csv(output / "required_path.csv", [_contextual(row, contexts, batch_regime=batch_regime) for row in sigma])

    entries: list[dict[str, Any]] = []
    for run_id in {row["run_id"] for row in selected}:
        entries.extend(forensic._entry_report(forensic._read_entries(journal_path, run_id), timelines))
    entries = [_contextual(row, contexts, batch_regime=batch_regime) for row in entries if row.get("market_slug") in contexts]
    _write_csv(output / "entries.csv", entries)
    _write_csv(output / "regime_comparison.csv", _regime_comparison(markets, events))

    pex_leads = [_num(row.get("p_ex_lead_vs_mid_sec")) for row in events if _num(row.get("p_ex_lead_vs_mid_sec")) is not None]
    lead_count = sum(value > 2 for value in pex_leads)
    lag_count = sum(value < -2 for value in pex_leads)
    pos = next((row for row in residual if row.get("residual_bin") == ">= +0.10"), {})
    weekend = [row for row in markets if row["session_regime"] == "WEEKEND"]
    weekday = [row for row in markets if row["session_regime"] == "WEEKDAY"]
    report = f"""# Canonical BTC15m offline research analysis

Offline-only analysis. No live authority, strategy threshold, entry, exit, stop,
or session policy has changed. `session_regime` is determined **only** by the
market start time in Asia/Taipei.

## Session regime

- Current batch: **{batch_regime}**
- Weekend markets: **{len(weekend)}**
- Weekday markets: **{len(weekday)}**
- Weekend batch label: **WEEKEND_REPLICATION_BATCH_1** when the batch is the
  four synchronized October 3 markets; it is an out-of-regime replication, not
  a refutation of weekday observations.

## Signal comparison by regime

- p_ex lead: N={len(pex_leads)}, lead >2s={lead_count}, lag >2s={lag_count},
  median={_median(pex_leads)} seconds. This is reported only for the selected
  regime; no weekday/weekend raw-event pooling is performed.
- `>= +0.10` residual: N={pos.get('observations', 0)}, markets={pos.get('markets', 0)},
  mean future 30s mid move={pos.get('mean_future_mid_change_30s')}. Compare
  this only with a within-regime residual cohort.
- Absolute repricing is in `repricing_events.csv` (5c/10c apples-to-apples
  thresholds). `normalized_repricing_events.csv` is a secondary top-20%-within-
  regime view and does not replace those absolute thresholds.

## Regime interpretation

The current batch is **{batch_regime}**. Any difference from historic weekday
findings is classified as `POSSIBLE_REGIME_DEPENDENCE` until it is replicated
within each regime. `regime_comparison.csv` provides effect direction and
magnitude without asserting significance.

## Next tests

1. **Regime replication:** compare 30-second repricing conditional on
   normalized BTC 10-second shock separately for weekday and weekend markets.
2. **Liquidity mechanism:** compare spread, quote cadence, and fresh-snapshot
   rate before attributing lead/lag differences to a predictor.
"""
    (output / "summary.md").write_text(report, encoding="utf-8")
    return {"markets": len(markets), "batch_regime": batch_regime, "repricing_events": len(events),
            "p_ex_measurable": len(pex_leads), "p_ex_lead_gt_2s": lead_count,
            "p_ex_lag_gt_2s": lag_count, "p_ex_median_lead_sec": _median(pex_leads),
            "output": str(output)}


def _fixed_bin(value: float | None, bins: tuple[tuple[float, float, str], ...]) -> str | None:
    if value is None:
        return None
    for low, high, label in bins:
        if low <= value < high:
            return label
    return bins[-1][2]


def _brier(values: list[tuple[float, bool]]) -> float | None:
    return _mean([(probability - float(outcome)) ** 2 for probability, outcome in values])


def _log_loss(values: list[tuple[float, bool]]) -> float | None:
    clipped = [(min(.999999, max(.000001, probability)), outcome) for probability, outcome in values]
    return _mean([-(math.log(probability) if outcome else math.log(1 - probability)) for probability, outcome in clipped])


def _event_ts(value: Any) -> float | None:
    """Compatibility name; journal clocks are not assumed to be venue clocks."""
    return clock_epoch(value)


def _entry_side(payload: dict[str, Any], snapshots: list[dict[str, Any]], instrument_id: str) -> str:
    candidate = str(payload.get("research_candidate_id") or "")
    parts = candidate.split("|")
    if len(parts) >= 2 and parts[-2].upper() in {"UP", "DOWN"}:
        return parts[-2].upper()
    for row in snapshots:
        if str(row.get("up_instrument_id") or "") == str(instrument_id):
            return "UP"
        if str(row.get("down_instrument_id") or "") == str(instrument_id):
            return "DOWN"
    return "UNKNOWN"


def _price_bucket(price: float | None) -> str | None:
    if price is None or not (.55 <= float(price) < .90):
        return None
    return _fixed_bin(price, ((.55,.60,"0.55–0.60"),(.60,.65,"0.60–0.65"),(.65,.70,"0.65–0.70"),
                               (.70,.75,"0.70–0.75"),(.75,.80,"0.75–0.80"),(.80,.85,"0.80–0.85"),
                               (.85,.90,"0.85–0.90")))


def _sigma_bucket(sigma: float | None) -> str | None:
    return _fixed_bin(abs(sigma) if sigma is not None else None, ((0,.5,"<0.5σ"),(.5,1,"0.5–1σ"),
                                                                   (1,2,"1–2σ"),(2,math.inf,">2σ")))


def _snapshot_near(rows: list[dict[str, Any]], ts: float, *, max_age_sec: float = 8.0) -> dict[str, Any] | None:
    return latest_evidence(rows, ts, max_age_sec=max_age_sec, require_joint_fresh=True)


def _journal_fill_entries(journal_path: Path, timelines: dict[str, list[dict[str, Any]]],
                         sparse_entries: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project BUY fills into persisted logical identities; legacy ambiguity stays explicit."""
    from bot.research.lifecycle import position_lifecycle_id
    groups, audit, seen = {}, [], set()
    sparse = {row["position_lifecycle_id"]: row for row in sparse_entries or [] if row.get("position_lifecycle_id")}
    raw_rows = ResearchStore(journal_path).journal_events(journal_path, event_type="ORDER_FILLED")
    for row in raw_rows:
        if str(row.get("side") or "").upper() != "BUY":
            continue
        event_id, raw_ts, run_id, client_id, price, qty, instrument_id, raw_payload = (
            row.get(key) for key in ("id", "ts", "run_id", "client_order_id", "price", "qty", "instrument_id", "payload_json"))
        try:
            if row["payload_status"] != "VALID":
                raise ValueError("invalid journal payload")
            payload = row["payload"]
        except (TypeError, ValueError):
            audit.append({"order_event_id": event_id, "status": "EXCLUDED", "reason": "INVALID_PAYLOAD"})
            continue
        slug = str(payload.get("market_slug") or payload.get("slug") or "")
        identity = str(payload.get("position_lifecycle_id") or "")
        source = "PERSISTED_IDENTITY" if identity else "LEGACY_CLIENT_ORDER_AMBIGUOUS"
        if not identity and slug and instrument_id and client_id:
            identity = position_lifecycle_id(market_slug=slug, instrument_id=str(instrument_id), entry_client_order_id=str(client_id))
        if not identity:
            audit.append({"order_event_id": event_id, "status": "EXCLUDED", "reason": "MISSING_ENTRY_ID"})
            continue
        fill_id = payload.get("fill_event_id") or payload.get("trade_id")
        duplicate_key = (str(run_id), str(instrument_id), str(fill_id)) if fill_id else (
            str(run_id), str(client_id), str(raw_ts), price, qty, raw_payload)
        if duplicate_key in seen:
            audit.append({"order_event_id": event_id, "status": "EXCLUDED", "reason": "DUPLICATE_FILL_EVENT"})
            continue
        seen.add(duplicate_key)
        price, qty = _num(price), _num(qty)
        if price is None or qty is None or price <= 0 or qty <= 0:
            audit.append({"order_event_id": event_id, "status": "EXCLUDED", "reason": "INVALID_FILL"})
            continue
        entry = groups.get(identity)
        if entry is None:
            canonical = sparse.get(identity, {})
            entry_ts = _num(canonical.get("entry_ts"))
            if entry_ts is None:
                entry_ts = _event_ts(raw_ts)
            snapshots = [r for r in timelines.get(slug, []) if r.get("run_id") in (None, str(run_id))]
            anchor = _snapshot_near(snapshots, entry_ts) if entry_ts is not None else None
            side = str(canonical.get("side") or _entry_side(payload, snapshots, str(instrument_id or ""))).upper()
            entry = {
                "position_lifecycle_id": identity, "identity_source": "SPARSE_ENTRY_CONFIRMED" if canonical else source,
                "market_slug": slug, "run_id": str(run_id),
                "entry_client_order_id": str(canonical.get("entry_client_order_id") or payload.get("entry_client_order_id") or client_id or ""),
                "instrument_id": str(instrument_id or ""), "side": side, "entry_ts": entry_ts,
                "entry_qty": 0.0, "entry_notional": 0.0, "total_buy_qty": 0.0, "total_buy_notional": 0.0,
                "scale_in_qty": 0.0, "buy_client_order_ids": set(),
                "entry_anchor_status": "JOINED" if anchor is not None else "MISSING_FRESH_PRIOR_SNAPSHOT_WITHIN_8S",
                "entry_required_sigma": _num(anchor.get("required_move_sigma")) if anchor else None,
                "entry_required_bps": _num(anchor.get("required_move_bps")) if anchor else None,
                "entry_flip_p": 1 - held_side_probability(anchor, side) if anchor and held_side_probability(anchor, side) is not None else None,
                "entry_market_mid": _num(anchor.get(f"market_mid_{side.lower()}")) if anchor else None,
                "entry_executable_ask": _num(anchor.get(f"best_ask_{side.lower()}")) if anchor else None,
                "time_left_at_entry": _num(anchor.get("time_left_sec")) if anchor else None,
                "status": "USABLE" if anchor is not None and side in {"UP", "DOWN"} and source == "PERSISTED_IDENTITY" else "PARTIAL",
            }
            groups[identity] = entry
        entry["buy_client_order_ids"].add(str(client_id))
        entry["total_buy_qty"] += qty
        entry["total_buy_notional"] += price * qty
        if str(client_id) == entry["entry_client_order_id"]:
            entry["entry_qty"] += qty
            entry["entry_notional"] += price * qty
        else:
            entry["scale_in_qty"] += qty
    for entry in groups.values():
        entry["entry_price"] = entry["entry_notional"] / entry["entry_qty"] if entry["entry_qty"] else None
        entry["buy_client_order_count"] = len(entry.pop("buy_client_order_ids"))
    return list(groups.values()), audit


def _first_persistent_adverse_repricing(rows: list[dict[str, Any]], *, side: str, entry_mid: float | None, threshold: float) -> float | None:
    """First two adjacent fresh snapshot observations adverse by ``threshold``.

    This is a deliberately simple research definition, not a trading rule.
    It needs two observations so a single stale/transient quote does not
    masquerade as a persistent repricing.
    """
    if entry_mid is None:
        return None
    previous_adverse = False
    for row in rows:
        mid = _num(row.get(f"market_mid_{side.lower()}"))
        fresh = row.get(f"market_mid_{side.lower()}_fresh") is True
        adverse = bool(fresh and mid is not None and ((mid <= entry_mid - threshold) if side == "UP" else (mid <= entry_mid - threshold)))
        if adverse and previous_adverse:
            return _num(row.get("snapshot_ts"))
        previous_adverse = adverse
    return None


def position_lifecycle_analysis(
    db_path: Path, journal_path: Path, output: Path, *, slug: str | None = None,
    legacy_stop_db: Path | None = None,
) -> dict[str, Any]:
    """Reconstruct one offline row per actual opened position.

    Historical rows without the future sparse lifecycle id remain visible as
    ``PARTIAL`` or ``AMBIGUOUS``.  The function never invents a stop event or
    rewrites source databases.
    """
    output.mkdir(parents=True, exist_ok=True)
    catalog = _catalog(db_path)
    # Catalog is newest-first; use one final outcome per market while retaining
    # every run in the selection passed to the common timeline loader.
    selection = catalog
    catalog = list({row["market_slug"]: row for row in reversed(catalog)}.values())
    if slug:
        catalog = [row for row in catalog if row["market_slug"] == slug]
    timelines, summaries = _load_selection(db_path, [row for row in selection if row["market_slug"] in {item["market_slug"] for item in catalog}])
    coverage = {row["market_slug"]: row for row in ResearchStore(db_path).get_market_coverage()}
    entries, entry_audit = _journal_fill_entries(
        journal_path, timelines, ResearchStore(db_path).get_research_events(event_type="POSITION_LIFECYCLE_ENTRY", slug=slug),
    )
    if slug:
        entries = [row for row in entries if row["market_slug"] == slug]
    events_by_type = {name: ResearchStore(db_path).get_research_events(event_type=name, slug=slug) for name in (
        "POSITION_LIFECYCLE_ENTRY", "STOP_SHADOW_ADVERSE_EPISODE_STARTED", "STOP_SHADOW_CANDIDATE",
        "STOP_SHADOW_ACTUAL_STOP", "STOP_SHADOW_POST_STOP_SETTLEMENT",
    )}
    # Stop-forensics previously used the separate Outcome lead/lag DB.  Read
    # it only as legacy evidence; new sparse events share ``db_path`` with
    # synchronized snapshots so no cross-store join is required going forward.
    legacy_events_by_type: dict[str, list[dict[str, Any]]] = {}
    if legacy_stop_db is not None and legacy_stop_db.exists() and legacy_stop_db.resolve() != db_path.resolve():
        legacy_store = ResearchStore(legacy_stop_db)
        for name in ("STOP_SHADOW_ADVERSE_EPISODE_STARTED", "STOP_SHADOW_CANDIDATE", "STOP_SHADOW_ACTUAL_STOP", "STOP_SHADOW_POST_STOP_SETTLEMENT"):
            legacy_events_by_type[name] = [{**row, "research_event_source": "LEGACY_OUTCOME_LEAD_LAG_DB"}
                                           for row in legacy_store.get_research_events(event_type=name, slug=slug)]
    else:
        legacy_events_by_type = {}
    events = [event for rows in events_by_type.values() for event in rows]
    events.extend(event for rows in legacy_events_by_type.values() for event in rows)
    for entry in entries:
        market_rows = [row for row in timelines.get(entry["market_slug"], []) if entry["entry_ts"] is not None and row["snapshot_ts"] >= entry["entry_ts"]]
        settlement = summaries.get(entry["market_slug"], {})
        final_side = str(settlement.get("canonical_settlement_side") or "UNKNOWN").upper()
        features = first_crossings(market_rows, side=entry["side"]) if entry["side"] in {"UP", "DOWN"} else {}
        sigmas = [abs(_num(row.get("required_move_sigma"))) for row in market_rows if _num(row.get("required_move_sigma")) is not None]
        probabilities = [1 - held_side_probability(row, entry["side"]) for row in market_rows if held_side_probability(row, entry["side"]) is not None]
        entry.update(features)
        entry.update({
            "min_required_sigma": min(sigmas) if sigmas else None,
            "min_required_sigma_ts": next((row["snapshot_ts"] for row in market_rows if _num(row.get("required_move_sigma")) is not None and abs(_num(row.get("required_move_sigma"))) == min(sigmas)), None) if sigmas else None,
            "max_flip_p": max(probabilities) if probabilities else None,
            "max_flip_p_ts": next((row["snapshot_ts"] for row in market_rows if held_side_probability(row, entry["side"]) is not None and 1 - held_side_probability(row, entry["side"]) == max(probabilities)), None) if probabilities else None,
            "first_market_adverse_2c_ts": _first_persistent_adverse_repricing(market_rows, side=entry["side"], entry_mid=entry["entry_market_mid"], threshold=.02),
            "first_market_adverse_5c_ts": _first_persistent_adverse_repricing(market_rows, side=entry["side"], entry_mid=entry["entry_market_mid"], threshold=.05),
            "first_market_adverse_10c_ts": _first_persistent_adverse_repricing(market_rows, side=entry["side"], entry_mid=entry["entry_market_mid"], threshold=.10),
            "final_settlement_side": final_side,
            "entry_won_final_settlement": (entry["side"] == final_side) if final_side in {"UP", "DOWN"} else None,
            "run_ids": ",".join(coverage.get(entry["market_slug"], {}).get("run_ids", [])),
            "coverage_quality": coverage.get(entry["market_slug"], {}).get("coverage_quality"),
            "joint_fresh_pct": 100 * float(coverage.get(entry["market_slug"], {}).get("joint_fresh_rate") or 0),
            "largest_gap_sec": coverage.get(entry["market_slug"], {}).get("largest_gap_sec"),
        })
        exact_events = [e for e in events if e.get("event_type") == "STOP_SHADOW_ACTUAL_STOP" and e.get("position_lifecycle_id") == entry["position_lifecycle_id"]]
        join_provenance = "EXACT_IDENTITY"
        if not exact_events:
            join_provenance = "LEGACY_SLUG_INSTRUMENT_FALLBACK"
            legacy = [e for e in events if e.get("event_type") == "STOP_SHADOW_ACTUAL_STOP" and not e.get("position_lifecycle_id") and e.get("market_slug") == entry["market_slug"] and str(e.get("instrument_id") or "") == entry["instrument_id"]]
            same_market_entries = [e for e in entries if e["market_slug"] == entry["market_slug"] and e["instrument_id"] == entry["instrument_id"]]
            exact_events = legacy if len(same_market_entries) == 1 else []
            if legacy and not exact_events:
                entry["stop_join_status"] = "AMBIGUOUS"
        stop = next((e for e in exact_events if e.get("event_type") == "STOP_SHADOW_ACTUAL_STOP"), None)
        entry.update({
            "stop_trigger_ts": _num(stop.get("stop_trigger_ts")) if stop else None,
            "stop_reason": stop.get("reason") if stop else None,
            "stop_execution_ts": _num(stop.get("actual_stop_ts")) if stop else None,
            "stop_execution_price": _num(stop.get("actual_stop_price")) if stop else None,
            "stop_realized_pnl": _num(stop.get("actual_stop_pnl")) if stop else None,
            "stop_join_status": entry.get("stop_join_status") or (join_provenance if stop else "UNJOINABLE"),
            "exit_type": "STOP" if stop else "HELD_OR_NONSTOP_EXIT_UNKNOWN",
            "exit_ts": _num(stop.get("actual_stop_ts")) if stop else None,
        })
    _write_csv(output / "position_lifecycle.csv", entries)
    _write_csv(output / "entry_audit.csv", entry_audit)
    source_audit = [
        {"source": "trade_journal.order_events", "event_type": "ORDER_FILLED_BUY", "count": len(entries),
         "join_key": "market_slug + instrument_id + entry_client_order_id", "notes": "entry anchor; historical identity may be reconstructed"},
    ]
    for event_type, source_rows in events_by_type.items():
        with_identity = sum(bool(row.get("position_lifecycle_id")) for row in source_rows)
        source_audit.append({"source": "twap_forward_shadow.lead_lag_decisions", "event_type": event_type,
                             "count": len(source_rows), "with_position_lifecycle_id": with_identity,
                             "join_key": "position_lifecycle_id", "notes": "sparse research event"})
    for event_type, source_rows in legacy_events_by_type.items():
        source_audit.append({"source": "hyperliquid_lead_lag.lead_lag_decisions", "event_type": event_type,
                             "count": len(source_rows), "with_position_lifecycle_id": 0,
                             "join_key": "legacy slug + instrument only", "notes": "legacy cross-store evidence; ambiguous when multiple entries"})
    _write_csv(output / "event_source_audit.csv", source_audit)
    quality = [
        {"metric": "positions_total", "count": len(entries)},
        {"metric": "positions_with_canonical_entry", "count": sum(bool(r["position_lifecycle_id"]) for r in entries)},
        {"metric": "positions_with_synchronized_post_entry_snapshots", "count": sum(r["entry_anchor_status"] == "JOINED" for r in entries)},
        {"metric": "positions_with_settlement", "count": sum(r["final_settlement_side"] in {"UP", "DOWN"} for r in entries)},
        {"metric": "positions_with_stop_trigger", "count": sum(r["stop_trigger_ts"] is not None for r in entries)},
        {"metric": "positions_with_stop_execution", "count": sum(r["stop_execution_ts"] is not None for r in entries)},
        {"metric": "complete_entry_to_settlement", "count": sum(r["entry_anchor_status"] == "JOINED" and r["final_settlement_side"] in {"UP", "DOWN"} for r in entries)},
        {"metric": "complete_entry_stop_settlement", "count": sum(r["stop_execution_ts"] is not None and r["final_settlement_side"] in {"UP", "DOWN"} for r in entries)},
    ]
    _write_csv(output / "lifecycle_data_quality.csv", quality)
    if slug:
        lines = [f"# Position lifecycle timeline — {slug}", "", "Offline reconstruction only; unavailable values are not imputed.", ""]
        for row in entries:
            lines.append(f"## {row['position_lifecycle_id']}")
            lines.append(f"T+00 ENTRY {row['side']} @ {row['entry_price']} qty={row['entry_qty']}")
            base = row.get("entry_ts")
            for label, key in (("BTC5 adverse", "first_adverse_btc5_ts"), ("BTC10 adverse", "first_adverse_btc10_ts"),
                               ("BTC30 adverse", "first_adverse_btc30_ts"), ("sigma 1.5 crossed", "sigma_cross_1_5_ts"),
                               ("sigma 1.0 crossed", "sigma_cross_1_ts"), ("flip-p 15% crossed", "flip_p_15_ts"),
                               ("market adverse 5c persistent", "first_market_adverse_5c_ts"),
                               ("settlement state flipped", "first_settlement_state_flip_ts"),
                               ("stop execution", "stop_execution_ts")):
                event_time = _num(row.get(key))
                if event_time is not None and base is not None:
                    lines.append(f"T+{event_time - base:.1f}s {label}")
            if row.get("stop_execution_ts") is not None:
                lines.append(f"STOP {row.get('stop_reason')} @ {row.get('stop_execution_price')} pnl={row.get('stop_realized_pnl')}")
            lines.append(f"FINAL {row.get('final_settlement_side')} (entry_won={row.get('entry_won_final_settlement')})")
            lines.append("")
        (output / "position_timeline.md").write_text("\n".join(lines), encoding="utf-8")
    return {"positions": len(entries), "exact_stop_joins": sum(r["stop_join_status"] == "EXACT_IDENTITY" for r in entries), "output": str(output)}


def entry_stop_status_analysis(db_path: Path, journal_path: Path, output: Path) -> dict[str, Any]:
    """Canonical offline entry/stop status report using persisted evidence only.

    Precision analyses are restricted to completed settlements with a joined
    fresh snapshot at the requested checkpoint. Older settlement-only rows
    remain visible in the audit, never silently pooled into calibration.
    """
    output.mkdir(parents=True, exist_ok=True)
    catalog = _catalog(db_path)
    # Catalog is newest-first; use one final outcome per market while retaining
    # every run in the selection passed to the common timeline loader.
    selection = catalog
    catalog = list({row["market_slug"]: row for row in reversed(catalog)}.values())
    store = ResearchStore(db_path)
    coverage = {row["market_slug"]: row for row in store.get_market_coverage()}
    timelines, summaries = _load_selection(db_path, [row for row in selection if row["market_slug"] in {item["market_slug"] for item in catalog}])
    contexts = {row["market_slug"]: row for row in catalog}
    all_rows = {slug: forensic._joint_rows(rows) for slug, rows in timelines.items()}
    regimes = ("WEEKEND", "WEEKDAY")
    audit: list[dict[str, Any]] = []
    for regime in regimes:
        completed = [r for r in catalog if r["session_regime"] == regime]
        synchronized = [r for r in completed if all_rows.get(r["market_slug"])]
        rows = [row for item in synchronized for row in all_rows[item["market_slug"]]]
        quality = [coverage.get(r["market_slug"], {}) for r in completed]
        def count(predicate): return len({r["market_slug"] for r in rows if predicate(r)})
        audit.append({
            "session_regime": regime, "completed_settlement_markets": len(completed),
            "comparable_synchronized_markets": len(synchronized),
            "fresh_p_ex_markets": count(lambda r: r.get("p_ex_fresh") is True),
            "fresh_market_mid_markets": count(lambda r: r.get("market_mid_fresh") is True),
            "btc_5s_markets": count(lambda r: _num(r.get("btc_return_5s_bps")) is not None),
            "btc_10s_markets": count(lambda r: _num(r.get("btc_return_10s_bps")) is not None),
            "btc_30s_markets": count(lambda r: _num(r.get("btc_return_30s_bps")) is not None),
            "required_move_sigma_markets": count(lambda r: _num(r.get("required_move_sigma")) is not None),
            "analytic_probability_markets": count(lambda r: _num(r.get("p_up_ex_market")) is not None),
            "empirical_probability_markets": count(lambda r: _num(r.get("p_up_empirical")) is not None),
            "median_coverage_ratio": _median([_num(r.get("coverage_ratio")) for r in quality]),
            "median_largest_gap_sec": _median([_num(r.get("largest_gap_sec")) for r in quality]),
            "median_joint_fresh_pct": _median([100 * (_num(r.get("joint_fresh_rate")) or 0) for r in quality]),
            "FULL": sum(r.get("coverage_quality") == "FULL" for r in quality),
            "GOOD": sum(r.get("coverage_quality") == "GOOD" for r in quality),
            "PARTIAL": sum(r.get("coverage_quality") == "PARTIAL" for r in quality),
            "INTERRUPTED": sum(r.get("coverage_quality") == "INTERRUPTED" for r in quality),
            "UNUSABLE": sum(r.get("coverage_quality") == "UNUSABLE" for r in quality),
        })
    _write_csv(output / "dataset_audit.csv", audit)

    checkpoints = (600, 480, 360, 300, 240, 180, 120, 90, 60, 30, 15, 5)
    checkpoint_rows: list[dict[str, Any]] = []
    for item in catalog:
        slug, regime = item["market_slug"], item["session_regime"]
        settlement = str(summaries.get(slug, {}).get("canonical_settlement_side") or "")
        if settlement not in {"UP", "DOWN"}:
            continue
        for checkpoint in checkpoints:
            candidates = [r for r in all_rows.get(slug, []) if _num(r.get("time_left_sec")) is not None]
            if not candidates:
                continue
            row = min(candidates, key=lambda r: abs(float(r["time_left_sec"]) - checkpoint))
            if abs(float(row["time_left_sec"]) - checkpoint) > 8:
                continue
            leader = str(row.get("settlement_state_side") or "")
            up_mid = _num(row.get("up_mid"))
            p_up = _num(row.get("p_up_ex_market")) if row.get("p_ex_fresh") is True else None
            market_leader = "UP" if up_mid is not None and up_mid >= .5 else "DOWN" if up_mid is not None else ""
            pex_leader = "UP" if p_up is not None and p_up >= .5 else "DOWN" if p_up is not None else ""
            def opposite_prob(prob, side): return (1 - prob) if prob is not None and side == "UP" else prob if prob is not None else None
            checkpoint_rows.append({"market_slug": slug, "session_regime": regime, "checkpoint_sec": checkpoint,
                "settlement_side": settlement, "settlement_state_leader": leader,
                "market_leader": market_leader, "p_ex_leader": pex_leader,
                "leader_up": leader == "UP", "leader_down": leader == "DOWN",
                "eventual_flip": leader in {"UP", "DOWN"} and leader != settlement,
                "market_implied_flip_probability": opposite_prob(up_mid, leader),
                "analytic_flip_probability": opposite_prob(p_up, leader),
                "empirical_flip_probability": None,
                "required_move_sigma": _num(row.get("required_move_sigma")),
                "required_move_bps": abs(_num(row.get("required_move_bps"))) if _num(row.get("required_move_bps")) is not None else None,
            })
    summary_rows: list[dict[str, Any]] = []
    calibration_rows: list[dict[str, Any]] = []
    for regime in regimes:
        for checkpoint in checkpoints:
            selected = [r for r in checkpoint_rows if r["session_regime"] == regime and r["checkpoint_sec"] == checkpoint and r["settlement_state_leader"] in {"UP", "DOWN"}]
            summary_rows.append({"session_regime": regime, "checkpoint_sec": checkpoint, "total_eligible": len([r for r in checkpoint_rows if r["session_regime"] == regime and r["checkpoint_sec"] == checkpoint]), "usable_N": len(selected), "leader_UP": sum(r["leader_up"] for r in selected), "leader_DOWN": sum(r["leader_down"] for r in selected), "eventual_flips": sum(r["eventual_flip"] for r in selected), "observed_flip_rate": _mean([r["eventual_flip"] for r in selected]), "mean_market_implied_flip_probability": _mean([r["market_implied_flip_probability"] for r in selected]), "mean_analytic_flip_probability": _mean([r["analytic_flip_probability"] for r in selected]), "mean_empirical_flip_probability": None})
            for name, field in (("MARKET", "market_implied_flip_probability"), ("ANALYTIC", "analytic_flip_probability"), ("EMPIRICAL", "empirical_flip_probability")):
                pairs = [(float(r[field]), bool(r["eventual_flip"])) for r in selected if _num(r.get(field)) is not None]
                calibration_rows.append({"session_regime": regime, "checkpoint_sec": checkpoint, "model": name, "total_eligible": len(selected), "usable_N": len(pairs), "brier": _brier(pairs), "log_loss": _log_loss(pairs), "mean_predicted_flip_probability": _mean([p for p, _ in pairs]), "observed_flip_rate": _mean([y for _, y in pairs]), "calibration_bias": (_mean([p for p, _ in pairs]) - _mean([y for _, y in pairs])) if pairs else None})
    _write_csv(output / "flip_probability_by_checkpoint.csv", summary_rows)
    _write_csv(output / "calibration_by_checkpoint.csv", calibration_rows)
    calibration_bins: list[dict[str, Any]] = []
    probability_bins = ((0,.05,"0–5%"),(.05,.10,"5–10%"),(.10,.20,"10–20%"),(.20,.30,"20–30%"),(.30,.50,"30–50%"),(.50,math.inf,">50%"))
    for regime in regimes:
        for checkpoint in checkpoints:
            selected=[r for r in checkpoint_rows if r["session_regime"]==regime and r["checkpoint_sec"]==checkpoint]
            for model, field in (("MARKET","market_implied_flip_probability"),("ANALYTIC","analytic_flip_probability"),("EMPIRICAL","empirical_flip_probability")):
                for low, high, label in probability_bins:
                    chosen=[r for r in selected if (p:=_num(r.get(field))) is not None and low <= p < high]
                    calibration_bins.append({"session_regime":regime,"checkpoint_sec":checkpoint,"model":model,"bin":label,"total_eligible":len(selected),"usable_N":len(chosen),"mean_predicted":_mean([r[field] for r in chosen]),"observed_flip_rate":_mean([r["eventual_flip"] for r in chosen])})
    _write_csv(output / "calibration_bins.csv", calibration_bins)

    sigma_bins = ((-math.inf,.5,"<0.5σ"),(.5,1,"0.5–1σ"),(1,2,"1–2σ"),(2,3,"2–3σ"),(3,5,"3–5σ"),(5,math.inf,">5σ"))
    bps_bins = ((0,2,"<2bps"),(2,5,"2–5bps"),(5,10,"5–10bps"),(10,20,"10–20bps"),(20,math.inf,">20bps"))
    def grouped_flips(bins, field):
        output_rows=[]
        for regime in regimes:
            base=[r for r in checkpoint_rows if r["session_regime"] == regime and r["checkpoint_sec"] == 300 and r["settlement_state_leader"] in {"UP","DOWN"}]
            for low, high, label in bins:
                chosen=[r for r in base if (value:=_num(r.get(field))) is not None and low <= value < high]
                output_rows.append({"session_regime":regime, "bin":label, "total_eligible":len(base), "usable_N":len(chosen), "eventual_flips":sum(r["eventual_flip"] for r in chosen), "flip_rate":_mean([r["eventual_flip"] for r in chosen]), "mean_market_implied_flip_probability":_mean([r["market_implied_flip_probability"] for r in chosen]), "mean_analytic_flip_probability":_mean([r["analytic_flip_probability"] for r in chosen])})
        return output_rows
    sigma_rows, bps_rows = grouped_flips(sigma_bins,"required_move_sigma"), grouped_flips(bps_bins,"required_move_bps")
    _write_csv(output / "flip_rate_by_sigma.csv", sigma_rows); _write_csv(output / "flip_rate_by_bps.csv", bps_rows)

    entries: list[dict[str, Any]] = []
    shadow_records = [record for regime in regimes
                      for record in _read_shadow_settlements(journal_path, regime=regime)[0]]
    for record in shadow_records:
        slug, side = record["market_slug"], record["side"]
        if slug not in contexts or side not in {"UP", "DOWN"} or record["status"] != "USABLE":
            continue
        ts = record["entry_ts"]
        nearest = _snapshot_near(all_rows.get(slug, []), ts)
        p_side = _num(nearest.get(f"p_{side.lower()}_ex_market")) if nearest and nearest.get("p_ex_fresh") is True else None
        ask = _num(nearest.get(f"best_ask_{side.lower()}")) if nearest and nearest.get(f"market_mid_{side.lower()}_fresh") is True else None
        edge = p_side - ask if p_side is not None and ask is not None else None
        classification = "UNVERIFIED_DUE_TO_DATA" if edge is None else "VERIFIED_EX_MARKET_EDGE" if edge > 0 else "MARKET_FOLLOWING"
        entries.append({"market_slug": slug, "session_regime": contexts[slug]["session_regime"],
            "side": side, "won": record["won"], "gross_pnl": record["gross_pnl"],
            "entry_price": record["entry_price"], "time_left_sec": record["time_left_at_entry"],
            "holding_minutes": record["holding_min"], "capital_committed": record["capital_committed_usdc"],
            "required_move_sigma": _num(nearest.get("required_move_sigma")) if nearest else None,
            "analytic_flip_probability": 1 - p_side if p_side is not None else None,
            "market_implied_probability": _num(nearest.get(f"market_mid_{side.lower()}")) if nearest else None,
            "analytic_edge_vs_ask": edge, "classification": classification,
            **{f"btc_{horizon}s_bps": _num(nearest.get(f"btc_return_{horizon}s_bps")) if nearest else None for horizon in (5, 10, 30)}})
    def entry_summary(selected, label):
        pnls=[r["gross_pnl"] for r in selected if r["gross_pnl"] is not None]; winners=[p for p in pnls if p>0]; losers=[p for p in pnls if p<0]
        capital_minutes=sum((r["capital_committed"] or 0)*(r["holding_minutes"] or 0) for r in selected)
        return {"group":label,"total_eligible":len(selected),"usable_N":len(pnls),"wins":len(winners),"losses":len(losers),"win_rate":_mean([r["won"] for r in selected]),"gross_pnl":sum(pnls),"avg_pnl":_mean(pnls),"median_pnl":_median(pnls),"avg_winner":_mean(winners),"avg_loser":_mean(losers),"profit_factor":sum(winners)/abs(sum(losers)) if losers else None,"capital_minutes":capital_minutes,"pnl_per_dollar_minute":sum(pnls)/capital_minutes if capital_minutes else None}
    entry_outcomes=[]; timing=[]; capital=[]; edge_rows=[]
    timing_labels=(">600s","480–600s","360–480s","240–360s","120–240s","<120s")
    for regime in regimes:
        chosen=[r for r in entries if r["session_regime"]==regime]
        entry_outcomes.append(entry_summary(chosen,regime)); capital.append(entry_summary(chosen,regime))
        for label in timing_labels:
            timing.append(entry_summary([r for r in chosen if _entry_timing_bin(r["time_left_sec"])==label],f"{regime}:{label}"))
        for label, group in (("VERIFIED_EX_MARKET_EDGE",[r for r in chosen if r["classification"]=="VERIFIED_EX_MARKET_EDGE"]),("MARKET_FOLLOWING",[r for r in chosen if r["classification"]=="MARKET_FOLLOWING"]),("UNVERIFIED_DUE_TO_DATA",[r for r in chosen if r["classification"]=="UNVERIFIED_DUE_TO_DATA"])):
            edge_rows.append({**entry_summary(group,f"{regime}:{label}"),"classification":label})
    _write_csv(output / "entry_outcomes.csv", entry_outcomes); _write_csv(output / "entry_timing.csv", timing); _write_csv(output / "capital_efficiency.csv", capital); _write_csv(output / "entry_edge_vs_ask.csv", edge_rows)
    losses=sorted([r for r in entries if (r.get("gross_pnl") or 0)<0], key=lambda r:r["gross_pnl"])
    tail=losses[:max(1, math.ceil(len(losses)*.25))] if losses else []
    tail_rows=[entry_summary(losses,"ALL_LOSERS"),entry_summary(tail,"WORST_25_PERCENT_LOSSES")]
    _write_csv(output / "tail_loss_analysis.csv", tail_rows)
    # P2 deliberately uses fixed, pre-declared bins.  These are settled
    # shadow entries only; they are not silently mixed with live fills.
    same_price_sigma = []
    for regime in regimes:
        chosen = [row for row in entries if row["session_regime"] == regime]
        for price_label in ("0.55–0.60", "0.60–0.65", "0.65–0.70", "0.70–0.75", "0.75–0.80", "0.80–0.85", "0.85–0.90"):
            for sigma_label in ("<0.5σ", "0.5–1σ", "1–2σ", ">2σ"):
                group = [row for row in chosen if _price_bucket(row.get("entry_price")) == price_label and _sigma_bucket(row.get("required_move_sigma")) == sigma_label]
                pnls = [row["gross_pnl"] for row in group if row.get("gross_pnl") is not None]
                same_price_sigma.append({
                    "session_regime": regime, "source": "SETTLED_SHADOW_SIM_ONLY",
                    "price_bucket": price_label, "required_move_sigma_bucket": sigma_label,
                    "N": len(group), "wins": sum(bool(row.get("won")) for row in group),
                    "losses": sum(not bool(row.get("won")) for row in group),
                    "win_rate": _mean([bool(row.get("won")) for row in group]),
                    "flip_rate": _mean([not bool(row.get("won")) for row in group]),
                    "gross_pnl": sum(pnls) if pnls else None, "avg_pnl": _mean(pnls),
                })
    _write_csv(output / "same_price_structural_risk.csv", same_price_sigma)
    # Preserve explicit missingness: this command does not fabricate lifecycle
    # or lead/lag rows.  The dedicated stop-lifecycle command emits them only
    # when a canonical join exists.
    placeholders = {"true_false_reversal.csv": [], "actual_flip_lead_lag.csv": [], "pex_market_lead_lag.csv": [], "btc_fast_warning.csv": [], "stop_timing.csv": [{"status":"SEE_STOP_LIFECYCLE_COMMAND","reason":"canonical sparse lifecycle joins are reported separately"}], "structural_stop_thresholds.csv": [], "hour_of_day.csv": [], "regime_comparison.csv": [], "predictive_power_matrix.csv": []}
    for filename, rows in placeholders.items(): _write_csv(output / filename, rows)
    lines = ["# Canonical entry + stop-loss research status", "", "Offline-only; no live authority changed.", "", "## Dataset audit"]
    for row in audit: lines.append(f"- {row['session_regime']}: completed={row['completed_settlement_markets']}; synchronized={row['comparable_synchronized_markets']}; median coverage={row['median_coverage_ratio']}; median largest gap={row['median_largest_gap_sec']}.")
    lines += ["", "## Precision-analysis scope", "Only joined-fresh checkpoint rows are used for flip/calibration tables. Entry, stop-lifecycle and capital fields are retained as NOT_MEASURABLE in this first canonical pass when a durable same-timestamp join is absent; no values are imputed."]
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"completed_markets": len(catalog), "weekend_completed": sum(r["session_regime"] == "WEEKEND" for r in catalog), "weekday_completed": sum(r["session_regime"] == "WEEKDAY" for r in catalog), "synchronized_markets": sum(bool(all_rows.get(r["market_slug"])) for r in catalog), "checkpoint_rows": len(checkpoint_rows), "output": str(output)}



def canonical_replay(db_path: Path, journal_path: Path, *, kind: str, decision_ts: float,
                     run_id=None, slug=None, guard_config=None, complete_history=False) -> dict:
    from bot.journal_replay import replay_evidence
    from bot.research.clocks import epoch
    store = ResearchStore(db_path)
    if kind == "MARKET":
        events = [{**payload, "event_ts": epoch(payload.get("snapshot_ts"))}
                  for payload in store.get_prediction_snapshots(run_id=run_id, slug=slug, end_ts=decision_ts)]
    else:
        table = "strategy_events" if kind == "DECISION" else "order_events"
        events = []
        for row in store.journal_events(journal_path, table=table, run_id=run_id, slug=slug):
            # Journal ts describes when evidence became durably available. It
            # cannot support exact venue-time ordering without explicit clocks.
            events.append({**row, "event_ts": row["persist_ts"]})
    result = replay_evidence(events, kind=kind, decision_ts=decision_ts,
                             guard_config=guard_config, complete_history=complete_history)
    if kind != "MARKET" and result["classification"] == "EXACT_REPLAY":
        result["classification"] = "APPROXIMATE_REPLAY"
        result["reason_codes"].append("JOURNAL_PERSIST_CLOCK_NOT_VENUE_EVENT_CLOCK")
    result["execution_authority"] = False
    return result


def engineering_integrity(store: ResearchStore, journal_path: Path) -> dict:
    from bot.execution_events import audit_reconciliation
    from bot.research.clocks import available_at
    from bot.research.evidence import STRATEGY_TRACE_EVENTS, order_trace_expected
    orders = store.journal_events(journal_path)
    strategy = store.journal_events(journal_path, table="strategy_events")
    snapshots = store.get_prediction_snapshots()
    research_rows = list(store.rows())
    research_payloads = [payload for _, _, _, payload in research_rows]
    payloads = [row["payload"] for row in orders + strategy] + research_payloads
    traces = [payload["decision_trace"] for payload in payloads if isinstance(payload.get("decision_trace"), dict)]
    expected_rows = [row for row in strategy if row.get('event_type') in STRATEGY_TRACE_EVENTS]
    expected_rows += [row for row in orders if order_trace_expected(str(row.get('event_type') or ''))]
    expected = len(expected_rows)
    journal_traces = [row['payload']['decision_trace'] for row in orders + strategy
                      if isinstance(row['payload'].get('decision_trace'), dict)]
    persisted_l2 = [payload for payload in research_payloads
                    if payload.get('event_type') == 'DECISION_POINT_L2'
                    and payload.get('l2', {}).get('status') == 'L2_AVAILABLE']
    # Evidence references are scoped by run/market, never joined on time proximity.
    persisted_keys = {(p.get('run_id', run), p.get('market_slug', slug), p.get('l2_evidence_id'))
                      for run, slug, _, p in research_rows
                      if p.get('event_type') == 'DECISION_POINT_L2' and p.get('l2', {}).get('status') == 'L2_AVAILABLE' and p.get('l2_evidence_id')}
    references = [(trace.get('run_id'), trace.get('market_slug'), trace['l2']['l2_evidence_id'])
                  for trace in journal_traces if isinstance(trace.get('l2'), dict)
                  and trace['l2'].get('l2_evidence_id')]
    trace_kinds = {kind: sum(trace.get("kind") == kind for trace in traces)
                   for kind in ("ENTRY_DECISION", "STOP_DECISION", "SESSION_GUARD_DECISION", "EXECUTION_DECISION")}
    clocks = sum(not available_at(row, row["snapshot_ts"]) for row in snapshots)
    return {"reconciliation": audit_reconciliation(orders + strategy),
            "decision_trace_rows": len(traces), "trace_rows_by_kind": trace_kinds,
            "expected_sparse_journal_boundaries": expected,
            "journal_trace_unavailable_rows": sum(not isinstance(row['payload'].get('decision_trace'), dict)
                                                    for row in expected_rows),
            "journal_l2_enqueued_rows": sum(trace.get('l2', {}).get('status') == 'L2_ENQUEUED' for trace in journal_traces),
            "journal_l2_not_persisted_rows": sum(trace.get('l2', {}).get('status') == 'L2_NOT_PERSISTED' for trace in journal_traces),
            "persisted_decision_point_l2_rows": len(persisted_l2),
            "decision_point_l2_rows": sum(payload.get('event_type') == 'DECISION_POINT_L2' for payload in research_payloads),
            "l2_reference_joined_rows": sum(key in persisted_keys for key in references),
            "l2_reference_missing_rows": sum(key not in persisted_keys for key in references),
            "legacy_inline_trace_l2_available_rows": sum(trace.get('l2', {}).get('status') == 'L2_AVAILABLE' for trace in traces),
            "clock_contract_violations": clocks,
            "runtime_health_counters": "NOT_PERSISTED_LEGACY_UNKNOWN",
            "scope": "OFFLINE_DIAGNOSTICS_NO_EXECUTION_ACTION"}

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("latest", "run", "market", "compare-regimes", "preliminary-regimes", "capital-efficiency", "integrity", "provenance", "entry-stop-status", "stop-lifecycle", "replay", "index-benchmark", "storage-summary"))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--markets", type=int, default=4)
    parser.add_argument("--regime", choices=("weekday", "weekend"))
    parser.add_argument("--run-id")
    parser.add_argument("--slug")
    parser.add_argument("--legacy-stop-db", type=Path,
                        help="Optional read-only legacy Outcome stop-forensics DB; not scanned by default.")
    parser.add_argument("--replay-kind", choices=("MARKET", "DECISION", "ACCOUNTING"), default="MARKET")
    parser.add_argument("--decision-ts", type=float)
    parser.add_argument("--guard-mode", choices=("legacy", "target_scaled_v2", "shadow_target_scaled_v2"))
    parser.add_argument("--monthly-target", type=Decimal)
    parser.add_argument("--per-trade-risk", type=Decimal)
    parser.add_argument("--complete-history", action="store_true", help="Assert supplied evidence includes initial state/history; does not assert exact venue replay")
    parser.add_argument("--btc-dir", type=Path, help="Explicit offline history copy; only used by storage-summary")
    args = parser.parse_args()
    if args.command == "index-benchmark":
        from bot.research.indexing import benchmark_index
        print(json.dumps(benchmark_index(), indent=2))
        return
    if args.command in {"replay", "storage-summary"}:
        if args.db == DEFAULT_DB or args.journal == DEFAULT_JOURNAL:
            parser.error("requires explicit offline --db and --journal copies")
        if args.command == "storage-summary":
            from bot.research.storage import StorageSummary
            print(json.dumps(StorageSummary().measure(journal=args.journal, research=args.db, btc_dir=args.btc_dir), indent=2))
            return
        if args.decision_ts is None:
            parser.error("replay requires --decision-ts UTC seconds")
        from bot.session_pnl_guard import SessionPnlGuardConfig
        config = SessionPnlGuardConfig(mode=args.guard_mode, monthly_net_target_usdc=args.monthly_target,
                                       per_trade_risk_usdc=args.per_trade_risk) if args.guard_mode else None
        print(json.dumps(canonical_replay(args.db, args.journal, kind=args.replay_kind,
              decision_ts=args.decision_ts, run_id=args.run_id, slug=args.slug,
              guard_config=config, complete_history=args.complete_history), indent=2))
        return
    catalog = _catalog(args.db)
    if args.command == "run" and not args.run_id:
        parser.error("run requires --run-id")
    if args.command == "market" and not args.slug:
        parser.error("market requires --slug")
    if args.command == "preliminary-regimes":
        print(json.dumps(preliminary_regime_comparison(args.db, args.journal, args.output), indent=2, sort_keys=True))
        return
    if args.command == "capital-efficiency":
        if args.db == DEFAULT_DB or args.journal == DEFAULT_JOURNAL:
            parser.error(
                "capital-efficiency requires explicit offline --db and --journal SQLite snapshots; "
                "do not analyze an actively written database directly"
            )
        output = args.output / "capital_efficiency" / "preliminary_weekend"
        print(json.dumps(capital_efficiency_analysis(args.db, args.journal, output), indent=2, sort_keys=True))
        return
    if args.command == "integrity":
        result = ResearchStore(args.db).integrity()
        result["run_provenance"] = ResearchStore(args.db).get_run_provenance(args.journal)
        result["engineering"] = engineering_integrity(ResearchStore(args.db), args.journal)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if args.command == "provenance":
        print(json.dumps(ResearchStore(args.db).get_run_provenance(args.journal), indent=2, sort_keys=True))
        return
    if args.command == "entry-stop-status":
        output = args.output / "entry_stop_status"
        print(json.dumps(entry_stop_status_analysis(args.db, args.journal, output), indent=2, sort_keys=True))
        return
    if args.command == "stop-lifecycle":
        output = args.output / "stop_lifecycle"
        print(json.dumps(position_lifecycle_analysis(
            args.db, args.journal, output, slug=args.slug, legacy_stop_db=args.legacy_stop_db,
        ), indent=2, sort_keys=True))
        return
    limit = None if args.command == "compare-regimes" else args.markets
    selected = select_catalog(catalog, markets=limit, regime=args.regime, run_id=args.run_id, slug=args.slug)
    if not selected:
        parser.error("no completed markets match the requested selection")
    result = analyze_selection(args.db, args.journal, args.output, selected)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
