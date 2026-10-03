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
from typing import Any, Iterable

from bot.entry_session_policy import TAIPEI
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
    uri = f"file:{db_path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        for run_id, slug, epoch_ns, payload_json in conn.execute(
            "SELECT run_id, slug, decision_epoch_ns, payload_json FROM lead_lag_decisions "
            "WHERE payload_json LIKE '%MARKET_TWAP_SUMMARY%' ORDER BY decision_epoch_ns"
        ):
            try:
                payload = json.loads(payload_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if payload.get("event_type") != "MARKET_TWAP_SUMMARY":
                continue
            market_slug = str(payload.get("market_slug") or slug or "")
            if not market_slug:
                continue
            row = {"run_id": str(run_id), "market_slug": market_slug,
                   "summary_epoch_ns": int(epoch_ns), **market_context(market_slug)}
            records[(str(run_id), market_slug)] = row
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
    for run_id, slugs in by_run.items():
        loaded, loaded_summaries = forensic._load(db_path, run_id, slugs)
        timelines.update(loaded)
        summaries.update(loaded_summaries)
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


def _brier(rows: list[dict[str, Any]], field: str) -> float | None:
    values = [(float(row[field]), 1.0 if row["observed_flip"] else 0.0) for row in rows
              if row.get("observed_flip") is not None and _num(row.get(field)) is not None]
    return sum((prediction - outcome) ** 2 for prediction, outcome in values) / len(values) if values else None


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
                       "market_brier": _brier(usable, "market_implied_flip_probability"),
                       "analytic_brier": _brier(usable, "analytic_flip_probability"),
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("latest", "run", "market", "compare-regimes", "preliminary-regimes"))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--markets", type=int, default=4)
    parser.add_argument("--regime", choices=("weekday", "weekend"))
    parser.add_argument("--run-id")
    parser.add_argument("--slug")
    args = parser.parse_args()
    catalog = _catalog(args.db)
    if args.command == "run" and not args.run_id:
        parser.error("run requires --run-id")
    if args.command == "market" and not args.slug:
        parser.error("market requires --slug")
    if args.command == "preliminary-regimes":
        print(json.dumps(preliminary_regime_comparison(args.db, args.journal, args.output), indent=2, sort_keys=True))
        return
    limit = None if args.command == "compare-regimes" else args.markets
    selected = select_catalog(catalog, markets=limit, regime=args.regime, run_id=args.run_id, slug=args.slug)
    if not selected:
        parser.error("no completed markets match the requested selection")
    result = analyze_selection(args.db, args.journal, args.output, selected)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
