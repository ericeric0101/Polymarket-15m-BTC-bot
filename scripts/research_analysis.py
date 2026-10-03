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
    parser.add_argument("command", choices=("latest", "run", "market", "compare-regimes"))
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
    limit = None if args.command == "compare-regimes" else args.markets
    selected = select_catalog(catalog, markets=limit, regime=args.regime, run_id=args.run_id, slug=args.slug)
    if not selected:
        parser.error("no completed markets match the requested selection")
    result = analyze_selection(args.db, args.journal, args.output, selected)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
