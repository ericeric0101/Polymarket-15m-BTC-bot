#!/usr/bin/env python3
"""Offline Track A/B analysis for synchronized prediction snapshots.

Primary endpoint is fresh UP-token midpoint repricing after 30 seconds.
This is descriptive prediction research, not a PnL study or trading rule.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


RESIDUAL_BINS = [(-math.inf, -.10, "<= -0.10"), (-.10, -.05, "-0.10 to -0.05"),
                 (-.05, -.02, "-0.05 to -0.02"), (-.02, .02, "-0.02 to +0.02"),
                 (.02, .05, "+0.02 to +0.05"), (.05, .10, "+0.05 to +0.10"),
                 (.10, math.inf, ">= +0.10")]


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def read_snapshots(db_path: str | Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    path = Path(db_path).resolve()
    uri = f"file:{path}?mode=ro"
    rows: list[dict[str, Any]] = []
    settlements: dict[str, str] = {}
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for record in conn.execute("SELECT slug, decision_epoch_ns, payload_json FROM lead_lag_decisions ORDER BY decision_epoch_ns"):
            try:
                payload = json.loads(record["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            event_type = payload.get("event_type")
            if event_type == "PREDICTION_RESEARCH_SNAPSHOT":
                payload.setdefault("market_slug", record["slug"])
                payload.setdefault("snapshot_ts", int(record["decision_epoch_ns"]) / 1e9)
                rows.append(payload)
            elif event_type == "MARKET_TWAP_SUMMARY":
                slug = str(payload.get("market_slug") or record["slug"] or "")
                side = str(payload.get("settlement_side") or "").upper()
                if slug and side in {"UP", "DOWN"}:
                    settlements[slug] = side
    return rows, settlements


def attach_future_repricing(rows: list[dict[str, Any]], horizons=(5, 10, 30, 60), tolerance_sec=1.5) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if _num(row.get("snapshot_ts")) is not None:
            grouped[str(row.get("market_slug") or "")].append(row)
    output = []
    for slug, group in grouped.items():
        group.sort(key=lambda x: float(x["snapshot_ts"]))
        for idx, row in enumerate(group):
            current_mid = _num(row.get("market_mid_up")) if row.get("market_mid_up_fresh") else None
            if current_mid is None:
                continue
            item = dict(row)
            for horizon in horizons:
                target = float(row["snapshot_ts"]) + horizon
                candidates = [future for future in group[idx + 1:]
                              if abs(float(future["snapshot_ts"]) - target) <= tolerance_sec
                              and future.get("market_mid_up_fresh") is True
                              and _num(future.get("market_mid_up")) is not None]
                future = min(candidates, key=lambda x: abs(float(x["snapshot_ts"]) - target)) if candidates else None
                item[f"future_mid_up_{horizon}s"] = _num(future.get("market_mid_up")) if future else None
                item[f"future_ts_{horizon}s"] = _num(future.get("snapshot_ts")) if future else None
                item[f"actual_horizon_{horizon}s"] = (float(future["snapshot_ts"]) - float(row["snapshot_ts"])) if future else None
                item[f"mid_repricing_{horizon}s"] = (float(future["market_mid_up"]) - current_mid) if future else None
            output.append(item)
    return output


def cluster_episodes(rows: list[dict[str, Any]], *, value_key: str, sign_key: str | None = None,
                     predicate=None, gap_sec: float = 15.0, representative="largest_abs") -> list[dict[str, Any]]:
    selected = [r for r in rows if _num(r.get(value_key)) is not None and (predicate(r) if predicate else True)]
    selected.sort(key=lambda r: (str(r.get("market_slug") or ""), float(r["snapshot_ts"])))
    episodes = []
    active: list[dict[str, Any]] = []
    for row in selected:
        value = float(row[value_key])
        sign = (str(row.get(sign_key)) if sign_key else ("positive" if value > 0 else "negative"))
        if active:
            last = active[-1]
            same = str(last.get("market_slug")) == str(row.get("market_slug"))
            last_sign = str(last.get(sign_key)) if sign_key else ("positive" if float(last[value_key]) > 0 else "negative")
            close = float(row["snapshot_ts"]) - float(last["snapshot_ts"]) <= gap_sec
            if not (same and sign == last_sign and close):
                episodes.append(_episode(active, value_key, representative))
                active = []
        active.append(row)
    if active:
        episodes.append(_episode(active, value_key, representative))
    return episodes


def _episode(rows, value_key, representative):
    row = (max(rows, key=lambda r: abs(float(r[value_key]))) if representative == "largest_abs" else rows[0])
    result = dict(row)
    result.update({"episode_observations": len(rows), "episode_start_ts": rows[0]["snapshot_ts"],
                   "episode_end_ts": rows[-1]["snapshot_ts"],
                   f"episode_mean_{value_key}": statistics.mean(float(r[value_key]) for r in rows)})
    return result


def _cluster_bootstrap_ci(rows: list[dict[str, Any]], value_key: str, *, seed=20261002, samples=1000) -> tuple[float | None, float | None]:
    by_market: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = _num(row.get(value_key))
        if value is not None:
            by_market[str(row.get("market_slug") or "")].append(value)
    markets = list(by_market)
    if len(markets) < 2:
        return None, None
    rng = random.Random(seed)
    values = []
    for _ in range(samples):
        chosen = [rng.choice(markets) for _ in markets]
        sampled = [x for market in chosen for x in by_market[market]]
        if sampled:
            values.append(statistics.mean(sampled))
    values.sort()
    return values[int(.025 * (len(values) - 1))], values[int(.975 * (len(values) - 1))]


def summarize_residual_bins(rows: list[dict[str, Any]], *, horizon=30) -> list[dict[str, Any]]:
    output = []
    key = f"mid_repricing_{horizon}s"
    for low, high, label in RESIDUAL_BINS:
        group = [r for r in rows if _num(r.get("residual_up")) is not None
                 and low <= float(r["residual_up"]) < high and _num(r.get(key)) is not None]
        episodes = cluster_episodes(group, value_key="residual_up", gap_sec=15)
        lo, hi = _cluster_bootstrap_ci(group, key)
        hit = [((float(r[key]) > 0) == (float(r["residual_up"]) > 0)) for r in group if float(r["residual_up"]) != 0 and float(r[key]) != 0]
        output.append({"residual_bin": label, "observations": len(group),
                       "episodes": len(episodes), "markets": len({str(r.get("market_slug")) for r in group}),
                       "mean_mid_repricing_30s": statistics.mean(float(r[key]) for r in group) if group else None,
                       "median_mid_repricing_30s": statistics.median(float(r[key]) for r in group) if group else None,
                       "directional_hit_rate": statistics.mean(hit) if hit else None,
                       "market_cluster_bootstrap_ci_low": lo, "market_cluster_bootstrap_ci_high": hi})
    return output


def summarize_disagreement(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for label, disagrees in (("AGREES", False), ("DISAGREES", True)):
        group = [r for r in rows if r.get("btc_disagree_10s") is disagrees
                 and _num(r.get("mid_repricing_30s")) is not None and r.get("active_side") in {"UP", "DOWN"}]
        for row in group:
            change = float(row["mid_repricing_30s"])
            row["held_side_change_30s"] = change if row["active_side"] == "UP" else -change
        episodes = cluster_episodes(group, value_key="held_side_change_30s", sign_key="active_side",
                                    gap_sec=15, representative="first") if group else []
        # Episode clustering by held side and label is handled per cohort; use a
        # temporary fixed cohort sign key to avoid joining opposite conditions.
        output.append({"btc_10s_group": label, "observations": len(group), "episodes": len(episodes),
                       "markets": len({str(r.get("market_slug")) for r in group}),
                       "mean_held_side_mid_change_30s": statistics.mean(float(r["episode_mean_held_side_change_30s"]) for r in episodes) if episodes else None,
                       "median_held_side_mid_change_30s": statistics.median(float(r["episode_mean_held_side_change_30s"]) for r in episodes) if episodes else None,
                       "adverse_repricing_rate": statistics.mean(float(r["episode_mean_held_side_change_30s"]) < 0 for r in episodes) if episodes else None,
                       "large_adverse_ge_5c_rate": statistics.mean(float(r["episode_mean_held_side_change_30s"]) <= -.05 for r in episodes) if episodes else None,
                       "large_adverse_ge_10c_rate": statistics.mean(float(r["episode_mean_held_side_change_30s"]) <= -.10 for r in episodes) if episodes else None})
    return output


def build_entry_rows(rows: list[dict[str, Any]], settlements: dict[str, str]) -> list[dict[str, Any]]:
    entries = [r for r in rows if r.get("snapshot_trigger") in {"entry_decision", "order_submit", "dry_run_order_submit"}]
    result = []
    seen = set()
    for row in sorted(entries, key=lambda r: float(r.get("snapshot_ts") or 0)):
        candidate = str(row.get("entry_candidate_id") or "")
        key = candidate or f"{row.get('market_slug')}:{row.get('snapshot_ts')}:{row.get('snapshot_trigger')}"
        if key in seen and row.get("snapshot_trigger") == "entry_decision":
            continue
        seen.add(key)
        side = str(row.get("entry_side") or row.get("active_side") or "").upper()
        mid_key, ask_key, bid_key = (("market_mid_up", "best_ask_up", "best_bid_up") if side == "UP"
                                     else ("market_mid_down", "best_ask_down", "best_bid_down"))
        mid = _num(row.get(mid_key)) if row.get("market_mid_" + side.lower() + "_fresh") else None
        ask, bid = _num(row.get(ask_key)), _num(row.get(bid_key))
        flags = []
        if not row.get("p_ex_fresh"):
            flags.append("ENTRY_PEX_STALE")
        if mid is None or ask is None:
            flags.append("ENTRY_MARKET_STALE")
        if not row.get("btc_fresh"):
            flags.append("ENTRY_BTC_STALE")
        if row.get("required_move_mode") == "UNAVAILABLE" or row.get("required_move_sigma") is None:
            flags.append("ENTRY_REQUIRED_PATH_UNAVAILABLE")
        result.append({"market_slug": row.get("market_slug"), "snapshot_ts": row.get("snapshot_ts"),
                       "snapshot_trigger": row.get("snapshot_trigger"), "entry_candidate_id": candidate,
                       "entry_side": side, "entry_price": row.get("entry_price"),
                       "fresh_p_ex_side": row.get("p_up_ex_market") if side == "UP" else row.get("p_down_ex_market"),
                       "fresh_mid_side": mid, "fresh_ask_side": ask, "fresh_bid_side": bid,
                       "edge_vs_mid": ((row.get("p_up_ex_market") if side == "UP" else row.get("p_down_ex_market")) - mid)
                           if mid is not None and row.get("p_ex_fresh") else None,
                       "edge_vs_ask": ((row.get("p_up_ex_market") if side == "UP" else row.get("p_down_ex_market")) - ask)
                           if ask is not None and row.get("p_ex_fresh") else None,
                       "btc_return_10s_bps": row.get("btc_return_10s_bps"),
                       "btc_disagree_10s": row.get("btc_disagree_10s"),
                       "required_move_sigma": row.get("required_move_sigma"),
                       "future_held_mid_change_5s": _held_change(row, 5),
                       "future_held_mid_change_10s": _held_change(row, 10),
                       "future_held_mid_change_30s": _held_change(row, 30),
                       "future_held_mid_change_60s": _held_change(row, 60),
                       "settlement_side": settlements.get(str(row.get("market_slug"))),
                       "settlement_result": ("WIN" if settlements.get(str(row.get("market_slug"))) == side else
                                             "LOSS" if settlements.get(str(row.get("market_slug"))) in {"UP", "DOWN"} and side else None),
                       "entry_snapshot_quality": "ENTRY_SYNC_COMPLETE" if not flags else "|".join(flags)})
    return result


def _held_change(row, horizon):
    value = _num(row.get(f"mid_repricing_{horizon}s"))
    if value is None:
        return None
    return value if str(row.get("entry_side") or row.get("active_side") or "").upper() == "UP" else -value


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str] | None = None):
    if columns is None:
        columns = list(dict.fromkeys(key for row in rows for key in row))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=columns,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def analyze(db_path: str | Path, output_dir: str | Path) -> dict[str, Any]:
    rows, settlements = read_snapshots(db_path)
    rows = attach_future_repricing(rows)
    valid_residual = [r for r in rows if _num(r.get("residual_up")) is not None and _num(r.get("mid_repricing_30s")) is not None]
    bins = summarize_residual_bins(valid_residual)
    episodes = cluster_episodes(valid_residual, value_key="residual_up", predicate=lambda r: abs(float(r["residual_up"])) >= .10, gap_sec=15)
    extreme_rows = []
    for row in episodes:
        extreme_rows.append({"timestamp": row.get("snapshot_ts"), "market_slug": row.get("market_slug"),
                             "residual_up": row.get("residual_up"), "current_mid_up": row.get("market_mid_up"),
                             "p_ex": row.get("p_up_ex_market"), "future_mid_30s": row.get("future_mid_up_30s"),
                             "repricing_30s": row.get("mid_repricing_30s"), "btc_return_10s_bps": row.get("btc_return_10s_bps"),
                             "required_move_sigma": row.get("required_move_sigma"),
                             "episode_observations": row.get("episode_observations")})
    disagreement = summarize_disagreement(rows)
    entry_rows = build_entry_rows(rows, settlements)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    _write_csv(out / "snapshot_quality.csv", _quality(rows))
    _write_csv(out / "residual_30s.csv", valid_residual)
    _write_csv(out / "residual_bins.csv", bins)
    _write_csv(out / "extreme_residual_episodes.csv", extreme_rows)
    _write_csv(out / "btc_disagreement_30s.csv", disagreement)
    _write_csv(out / "entry_snapshots.csv", entry_rows)
    summary = _summary(rows, valid_residual, bins, episodes, disagreement, entry_rows)
    (out / "summary.md").write_text(summary, encoding="utf-8")
    return {"snapshots": len(rows), "residual_observations": len(valid_residual), "entries": len(entry_rows),
            "output_dir": str(out)}


def _quality(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[str(row.get("market_slug") or "")].append(row)
    return [{"market_slug": slug, "snapshots": len(group),
             "pex_fresh": sum(bool(x.get("p_ex_fresh")) for x in group),
             "market_mid_fresh": sum(bool(x.get("market_mid_fresh")) for x in group),
             "btc_fresh": sum(bool(x.get("btc_fresh")) for x in group),
             "joint_fresh": sum(bool(x.get("joint_fresh")) for x in group),
             "joint_fresh_pct": 100 * sum(bool(x.get("joint_fresh")) for x in group) / len(group),
             "mean_interval_sec": statistics.mean(float(x["snapshot_interval_sec"]) for x in group
                                                    if _num(x.get("snapshot_interval_sec")) is not None) if any(_num(x.get("snapshot_interval_sec")) is not None for x in group) else None}
            for slug, group in sorted(groups.items())]


def _summary(rows, residual, bins, episodes, disagreement, entries):
    joint = sum(bool(r.get("joint_fresh")) for r in rows)
    joint_pct = 100 * joint / len(rows) if rows else 0.0
    eligible_entries = len(entries)
    synced_entries = sum(r["entry_snapshot_quality"] == "ENTRY_SYNC_COMPLETE" for r in entries)
    entry_pct = 100 * synced_entries / eligible_entries if eligible_entries else 0.0
    pos = [r for r in residual if float(r["residual_up"]) >= .10]
    neg = [r for r in residual if float(r["residual_up"]) <= -.10]
    pos_episodes = [r for r in episodes if float(r.get("residual_up") or 0) >= .10]
    neg_episodes = [r for r in episodes if float(r.get("residual_up") or 0) <= -.10]
    pos_markets = {str(r.get("market_slug")) for r in pos_episodes}
    neg_markets = {str(r.get("market_slug")) for r in neg_episodes}
    h1 = "INSUFFICIENT_DATA"
    if len(pos_markets) >= 30 and len(neg_markets) >= 30:
        def market_directional_rate(cohort, expected_positive):
            by_market = defaultdict(list)
            for row in cohort:
                delta = _num(row.get("mid_repricing_30s"))
                if delta is not None:
                    by_market[str(row.get("market_slug"))].append((delta > 0) if expected_positive else (delta < 0))
            market_rates = [statistics.mean(values) for values in by_market.values() if values]
            return statistics.mean(market_rates) if market_rates else 0.0
        pos_hit = market_directional_rate(pos_episodes, True)
        neg_hit = market_directional_rate(neg_episodes, False)
        h1 = "SUPPORTED_DESCRIPTIVELY" if pos_hit > .5 and neg_hit > .5 else "NOT_SUPPORTED"
    by_group = {r["btc_10s_group"]: r for r in disagreement}
    h2 = "INSUFFICIENT_DATA"
    if all(by_group.get(x, {}).get("markets", 0) >= 30 for x in ("AGREES", "DISAGREES")):
        h2 = "SUPPORTED_DESCRIPTIVELY" if by_group["DISAGREES"]["adverse_repricing_rate"] > by_group["AGREES"]["adverse_repricing_rate"] else "NOT_SUPPORTED"
    table = "\n".join(f"| {r['residual_bin']} | {r['observations']} | {r['episodes']} | {r['markets']} | {_fmt(r['mean_mid_repricing_30s'])} | {_fmt(r['median_mid_repricing_30s'])} | {_fmt(r['directional_hit_rate'])} | {_fmt(r['market_cluster_bootstrap_ci_low'])}–{_fmt(r['market_cluster_bootstrap_ci_high'])} |" for r in bins)
    dtable = "\n".join(f"| {r['btc_10s_group']} | {r['observations']} | {r['episodes']} | {r['markets']} | {_fmt(r['mean_held_side_mid_change_30s'])} | {_fmt(r['adverse_repricing_rate'])} | {_fmt(r['large_adverse_ge_5c_rate'])} | {_fmt(r['large_adverse_ge_10c_rate'])} |" for r in disagreement)
    return f"""# Synchronized prediction snapshot analysis

Joint fresh coverage: {joint}/{len(rows)} ({joint_pct:.1f}%)
Entry sync coverage: {synced_entries}/{eligible_entries} ({entry_pct:.1f}%)
Residual 30s hypothesis: {h1} (extreme episodes: positive={len(pos_episodes)}/{len(pos_markets)} markets, negative={len(neg_episodes)}/{len(neg_markets)} markets)
BTC disagreement hypothesis: {h2}
Runtime impact: telemetry logs capture/enqueue p95 latency and research queue/drop counters; no live authority
Live authority: NO — research only

## Track A — residual vs 30s UP-mid repricing

Current and future observations require fresh UP midpoint. Episodes combine same-market, same-sign observations within 15 seconds; confidence intervals resample markets as clusters. Bins are fixed, not optimized. Values are decimal probability points.

| Residual bin | Observations | Episodes | Markets | Mean repricing | Median | Directional hit | Market-cluster bootstrap 95% CI |
|---|---:|---:|---:|---:|---:|---:|---:|
{table}

Extreme event episode rows are in `extreme_residual_episodes.csv`; all paired observations are in `residual_30s.csv`.

## Track B — BTC 10s agreement/disagreement

Held-side change is UP mid change for UP and its negation for DOWN. Adverse means held-side repricing below zero.

| Group | Observations | Episodes | Markets | Mean held-side change | Adverse rate | ≤−5¢ rate | ≤−10¢ rate |
|---|---:|---:|---:|---:|---:|---:|---:|
{dtable}

## Entry snapshots

Eligible forced snapshots: {eligible_entries}; complete sync: {synced_entries}. Per-entry p_ex-minus-ask is included only when p_ex and that side's quote are fresh. Settlement labels are secondary context; 30-second repricing is primary.

## Interpretation / falsification

- H1 requires at least 30 distinct markets in each positive-extreme and negative-extreme cohort before classification; otherwise `INSUFFICIENT_DATA`. If sufficiently sampled and neither side has directional hit-rate above 50%, classify `NOT_SUPPORTED`.
- H2 requires at least 30 distinct markets in each BTC group; otherwise `INSUFFICIENT_DATA`. It is descriptively supported only if disagreement has a higher adverse repricing rate than agreement.
- 1 Hz observations are correlated. Episode/market counts are shown alongside observation counts; no live strategy conclusion follows from this report.
"""


def _fmt(x):
    return "—" if _num(x) is None else f"{float(x):.4f}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/research/twap_forward_shadow.db")
    parser.add_argument("--output", default="reports/prediction_snapshot")
    args = parser.parse_args()
    print(json.dumps(analyze(args.db, args.output), indent=2))


if __name__ == "__main__":
    main()
